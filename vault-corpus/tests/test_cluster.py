"""Tests for the clustering + MOC generation module.

Covers:

* loading English chunks + embeddings from the store
* cluster_embeddings (HDBSCAN + k-means fallback)
* largest_non_noise_clusters ranking
* cluster_centroid + top_central_indices ranking
* slugify, render_moc_markdown, wikilink format
* generate_mocs end-to-end with deterministic fake clustering
* vault-immutability guard on write_moc_file
* LLM title/summary path with a fake chat client
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from vault_corpus import cluster as cluster_mod
from vault_corpus.chunker import Chunk, compute_chunk_id
from vault_corpus.store import EMBEDDING_DIM, init_db, upsert_chunk


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _basis_vec(index: int, dim: int = EMBEDDING_DIM) -> list[float]:
    v = [0.0] * dim
    v[index] = 1.0
    return v


def _seed_clustered_corpus(conn: sqlite3.Connection) -> dict[str, str]:
    """Seed an English corpus with three obvious clusters + noise.

    Cluster A: 12 chunks near basis_0 (with small noise on basis 1000)
    Cluster B: 11 chunks near basis_1 (with small noise on basis 1001)
    Cluster C: 10 chunks near basis_2 (with small noise on basis 1002)
    Noise: 3 chunks at basis_500, basis_600, basis_700 (isolated)

    Returns ``{label: chunk_id}`` mapping.
    """
    label_to_id: dict[str, str] = {}

    def _add(label: str, source: str, head: str, body: str, vec: list[float]) -> None:
        src = Path(source)
        chain = [head]
        cid = compute_chunk_id(src, chain, body)
        label_to_id[label] = cid
        upsert_chunk(
            conn,
            Chunk(
                source_path=src,
                heading_chain=chain,
                body=body,
                chunk_id=cid,
                lang="en",
                frontmatter={"label": label},
            ),
            vec,
        )

    rng = np.random.default_rng(0)
    for i in range(12):
        v = _basis_vec(0)
        # tiny noise so HDBSCAN sees a real density rather than identical points
        v[1000 + i] = float(rng.uniform(0.01, 0.05))
        _add(
            f"A{i}",
            f"01 Command Center/topic-a-{i}.md",
            f"Alpha {i}",
            f"alpha cluster body {i}\n",
            v,
        )
    for i in range(11):
        v = _basis_vec(1)
        v[1100 + i] = float(rng.uniform(0.01, 0.05))
        _add(
            f"B{i}",
            f"01 Command Center/topic-b-{i}.md",
            f"Beta {i}",
            f"beta cluster body {i}\n",
            v,
        )
    for i in range(10):
        v = _basis_vec(2)
        v[1200 + i] = float(rng.uniform(0.01, 0.05))
        _add(
            f"C{i}",
            f"01 Command Center/topic-c-{i}.md",
            f"Gamma {i}",
            f"gamma cluster body {i}\n",
            v,
        )

    # Isolated noise points (orthogonal to every cluster centroid)
    for i, basis in enumerate((500, 600, 700)):
        _add(
            f"N{i}",
            f"01 Command Center/noise-{i}.md",
            f"Noise {i}",
            f"noise body {i}\n",
            _basis_vec(basis),
        )

    return label_to_id


@pytest.fixture()
def conn(tmp_path: Path):
    c = init_db(tmp_path / "corpus.db")
    try:
        yield c
    finally:
        c.close()


# ---------------------------------------------------------------------------
# load_embedded_english_chunks
# ---------------------------------------------------------------------------


def test_load_embedded_english_chunks_excludes_korean_and_unembedded(
    conn: sqlite3.Connection,
) -> None:
    src_en = Path("01 Command Center/en.md")
    src_ko = Path("00 Get Things Done/ko.md")
    src_pending = Path("01 Command Center/pending.md")

    cid_en = compute_chunk_id(src_en, ["A"], "en body\n")
    cid_ko = compute_chunk_id(src_ko, ["가"], "ko body\n")
    cid_pending = compute_chunk_id(src_pending, ["P"], "pending body\n")

    upsert_chunk(
        conn,
        Chunk(
            source_path=src_en,
            heading_chain=["A"],
            body="en body\n",
            chunk_id=cid_en,
            lang="en",
            frontmatter={},
        ),
        _basis_vec(0),
    )
    upsert_chunk(
        conn,
        Chunk(
            source_path=src_ko,
            heading_chain=["가"],
            body="ko body\n",
            chunk_id=cid_ko,
            lang="ko",
            frontmatter={},
        ),
        _basis_vec(1),
    )
    upsert_chunk(
        conn,
        Chunk(
            source_path=src_pending,
            heading_chain=["P"],
            body="pending body\n",
            chunk_id=cid_pending,
            lang="en",
            frontmatter={},
        ),
        None,
    )

    corpus = cluster_mod.load_embedded_english_chunks(conn)
    assert len(corpus.chunks) == 1
    assert corpus.chunks[0].chunk_id == cid_en
    assert corpus.matrix.shape == (1, EMBEDDING_DIM)
    assert corpus.matrix.dtype == np.float32


def test_load_embedded_english_chunks_empty_db_returns_empty(
    conn: sqlite3.Connection,
) -> None:
    corpus = cluster_mod.load_embedded_english_chunks(conn)
    assert corpus.chunks == []
    assert corpus.matrix.shape[0] == 0


# ---------------------------------------------------------------------------
# Clustering — HDBSCAN + k-means fallback
# ---------------------------------------------------------------------------


def test_cluster_embeddings_hdbscan_finds_three_clusters(conn: sqlite3.Connection) -> None:
    _seed_clustered_corpus(conn)
    corpus = cluster_mod.load_embedded_english_chunks(conn)
    labels = cluster_mod.cluster_embeddings(
        corpus.matrix,
        algo="hdbscan",
        min_cluster_size=5,
        min_samples=2,
        k_fallback=3,
    )
    non_noise = {int(l) for l in labels if int(l) != -1}
    assert len(non_noise) >= 3, f"expected ≥3 clusters, got {non_noise}"


def test_cluster_embeddings_kmeans_assigns_every_point(conn: sqlite3.Connection) -> None:
    _seed_clustered_corpus(conn)
    corpus = cluster_mod.load_embedded_english_chunks(conn)
    labels = cluster_mod.cluster_embeddings(
        corpus.matrix, algo="kmeans", k_fallback=3
    )
    assert (-1 not in set(int(l) for l in labels))
    assert len(set(int(l) for l in labels)) == 3


def test_cluster_embeddings_falls_back_to_kmeans_when_hdbscan_underproduces(
    monkeypatch, conn: sqlite3.Connection
) -> None:
    """When HDBSCAN returns < k clusters, we must fall back to k-means."""
    _seed_clustered_corpus(conn)
    corpus = cluster_mod.load_embedded_english_chunks(conn)

    def fake_hdbscan(_normalized, *, min_cluster_size, min_samples):  # noqa: ARG001
        # Pretend HDBSCAN only found 1 cluster.
        return np.zeros((corpus.matrix.shape[0],), dtype=np.int32)

    monkeypatch.setattr(cluster_mod, "_hdbscan_labels", fake_hdbscan)
    labels = cluster_mod.cluster_embeddings(corpus.matrix, k_fallback=5)
    non_noise = {int(l) for l in labels if int(l) != -1}
    # Fallback ran → should now have 5 buckets.
    assert len(non_noise) == 5


def test_cluster_embeddings_empty_matrix_returns_empty_labels() -> None:
    labels = cluster_mod.cluster_embeddings(np.zeros((0, 0), dtype=np.float32))
    assert labels.shape == (0,)


def test_cluster_embeddings_rejects_unknown_algo() -> None:
    with pytest.raises(ValueError, match="unsupported algo"):
        cluster_mod.cluster_embeddings(
            np.zeros((1, 3), dtype=np.float32), algo="dbscan"
        )


# ---------------------------------------------------------------------------
# largest_non_noise_clusters ordering
# ---------------------------------------------------------------------------


def test_largest_non_noise_clusters_orders_by_size_then_id() -> None:
    labels = np.array([-1, -1, 0, 0, 0, 1, 1, 2, 2, 2, 2, 3], dtype=np.int32)
    # sizes: 0=3, 1=2, 2=4, 3=1 → ranked: 2, 0, 1, 3
    top = cluster_mod.largest_non_noise_clusters(labels, n=4)
    assert top == [2, 0, 1, 3]


def test_largest_non_noise_clusters_skips_noise() -> None:
    labels = np.array([-1, -1, -1, 0], dtype=np.int32)
    assert cluster_mod.largest_non_noise_clusters(labels, n=10) == [0]


def test_largest_non_noise_clusters_returns_fewer_than_n_when_short() -> None:
    labels = np.array([0, 1], dtype=np.int32)
    assert cluster_mod.largest_non_noise_clusters(labels, n=10) == [0, 1]


def test_largest_non_noise_clusters_ties_break_by_id() -> None:
    labels = np.array([0, 0, 1, 1, 2, 2], dtype=np.int32)
    # All size 2 — ascending cluster id wins.
    assert cluster_mod.largest_non_noise_clusters(labels, n=3) == [0, 1, 2]


# ---------------------------------------------------------------------------
# Centrality
# ---------------------------------------------------------------------------


def test_top_central_indices_ranks_by_cosine_to_centroid() -> None:
    # 3 members: two near basis 0, one outlier toward basis 1.
    matrix = np.array(
        [
            [1.0, 0.0, 0.0],
            [0.95, 0.31, 0.0],
            [0.0, 1.0, 0.0],
        ],
        dtype=np.float32,
    )
    ranked = cluster_mod.top_central_indices(matrix, [0, 1, 2], top_k=3)
    # 0 and 1 should beat 2 since centroid lies along basis 0.
    assert ranked[-1] == 2


def test_top_central_indices_empty_members_returns_empty() -> None:
    matrix = np.zeros((5, 3), dtype=np.float32)
    assert cluster_mod.top_central_indices(matrix, [], top_k=5) == []


def test_top_central_indices_respects_top_k_cap() -> None:
    matrix = np.eye(5, dtype=np.float32)
    ranked = cluster_mod.top_central_indices(matrix, [0, 1, 2, 3, 4], top_k=2)
    assert len(ranked) == 2


# ---------------------------------------------------------------------------
# Slug + wikilink + markdown rendering
# ---------------------------------------------------------------------------


def test_slugify_lowercases_and_dashes() -> None:
    assert cluster_mod.slugify("My Topic Title") == "my-topic-title"


def test_slugify_strips_non_alphanumeric() -> None:
    assert cluster_mod.slugify("LLM/RAG: A Map!") == "llm-rag-a-map"


def test_slugify_empty_input_becomes_moc() -> None:
    assert cluster_mod.slugify("   ") == "moc"
    assert cluster_mod.slugify("...") == "moc"


def test_slugify_caps_length_at_60() -> None:
    long = "a" * 200
    assert len(cluster_mod.slugify(long)) <= 60


def test_render_moc_markdown_contains_title_size_summary_and_wikilinks() -> None:
    chunks = [
        Chunk(
            source_path=Path("01 Command Center/note-one.md"),
            heading_chain=["Alpha"],
            body="alpha body\n",
            chunk_id="c1",
            lang="en",
            frontmatter={},
        ),
        Chunk(
            source_path=Path("03 Resources/sub/note-two.md"),
            heading_chain=["Beta", "Subtopic"],
            body="beta body\n",
            chunk_id="c2",
            lang="en",
            frontmatter={},
        ),
    ]
    md = cluster_mod.render_moc_markdown(
        title="Topic Title",
        cluster_size=42,
        top_chunks=chunks,
        summary="Two-sentence summary. Of the cluster.",
    )
    assert md.startswith("# Topic Title")
    assert "Cluster size:** 42" in md
    assert "Two-sentence summary." in md
    assert "[[01 Command Center/note-one]]" in md
    assert "[[03 Resources/sub/note-two]]" in md
    # Heading chain rendered next to the wikilink for context.
    assert "Beta › Subtopic" in md


# ---------------------------------------------------------------------------
# Vault immutability guard
# ---------------------------------------------------------------------------


def test_write_moc_file_refuses_inside_vault(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    inside = vault / "MOCs"
    with pytest.raises(ValueError, match="vault_root"):
        cluster_mod.write_moc_file(
            inside, "topic", "# x\n", vault_root=vault
        )
    assert not inside.exists(), "must not create directory inside vault"


def test_write_moc_file_outside_vault_succeeds(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    out = tmp_path / "project" / "data" / "moc_samples"
    path = cluster_mod.write_moc_file(out, "topic", "# x\n", vault_root=vault)
    assert path.read_text(encoding="utf-8") == "# x\n"
    assert path.name == "MOC-topic.md"


def test_write_moc_file_no_vault_root_writes_anywhere(tmp_path: Path) -> None:
    out = tmp_path / "anywhere"
    path = cluster_mod.write_moc_file(out, "topic", "# x\n", vault_root=None)
    assert path.exists()


# ---------------------------------------------------------------------------
# LLM-backed title + summary
# ---------------------------------------------------------------------------


class _FakeChatCompletions:
    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def create(self, **payload: Any) -> dict[str, Any]:
        self.calls.append(payload)
        text = self._replies.pop(0) if self._replies else ""
        return {
            "choices": [
                {"message": {"content": text}, "finish_reason": "stop", "index": 0}
            ]
        }


class _FakeChat:
    def __init__(self, replies: list[str]) -> None:
        self.completions = _FakeChatCompletions(replies)


class _FakeChatClient:
    def __init__(self, replies: list[str]) -> None:
        self.chat = _FakeChat(replies)


def _make_chunks(n: int) -> list[Chunk]:
    out: list[Chunk] = []
    for i in range(n):
        src = Path(f"01 Command Center/note-{i}.md")
        chain = [f"Heading {i}"]
        body = f"## Heading {i}\nbody {i}\n"
        out.append(
            Chunk(
                source_path=src,
                heading_chain=chain,
                body=body,
                chunk_id=compute_chunk_id(src, chain, body),
                lang="en",
                frontmatter={},
            )
        )
    return out


def test_generate_topic_title_uses_llm_response() -> None:
    client = _FakeChatClient(["Distributed Systems Notes"])
    title = cluster_mod.generate_topic_title(_make_chunks(3), client=client)
    assert title == "Distributed Systems Notes"
    assert client.chat.completions.calls[0]["temperature"] == 0


def test_generate_topic_title_strips_trailing_punctuation_and_quotes() -> None:
    client = _FakeChatClient(['"Bayesian Inference Patterns."'])
    assert (
        cluster_mod.generate_topic_title(_make_chunks(2), client=client)
        == "Bayesian Inference Patterns"
    )


def test_generate_topic_title_falls_back_when_no_client() -> None:
    chunks = _make_chunks(2)
    title = cluster_mod.generate_topic_title(chunks, client=None)
    assert "Heading 0" in title


def test_generate_cluster_summary_uses_llm_response() -> None:
    client = _FakeChatClient(["Two sentences. About the cluster."])
    summary = cluster_mod.generate_cluster_summary(_make_chunks(2), client=client)
    assert summary == "Two sentences. About the cluster."


def test_generate_cluster_summary_falls_back_when_no_client() -> None:
    summary = cluster_mod.generate_cluster_summary(_make_chunks(7), client=None)
    assert "7" in summary


# ---------------------------------------------------------------------------
# End-to-end generate_mocs
# ---------------------------------------------------------------------------


def test_generate_mocs_writes_top_n_files_outside_vault(
    monkeypatch, tmp_path: Path, conn: sqlite3.Connection
) -> None:
    _seed_clustered_corpus(conn)

    # Force the clustering output to a known shape so the test does not
    # depend on HDBSCAN heuristics.
    n_rows = cluster_mod.load_embedded_english_chunks(conn).matrix.shape[0]

    def fake_cluster(matrix, **_kwargs):  # noqa: ARG001
        # First 12 → cluster 0, next 11 → cluster 1, next 10 → cluster 2,
        # remaining 3 → noise (-1).
        labels = np.full((n_rows,), -1, dtype=np.int32)
        labels[:12] = 0
        labels[12:23] = 1
        labels[23:33] = 2
        return labels

    monkeypatch.setattr(cluster_mod, "cluster_embeddings", fake_cluster)

    out_dir = tmp_path / "data" / "moc_samples"
    vault = tmp_path / "obsidian_vault"
    vault.mkdir()

    results = cluster_mod.generate_mocs(
        conn,
        out_dir=out_dir,
        n=3,
        client=None,
        vault_root=vault,
    )

    assert len(results) == 3
    # Largest cluster first.
    assert [r.cluster_size for r in results] == [12, 11, 10]
    for r in results:
        assert r.path.exists()
        assert r.path.parent == out_dir
        assert r.path.name.startswith("MOC-")
        # Vault is untouched.
        assert not any(vault.rglob("*"))


def test_generate_mocs_returns_empty_when_no_english_chunks(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    out_dir = tmp_path / "data" / "moc_samples"
    results = cluster_mod.generate_mocs(conn, out_dir=out_dir, n=5, client=None)
    assert results == []
    assert not out_dir.exists()


def test_generate_mocs_refuses_to_write_under_vault_root(
    monkeypatch, tmp_path: Path, conn: sqlite3.Connection
) -> None:
    _seed_clustered_corpus(conn)
    n_rows = cluster_mod.load_embedded_english_chunks(conn).matrix.shape[0]

    def fake_cluster(matrix, **_kwargs):  # noqa: ARG001
        labels = np.zeros((n_rows,), dtype=np.int32)
        return labels

    monkeypatch.setattr(cluster_mod, "cluster_embeddings", fake_cluster)

    vault = tmp_path / "vault"
    vault.mkdir()
    inside = vault / "MOCs"
    with pytest.raises(ValueError, match="vault_root"):
        cluster_mod.generate_mocs(
            conn, out_dir=inside, n=1, client=None, vault_root=vault
        )
    assert not inside.exists()


def test_generate_mocs_uses_llm_title_when_client_provided(
    monkeypatch, tmp_path: Path, conn: sqlite3.Connection
) -> None:
    _seed_clustered_corpus(conn)
    n_rows = cluster_mod.load_embedded_english_chunks(conn).matrix.shape[0]

    def fake_cluster(matrix, **_kwargs):  # noqa: ARG001
        # Two clusters of equal size to force 2 LLM calls per item.
        labels = np.full((n_rows,), -1, dtype=np.int32)
        labels[:12] = 0
        labels[12:23] = 1
        return labels

    monkeypatch.setattr(cluster_mod, "cluster_embeddings", fake_cluster)

    # 2 clusters × (title + summary) = 4 replies.
    client = _FakeChatClient(
        ["First Topic", "First summary.", "Second Topic", "Second summary."]
    )

    results = cluster_mod.generate_mocs(
        conn,
        out_dir=tmp_path / "moc_out",
        n=2,
        client=client,
    )
    titles = [r.title for r in results]
    assert titles == ["First Topic", "Second Topic"]
    # Each MOC file embeds the LLM title in its H1.
    for r, expected in zip(results, ["First Topic", "Second Topic"]):
        first_line = r.path.read_text(encoding="utf-8").splitlines()[0]
        assert first_line == f"# {expected}"
