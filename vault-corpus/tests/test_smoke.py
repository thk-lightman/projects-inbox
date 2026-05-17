"""Tests for vault_corpus.smoke.

Covers:

- Sub-AC 8.1 — :func:`load_smoke_queries` YAML parsing + default file.
- Sub-AC 8.2 — :func:`run_query` shape and similarity-desc ordering against
  a seeded in-memory/tmp SQLite-VSS index fixture, with a fake OpenAI
  embeddings client so no real network call ever fires.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from vault_corpus.chunker import Chunk, compute_chunk_id
from vault_corpus.smoke import (
    DEFAULT_MIN_RESULTS,
    DEFAULT_QUERIES_PATH,
    DEFAULT_SIMILARITY_FLOOR,
    SmokeQueryError,
    evaluate_results,
    format_results,
    load_smoke_queries,
    run_query,
)
from vault_corpus.store import EMBEDDING_DIM, EMBEDDING_MODEL, init_db, upsert_chunk


FIXTURE_YAML = """\
queries:
  - query: "how to write daily standup notes"
    description: "daily-ops productivity recall"
  - query: "transformer paper notes"
  - query: "personal goal setting framework"
    description: "vision-center recall"
  - query: "PARA method resource organization"
  - query: "python testing best practices"
    description: "tooling / craft recall"
"""


def test_load_smoke_queries_returns_5_queries_with_expected_schema(
    tmp_path: Path,
) -> None:
    """Fixture YAML round-trips into 5 dicts of the documented shape."""
    f = tmp_path / "smoke.yaml"
    f.write_text(FIXTURE_YAML, encoding="utf-8")

    queries = load_smoke_queries(f)

    assert isinstance(queries, list)
    assert len(queries) == 5

    for i, q in enumerate(queries):
        assert isinstance(q, dict), f"queries[{i}] not a dict"
        assert "query" in q, f"queries[{i}] missing `query`"
        assert isinstance(q["query"], str)
        assert q["query"].strip(), f"queries[{i}].query is empty"
        # description is optional; when present it must be str (never None).
        if "description" in q:
            assert isinstance(q["description"], str)
        # No fields outside the whitelist leak through.
        assert set(q).issubset({"query", "description"})

    # Spot-check exact content + the "absent key, not None" semantics.
    assert queries[0]["query"] == "how to write daily standup notes"
    assert queries[0]["description"] == "daily-ops productivity recall"
    assert "description" not in queries[1]
    assert "description" not in queries[3]


def test_default_smoke_queries_yaml_ships_and_has_5_queries() -> None:
    """The packaged default file exists and loads to exactly 5 queries."""
    assert DEFAULT_QUERIES_PATH.is_file(), (
        f"default smoke_queries.yaml missing at {DEFAULT_QUERIES_PATH}"
    )
    queries = load_smoke_queries()  # no arg -> default
    assert len(queries) == 5
    for q in queries:
        assert isinstance(q["query"], str)
        assert q["query"].strip()


def test_load_smoke_queries_accepts_str_path(tmp_path: Path) -> None:
    f = tmp_path / "smoke.yaml"
    f.write_text(FIXTURE_YAML, encoding="utf-8")
    queries = load_smoke_queries(str(f))
    assert len(queries) == 5


def test_missing_file_raises_filenotfounderror(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_smoke_queries(tmp_path / "does_not_exist.yaml")


@pytest.mark.parametrize(
    "yaml_text",
    [
        # empty list
        "queries: []\n",
        # `queries` not a list
        "queries: 5\n",
        # top-level not a mapping
        "- query: foo\n",
        # missing top-level `queries`
        "foo: bar\n",
        # item not a mapping
        "queries:\n  - hello\n",
        # unknown field
        "queries:\n  - query: ok\n    weight: 1.0\n",
        # missing `query`
        "queries:\n  - description: lonely\n",
        # empty `query`
        "queries:\n  - query: ''\n",
        # `query` not str
        "queries:\n  - query: 5\n",
        # `description` not str
        "queries:\n  - query: ok\n    description: 5\n",
    ],
)
def test_malformed_yaml_raises_smoke_query_error(
    tmp_path: Path, yaml_text: str
) -> None:
    f = tmp_path / "bad.yaml"
    f.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(SmokeQueryError):
        load_smoke_queries(f)


# ---------------------------------------------------------------------------
# Sub-AC 8.2 — run_query: single vector search returning dicts.
#
# Mirrors the seeded-orthonormal-basis pattern from test_store.py so the
# expected top-1 and the full ranking are provable without floating-point
# slack, and so the embeddings client is fully faked (zero network I/O).
# ---------------------------------------------------------------------------


@dataclass
class _FakeEmbedding:
    embedding: list[float]


@dataclass
class _FakeResponse:
    data: list[_FakeEmbedding]


class _MappedEmbeddingsAPI:
    """Fake embeddings client keyed on the literal query string.

    Lets a test pin a specific query vector to a specific seeded chunk's
    basis vector, so the expected top-1 is deterministic.
    """

    def __init__(self, mapping: dict[str, list[float]]):
        self.mapping = mapping
        self.calls: list[dict[str, Any]] = []

    def create(self, *, model: str, input: str) -> _FakeResponse:
        self.calls.append({"model": model, "input": input})
        if input not in self.mapping:
            raise KeyError(f"no fake embedding registered for query={input!r}")
        return _FakeResponse(data=[_FakeEmbedding(embedding=self.mapping[input])])


class _MappedOpenAIClient:
    def __init__(self, mapping: dict[str, list[float]]):
        self.embeddings = _MappedEmbeddingsAPI(mapping)


def _unit_vec(index: int, dim: int = EMBEDDING_DIM) -> list[float]:
    """Standard-basis vector ``e_index`` of length ``dim``."""
    v = [0.0] * dim
    v[index] = 1.0
    return v


@pytest.fixture()
def seeded_index(tmp_path: Path):
    """An open SQLite index seeded with 3 EN + 2 KR chunks (orthonormal embeds).

    EN chunks use basis vectors e_0, e_1, e_2 so a query vector with weights
    on multiple bases lets us assert the exact descending order. The KR
    chunks are present to prove ``run_query`` filters them out — the smoke
    gate measures the English mirror corpus only.
    """
    conn = init_db(tmp_path / "corpus.db")
    rows = [
        ("en1", "en", "01 Command Center/en-alpha.md", ["Alpha"], "english alpha body\n", 0),
        ("en2", "en", "01 Command Center/en-beta.md",  ["Beta"],  "english beta body\n",  1),
        ("en3", "en", "01 Command Center/en-gamma.md", ["Gamma"], "english gamma body\n", 2),
        ("kr1", "ko", "00 Get Things Done/kr-one.md", ["하나"],   "한국어 본문 1\n",       3),
        ("kr2", "ko", "00 Get Things Done/kr-two.md", ["둘"],     "한국어 본문 2\n",       4),
    ]
    label_to_id: dict[str, str] = {}
    for label, lang, src, chain, body, basis in rows:
        src_path = Path(src)
        cid = compute_chunk_id(src_path, chain, body)
        label_to_id[label] = cid
        upsert_chunk(
            conn,
            Chunk(
                source_path=src_path,
                heading_chain=chain,
                body=body,
                chunk_id=cid,
                lang=lang,
                frontmatter={"label": label},
            ),
            _unit_vec(basis),
        )
    try:
        yield conn, label_to_id
    finally:
        conn.close()


def test_run_query_returns_dicts_with_required_shape(seeded_index) -> None:
    conn, _ids = seeded_index
    client = _MappedOpenAIClient({"q": _unit_vec(1)})  # → en2

    results = run_query(conn, "q", top_k=3, client=client)

    assert isinstance(results, list)
    assert len(results) == 3, "3 EN chunks seeded → 3 results expected"

    for i, r in enumerate(results):
        assert isinstance(r, dict), f"results[{i}] not a dict: {type(r).__name__}"
        assert set(r.keys()) == {"note_path", "heading_chain", "similarity"}, (
            f"results[{i}] keys {set(r.keys())} != expected"
        )
        assert isinstance(r["note_path"], str)
        assert isinstance(r["heading_chain"], list)
        assert all(isinstance(s, str) for s in r["heading_chain"])
        assert isinstance(r["similarity"], float)
        assert -1.0 - 1e-6 <= r["similarity"] <= 1.0 + 1e-6


def test_run_query_orders_results_by_similarity_descending(seeded_index) -> None:
    """Top-k order must be by ``similarity`` descending.

    Build a query with strong projection on en1, weaker on en2, tiny on en3.
    Expected order: en1, en2, en3 — and similarity values strictly decreasing.
    """
    conn, _ids = seeded_index
    q = [0.0] * EMBEDDING_DIM
    q[0] = 0.9   # → en1
    q[1] = 0.4   # → en2
    q[2] = 0.1   # → en3
    client = _MappedOpenAIClient({"ranked": q})

    results = run_query(conn, "ranked", top_k=3, client=client)

    sims = [r["similarity"] for r in results]
    assert sims == sorted(sims, reverse=True), f"not desc-ordered: {sims}"
    assert sims[0] > sims[1] > sims[2], f"expected strict desc, got {sims}"

    paths = [r["note_path"] for r in results]
    assert paths == [
        "01 Command Center/en-alpha.md",
        "01 Command Center/en-beta.md",
        "01 Command Center/en-gamma.md",
    ]


def test_run_query_excludes_korean_chunks(seeded_index) -> None:
    """Smoke gate measures English mirror corpus only — KR rows must not leak."""
    conn, _ids = seeded_index
    # Query vector identical to a Korean chunk's basis (e_3) — if filter is
    # broken, that KR row would top the result by cosine = 1.0.
    client = _MappedOpenAIClient({"q": _unit_vec(3)})  # → kr1's basis

    results = run_query(conn, "q", top_k=5, client=client)

    for r in results:
        assert not r["note_path"].startswith("00 Get Things Done"), (
            f"Korean chunk leaked: {r['note_path']}"
        )
    # All returned chunks should be English (3 EN seeded → 3 results).
    assert len(results) == 3


def test_run_query_top1_is_correct_chunk(seeded_index) -> None:
    conn, _ids = seeded_index
    client = _MappedOpenAIClient({"find beta": _unit_vec(1)})  # basis 1 = en2

    results = run_query(conn, "find beta", top_k=3, client=client)

    assert results[0]["note_path"] == "01 Command Center/en-beta.md"
    assert results[0]["heading_chain"] == ["Beta"]
    # Orthonormal seed → exact cosine 1.0 against the matching basis query.
    assert results[0]["similarity"] == pytest.approx(1.0)
    # And the other two are exactly 0.0 (orthogonal bases).
    assert results[1]["similarity"] == pytest.approx(0.0, abs=1e-6)
    assert results[2]["similarity"] == pytest.approx(0.0, abs=1e-6)


def test_run_query_default_top_k_is_5(seeded_index) -> None:
    conn, _ids = seeded_index
    client = _MappedOpenAIClient({"q": _unit_vec(0)})
    # Only 3 EN chunks seeded → expect 3 results even though top_k defaults to 5.
    results = run_query(conn, "q", client=client)
    assert len(results) == 3


def test_run_query_respects_top_k_limit(seeded_index) -> None:
    conn, _ids = seeded_index
    client = _MappedOpenAIClient({"q": _unit_vec(0)})
    results = run_query(conn, "q", top_k=1, client=client)
    assert len(results) == 1
    assert results[0]["note_path"] == "01 Command Center/en-alpha.md"


def test_run_query_top_k_zero_skips_embedding_call(seeded_index) -> None:
    conn, _ids = seeded_index
    client = _MappedOpenAIClient({})  # would KeyError if embed was called
    results = run_query(conn, "anything", top_k=0, client=client)
    assert results == []
    assert client.embeddings.calls == []


def test_run_query_empty_index_returns_empty_list(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "empty.db")
    try:
        client = _MappedOpenAIClient({"q": _unit_vec(0)})
        assert run_query(conn, "q", top_k=5, client=client) == []
    finally:
        conn.close()


def test_run_query_passes_correct_model_to_embed(seeded_index) -> None:
    conn, _ids = seeded_index
    client = _MappedOpenAIClient({"q": _unit_vec(0)})
    run_query(conn, "q", top_k=1, client=client)
    assert client.embeddings.calls == [
        {"model": EMBEDDING_MODEL, "input": "q"}
    ]


def test_run_query_note_path_is_string_not_path(seeded_index) -> None:
    """note_path must be a JSON-friendly string, not a pathlib.Path."""
    conn, _ids = seeded_index
    client = _MappedOpenAIClient({"q": _unit_vec(0)})
    results = run_query(conn, "q", top_k=1, client=client)
    assert type(results[0]["note_path"]) is str


# ---------------------------------------------------------------------------
# Sub-AC 8.3 — evaluate_results: pure gate function with boundary coverage.
#
# Required boundary cases per AC:
#   - exactly 3 above floor → pass
#   - exactly 2 above floor → fail
#   - all below floor       → fail
#   - empty results         → fail
# Additional coverage: floor inclusivity, return-tuple shape, default args
# match documented gate, custom thresholds, and type-error guardrails.
# ---------------------------------------------------------------------------


def _result(sim: float, path: str = "x.md") -> dict[str, Any]:
    """Minimal result dict shaped like ``run_query`` output."""
    return {"note_path": path, "heading_chain": [], "similarity": sim}


def test_evaluate_results_default_thresholds_match_documented_gate() -> None:
    """Defaults must match the smoke-gate contract (floor=0.20, min=3)."""
    assert DEFAULT_SIMILARITY_FLOOR == 0.20
    assert DEFAULT_MIN_RESULTS == 3


def test_evaluate_results_exactly_three_above_floor_passes() -> None:
    """Boundary: 3 above floor → pass, count == 3."""
    results = [_result(0.91), _result(0.55), _result(0.21), _result(0.10)]
    passed, count = evaluate_results(results)
    assert passed is True
    assert count == 3


def test_evaluate_results_two_above_floor_fails() -> None:
    """Boundary: only 2 above floor → fail, count == 2."""
    results = [_result(0.80), _result(0.40), _result(0.19), _result(0.05)]
    passed, count = evaluate_results(results)
    assert passed is False
    assert count == 2


def test_evaluate_results_all_below_floor_fails() -> None:
    """Boundary: every result below floor → fail, count == 0."""
    results = [_result(0.19), _result(0.10), _result(0.05), _result(-0.10)]
    passed, count = evaluate_results(results)
    assert passed is False
    assert count == 0


def test_evaluate_results_empty_results_fails() -> None:
    """Boundary: empty input → fail, count == 0."""
    passed, count = evaluate_results([])
    assert passed is False
    assert count == 0


def test_evaluate_results_score_equal_to_floor_counts_as_above() -> None:
    """Floor is inclusive — a score equal to the floor passes the threshold."""
    results = [_result(0.20), _result(0.20), _result(0.20)]
    passed, count = evaluate_results(results, floor=0.20, min_count=3)
    assert passed is True
    assert count == 3


def test_evaluate_results_returns_tuple_of_bool_and_int() -> None:
    """Return shape contract: (bool, int) — not (bool, bool) or (int, int)."""
    passed, count = evaluate_results([_result(0.5)] * 3)
    assert isinstance(passed, bool)
    assert isinstance(count, int)
    assert not isinstance(count, bool)  # bool subclasses int — guard the test


def test_evaluate_results_custom_min_count_and_floor() -> None:
    """Operator can tighten the gate (higher floor, higher min_count)."""
    results = [_result(0.95), _result(0.60), _result(0.50), _result(0.30)]
    # floor=0.50 → 3 above (0.95, 0.60, 0.50). min_count=4 → fail.
    passed, count = evaluate_results(results, floor=0.50, min_count=4)
    assert passed is False
    assert count == 3
    # Same data, min_count=3 → pass.
    passed2, count2 = evaluate_results(results, floor=0.50, min_count=3)
    assert passed2 is True
    assert count2 == 3


def test_evaluate_results_accepts_generator_input() -> None:
    """``results`` is typed as Iterable — generators must work, not just lists."""
    gen = (_result(s) for s in [0.9, 0.5, 0.25])
    passed, count = evaluate_results(gen)
    assert passed is True
    assert count == 3


def test_evaluate_results_min_count_zero_always_passes() -> None:
    """min_count=0 degenerate gate — even empty results pass."""
    passed, count = evaluate_results([], min_count=0)
    assert passed is True
    assert count == 0


def test_evaluate_results_negative_min_count_raises() -> None:
    with pytest.raises(ValueError):
        evaluate_results([_result(0.5)], min_count=-1)


def test_evaluate_results_missing_similarity_key_raises() -> None:
    bad = [{"note_path": "x.md", "heading_chain": []}]
    with pytest.raises(KeyError):
        evaluate_results(bad)


def test_evaluate_results_non_numeric_similarity_raises() -> None:
    bad = [{"note_path": "x.md", "heading_chain": [], "similarity": "0.9"}]
    with pytest.raises(TypeError):
        evaluate_results(bad)


def test_evaluate_results_bool_similarity_rejected() -> None:
    """``True`` would silently compare as ``1.0`` — must be rejected loudly."""
    bad = [{"note_path": "x.md", "heading_chain": [], "similarity": True}]
    with pytest.raises(TypeError):
        evaluate_results(bad)


def test_evaluate_results_integrates_with_run_query_output(seeded_index) -> None:
    """Real ``run_query`` output flows directly into ``evaluate_results``.

    With the orthonormal-basis seed, a query aligned to e_0 yields cosine
    [1.0, 0.0, 0.0] — exactly 1 above floor → fail with default min_count=3.
    """
    conn, _ids = seeded_index
    client = _MappedOpenAIClient({"q": _unit_vec(0)})
    results = run_query(conn, "q", top_k=3, client=client)
    passed, count = evaluate_results(results)
    assert count == 1
    assert passed is False


# ---------------------------------------------------------------------------
# Sub-AC 8.4 — format_results: human-readable rendering of top-k results
# ---------------------------------------------------------------------------


_SAMPLE_RESULTS: list[dict[str, Any]] = [
    {
        "note_path": "01 Command Center/daily/2025-05-17.md",
        "heading_chain": ["Standup", "Yesterday"],
        "similarity": 0.8123,
    },
    {
        "note_path": "02 Vision Center/2025-goals.md",
        "heading_chain": ["Q2"],
        "similarity": 0.6017,
    },
    {
        "note_path": "03 Resources/papers/attention.md",
        "heading_chain": [],
        "similarity": 0.5500,
    },
]


def test_format_results_contains_three_fields_per_row() -> None:
    """Each rendered row must carry path, heading_chain content, and score.

    The seed contract for sub-AC 8.4 names the three fields explicitly —
    this asserts none silently drops out of the rendered block.
    """
    out = format_results("standup notes", _SAMPLE_RESULTS)

    for r in _SAMPLE_RESULTS:
        assert r["note_path"] in out, f"missing path {r['note_path']}"
        expected_heading = (
            " > ".join(r["heading_chain"]) if r["heading_chain"] else "(no headings)"
        )
        assert expected_heading in out, f"missing heading {expected_heading!r}"
        assert f"{r['similarity']:.4f}" in out, (
            f"missing score for {r['note_path']}"
        )


def test_format_results_matches_snapshot() -> None:
    """Stable snapshot — guards against accidental layout drift.

    The exact text is part of the operator-facing contract: changing it
    breaks copy-paste workflows and grep recipes built on top of the output.
    """
    out = format_results("standup notes", _SAMPLE_RESULTS)
    expected = (
        "query: standup notes\n"
        "  1. path: 01 Command Center/daily/2025-05-17.md\n"
        "     heading: Standup > Yesterday\n"
        "     score: 0.8123\n"
        "  2. path: 02 Vision Center/2025-goals.md\n"
        "     heading: Q2\n"
        "     score: 0.6017\n"
        "  3. path: 03 Resources/papers/attention.md\n"
        "     heading: (no headings)\n"
        "     score: 0.5500"
    )
    assert out == expected


def test_format_results_query_header_present() -> None:
    """Header line carries the query verbatim, including whitespace."""
    out = format_results("  spaced query  ", _SAMPLE_RESULTS[:1])
    assert out.startswith("query:   spaced query  \n")


def test_format_results_truncates_to_top_k() -> None:
    """``top_k`` caps the number of rendered rows (default 5)."""
    many = [
        {
            "note_path": f"note-{i}.md",
            "heading_chain": [f"H{i}"],
            "similarity": 0.9 - i * 0.05,
        }
        for i in range(8)
    ]
    out = format_results("q", many)
    # Default top_k=5 → rows 1..5 visible, row 6 absent.
    assert "  1." in out
    assert "  5." in out
    assert "  6." not in out
    assert "note-5.md" not in out  # the 6th item (index 5) is dropped


def test_format_results_empty_results_renders_no_results_line() -> None:
    """Empty results still emit the header plus a visible empty marker."""
    out = format_results("nothing matches", [])
    assert out == "query: nothing matches\n  (no results)"


def test_format_results_top_k_zero_renders_no_rows() -> None:
    """``top_k <= 0`` collapses to the empty-marker form."""
    out = format_results("q", _SAMPLE_RESULTS, top_k=0)
    assert out == "query: q\n  (no results)"


def test_format_results_rejects_non_numeric_similarity() -> None:
    """Bad ``similarity`` types surface as ``TypeError``, not silent ``str``."""
    bad = [{"note_path": "x.md", "heading_chain": [], "similarity": "0.9"}]
    with pytest.raises(TypeError):
        format_results("q", bad)


def test_format_results_integrates_with_run_query_output(seeded_index) -> None:
    """End-to-end: ``run_query`` output flows straight into ``format_results``."""
    conn, _ids = seeded_index
    client = _MappedOpenAIClient({"q": _unit_vec(0)})
    results = run_query(conn, "q", top_k=3, client=client)
    out = format_results("q", results)
    assert out.startswith("query: q\n")
    # 3 numbered rows, each with the three labelled fields.
    for n in (1, 2, 3):
        assert f"  {n}. path: " in out
    assert out.count("     heading: ") == 3
    assert out.count("     score: ") == 3
