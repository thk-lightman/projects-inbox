"""Topic clustering + Map-of-Content (MOC) sample generation.

This module loads all English chunk embeddings from the store, runs
density-based clustering (HDBSCAN preferred, k-means fallback), and renders
one markdown MOC file per cluster under ``data/moc_samples/``.

Defaults documented in ``docs/architecture.md``:

* Algorithm: HDBSCAN with ``min_cluster_size=10``, ``min_samples=5``.
* Distance: cosine, computed via L2-normalized vectors + euclidean metric
  (HDBSCAN's preferred path — equivalent to cosine on normalized vectors and
  considerably faster than passing ``metric="cosine"`` directly).
* Fallback: if HDBSCAN produces fewer than ``n`` non-noise clusters, switch
  to k-means with ``k = n`` to guarantee the operator gets the requested
  number of sample MOCs.
* Top-5 most central chunks per cluster = highest cosine similarity to the
  cluster centroid (mean of L2-normalized member vectors).

Every MOC file is written **only** under the project repo's output dir.
:func:`generate_mocs` and :func:`write_moc_file` both refuse to write inside
``vault_root`` so a misconfigured path can never mutate the Obsidian vault.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from .chunker import Chunk
from .store import _decode_embedding_blob


# ---------------------------------------------------------------------------
# DB → in-memory matrix
# ---------------------------------------------------------------------------


_SELECT_EN_EMBEDDED_SQL = """
SELECT chunk_id, source_path, heading_chain, lang, body, front_matter, embedding
FROM chunks
WHERE lang = 'en' AND embedding IS NOT NULL
""".strip()


@dataclass(frozen=True)
class EmbeddedCorpus:
    """An in-memory view of every English chunk + its embedding.

    Attributes:
        chunks: Ordered list of :class:`Chunk` rows reconstructed from SQLite.
        matrix: ``(n_chunks, dim)`` float32 ndarray of embeddings, in the
            same order as ``chunks``. ``matrix[i]`` is the embedding for
            ``chunks[i]``.
    """

    chunks: list[Chunk]
    matrix: np.ndarray


def load_embedded_english_chunks(conn: sqlite3.Connection) -> EmbeddedCorpus:
    """Pull every English chunk with a non-NULL embedding into memory.

    Returns an :class:`EmbeddedCorpus` whose ``chunks`` list and ``matrix``
    rows share the same ordering. Korean rows are filtered out at the SQL
    level so they cannot leak into the clustering input.
    """
    import json

    rows = conn.execute(_SELECT_EN_EMBEDDED_SQL).fetchall()
    chunks: list[Chunk] = []
    vectors: list[np.ndarray] = []
    for row in rows:
        chunks.append(
            Chunk(
                source_path=Path(row[1]),
                heading_chain=list(json.loads(row[2])),
                body=row[4],
                chunk_id=row[0],
                lang=row[3],
                frontmatter=dict(json.loads(row[5])),
            )
        )
        vectors.append(_decode_embedding_blob(row[6]))

    if not vectors:
        return EmbeddedCorpus(chunks=[], matrix=np.zeros((0, 0), dtype=np.float32))

    matrix = np.vstack(vectors).astype(np.float32, copy=False)
    return EmbeddedCorpus(chunks=chunks, matrix=matrix)


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def _l2_normalize(matrix: np.ndarray) -> np.ndarray:
    """Row-wise L2-normalize ``matrix``. Zero-norm rows are left as zeros.

    Cosine similarity over the original vectors equals euclidean-derived
    dot-product over the normalized ones — so normalizing once up front lets
    every downstream step use the cheaper euclidean code path.
    """
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    return (matrix / norms).astype(np.float32, copy=False)


def cluster_embeddings(
    matrix: np.ndarray,
    *,
    algo: str = "hdbscan",
    min_cluster_size: int = 10,
    min_samples: int = 5,
    k_fallback: int = 10,
    random_state: int = 42,
) -> np.ndarray:
    """Cluster ``matrix`` rows. Returns a length-``n`` label array.

    Algorithm selection:

    * ``"hdbscan"`` (default): density-based clustering on L2-normalized
      vectors with euclidean metric (= cosine). Noise points get label
      ``-1``. If HDBSCAN finds fewer than ``k_fallback`` non-noise clusters
      we automatically fall back to k-means so the operator always gets a
      usable number of clusters.
    * ``"kmeans"``: scikit-learn k-means with ``n_clusters=k_fallback``
      on L2-normalized vectors. Every point receives a non-negative label;
      there is no "noise" bucket.

    Args:
        matrix: ``(n_chunks, dim)`` ndarray of chunk embeddings.
        algo: ``"hdbscan"`` or ``"kmeans"``.
        min_cluster_size: HDBSCAN ``min_cluster_size``. Documented default 10.
        min_samples: HDBSCAN ``min_samples``. Documented default 5.
        k_fallback: ``n_clusters`` for k-means (either explicit or used as
            the fallback when HDBSCAN under-produces).
        random_state: Seed for the k-means initializer. HDBSCAN is
            deterministic given the same input so this only affects k-means.

    Returns:
        Integer ndarray of length ``matrix.shape[0]``. ``-1`` marks noise
        (HDBSCAN only); ``>=0`` is a real cluster id.
    """
    if matrix.shape[0] == 0:
        return np.zeros((0,), dtype=np.int32)

    normalized = _l2_normalize(matrix)

    if algo == "kmeans":
        return _kmeans_labels(normalized, k=k_fallback, random_state=random_state)

    if algo != "hdbscan":
        raise ValueError(f"unsupported algo: {algo!r}")

    labels = _hdbscan_labels(
        normalized,
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
    )
    n_clusters = len({int(l) for l in labels if int(l) != -1})
    if n_clusters >= k_fallback:
        return labels

    return _kmeans_labels(normalized, k=k_fallback, random_state=random_state)


def _hdbscan_labels(
    normalized: np.ndarray, *, min_cluster_size: int, min_samples: int
) -> np.ndarray:
    """Run HDBSCAN on already L2-normalized vectors. Returns int label array.

    Local import keeps the (heavy) hdbscan dependency off the module load
    path — tests that mock out clustering never have to import it.
    """
    import hdbscan  # local import: heavy C-extension

    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
        metric="euclidean",
    )
    labels = clusterer.fit_predict(normalized)
    return np.asarray(labels, dtype=np.int32)


def _kmeans_labels(
    normalized: np.ndarray, *, k: int, random_state: int
) -> np.ndarray:
    """Run scikit-learn KMeans on normalized vectors.

    Caps ``k`` at ``n_samples`` so the call never raises on tiny corpora —
    the cluster step is best-effort, not a hard precondition.
    """
    from sklearn.cluster import KMeans

    n_samples = normalized.shape[0]
    effective_k = max(1, min(k, n_samples))
    km = KMeans(n_clusters=effective_k, n_init=10, random_state=random_state)
    labels = km.fit_predict(normalized)
    return np.asarray(labels, dtype=np.int32)


# ---------------------------------------------------------------------------
# Cluster selection + centrality
# ---------------------------------------------------------------------------


def largest_non_noise_clusters(labels: np.ndarray, n: int) -> list[int]:
    """Return the ``n`` largest cluster ids (label != -1), descending by size.

    Ties are broken by cluster id ascending so the result is deterministic
    across runs. Returns fewer than ``n`` ids if the clustering produced
    fewer non-noise clusters.
    """
    counts: dict[int, int] = {}
    for label in labels.tolist():
        if label == -1:
            continue
        counts[int(label)] = counts.get(int(label), 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [cid for cid, _size in ranked[:n]]


def cluster_centroid(matrix: np.ndarray, member_indices: list[int]) -> np.ndarray:
    """Mean of L2-normalized member vectors, then re-normalized.

    The double normalization keeps the centroid on the unit hypersphere so
    cosine similarity against it is a plain dot product.
    """
    members = _l2_normalize(matrix[member_indices])
    mean = members.mean(axis=0)
    norm = float(np.linalg.norm(mean))
    if norm == 0.0:
        return mean.astype(np.float32, copy=False)
    return (mean / norm).astype(np.float32, copy=False)


def top_central_indices(
    matrix: np.ndarray, member_indices: list[int], top_k: int = 5
) -> list[int]:
    """Return up to ``top_k`` member indices ranked by cosine to centroid.

    Members are returned in descending similarity order. Ties are broken by
    the original ``member_indices`` order so output is fully deterministic.
    """
    if not member_indices:
        return []
    centroid = cluster_centroid(matrix, member_indices)
    members = _l2_normalize(matrix[member_indices])
    sims = members @ centroid
    # argsort descending; mergesort is stable so ties keep input order.
    order = np.argsort(-sims, kind="mergesort")
    ranked = [member_indices[i] for i in order.tolist()]
    return ranked[:top_k]


# ---------------------------------------------------------------------------
# LLM helpers — topic title + cluster summary
# ---------------------------------------------------------------------------


class _ChatClient(Protocol):
    chat: Any


def _truncate(body: str, max_chars: int = 600) -> str:
    """Truncate a chunk body for LLM prompts. Newlines preserved."""
    if len(body) <= max_chars:
        return body
    return body[: max_chars - 1] + "…"


def _format_chunks_for_prompt(chunks: list[Chunk], limit: int = 5) -> str:
    """Render the top-``limit`` cluster members as a numbered LLM prompt block."""
    lines: list[str] = []
    for i, chunk in enumerate(chunks[:limit], start=1):
        head = " > ".join(chunk.heading_chain) if chunk.heading_chain else "(no heading)"
        lines.append(f"[{i}] {head}\n{_truncate(chunk.body)}")
    return "\n\n---\n\n".join(lines)


_TOPIC_TITLE_SYSTEM = (
    "You name topic clusters from chunk excerpts. "
    "Output a 3–8 word English title that captures the shared topic. "
    "No quotes, no trailing punctuation, no commentary."
)


_CLUSTER_SUMMARY_SYSTEM = (
    "You summarize topic clusters from chunk excerpts. "
    "Output 2–3 plain English sentences describing the shared topic and what "
    "the cluster members have in common. No bullet points, no preface."
)


DEFAULT_MOC_MODEL = "gpt-4o-mini"


def generate_topic_title(
    chunks: list[Chunk],
    *,
    client: _ChatClient | None,
    model: str = DEFAULT_MOC_MODEL,
) -> str:
    """LLM-generate a 3–8 word topic title for the cluster.

    Falls back to a deterministic heading-derived title when ``client`` is
    ``None`` so tests and offline runs still produce a usable MOC file. The
    fallback joins the most common leading heading or the first chunk's
    first heading; never returns an empty string.
    """
    if client is None:
        return _fallback_title(chunks)

    prompt = (
        "Cluster excerpts:\n\n"
        + _format_chunks_for_prompt(chunks)
        + "\n\nReturn only the title."
    )
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": _TOPIC_TITLE_SYSTEM},
            {"role": "user", "content": prompt},
        ],
    }
    response = client.chat.completions.create(**payload)
    title = _extract_chat_text(response).strip().strip('"').strip("'")
    title = title.rstrip(".!?")
    return title or _fallback_title(chunks)


def generate_cluster_summary(
    chunks: list[Chunk],
    *,
    client: _ChatClient | None,
    model: str = DEFAULT_MOC_MODEL,
) -> str:
    """LLM-generate a 2–3 sentence summary for the cluster.

    Falls back to a deterministic 1-sentence stub when ``client`` is ``None``.
    """
    if client is None:
        return _fallback_summary(chunks)

    prompt = (
        "Cluster excerpts:\n\n"
        + _format_chunks_for_prompt(chunks)
        + "\n\nReturn only the 2–3 sentence summary."
    )
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": _CLUSTER_SUMMARY_SYSTEM},
            {"role": "user", "content": prompt},
        ],
    }
    response = client.chat.completions.create(**payload)
    text = _extract_chat_text(response).strip()
    return text or _fallback_summary(chunks)


def _extract_chat_text(response: Any) -> str:
    """Pull ``choices[0].message.content`` from a chat completion response.

    Mirrors the translator's tolerant SDK-or-dict resolver so this module is
    independently testable with either real SDK objects or hand-crafted
    dicts.
    """
    if response is None:
        return ""
    choices = (
        response["choices"] if isinstance(response, dict) else getattr(response, "choices", None)
    )
    if not choices:
        return ""
    first = choices[0]
    message = first["message"] if isinstance(first, dict) else getattr(first, "message", None)
    if message is None:
        return ""
    content = (
        message["content"] if isinstance(message, dict) else getattr(message, "content", "")
    )
    return content or ""


def _fallback_title(chunks: list[Chunk]) -> str:
    """Heading-derived title used when no LLM client is available."""
    if not chunks:
        return "Untitled Cluster"
    for chunk in chunks:
        if chunk.heading_chain:
            return " ".join(chunk.heading_chain[:3])[:80] or "Untitled Cluster"
    # No headings anywhere — derive from first non-empty line of first chunk.
    first = chunks[0].body.strip().splitlines()
    if first:
        return first[0][:80]
    return "Untitled Cluster"


def _fallback_summary(chunks: list[Chunk]) -> str:
    """Plain-text summary stub used when no LLM client is available."""
    n = len(chunks)
    return f"Cluster of {n} chunks grouped by embedding similarity."


# ---------------------------------------------------------------------------
# MOC rendering + filesystem write
# ---------------------------------------------------------------------------


_SLUG_BAD_CHARS = re.compile(r"[^a-z0-9]+")


def slugify(title: str) -> str:
    """Convert a title to a filename-safe slug.

    Lowercase ASCII, dashes for non-alphanumerics, max 60 chars. Empty input
    or input that contains no alphanumeric character collapses to ``"moc"``
    so the output is always a valid filename component.
    """
    lowered = title.strip().lower()
    slug = _SLUG_BAD_CHARS.sub("-", lowered).strip("-")
    slug = slug[:60].rstrip("-")
    return slug or "moc"


def _vault_relative_wikilink(source_path: Path) -> str:
    """Render an Obsidian-style wikilink for a vault-relative source path.

    Obsidian's `[[...]]` resolver works with either a basename or a
    vault-relative path. We emit the full path-without-extension so links
    stay unambiguous even when two notes share a filename across folders.
    """
    raw = str(source_path)
    if raw.endswith(".md"):
        raw = raw[:-3]
    return f"[[{raw}]]"


def render_moc_markdown(
    title: str,
    cluster_size: int,
    top_chunks: list[Chunk],
    summary: str,
) -> str:
    """Render the MOC markdown body. Pure function — no I/O."""
    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"**Cluster size:** {cluster_size} chunks")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(summary.strip())
    lines.append("")
    lines.append("## Top central chunks")
    lines.append("")
    for chunk in top_chunks:
        wikilink = _vault_relative_wikilink(chunk.source_path)
        head = " › ".join(chunk.heading_chain) if chunk.heading_chain else "(no heading)"
        lines.append(f"- {wikilink} — {head}")
    lines.append("")
    return "\n".join(lines)


def _assert_outside_vault(out_dir: Path, vault_root: Path | None) -> None:
    """Refuse to write MOC files inside the Obsidian vault directory.

    This is the seed contract's vault-immutability guarantee enforced at the
    write boundary. Resolves both paths so a relative ``vault_root`` or a
    symlink cannot smuggle the output under the vault tree.
    """
    if vault_root is None:
        return
    out_resolved = out_dir.resolve()
    vault_resolved = vault_root.resolve()
    try:
        out_resolved.relative_to(vault_resolved)
    except ValueError:
        return
    raise ValueError(
        f"refusing to write MOC files inside vault_root={vault_resolved!s} "
        f"(out_dir resolves to {out_resolved!s})"
    )


def write_moc_file(
    out_dir: Path,
    slug: str,
    markdown: str,
    *,
    vault_root: Path | None = None,
) -> Path:
    """Write a single MOC markdown file to ``out_dir/MOC-<slug>.md``.

    Creates ``out_dir`` if needed. Raises if ``out_dir`` resolves inside
    ``vault_root``. Returns the path to the written file.
    """
    out_dir = Path(out_dir)
    _assert_outside_vault(out_dir, vault_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"MOC-{slug}.md"
    path.write_text(markdown, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class MocResult:
    """One generated MOC: where it lives + how big its cluster was."""

    cluster_id: int
    cluster_size: int
    title: str
    slug: str
    path: Path
    top_chunk_ids: list[str] = field(default_factory=list)


def generate_mocs(
    conn: sqlite3.Connection,
    *,
    out_dir: Path,
    n: int = 10,
    algo: str = "hdbscan",
    min_cluster_size: int = 10,
    min_samples: int = 5,
    top_k: int = 5,
    client: _ChatClient | None = None,
    model: str = DEFAULT_MOC_MODEL,
    vault_root: Path | None = None,
) -> list[MocResult]:
    """End-to-end: cluster English chunks and write the top-``n`` MOCs.

    Pipeline:

    1. :func:`load_embedded_english_chunks` — read all EN rows + vectors.
    2. :func:`cluster_embeddings` — HDBSCAN (or k-means fallback).
    3. :func:`largest_non_noise_clusters` — pick the top ``n`` cluster ids.
    4. For each cluster:
        a. :func:`top_central_indices` — pick top-``top_k`` central chunks.
        b. :func:`generate_topic_title` + :func:`generate_cluster_summary` —
           LLM-name and LLM-summarize. When ``client`` is ``None`` both fall
           back to deterministic heading-derived text.
        c. :func:`write_moc_file` — write ``MOC-<slug>.md`` under ``out_dir``.
           Refuses if ``out_dir`` resolves inside ``vault_root``.

    Returns one :class:`MocResult` per MOC actually written, ordered from
    largest cluster to smallest. The returned list may be shorter than ``n``
    when the corpus contains fewer than ``n`` non-noise clusters.
    """
    corpus = load_embedded_english_chunks(conn)
    if corpus.matrix.shape[0] == 0:
        return []

    labels = cluster_embeddings(
        corpus.matrix,
        algo=algo,
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
        k_fallback=n,
    )
    cluster_ids = largest_non_noise_clusters(labels, n=n)

    results: list[MocResult] = []
    used_slugs: set[str] = set()
    for cid in cluster_ids:
        member_indices = [i for i, l in enumerate(labels.tolist()) if int(l) == cid]
        if not member_indices:
            continue
        central = top_central_indices(corpus.matrix, member_indices, top_k=top_k)
        top_chunks = [corpus.chunks[i] for i in central]

        title = generate_topic_title(top_chunks, client=client, model=model)
        summary = generate_cluster_summary(top_chunks, client=client, model=model)
        slug = _unique_slug(slugify(title), used_slugs)
        used_slugs.add(slug)

        markdown = render_moc_markdown(
            title=title,
            cluster_size=len(member_indices),
            top_chunks=top_chunks,
            summary=summary,
        )
        path = write_moc_file(out_dir, slug, markdown, vault_root=vault_root)
        results.append(
            MocResult(
                cluster_id=int(cid),
                cluster_size=len(member_indices),
                title=title,
                slug=slug,
                path=path,
                top_chunk_ids=[c.chunk_id for c in top_chunks],
            )
        )
    return results


def _unique_slug(slug: str, used: set[str]) -> str:
    """Append ``-2``, ``-3``, ... when a slug collides. Stable across runs."""
    if slug not in used:
        return slug
    i = 2
    while f"{slug}-{i}" in used:
        i += 1
    return f"{slug}-{i}"


__all__ = [
    "EmbeddedCorpus",
    "MocResult",
    "load_embedded_english_chunks",
    "cluster_embeddings",
    "largest_non_noise_clusters",
    "cluster_centroid",
    "top_central_indices",
    "generate_topic_title",
    "generate_cluster_summary",
    "render_moc_markdown",
    "slugify",
    "write_moc_file",
    "generate_mocs",
    "DEFAULT_MOC_MODEL",
]
