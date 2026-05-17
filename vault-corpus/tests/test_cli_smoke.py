"""Tests for the `vault-corpus smoke-test` CLI subcommand (Sub-AC 8.5).

Verifies the typer wiring composes ``load_smoke_queries`` → ``run_query``
→ ``evaluate_results`` → ``format_results`` correctly and surfaces the
gate verdict through the process exit code:

* Exit ``0`` when every query passes the floor + min_count gate.
* Exit ``1`` when any single query fails the gate.
* ``--floor`` flag override re-grades the same retrieval set without
  re-embedding (the floor is purely a post-retrieval threshold).

All OpenAI integration points are mocked — the test must never touch the
network and must never require an ``OPENAI_API_KEY`` in the environment.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from vault_corpus import cli as cli_mod
from vault_corpus.chunker import Chunk, compute_chunk_id
from vault_corpus.cli import app
from vault_corpus.store import EMBEDDING_DIM, init_db, upsert_chunk


runner = CliRunner()


# ---------------------------------------------------------------------------
# Fake OpenAI embeddings client — keyed on the literal query string so each
# smoke query can be pinned to a deterministic vector against the seeded
# orthonormal-basis index. Zero network I/O.
# ---------------------------------------------------------------------------


@dataclass
class _FakeEmbedding:
    embedding: list[float]


@dataclass
class _FakeResponse:
    data: list[_FakeEmbedding]


class _MappedEmbeddingsAPI:
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
    v = [0.0] * dim
    v[index] = 1.0
    return v


def _sum_unit_vec(indices: list[int], dim: int = EMBEDDING_DIM) -> list[float]:
    """Equal-weight sum of basis vectors, normalised to unit length.

    Cosine against any single basis ``e_i`` in ``indices`` is exactly
    ``1/sqrt(len(indices))`` — a known, easy-to-assert quantity.
    """
    v = [0.0] * dim
    weight = 1.0 / math.sqrt(len(indices))
    for i in indices:
        v[i] = weight
    return v


# ---------------------------------------------------------------------------
# Seeded index — 5 EN + 2 KR chunks. Each smoke query in the fixture YAML
# maps to a vector designed so 3 EN chunks land above any reasonable floor
# (cosine ≈ 0.577) and the other 2 sit at exactly 0.0.
# ---------------------------------------------------------------------------


def _seed_corpus(db_path: Path) -> sqlite3.Connection:
    conn = init_db(db_path)
    rows = [
        ("en", "01 Command Center/en-alpha.md", ["Alpha"], "english alpha body\n", 0),
        ("en", "01 Command Center/en-beta.md",  ["Beta"],  "english beta body\n",  1),
        ("en", "01 Command Center/en-gamma.md", ["Gamma"], "english gamma body\n", 2),
        ("en", "01 Command Center/en-delta.md", ["Delta"], "english delta body\n", 3),
        ("en", "01 Command Center/en-eps.md",   ["Eps"],   "english eps body\n",   4),
        ("ko", "00 Get Things Done/kr-one.md", ["하나"],   "한국어 본문 1\n",       5),
        ("ko", "00 Get Things Done/kr-two.md", ["둘"],     "한국어 본문 2\n",       6),
    ]
    for lang, src, chain, body, basis in rows:
        src_path = Path(src)
        cid = compute_chunk_id(src_path, chain, body)
        upsert_chunk(
            conn,
            Chunk(
                source_path=src_path,
                heading_chain=chain,
                body=body,
                chunk_id=cid,
                lang=lang,
                frontmatter={},
            ),
            _unit_vec(basis),
        )
    return conn


_PASS_YAML = """\
queries:
  - query: "query one"
    description: "spans alpha/beta/gamma"
  - query: "query two"
    description: "spans gamma/delta/eps"
"""


def _pass_mapping() -> dict[str, list[float]]:
    # query one  → cosine ≈ 0.577 against e_0, e_1, e_2; 0.0 against e_3, e_4
    # query two  → cosine ≈ 0.577 against e_2, e_3, e_4; 0.0 against e_0, e_1
    return {
        "query one": _sum_unit_vec([0, 1, 2]),
        "query two": _sum_unit_vec([2, 3, 4]),
    }


@pytest.fixture()
def pass_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Seeded DB + YAML where both queries pass the default gate."""
    db_path = tmp_path / "corpus.db"
    conn = _seed_corpus(db_path)
    conn.close()

    yaml_path = tmp_path / "queries.yaml"
    yaml_path.write_text(_PASS_YAML, encoding="utf-8")

    client = _MappedOpenAIClient(_pass_mapping())
    monkeypatch.setattr(cli_mod, "_make_openai_client", lambda: client)

    return db_path, yaml_path, client


# ---------------------------------------------------------------------------
# Pass scenario — both queries clear default floor (0.20) with 3 results.
# ---------------------------------------------------------------------------


def test_smoke_test_exit_zero_when_all_queries_pass(pass_setup) -> None:
    db_path, yaml_path, client = pass_setup

    result = runner.invoke(
        app,
        [
            "smoke-test",
            "--db", str(db_path),
            "--queries", str(yaml_path),
        ],
    )

    assert result.exit_code == 0, result.output
    # Aggregated summary visible.
    assert "2/2 queries passed" in result.output
    # Per-query PASS verdicts present, no FAIL surfaced.
    assert result.output.count("PASS") == 2
    assert "FAIL" not in result.output
    # Both queries actually embedded (one call each).
    embed_inputs = [c["input"] for c in client.embeddings.calls]
    assert sorted(embed_inputs) == ["query one", "query two"]


def test_smoke_test_renders_format_results_blocks(pass_setup) -> None:
    """Output contains the per-query format_results block (header + rows)."""
    db_path, yaml_path, _client = pass_setup

    result = runner.invoke(
        app,
        [
            "smoke-test",
            "--db", str(db_path),
            "--queries", str(yaml_path),
        ],
    )

    assert result.exit_code == 0, result.output
    # format_results headers for each query.
    assert "query: query one" in result.output
    assert "query: query two" in result.output
    # Above-floor source paths surface in the rendered rows.
    assert "01 Command Center/en-alpha.md" in result.output
    assert "01 Command Center/en-eps.md" in result.output


# ---------------------------------------------------------------------------
# Fail scenario — at least one query falls below the gate → exit 1.
# ---------------------------------------------------------------------------


_FAIL_YAML = """\
queries:
  - query: "only one match"
    description: "lands on a single basis chunk → 1 above floor"
"""


def test_smoke_test_exit_one_when_any_query_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "corpus.db"
    conn = _seed_corpus(db_path)
    conn.close()

    yaml_path = tmp_path / "queries.yaml"
    yaml_path.write_text(_FAIL_YAML, encoding="utf-8")

    # Single-basis query → 1 result above floor (cosine 1.0); the rest are
    # exactly 0.0 → fewer than min_count=3 → gate fails for this query.
    client = _MappedOpenAIClient({"only one match": _unit_vec(0)})
    monkeypatch.setattr(cli_mod, "_make_openai_client", lambda: client)

    result = runner.invoke(
        app,
        [
            "smoke-test",
            "--db", str(db_path),
            "--queries", str(yaml_path),
        ],
    )

    assert result.exit_code == 1, result.output
    assert "FAIL" in result.output
    assert "0/1 queries passed" in result.output


# ---------------------------------------------------------------------------
# --floor flag override — re-grades the same retrieval set; tight floor
# turns the pass scenario into a fail scenario.
# ---------------------------------------------------------------------------


def test_smoke_test_floor_override_flips_pass_to_fail(pass_setup) -> None:
    """Tighten the floor above the seeded cosine → gate fails → exit 1."""
    db_path, yaml_path, _client = pass_setup

    # Seeded cosines are ≈ 0.577. Floor=0.99 → 0 above floor per query.
    result = runner.invoke(
        app,
        [
            "smoke-test",
            "--db", str(db_path),
            "--queries", str(yaml_path),
            "--floor", "0.99",
        ],
    )

    assert result.exit_code == 1, result.output
    assert "0/2 queries passed" in result.output
    assert result.output.count("FAIL") == 2


def test_smoke_test_floor_override_loose_still_passes(pass_setup) -> None:
    """Loose floor — gate still passes (sanity check on the override path)."""
    db_path, yaml_path, _client = pass_setup

    result = runner.invoke(
        app,
        [
            "smoke-test",
            "--db", str(db_path),
            "--queries", str(yaml_path),
            "--floor", "0.05",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "2/2 queries passed" in result.output


# ---------------------------------------------------------------------------
# Misc CLI surface checks.
# ---------------------------------------------------------------------------


def test_smoke_test_help_lists_flags() -> None:
    result = runner.invoke(app, ["smoke-test", "--help"])
    assert result.exit_code == 0
    for flag in ("--db", "--queries", "--floor", "--min-count", "--top-k"):
        assert flag in result.output


def test_smoke_test_missing_db_exits_two(tmp_path: Path) -> None:
    """Pointing --db at a non-existent path is an operator error → exit 2."""
    result = runner.invoke(
        app,
        [
            "smoke-test",
            "--db", str(tmp_path / "does-not-exist.db"),
        ],
    )
    assert result.exit_code == 2
    assert "not found" in result.output
