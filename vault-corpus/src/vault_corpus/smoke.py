"""Smoke-test query loading.

Parse a YAML file of smoke queries into a list of normalized dicts. Each
query has a required ``query`` field (non-empty string) and an optional
``description`` field. Consumed by ``vault-corpus smoke-test`` to gate
"is the corpus usable": each query must return at least 3 results above
the similarity floor for the gate to pass.

The default query set ships alongside this module at
``smoke_queries.yaml`` (see :data:`DEFAULT_QUERIES_PATH`).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from vault_corpus.store import EMBEDDING_MODEL, search_with_scores


# Default query set ships in-package so `load_smoke_queries()` works out of
# the box with no operator config. Path resolves at import time but the file
# is only opened when a caller actually requests it.
DEFAULT_QUERIES_PATH: Path = Path(__file__).parent / "smoke_queries.yaml"

# Default per-query gate parameters. Documented in README and seed contract:
# every smoke query must return at least 3 results with similarity >= 0.20.
# The floor is *inclusive* — a result whose score equals the floor counts
# as "above floor". This matches how operators tune the floor by reading
# observed top-k scores and picking the value at the noise/signal boundary.
DEFAULT_SIMILARITY_FLOOR: float = 0.20
DEFAULT_MIN_RESULTS: int = 3

# Whitelist for per-query keys. Anything else is a typo and we surface it
# loudly so a stale field name doesn't silently become a no-op.
_ALLOWED_FIELDS: frozenset[str] = frozenset({"query", "description"})


class SmokeQueryError(ValueError):
    """Raised when a smoke-queries YAML file is structurally malformed."""


def load_smoke_queries(
    path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Parse a smoke-queries YAML file into a list of query dicts.

    Expected YAML schema::

        queries:
          - query: "how to write daily standup notes"
            description: "daily-ops recall"          # optional
          - query: "transformer paper notes"

    Args:
        path: Filesystem path to the YAML file. If ``None``, the packaged
            default (:data:`DEFAULT_QUERIES_PATH`) is loaded, which ships
            5 queries covering the major topical lobes of the vault.

    Returns:
        A list of dicts. Each dict has:

        - ``"query"`` (``str``, required, non-empty, stripped of nothing —
          we preserve the operator's exact phrasing).
        - ``"description"`` (``str``, present only when supplied in YAML;
          never ``None`` — absent key vs. ``None`` value is a meaningful
          distinction downstream).

    Raises:
        FileNotFoundError: ``path`` (or the default) does not exist.
        SmokeQueryError: YAML is not a mapping, ``queries`` key is missing,
            ``queries`` is not a non-empty list, an item is not a mapping,
            an item carries an unknown field, ``query`` is missing/empty/
            non-string, or ``description`` is non-string.
    """
    p = Path(path) if path is not None else DEFAULT_QUERIES_PATH
    p = p.expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"smoke queries file not found: {p}")

    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise SmokeQueryError(
            "top-level YAML must be a mapping with a `queries:` key "
            f"(got {type(raw).__name__})"
        )
    if "queries" not in raw:
        raise SmokeQueryError("missing top-level `queries:` key")

    items = raw["queries"]
    if not isinstance(items, list):
        raise SmokeQueryError(
            f"`queries` must be a list (got {type(items).__name__})"
        )
    if not items:
        raise SmokeQueryError("`queries` list is empty")

    out: list[dict[str, Any]] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise SmokeQueryError(
                f"queries[{i}] must be a mapping (got {type(item).__name__})"
            )
        extra = set(item) - _ALLOWED_FIELDS
        if extra:
            raise SmokeQueryError(
                f"queries[{i}] has unknown field(s): {sorted(extra)}"
            )

        q = item.get("query")
        if not isinstance(q, str) or not q.strip():
            raise SmokeQueryError(
                f"queries[{i}].query missing or not a non-empty string"
            )

        normalized: dict[str, Any] = {"query": q}
        if "description" in item:
            desc = item["description"]
            if not isinstance(desc, str):
                raise SmokeQueryError(
                    f"queries[{i}].description must be a string "
                    f"(got {type(desc).__name__})"
                )
            normalized["description"] = desc
        out.append(normalized)

    return out


def run_query(
    index: sqlite3.Connection,
    query: str,
    top_k: int = 5,
    *,
    client: Any | None = None,
    model: str = EMBEDDING_MODEL,
) -> list[dict[str, Any]]:
    """Run a single vector search against the English chunk index.

    Embeds ``query`` and returns the top-``top_k`` English chunks by cosine
    similarity. Korean source rows are excluded by the underlying SQL filter
    (see :func:`vault_corpus.store.search_with_scores`), so the smoke-test
    gate is always measured against the English mirror corpus only — which
    is the only space the operator's English queries live in.

    The dict shape is deliberately stable and JSON-friendly so it can flow
    straight into ``vault-corpus smoke-test`` output, a future MCP server,
    or a CLI ``search`` command without reshaping.

    Args:
        index: Open SQLite connection produced by
            :func:`vault_corpus.store.init_db`. Holds the ``chunks`` table
            populated by the build pipeline.
        query: Natural-language English query string. Sent verbatim to
            OpenAI embeddings — no normalization, no truncation.
        top_k: Maximum number of results. Default 5 (the smoke-test gate
            checks "at least 3 above floor", so 5 gives the gate slack
            without inflating API cost). ``<= 0`` returns an empty list.
        client: Optional OpenAI-compatible embedding client. ``None``
            triggers the default :func:`vault_corpus.store._default_client`.
            Injected by unit tests to avoid any network call.
        model: Embedding model id. Defaults to :data:`EMBEDDING_MODEL`.
            Must match the model used to embed the stored chunks.

    Returns:
        List of result dicts, ordered by ``similarity`` descending. Each
        dict has exactly three keys:

        - ``"note_path"`` (``str``): vault-relative path of the originating
          Korean note. Stringified — the original :class:`pathlib.Path`
          would not survive JSON serialization.
        - ``"heading_chain"`` (``list[str]``): ordered ``##``/``###``
          titles leading to the chunk inside its source note. Empty list
          when the note had no ``##`` headings.
        - ``"similarity"`` (``float``): cosine similarity in ``[-1.0, 1.0]``
          between the query embedding and the chunk embedding.
    """
    scored = search_with_scores(
        index, query, top_k, client=client, model=model
    )
    return [
        {
            "note_path": str(chunk.source_path),
            "heading_chain": list(chunk.heading_chain),
            "similarity": similarity,
        }
        for chunk, similarity in scored
    ]


def format_results(
    query: str,
    results: Iterable[Mapping[str, Any]],
    *,
    top_k: int = 5,
) -> str:
    """Render search results as a human-readable text block.

    Designed for terminal output of a ``vault-corpus search`` style command and
    for ``vault-corpus smoke-test`` per-query diagnostics. The output is a
    multi-line string with one **query header line** followed by up to
    ``top_k`` numbered result rows. Every row carries exactly three labelled
    fields — ``path``, ``heading``, ``score`` — so downstream eyeballing and
    snapshot tests can rely on a stable shape.

    The format is deliberately plain text (no ANSI, no tables): it stays
    diff-friendly, paste-friendly into MOC/note files, and trivially testable.
    No trailing newline — callers ``print()`` it and the print adds the final
    newline themselves.

    Args:
        query: The query string the results were retrieved for. Rendered
            verbatim in the header line (no stripping, no truncation) so an
            empty or whitespace query is visible to the operator as such.
        results: Iterable of result dicts produced by :func:`run_query` (or
            any compatible producer). Each dict must carry ``"note_path"``,
            ``"heading_chain"``, ``"similarity"``. Iteration order is the
            display order — callers that need sorting should sort before
            calling. The iterable is consumed once.
        top_k: Maximum number of rows to render. Default 5 matches the
            smoke-test gate. Extra results are silently dropped; fewer
            results render fewer rows. ``<= 0`` renders the header only.

    Returns:
        Multi-line string. Schema::

            query: <query verbatim>
              1. path: <note_path>
                 heading: <h1 > h2 > h3>          # or "(no headings)" if empty
                 score: 0.8123                    # 4-decimal cosine similarity
              2. path: ...
                 heading: ...
                 score: ...

        When ``results`` is empty (or ``top_k <= 0``), only the header line
        is returned, followed by a single ``  (no results)`` indent line so
        the empty case is visibly distinct from a truncated terminal.

    Raises:
        KeyError: a result dict is missing one of the three required keys.
        TypeError: ``similarity`` is not numeric, or ``heading_chain`` is not
            iterable.
    """
    lines: list[str] = [f"query: {query}"]

    rows = list(results)
    if top_k <= 0 or not rows:
        lines.append("  (no results)")
        return "\n".join(lines)

    # Render at most `top_k` rows; preserve caller iteration order so this
    # function stays a pure formatter (no implicit re-ranking).
    for idx, r in enumerate(rows[:top_k], start=1):
        path = str(r["note_path"])
        chain = list(r["heading_chain"])
        sim = r["similarity"]
        if isinstance(sim, bool) or not isinstance(sim, (int, float)):
            raise TypeError(
                f"similarity must be numeric (got {type(sim).__name__})"
            )
        heading = " > ".join(chain) if chain else "(no headings)"
        lines.append(f"  {idx}. path: {path}")
        lines.append(f"     heading: {heading}")
        lines.append(f"     score: {float(sim):.4f}")

    return "\n".join(lines)


def evaluate_results(
    results: Iterable[Mapping[str, Any]],
    floor: float = DEFAULT_SIMILARITY_FLOOR,
    min_count: int = DEFAULT_MIN_RESULTS,
) -> tuple[bool, int]:
    """Decide whether one query's results clear the smoke-test gate.

    Pure function: no I/O, no embedding calls, no DB. Given the list of
    result dicts produced by :func:`run_query` (or any compatible producer),
    it counts how many entries have ``similarity >= floor`` and returns
    ``(passed, count_above_floor)`` where ``passed`` is
    ``count_above_floor >= min_count``.

    The floor is **inclusive**: a result whose score is exactly equal to
    the floor counts as above. That matches the operator's mental model —
    the floor is the calibrated noise/signal boundary, not a strict
    exclusion.

    Args:
        results: Iterable of result dicts. Each dict must have a numeric
            ``"similarity"`` key; other keys are ignored. The iterable is
            consumed once and may be empty.
        floor: Similarity threshold. A result with
            ``similarity >= floor`` is counted as "above floor". Defaults
            to :data:`DEFAULT_SIMILARITY_FLOOR` (0.20).
        min_count: Minimum number of above-floor results required to pass
            the gate for this query. Defaults to :data:`DEFAULT_MIN_RESULTS`
            (3).

    Returns:
        ``(passed, count_above_floor)`` where ``passed`` is a ``bool`` and
        ``count_above_floor`` is the number of results whose similarity is
        at or above ``floor``.

    Raises:
        ValueError: ``min_count`` is negative.
        KeyError: A result dict has no ``"similarity"`` key.
        TypeError: A result's ``"similarity"`` value is not numeric.
    """
    if min_count < 0:
        raise ValueError(f"min_count must be >= 0 (got {min_count})")

    count = 0
    for r in results:
        sim = r["similarity"]
        # bool is a subclass of int in Python; reject it explicitly so a
        # silent type error upstream doesn't masquerade as a valid score.
        if isinstance(sim, bool) or not isinstance(sim, (int, float)):
            raise TypeError(
                f"similarity must be numeric (got {type(sim).__name__})"
            )
        if sim >= floor:
            count += 1
    return (count >= min_count, count)
