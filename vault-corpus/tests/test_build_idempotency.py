"""Idempotency integration test for the ``vault-corpus build`` CLI (Sub-AC 6.2.4).

Runs the full Typer-wired ``build`` command twice end-to-end against a
small fixture vault with mocked translate and embed clients, then asserts:

1. Second invocation triggers **zero** translate API calls.
2. Second invocation triggers **zero** embed API calls.
3. Every row in the ``chunks`` table is byte-for-byte identical between the
   two runs (chunk_id, source_path, heading_chain, lang, body, front_matter,
   build_ts, file_fingerprint, embedding blob).
4. The DB chunk count does not change.
5. The fixture vault itself is byte-for-byte identical before and after
   (vault immutability invariant from the seed contract).

This is the integration-level counterpart to the unit-level cache-hit test
in ``test_pipeline.py``. The point of duplicating coverage at the CLI seam
is to catch wiring regressions in ``cli.build`` itself — e.g. if a future
edit forgot to forward an injected ``translate``/``embed`` override, or
silently re-instantiated the OpenAI client per-chunk, the unit test
wouldn't notice but this one would.

All OpenAI integration points are mocked. The test must never touch the
network and must not require ``OPENAI_API_KEY`` in the environment.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

import pytest
from typer.testing import CliRunner

from vault_corpus import cli as cli_mod
from vault_corpus import pipeline as pipeline_mod
from vault_corpus.chunker import Chunk
from vault_corpus.cli import app
from vault_corpus.store import EMBEDDING_DIM


runner = CliRunner()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeOpenAI:
    """Sentinel client. The build command forwards this object to the
    pipeline; with both translate and embed callables stubbed out, no SDK
    method on it ever gets called, so a no-op class suffices.
    """

    def __init__(self) -> None:
        # The build command instantiates ``OpenAI()`` with no args; matching
        # the signature keeps the seam compatible without needing an API key.
        self.tag = "fake-openai"


class _CallCounter:
    """Mutable counter shared by the translate and embed fakes.

    Using a small object instead of two free ints makes it trivial to reset
    between invocations and to pass the same instance into the wrapped
    ``build_pipeline`` closure.
    """

    def __init__(self) -> None:
        self.translate_calls: int = 0
        self.embed_calls: int = 0

    def reset(self) -> None:
        self.translate_calls = 0
        self.embed_calls = 0


def _make_translate_fake(counter: _CallCounter):
    """Return a translate callable that produces a deterministic English
    mirror chunk and increments the shared call counter.

    The English body is derived purely from the Korean body — no model
    randomness — so two cold runs would produce byte-identical rows even if
    the second run *did* re-translate. That property lets the row-equality
    assertion stay meaningful: if the second run accidentally translates,
    we still catch it via the call-counter assertion.
    """

    def fake_translate(chunk: Chunk, _client: Any) -> Chunk:
        counter.translate_calls += 1
        return replace(chunk, body=f"EN[{chunk.body}]", lang="en")

    return fake_translate


def _make_embed_fake(counter: _CallCounter):
    """Return an embed callable that produces a deterministic 3072-dim
    vector derived from a SHA-256 of the input text.

    Determinism matters: the embedding column is part of the DB-state
    equality assertion. If embeddings drifted between runs, we couldn't
    distinguish "the pipeline re-embedded" from "the fake produced new
    noise".
    """

    def fake_embed(text: str, _client: Any) -> list[float]:
        counter.embed_calls += 1
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        # Tile the 32-byte digest to fill EMBEDDING_DIM floats in [-1, 1].
        # Cheap, deterministic, content-keyed.
        vec = [
            ((digest[i % len(digest)] / 255.0) * 2.0) - 1.0
            for i in range(EMBEDDING_DIM)
        ]
        return vec

    return fake_embed


def _make_wrapped_build_pipeline(counter: _CallCounter):
    """Wrap the real :func:`pipeline.build_pipeline` so the CLI's bound
    call site injects our translate / embed fakes.

    The CLI calls ``build_pipeline(vault, conn, t_client, e_client)``
    positionally with no overrides. To inject overrides without editing
    ``cli.build``, we monkeypatch ``cli_mod.build_pipeline`` to this
    wrapper, which adds ``translate=`` and ``embed=`` before delegating to
    the real pipeline. The real scan / read / chunk / upsert paths run for
    real so the DB state we compare is authentic.
    """
    translate_fake = _make_translate_fake(counter)
    embed_fake = _make_embed_fake(counter)

    def wrapped(vault_path: Path, db: sqlite3.Connection, t_client, e_client, **kw):
        return pipeline_mod.build_pipeline(
            vault_path,
            db,
            t_client,
            e_client,
            translate=translate_fake,
            embed=embed_fake,
            **kw,
        )

    return wrapped


# ---------------------------------------------------------------------------
# Fixture vault
# ---------------------------------------------------------------------------


def _make_vault(root: Path) -> None:
    """Create a fixture vault under ``root`` covering several chunk shapes:

    * single ``##`` chunk
    * two-section note with nested ``###``
    * heading-free whole-note chunk

    All in-scope. Three notes ⇒ four+ chunks, enough to make a single
    accidental re-translate detectable.
    """
    a = root / "00 Get Things Done" / "alpha.md"
    a.parent.mkdir(parents=True, exist_ok=True)
    a.write_text("## 알파\n첫 번째 메모.\n", encoding="utf-8")

    b = root / "01 Command Center" / "beta.md"
    b.parent.mkdir(parents=True, exist_ok=True)
    b.write_text(
        "## 베타\n섹션 본문.\n### 베타-원\n중첩 본문.\n",
        encoding="utf-8",
    )

    c = root / "03 Resources" / "gamma.md"
    c.parent.mkdir(parents=True, exist_ok=True)
    c.write_text("헤딩 없는 본문.\n", encoding="utf-8")


def _hash_vault(root: Path) -> dict[str, str]:
    """Return ``{relative_path: sha256}`` for every file under ``root``.

    Used to assert vault immutability: every file the test wrote (and only
    those) must remain byte-identical across both CLI invocations.
    """
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _snapshot_db(db_path: Path) -> list[tuple]:
    """Return a sorted list of full rows from the ``chunks`` table.

    Includes every column, so any drift (body, build_ts, embedding blob,
    fingerprint) breaks equality. Sorted by ``chunk_id`` so insertion
    order can't masquerade as a difference.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            """
            SELECT chunk_id, source_path, heading_chain, lang, body,
                   front_matter, build_ts, file_fingerprint, embedding
            FROM chunks
            ORDER BY chunk_id
            """
        ).fetchall()
    finally:
        conn.close()
    return rows


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


def test_build_twice_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Run ``vault-corpus build`` twice against the same fixture vault.

    Second invocation must:

    * Make zero translate calls.
    * Make zero embed calls.
    * Leave the ``chunks`` table byte-identical to the first invocation.
    * Leave the vault byte-identical to its pre-build state.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    _make_vault(vault)
    db_path = tmp_path / "corpus.db"

    pre_vault_hashes = _hash_vault(vault)

    counter = _CallCounter()
    wrapped = _make_wrapped_build_pipeline(counter)

    monkeypatch.setattr(cli_mod, "build_pipeline", wrapped)
    monkeypatch.setattr(cli_mod, "OpenAI", _FakeOpenAI)

    # ----- first (cold) build --------------------------------------------
    result1 = runner.invoke(
        app,
        ["build", "--vault-path", str(vault), "--db", str(db_path)],
    )
    assert result1.exit_code == 0, result1.output

    cold_translate = counter.translate_calls
    cold_embed = counter.embed_calls
    assert cold_translate > 0, (
        "fixture vault must produce at least one chunk on the cold run"
    )
    assert cold_embed == cold_translate, (
        "every translated chunk must be embedded on the cold run"
    )

    cold_snapshot = _snapshot_db(db_path)
    cold_count = len(cold_snapshot)
    assert cold_count >= 4, "fixture should land at least 4 chunks in the DB"
    assert f"chunks translated: {cold_translate}" in result1.output

    # Vault untouched by the cold build.
    assert _hash_vault(vault) == pre_vault_hashes, (
        "vault must be byte-identical after cold build (immutability)"
    )

    # ----- second (warm) build -------------------------------------------
    counter.reset()

    result2 = runner.invoke(
        app,
        ["build", "--vault-path", str(vault), "--db", str(db_path)],
    )
    assert result2.exit_code == 0, result2.output

    # Idempotency core assertion: zero translate AND zero embed calls.
    assert counter.translate_calls == 0, (
        f"warm rebuild must trigger zero translate calls, got {counter.translate_calls}"
    )
    assert counter.embed_calls == 0, (
        f"warm rebuild must trigger zero embed calls, got {counter.embed_calls}"
    )

    # CLI stdout reflects the same invariant.
    assert "chunks translated: 0" in result2.output
    assert "chunks embedded: 0" in result2.output
    assert f"skipped existing: {cold_count}" in result2.output

    # ----- DB state byte-identical ---------------------------------------
    warm_snapshot = _snapshot_db(db_path)
    assert len(warm_snapshot) == cold_count, (
        f"row count drifted: cold={cold_count} warm={len(warm_snapshot)}"
    )
    assert warm_snapshot == cold_snapshot, (
        "warm rebuild must leave every chunks-table row byte-identical "
        "(including build_ts and embedding blob)"
    )

    # ----- vault still untouched -----------------------------------------
    assert _hash_vault(vault) == pre_vault_hashes, (
        "vault must remain byte-identical after warm build (immutability)"
    )


def test_build_twice_with_one_modified_file_only_reprocesses_changed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Modify one chunk between runs; only the changed chunk re-translates.

    Companion assertion to the pure idempotency test: confirms the
    "zero API calls" property is driven by ``chunk_id`` content-equality
    (the documented contract), not by accidental short-circuits like
    "skip the second run entirely". A single byte-level body change in one
    file must trigger exactly one new translate and one new embed call;
    every other chunk must still be skipped.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    _make_vault(vault)
    db_path = tmp_path / "corpus.db"

    counter = _CallCounter()
    wrapped = _make_wrapped_build_pipeline(counter)

    monkeypatch.setattr(cli_mod, "build_pipeline", wrapped)
    monkeypatch.setattr(cli_mod, "OpenAI", _FakeOpenAI)

    # Cold build.
    result1 = runner.invoke(
        app,
        ["build", "--vault-path", str(vault), "--db", str(db_path)],
    )
    assert result1.exit_code == 0, result1.output
    cold_translate = counter.translate_calls
    assert cold_translate >= 4

    # Mutate one file: change body of the single-chunk alpha note.
    alpha = vault / "00 Get Things Done" / "alpha.md"
    alpha.write_text("## 알파\n수정된 본문.\n", encoding="utf-8")

    counter.reset()
    result2 = runner.invoke(
        app,
        ["build", "--vault-path", str(vault), "--db", str(db_path)],
    )
    assert result2.exit_code == 0, result2.output

    assert counter.translate_calls == 1, (
        f"only the modified chunk should re-translate, got {counter.translate_calls}"
    )
    assert counter.embed_calls == 1, (
        f"only the modified chunk should re-embed, got {counter.embed_calls}"
    )
