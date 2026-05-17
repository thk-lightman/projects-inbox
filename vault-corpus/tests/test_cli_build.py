"""Tests for the `vault-corpus build` CLI subcommand (Sub-AC 6.2.3).

Verifies that the typer wiring:

* Exits with code 0 on a successful build.
* Constructs the OpenAI clients via the patchable module-level seam.
* Invokes :func:`vault_corpus.pipeline.build_pipeline` exactly once with
  the args supplied on the command line (vault path, db connection,
  translate client, embed client).
* Opens the SQLite store and closes it after the pipeline returns.

All OpenAI integration points are mocked — the test must never touch the
network and must never require an ``OPENAI_API_KEY`` in the environment.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from vault_corpus import cli as cli_mod
from vault_corpus.cli import app
from vault_corpus.pipeline import BuildReport


runner = CliRunner()


class _FakeOpenAI:
    """Minimal stand-in for :class:`openai.OpenAI`.

    The real SDK class raises immediately when ``OPENAI_API_KEY`` is
    missing, which would make this test environment-dependent. The build
    command never calls any SDK method directly — it only forwards the
    client object to ``build_pipeline`` — so a tag-only sentinel suffices.
    """

    instances: list["_FakeOpenAI"] = []

    def __init__(self) -> None:
        self.tag = f"fake-openai-{len(_FakeOpenAI.instances)}"
        _FakeOpenAI.instances.append(self)


@pytest.fixture(autouse=True)
def _reset_fake_openai() -> None:
    _FakeOpenAI.instances.clear()
    yield
    _FakeOpenAI.instances.clear()


def test_build_exits_zero_and_invokes_pipeline_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CliRunner test: exit code 0, ``build_pipeline`` called exactly once with expected args."""
    calls: list[dict] = []

    def fake_build_pipeline(
        vault_path: Path,
        db,
        translate_client,
        embed_client,
        **kwargs,
    ) -> BuildReport:
        calls.append(
            {
                "vault_path": vault_path,
                "db": db,
                "translate_client": translate_client,
                "embed_client": embed_client,
                "kwargs": kwargs,
            }
        )
        return BuildReport(
            files_scanned=2,
            chunks_seen=3,
            chunks_translated=3,
            chunks_embedded=3,
            chunks_upserted=3,
            skipped_existing=0,
            failed_files=[],
        )

    monkeypatch.setattr(cli_mod, "build_pipeline", fake_build_pipeline)
    monkeypatch.setattr(cli_mod, "OpenAI", _FakeOpenAI)

    db_path = tmp_path / "corpus.db"
    vault = tmp_path / "vault"
    vault.mkdir()

    result = runner.invoke(
        app,
        [
            "build",
            "--vault-path",
            str(vault),
            "--db",
            str(db_path),
        ],
    )

    assert result.exit_code == 0, result.output

    # Pipeline invoked exactly once.
    assert len(calls) == 1
    call = calls[0]

    # Vault path forwarded verbatim from the CLI arg.
    assert call["vault_path"] == vault

    # db arg is an open SQLite connection, not the Path.
    import sqlite3

    assert isinstance(call["db"], sqlite3.Connection)

    # Both clients are the patched fake — the seam in cli._make_openai_client
    # resolved through the patched module-level ``OpenAI`` symbol.
    assert isinstance(call["translate_client"], _FakeOpenAI)
    assert isinstance(call["embed_client"], _FakeOpenAI)

    # Counts printed back to stdout so the operator sees the build summary.
    assert "files scanned: 2" in result.output
    assert "chunks upserted: 3" in result.output

    # The SQLite store file exists on disk (init_db ran for real).
    assert db_path.exists()


def test_build_help_lists_flags() -> None:
    result = runner.invoke(app, ["build", "--help"])
    assert result.exit_code == 0
    for flag in ("--vault-path", "--db"):
        assert flag in result.output


def test_build_closes_connection_even_when_pipeline_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ``try/finally`` in ``build`` must close the SQLite connection on error.

    Wraps :func:`vault_corpus.store.init_db` so the test can observe whether
    ``conn.close()`` was reached. ``sqlite3.Connection`` rejects attribute
    assignment, so we wrap the connection in a tiny proxy whose ``close``
    flips a flag before delegating.
    """
    closed: list[bool] = []

    real_init_db = cli_mod.init_db

    class _ClosingProxy:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def close(self) -> None:
            closed.append(True)
            self._conn.close()

    def tracking_init_db(path):
        return _ClosingProxy(real_init_db(path))

    def exploding_pipeline(*_args, **_kwargs) -> BuildReport:
        raise RuntimeError("simulated pipeline failure")

    monkeypatch.setattr(cli_mod, "init_db", tracking_init_db)
    monkeypatch.setattr(cli_mod, "build_pipeline", exploding_pipeline)
    monkeypatch.setattr(cli_mod, "OpenAI", _FakeOpenAI)

    db_path = tmp_path / "corpus.db"
    vault = tmp_path / "vault"
    vault.mkdir()

    result = runner.invoke(
        app,
        ["build", "--vault-path", str(vault), "--db", str(db_path)],
    )

    # Non-zero exit (RuntimeError surfaced through typer).
    assert result.exit_code != 0
    # Connection closed despite the exception.
    assert closed == [True]
