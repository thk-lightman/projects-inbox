"""Sub-AC 6.1: vault-corpus status command.

Seeds a temp SQLite DB via the real ``init_db`` + ``upsert_chunk`` helpers
(so the test exercises the same write path the build pipeline uses) and then
invokes the typer CLI to assert that the printed status reflects the seeded
data exactly: chunk counts grouped by ``lang``, last ``build_ts``, and the
distinct ``source_path`` file count.

No network I/O — embeddings are passed as ``None`` so no OpenAI client is
constructed at any point.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from vault_corpus.chunker import Chunk, compute_chunk_id
from vault_corpus.cli import app
from vault_corpus.store import init_db, upsert_chunk

runner = CliRunner()


def _mk_chunk(path: Path, chain: list[str], body: str, lang: str) -> Chunk:
    return Chunk(
        source_path=path,
        heading_chain=chain,
        body=body,
        chunk_id=compute_chunk_id(path, chain, body),
        lang=lang,
    )


def _seed(db_path: Path) -> None:
    """Seed two source files with 3 ko chunks + 1 en chunk across them."""
    conn = init_db(db_path)
    try:
        note_a = Path("00 Get Things Done/note_a.md")
        note_b = Path("01 Command Center/note_b.md")

        upsert_chunk(
            conn,
            _mk_chunk(note_a, ["Intro"], "본문 1", lang="ko"),
            embedding=None,
            file_fingerprint="fpA",
            build_ts="2026-01-01T00:00:00+00:00",
        )
        upsert_chunk(
            conn,
            _mk_chunk(note_a, ["Body"], "본문 2", lang="ko"),
            embedding=None,
            file_fingerprint="fpA",
            build_ts="2026-01-02T00:00:00+00:00",
        )
        upsert_chunk(
            conn,
            _mk_chunk(note_a, ["Intro"], "English 1", lang="en"),
            embedding=None,
            file_fingerprint="fpA",
            build_ts="2026-01-03T00:00:00+00:00",
        )
        upsert_chunk(
            conn,
            _mk_chunk(note_b, [], "solo body", lang="ko"),
            embedding=None,
            file_fingerprint="fpB",
            build_ts="2026-03-15T12:30:00+00:00",
        )
    finally:
        conn.close()


def test_status_reports_counts_grouped_by_lang(tmp_path: Path) -> None:
    db = tmp_path / "seed.db"
    _seed(db)

    result = runner.invoke(app, ["status", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "ko: 3" in result.output
    assert "en: 1" in result.output


def test_status_reports_distinct_source_path_count(tmp_path: Path) -> None:
    db = tmp_path / "seed.db"
    _seed(db)

    result = runner.invoke(app, ["status", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "distinct source files: 2" in result.output


def test_status_reports_last_build_ts(tmp_path: Path) -> None:
    db = tmp_path / "seed.db"
    _seed(db)

    result = runner.invoke(app, ["status", "--db", str(db)])
    assert result.exit_code == 0, result.output
    # MAX(build_ts) over the seeded rows is the 2026-03-15 stamp on note_b.
    assert "2026-03-15T12:30:00+00:00" in result.output


def test_status_reports_total_chunks(tmp_path: Path) -> None:
    db = tmp_path / "seed.db"
    _seed(db)

    result = runner.invoke(app, ["status", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "total chunks: 4" in result.output


def test_status_on_empty_db_does_not_crash(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    # init_db creates the schema with zero rows.
    init_db(db).close()

    result = runner.invoke(app, ["status", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "total chunks: 0" in result.output
    assert "distinct source files: 0" in result.output
    assert "(never)" in result.output


def test_status_on_nonexistent_db_initializes_and_reports_empty(tmp_path: Path) -> None:
    """Pointing at a missing file should not error — init_db creates the schema."""
    db = tmp_path / "fresh" / "new.db"
    assert not db.exists()

    result = runner.invoke(app, ["status", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert db.exists()
    assert "total chunks: 0" in result.output


def test_status_command_appears_in_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "status" in result.output


def test_status_help_documents_db_flag() -> None:
    result = runner.invoke(app, ["status", "--help"])
    assert result.exit_code == 0
    assert "--db" in result.output
