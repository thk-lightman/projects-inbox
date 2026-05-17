"""Integration tests for the delta build pipeline.

Covers:

* ``git_diff_changed_files`` filters to scoped, .md, non-hidden paths
* ``process_delta`` re-chunks supplied files, deletes obsolete chunks,
  re-translates only new chunks, leaves untouched files alone
* CLI ``vault-corpus build --delta`` wires the two together end-to-end
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vault_corpus import cli as cli_mod
from vault_corpus.chunker import Chunk
from vault_corpus.pipeline import (
    BuildReport,
    git_diff_changed_files,
    process_delta,
)
from vault_corpus.store import init_db, upsert_chunk


# ---------------------------------------------------------------------------
# git_diff_changed_files
# ---------------------------------------------------------------------------


def _make_runner(stdout: str):
    def runner(argv: list[str], cwd: Path) -> str:
        return stdout

    return runner


def test_git_diff_filters_to_scoped_md(tmp_path: Path):
    runner = _make_runner(
        "\n".join(
            [
                "00 Get Things Done/today.md",
                "01 Command Center/sub/plan.md",
                "04 PracticeMakesPerfect/skip.md",
                "03 Resources/.hidden/secret.md",
                "03 Resources/visible.md",
                ".obsidian/config.json",
                "03 Resources/not-markdown.txt",
                "",
            ]
        )
    )

    out = git_diff_changed_files(tmp_path, ref="HEAD~1", runner=runner)

    rels = [p.relative_to(tmp_path).as_posix() for p in out]
    assert rels == [
        "00 Get Things Done/today.md",
        "01 Command Center/sub/plan.md",
        "03 Resources/visible.md",
    ]


def test_git_diff_empty_diff_returns_empty(tmp_path: Path):
    out = git_diff_changed_files(tmp_path, runner=_make_runner(""))
    assert out == []


def test_git_diff_raises_on_missing_vault(tmp_path: Path):
    with pytest.raises(NotADirectoryError):
        git_diff_changed_files(tmp_path / "missing", runner=_make_runner(""))


def test_git_diff_real_subprocess_against_init_only_repo(tmp_path: Path):
    # Build a 2-commit fake vault repo and confirm the helper finds the changed
    # file via the real subprocess runner (no monkey patching).
    vault = tmp_path / "vault"
    scoped = vault / "00 Get Things Done"
    scoped.mkdir(parents=True)
    note = scoped / "note.md"
    note.write_text("# original\n## h1\nbody\n", encoding="utf-8")

    def run_git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=str(vault), check=True, capture_output=True)

    run_git("init", "-q")
    run_git("config", "user.email", "test@example.com")
    run_git("config", "user.name", "test")
    run_git("add", ".")
    run_git("commit", "-q", "-m", "init")
    note.write_text("# updated\n## h1\nbody changed\n", encoding="utf-8")
    run_git("add", ".")
    run_git("commit", "-q", "-m", "update")

    out = git_diff_changed_files(vault, ref="HEAD~1")
    assert [p.relative_to(vault).as_posix() for p in out] == [
        "00 Get Things Done/note.md"
    ]


# ---------------------------------------------------------------------------
# process_delta
# ---------------------------------------------------------------------------


def _fake_translate(chunk: Chunk, _client) -> Chunk:
    return Chunk(
        source_path=chunk.source_path,
        heading_chain=[f"[EN] {h}" for h in chunk.heading_chain],
        body=f"EN: {chunk.body}",
        lang="en",
        chunk_id=chunk.chunk_id,
        frontmatter=dict(chunk.frontmatter),
    )


def _fake_embed(_text: str, _client) -> list[float]:
    return [0.1] * 3072


def test_process_delta_reprocesses_only_supplied_files(tmp_path: Path):
    vault = tmp_path / "vault"
    scoped = vault / "00 Get Things Done"
    scoped.mkdir(parents=True)
    changed = scoped / "changed.md"
    untouched = scoped / "untouched.md"
    changed.write_text("# title\n## first\nold body\n", encoding="utf-8")
    untouched.write_text("# title\n## first\nstable\n", encoding="utf-8")

    db_path = tmp_path / "corpus.db"
    conn = init_db(db_path)

    # Modify the changed file before delta runs so its chunk_id flips.
    changed.write_text("# title\n## first\nnew body\n", encoding="utf-8")

    report = process_delta(
        conn,
        [changed],
        translate_client=object(),
        embed_client=object(),
        translate=_fake_translate,
        embed=_fake_embed,
    )

    assert isinstance(report, BuildReport)
    assert report.files_scanned == 1
    assert report.chunks_seen >= 1
    assert report.chunks_translated == report.chunks_seen
    assert report.chunks_embedded == report.chunks_seen
    assert report.chunks_upserted == report.chunks_seen
    assert report.cost.translate_calls == report.chunks_seen
    assert report.cost.embed_calls == report.chunks_seen
    # untouched.md was never seen by the delta pipeline.
    src_paths = {
        row[0]
        for row in conn.execute("SELECT DISTINCT source_path FROM chunks").fetchall()
    }
    assert str(changed) in src_paths
    assert str(untouched) not in src_paths

    conn.close()


def test_process_delta_skips_already_translated(tmp_path: Path):
    vault = tmp_path / "vault"
    scoped = vault / "00 Get Things Done"
    scoped.mkdir(parents=True)
    note = scoped / "note.md"
    note.write_text("# title\n## first\nbody\n", encoding="utf-8")

    db_path = tmp_path / "corpus.db"
    conn = init_db(db_path)

    # Pre-seed the DB with the English mirror so process_delta should skip.
    from vault_corpus.chunker import chunk_note

    ko_chunks = chunk_note(note, note.read_text(encoding="utf-8"))
    for ko in ko_chunks:
        en = _fake_translate(ko, None)
        upsert_chunk(conn, en, _fake_embed("", None), file_fingerprint="seeded")

    report = process_delta(
        conn,
        [note],
        translate_client=object(),
        embed_client=object(),
        translate=_fake_translate,
        embed=_fake_embed,
    )

    assert report.chunks_translated == 0
    assert report.chunks_embedded == 0
    assert report.cost.total_calls() == 0
    assert report.skipped_existing == len(ko_chunks)

    conn.close()


def test_process_delta_evicts_obsolete_chunks(tmp_path: Path):
    """Heading removed → row for that heading is deleted from DB."""
    vault = tmp_path / "vault"
    scoped = vault / "00 Get Things Done"
    scoped.mkdir(parents=True)
    note = scoped / "note.md"
    note.write_text("# t\n## a\nbody a\n## b\nbody b\n", encoding="utf-8")

    db_path = tmp_path / "corpus.db"
    conn = init_db(db_path)

    from vault_corpus.chunker import chunk_note

    ko_chunks = chunk_note(note, note.read_text(encoding="utf-8"))
    for ko in ko_chunks:
        en = _fake_translate(ko, None)
        upsert_chunk(conn, en, _fake_embed("", None), file_fingerprint="seeded")

    assert (
        conn.execute("SELECT COUNT(*) FROM chunks WHERE lang='en'").fetchone()[0]
        == len(ko_chunks)
    )

    # Drop heading 'b' from the note on disk; delta should reap its row.
    note.write_text("# t\n## a\nbody a\n", encoding="utf-8")

    report = process_delta(
        conn,
        [note],
        translate_client=object(),
        embed_client=object(),
        translate=_fake_translate,
        embed=_fake_embed,
    )

    assert report.chunks_deleted >= 1
    remaining = conn.execute(
        "SELECT body FROM chunks WHERE lang='en'"
    ).fetchall()
    assert all("body b" not in row[0] for row in remaining)

    conn.close()


# ---------------------------------------------------------------------------
# CLI wiring: build --delta
# ---------------------------------------------------------------------------


def test_cli_build_delta_invokes_process_delta(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    calls = {"process": 0, "build": 0}

    def fake_make_client():
        return object()

    def fake_git(_vault_path, ref="HEAD~1", **_kw):
        return [tmp_path / "00 Get Things Done" / "x.md"]

    def fake_process(*_args, **_kw):
        calls["process"] += 1
        return BuildReport(
            files_scanned=1,
            chunks_seen=1,
            chunks_translated=0,
            chunks_embedded=0,
            chunks_upserted=0,
            skipped_existing=1,
            chunks_deleted=0,
        )

    def fake_build(*_args, **_kw):
        calls["build"] += 1
        return BuildReport()

    monkeypatch.setattr(cli_mod, "_make_openai_client", fake_make_client)
    monkeypatch.setattr(cli_mod, "git_diff_changed_files", fake_git)
    monkeypatch.setattr(cli_mod, "process_delta", fake_process)
    monkeypatch.setattr(cli_mod, "build_pipeline", fake_build)

    db_path = tmp_path / "corpus.db"
    result = runner.invoke(
        cli_mod.app,
        [
            "build",
            "--delta",
            "--vault-path",
            str(tmp_path),
            "--db",
            str(db_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls["process"] == 1
    assert calls["build"] == 0
    assert "delta:" in result.output
    assert "1 in-scope file(s) changed" in result.output
    assert "api calls:" in result.output


def test_cli_build_without_delta_invokes_full_pipeline(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    calls = {"process": 0, "build": 0}

    monkeypatch.setattr(cli_mod, "_make_openai_client", lambda: object())
    monkeypatch.setattr(
        cli_mod,
        "process_delta",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not run")),
    )

    def fake_build(*_args, **_kw):
        calls["build"] += 1
        return BuildReport()

    monkeypatch.setattr(cli_mod, "build_pipeline", fake_build)

    db_path = tmp_path / "corpus.db"
    result = runner.invoke(
        cli_mod.app,
        ["build", "--vault-path", str(tmp_path), "--db", str(db_path)],
    )
    assert result.exit_code == 0, result.output
    assert calls["build"] == 1
