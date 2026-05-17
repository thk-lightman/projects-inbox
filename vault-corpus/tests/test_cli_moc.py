"""Tests for the `vault-corpus moc generate` CLI subcommand."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from typer.testing import CliRunner

from vault_corpus import cluster as cluster_mod
from vault_corpus.chunker import Chunk, compute_chunk_id
from vault_corpus.cli import app
from vault_corpus.store import EMBEDDING_DIM, init_db, upsert_chunk


runner = CliRunner()


def _basis_vec(index: int) -> list[float]:
    v = [0.0] * EMBEDDING_DIM
    v[index] = 1.0
    return v


def _seed_minimal(db_path: Path) -> None:
    conn = init_db(db_path)
    try:
        for i in range(3):
            src = Path(f"01 Command Center/note-{i}.md")
            body = f"## Topic {i}\nbody {i}\n"
            chain = [f"Topic {i}"]
            cid = compute_chunk_id(src, chain, body)
            upsert_chunk(
                conn,
                Chunk(
                    source_path=src,
                    heading_chain=chain,
                    body=body,
                    chunk_id=cid,
                    lang="en",
                    frontmatter={},
                ),
                _basis_vec(i),
            )
    finally:
        conn.close()


def test_moc_generate_help_lists_subcommand() -> None:
    result = runner.invoke(app, ["moc", "--help"])
    assert result.exit_code == 0
    assert "generate" in result.output


def test_moc_generate_help_lists_flags() -> None:
    result = runner.invoke(app, ["moc", "generate", "--help"])
    assert result.exit_code == 0
    for flag in ("--n", "--db", "--out-dir", "--algo", "--no-llm", "--vault-root"):
        assert flag in result.output


def test_moc_generate_missing_db_returns_exit_2(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "moc",
            "generate",
            "--n",
            "1",
            "--db",
            str(tmp_path / "missing.db"),
            "--out-dir",
            str(tmp_path / "out"),
            "--no-llm",
        ],
    )
    assert result.exit_code == 2
    assert "not found" in result.output.lower() or "not found" in (result.stderr or "").lower()


def test_moc_generate_empty_db_returns_exit_1(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    init_db(db_path).close()  # creates schema, no rows
    result = runner.invoke(
        app,
        [
            "moc",
            "generate",
            "--n",
            "1",
            "--db",
            str(db_path),
            "--out-dir",
            str(tmp_path / "out"),
            "--no-llm",
        ],
    )
    assert result.exit_code == 1
    assert "no MOCs" in result.output


def test_moc_generate_writes_files_no_llm_path(
    monkeypatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "corpus.db"
    _seed_minimal(db_path)

    # Pin clustering output: 3 chunks → 1 single cluster.
    def fake_cluster(matrix, **_kwargs):  # noqa: ARG001
        return np.zeros((matrix.shape[0],), dtype=np.int32)

    monkeypatch.setattr(cluster_mod, "cluster_embeddings", fake_cluster)

    out_dir = tmp_path / "data" / "moc_samples"
    vault = tmp_path / "vault"
    vault.mkdir()

    result = runner.invoke(
        app,
        [
            "moc",
            "generate",
            "--n",
            "1",
            "--db",
            str(db_path),
            "--out-dir",
            str(out_dir),
            "--no-llm",
            "--vault-root",
            str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    files = list(out_dir.glob("MOC-*.md"))
    assert len(files) == 1
    # Vault byte-identical (still empty as created).
    assert list(vault.rglob("*")) == []


def test_moc_generate_refuses_when_out_dir_inside_vault(
    monkeypatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "corpus.db"
    _seed_minimal(db_path)

    def fake_cluster(matrix, **_kwargs):  # noqa: ARG001
        return np.zeros((matrix.shape[0],), dtype=np.int32)

    monkeypatch.setattr(cluster_mod, "cluster_embeddings", fake_cluster)

    vault = tmp_path / "vault"
    vault.mkdir()
    inside = vault / "MOCs"

    result = runner.invoke(
        app,
        [
            "moc",
            "generate",
            "--n",
            "1",
            "--db",
            str(db_path),
            "--out-dir",
            str(inside),
            "--no-llm",
            "--vault-root",
            str(vault),
        ],
    )
    # Click translates the raised ValueError into a non-zero exit code.
    assert result.exit_code != 0
    assert not inside.exists()
