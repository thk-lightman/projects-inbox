"""Tests for the build pipeline orchestrator.

Covers Sub-AC 6.2.1:

* ``build_pipeline`` sequences ``scan → chunk → translate → embed → upsert``
  in that order for every chunk produced from a fixture vault.
* Every chunk landed in the DB carries ``lang = 'en'`` (the corpus is the
  English mirror, not the Korean source).
* Re-running the pipeline on an unchanged vault triggers zero translation
  and zero embedding calls (delta semantics on the same ``chunk_id``).
* Per-file failures are isolated: one bad note never aborts the build.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from vault_corpus.chunker import Chunk, chunk_note, compute_chunk_id
from vault_corpus.pipeline import BuildReport, build_pipeline, should_skip_chunk
from vault_corpus.store import EMBEDDING_DIM, init_db, upsert_chunk


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_vault(root: Path) -> list[Path]:
    """Create a tiny on-disk vault with three scoped Korean notes.

    Layout:

    ::

        <root>/
          00 Get Things Done/
            note-one.md       (## 목표 → 1 chunk)
          01 Command Center/
            note-two.md       (## A / ### B → 2 chunks)
          03 Resources/
            note-three.md     (no `##` headings → 1 whole-note chunk)
          90System/
            ignored.md        (out of scope — skipped by scanner)

    Returns the in-scope paths in scanner-sort order.
    """
    paths: list[Path] = []

    a = root / "00 Get Things Done" / "note-one.md"
    a.parent.mkdir(parents=True, exist_ok=True)
    a.write_text("## 목표\n매일 코드를 짠다.\n", encoding="utf-8")
    paths.append(a)

    b = root / "01 Command Center" / "note-two.md"
    b.parent.mkdir(parents=True, exist_ok=True)
    b.write_text(
        "## A\n첫 번째 섹션.\n### B\n중첩된 섹션.\n",
        encoding="utf-8",
    )
    paths.append(b)

    c = root / "03 Resources" / "note-three.md"
    c.parent.mkdir(parents=True, exist_ok=True)
    c.write_text("헤딩 없는 본문.\n", encoding="utf-8")
    paths.append(c)

    # Out-of-scope file — scanner must drop it so the pipeline never sees it.
    out = root / "90System" / "ignored.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("## ignored\nshould not appear in DB.\n", encoding="utf-8")

    return sorted(paths)


class _RecordingClient:
    """Sentinel client object whose identity the pipeline forwards to fakes.

    Carries a ``name`` so the call-order log can prove the *correct* client
    was handed to each step (translate vs embed) — not just any client.
    """

    def __init__(self, name: str) -> None:
        self.name = name


def _make_translate_fake(call_log: list[tuple]):
    """Build a ``translate`` callable that records calls and returns an
    English mirror chunk with the same ``chunk_id`` and ``lang='en'``.
    """

    def fake_translate(chunk: Chunk, client: Any) -> Chunk:
        call_log.append(("translate", chunk.chunk_id, getattr(client, "name", None)))
        # Mirror the Korean body to a sentinel English body. Heading chain
        # tracks the original so the upserted row preserves structure.
        english_body = "EN[" + chunk.body + "]"
        return replace(chunk, body=english_body, lang="en")

    return fake_translate


def _make_embed_fake(call_log: list[tuple]):
    """Build an ``embed`` callable that records calls and returns a
    deterministic 3072-float vector (varies by chunk so rows differ).
    """

    counter = {"i": 0}

    def fake_embed(text: str, client: Any) -> list[float]:
        call_log.append(("embed", text, getattr(client, "name", None)))
        counter["i"] += 1
        # Vector with a single non-zero slot — distinguishable per call.
        vec = [0.0] * EMBEDDING_DIM
        vec[counter["i"] % EMBEDDING_DIM] = 1.0
        return vec

    return fake_embed


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_build_pipeline_call_order_and_lang_en(tmp_path: Path) -> None:
    """Sub-AC 6.2.1 main assertion.

    After a cold build:

    * Calls are interleaved per chunk as ``translate → embed`` (the scan
      and chunk steps happen earlier outside the per-chunk loop).
    * Every chunk produced by the chunker resulted in one translate call
      and one embed call, both reaching the correctly-named client.
    * Every row in the DB carries ``lang = 'en'``.
    """
    vault = tmp_path / "vault"
    expected_files = _make_vault(vault)

    # Expected chunk universe = whatever the real chunker produces from the
    # three in-scope files. We derive it instead of hard-coding so the test
    # tracks the chunker's behaviour, not a stale snapshot.
    expected_chunks: list[Chunk] = []
    for p in expected_files:
        expected_chunks.extend(chunk_note(p, p.read_text(encoding="utf-8")))
    expected_chunk_ids = [c.chunk_id for c in expected_chunks]
    assert len(expected_chunk_ids) >= 4, "fixture should yield at least 4 chunks"

    db_path = tmp_path / "corpus.db"
    conn = init_db(db_path)

    call_log: list[tuple] = []
    translate_client = _RecordingClient("translate")
    embed_client = _RecordingClient("embed")

    try:
        report = build_pipeline(
            vault,
            conn,
            translate_client,
            embed_client,
            translate=_make_translate_fake(call_log),
            embed=_make_embed_fake(call_log),
        )

        # --- ordering ------------------------------------------------------
        # The pipeline guarantees translate(X) precedes embed(X) for every
        # chunk_id. Walk the log and verify per-chunk pairing.
        for i in range(0, len(call_log), 2):
            assert call_log[i][0] == "translate", call_log
            assert call_log[i + 1][0] == "embed", call_log
            # Correct client routed to each step.
            assert call_log[i][2] == "translate"
            assert call_log[i + 1][2] == "embed"

        translated_ids = [entry[1] for entry in call_log if entry[0] == "translate"]
        assert translated_ids == expected_chunk_ids, (
            "translate calls must cover every produced chunk, in chunker order"
        )

        # --- counts --------------------------------------------------------
        assert report.files_scanned == 3  # out-of-scope file excluded
        assert report.chunks_seen == len(expected_chunk_ids)
        assert report.chunks_translated == len(expected_chunk_ids)
        assert report.chunks_embedded == len(expected_chunk_ids)
        assert report.chunks_upserted == len(expected_chunk_ids)
        assert report.skipped_existing == 0
        assert report.failed_files == []

        # --- DB state ------------------------------------------------------
        rows = conn.execute(
            "SELECT chunk_id, lang, body, embedding FROM chunks"
        ).fetchall()
        assert len(rows) == len(expected_chunk_ids)
        for chunk_id, lang, body, embedding in rows:
            assert lang == "en", f"row {chunk_id} should be English, got {lang}"
            assert body.startswith("EN["), f"row {chunk_id} body was not translated"
            assert embedding is not None and len(embedding) == EMBEDDING_DIM * 4
        stored_ids = {row[0] for row in rows}
        assert stored_ids == set(expected_chunk_ids), (
            "DB must hold exactly the chunks the chunker produced"
        )
    finally:
        conn.close()


def test_build_pipeline_skips_unchanged_chunks(tmp_path: Path) -> None:
    """Second pass over an unchanged vault triggers zero API calls.

    This is the cache-hit / delta-build invariant: same ``chunk_id`` plus
    existing embedding ⇒ skip translate AND skip embed.
    """
    vault = tmp_path / "vault"
    _make_vault(vault)

    db_path = tmp_path / "corpus.db"
    conn = init_db(db_path)

    try:
        cold_log: list[tuple] = []
        cold_report = build_pipeline(
            vault,
            conn,
            _RecordingClient("translate"),
            _RecordingClient("embed"),
            translate=_make_translate_fake(cold_log),
            embed=_make_embed_fake(cold_log),
        )
        assert cold_report.chunks_translated > 0
        cold_chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

        warm_log: list[tuple] = []
        warm_report = build_pipeline(
            vault,
            conn,
            _RecordingClient("translate"),
            _RecordingClient("embed"),
            translate=_make_translate_fake(warm_log),
            embed=_make_embed_fake(warm_log),
        )

        assert warm_log == [], "warm rebuild must produce zero API calls"
        assert warm_report.chunks_translated == 0
        assert warm_report.chunks_embedded == 0
        assert warm_report.skipped_existing == cold_report.chunks_seen
        warm_chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        assert warm_chunk_count == cold_chunk_count, "row count must not change"
    finally:
        conn.close()


def test_build_pipeline_isolates_per_file_failures(tmp_path: Path) -> None:
    """A read-error on one file does not abort the rest of the build."""
    vault = tmp_path / "vault"
    expected = _make_vault(vault)

    db_path = tmp_path / "corpus.db"
    conn = init_db(db_path)

    bad_path = expected[0]

    def flaky_read(path: Path) -> str:
        if path == bad_path:
            raise OSError("simulated read failure")
        return path.read_text(encoding="utf-8")

    call_log: list[tuple] = []

    try:
        report = build_pipeline(
            vault,
            conn,
            _RecordingClient("translate"),
            _RecordingClient("embed"),
            read=flaky_read,
            translate=_make_translate_fake(call_log),
            embed=_make_embed_fake(call_log),
        )
        assert bad_path in report.failed_files
        assert report.files_scanned == 3
        # Some chunks (from the surviving 2 files) should still be in DB.
        n_en = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE lang = 'en'"
        ).fetchone()[0]
        assert n_en > 0
        # No chunk_id from the failing file made it in.
        bad_chunks = chunk_note(bad_path, bad_path.read_text(encoding="utf-8"))
        for bc in bad_chunks:
            row = conn.execute(
                "SELECT 1 FROM chunks WHERE chunk_id = ?", (bc.chunk_id,)
            ).fetchone()
            assert row is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sub-AC 6.2.2: should_skip_chunk(db, chunk_id) predicate
# ---------------------------------------------------------------------------


def _make_chunk(source_path: Path, body: str, lang: str) -> Chunk:
    """Build a Chunk with a real chunk_id for direct DB insertion."""
    heading_chain: list[str] = []
    cid = compute_chunk_id(source_path, heading_chain, body)
    return Chunk(
        source_path=source_path,
        heading_chain=heading_chain,
        body=body,
        chunk_id=cid,
        lang=lang,
        frontmatter={},
    )


def test_should_skip_chunk_returns_false_when_absent(tmp_path: Path) -> None:
    """Empty DB ⇒ never skip."""
    conn = init_db(tmp_path / "corpus.db")
    try:
        assert should_skip_chunk(conn, "nonexistent-chunk-id") is False
    finally:
        conn.close()


def test_should_skip_chunk_returns_false_when_only_ko_row_present(
    tmp_path: Path,
) -> None:
    """Korean-source row only ⇒ no skip; translation pass still needed."""
    conn = init_db(tmp_path / "corpus.db")
    try:
        ko = _make_chunk(tmp_path / "note.md", "한국어 본문", lang="ko")
        upsert_chunk(conn, ko, embedding=None)
        assert should_skip_chunk(conn, ko.chunk_id) is False
    finally:
        conn.close()


def test_should_skip_chunk_returns_true_when_en_row_present(
    tmp_path: Path,
) -> None:
    """English row present (regardless of embedding) ⇒ skip."""
    conn = init_db(tmp_path / "corpus.db")
    try:
        en = _make_chunk(tmp_path / "note.md", "english body", lang="en")
        upsert_chunk(conn, en, embedding=None)
        assert should_skip_chunk(conn, en.chunk_id) is True
    finally:
        conn.close()


def test_build_pipeline_returns_buildreport_type(tmp_path: Path) -> None:
    """Public return type is the documented :class:`BuildReport`."""
    vault = tmp_path / "vault"
    (vault / "00 Get Things Done").mkdir(parents=True)
    (vault / "00 Get Things Done" / "n.md").write_text(
        "## h\nbody\n", encoding="utf-8"
    )
    db_path = tmp_path / "corpus.db"
    conn = init_db(db_path)
    try:
        report = build_pipeline(
            vault,
            conn,
            _RecordingClient("t"),
            _RecordingClient("e"),
            translate=_make_translate_fake([]),
            embed=_make_embed_fake([]),
        )
        assert isinstance(report, BuildReport)
    finally:
        conn.close()
