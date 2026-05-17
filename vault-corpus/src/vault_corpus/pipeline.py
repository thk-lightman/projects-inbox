"""Build pipeline orchestrator.

Sequences the full scan → chunk → translate-if-missing → embed → upsert
flow for every scoped vault note. Pure orchestration: every external
dependency (filesystem reads, OpenAI translation, OpenAI embedding, SQLite
writes) is reached via a small handful of named callables so unit tests can
inject mocks and assert on call order.

Translation is skipped when the SQLite store already contains an English
row keyed by the same ``chunk_id`` *and* that row carries a non-NULL
embedding. The ``chunk_id`` is content-derived (see
:func:`vault_corpus.chunker.compute_chunk_id`), so an unchanged source
chunk re-produces the same id on every run — this is what makes a repeated
``build`` invocation cost zero OpenAI calls when nothing changed.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from vault_corpus.chunker import Chunk, chunk_note
from vault_corpus.cost import ApiCostTracker
from vault_corpus.scanner import (
    SCOPED_DIRS,
    _has_hidden_segment,
    file_fingerprint,
    list_scoped_files,
)
from vault_corpus.store import delete_obsolete_chunks
from vault_corpus.store import embed as default_embed
from vault_corpus.store import upsert_chunk
from vault_corpus.translator import translate_chunk as default_translate_chunk


log = logging.getLogger(__name__)


# Default callable plumbing. Exposed at module scope so tests can override
# them via the function parameters without monkey-patching imports.
TranslateFn = Callable[[Chunk, Any], Chunk]
EmbedFn = Callable[[str, Any], list[float]]
ScanFn = Callable[[Path], list[Path]]
ReadFn = Callable[[Path], str]
ChunkFn = Callable[[Path, str], list[Chunk]]


def _default_translate(chunk: Chunk, client: Any) -> Chunk:
    """Thin adapter: forward to :func:`translator.translate_chunk`."""
    return default_translate_chunk(chunk, client)


def _default_embed(text: str, client: Any) -> list[float]:
    """Thin adapter: forward to :func:`store.embed`."""
    return default_embed(text, client=client)


def _default_read(path: Path) -> str:
    """Read a vault note as UTF-8 text. Never writes."""
    return path.read_text(encoding="utf-8")


@dataclass
class BuildReport:
    """Summary returned by :func:`build_pipeline`.

    Attributes:
        files_scanned: Number of vault notes returned by the scanner.
        chunks_seen: Total Korean chunks produced by the chunker across all
            files (preamble chunks counted; entirely-front-matter notes
            contribute 0).
        chunks_translated: Number of chunks that triggered a fresh
            translation call. Equals ``chunks_seen`` on a cold build, drops
            toward 0 on a warm re-run when the DB already holds the
            English mirror with an embedding.
        chunks_embedded: Number of chunks that triggered a fresh embedding
            call. Tracks ``chunks_translated`` 1:1 in the current pipeline
            but is reported separately so the call order assertion can
            distinguish the two passes.
        chunks_upserted: Number of rows written to the ``chunks`` table.
            Equals the number of English chunks the build produced — every
            English chunk is upserted, even when the row already existed,
            so the ``build_ts`` column reflects the latest build.
        skipped_existing: Chunks the pipeline skipped because the DB
            already had an English row with an embedding for the same
            ``chunk_id``. Used by the delta-build smoke test to verify
            zero re-translation on an unchanged corpus.
        failed_files: Source-paths of vault notes that raised during read
            or chunking. The pipeline never aborts on a single bad file —
            failures are collected and reported.
    """

    files_scanned: int = 0
    chunks_seen: int = 0
    chunks_translated: int = 0
    chunks_embedded: int = 0
    chunks_upserted: int = 0
    skipped_existing: int = 0
    chunks_deleted: int = 0
    failed_files: list[Path] = field(default_factory=list)
    cost: ApiCostTracker = field(default_factory=ApiCostTracker)


def should_skip_chunk(db: sqlite3.Connection, chunk_id: str) -> bool:
    """Return ``True`` when ``chunk_id`` already exists in the store with ``lang='en'``.

    Delta-build predicate used by callers that want a single-chunk cache-hit
    check without materializing the full English chunk-id set. Semantics:

    * Returns ``False`` when no row with ``chunk_id`` exists.
    * Returns ``False`` when the only row(s) for ``chunk_id`` carry
      ``lang='ko'`` (Korean-source-only — translation has not been written
      back yet).
    * Returns ``True`` as soon as any row with ``chunk_id`` carries
      ``lang='en'`` — the English mirror is already on disk and the
      translate+embed pass can be skipped.

    The check is intentionally embedding-agnostic: we treat *presence of an
    English row* as the skip signal, not whether the embedding column has
    been populated. Embedding-aware skipping lives in
    :func:`_existing_en_chunk_ids` and the main :func:`build_pipeline` loop.

    Args:
        db: Open SQLite connection produced by :func:`vault_corpus.store.init_db`.
        chunk_id: Content-derived SHA-256 from
            :func:`vault_corpus.chunker.compute_chunk_id`.

    Returns:
        ``True`` if an English row exists for ``chunk_id``; otherwise ``False``.
    """
    row = db.execute(
        "SELECT 1 FROM chunks WHERE chunk_id = ? AND lang = 'en' LIMIT 1",
        (chunk_id,),
    ).fetchone()
    return row is not None


def _existing_en_chunk_ids(conn: sqlite3.Connection) -> set[str]:
    """Return ``chunk_id`` for every English row that already has an embedding.

    A single batched query — the call sites use the set as a fast membership
    check during the per-chunk loop, so we never re-hit the DB inside the
    inner loop. Returns an empty set on first build.
    """
    cur = conn.execute(
        "SELECT chunk_id FROM chunks WHERE lang = 'en' AND embedding IS NOT NULL"
    )
    return {row[0] for row in cur.fetchall()}


def _iter_file_chunks(
    files: Iterable[Path],
    *,
    read: ReadFn,
    chunker: ChunkFn,
    report: BuildReport,
) -> Iterable[tuple[Path, str, list[Chunk]]]:
    """Yield ``(path, fingerprint, chunks)`` for every readable file.

    Errors on a single file (missing, unreadable, malformed UTF-8, chunker
    raises) append the path to ``report.failed_files`` and continue — one
    bad note must not abort the whole corpus build.
    """
    for path in files:
        report.files_scanned += 1
        try:
            text = read(path)
        except OSError as exc:
            log.warning("read failed for %s: %s", path, exc)
            report.failed_files.append(path)
            continue
        try:
            chunks = chunker(path, text)
        except Exception as exc:  # noqa: BLE001 — chunker is third-party-ish
            log.warning("chunk failed for %s: %s", path, exc)
            report.failed_files.append(path)
            continue
        try:
            fp = file_fingerprint(path)
        except OSError as exc:
            log.warning("fingerprint failed for %s: %s", path, exc)
            fp = ""
        yield path, fp, chunks


def build_pipeline(
    vault_path: Path,
    db: sqlite3.Connection,
    translate_client: Any,
    embed_client: Any,
    *,
    scan: ScanFn = list_scoped_files,
    read: ReadFn = _default_read,
    chunker: ChunkFn = chunk_note,
    translate: TranslateFn = _default_translate,
    embed: EmbedFn = _default_embed,
) -> BuildReport:
    """Run the full scan → chunk → translate → embed → upsert pipeline.

    Ordering guarantee for a single chunk:

    1. ``scan(vault_path)`` enumerates the in-scope vault notes.
    2. For each file: ``read(path)`` → ``chunker(path, text)`` →
       ``file_fingerprint(path)``.
    3. For each Korean chunk:

       a. If the DB already has an English row with an embedding for the
          same ``chunk_id``, skip translation+embedding (delta semantics).
       b. Otherwise ``translate(ko_chunk, translate_client)`` →
          English chunk, then ``embed(en_chunk.body, embed_client)`` →
          3072-float vector.
       c. ``upsert_chunk(db, en_chunk, vector, file_fingerprint=fp)``
          writes the row. Every English chunk is upserted (even when the
          translation was skipped) so ``build_ts`` reflects the latest run.

    Args:
        vault_path: Obsidian vault root. Scanner filters this to the 5
            scoped top-level directories — anything outside is ignored.
        db: Open SQLite connection from :func:`vault_corpus.store.init_db`.
            Writes happen inside per-row transactions managed by
            :func:`upsert_chunk`; the caller still owns connection close.
        translate_client: Object exposing
            ``client.chat.completions.create`` (real ``openai.OpenAI``
            instance, or a unit-test fake). Forwarded verbatim to
            ``translate``.
        embed_client: Object exposing
            ``client.embeddings.create`` (real ``openai.OpenAI`` instance,
            or a unit-test fake). Forwarded verbatim to ``embed``.
        scan: Override for the file enumerator. Defaults to
            :func:`vault_corpus.scanner.list_scoped_files`.
        read: Override for the per-file reader. Tests inject this to
            avoid hitting disk.
        chunker: Override for ``(path, text) -> list[Chunk]``. Defaults to
            :func:`vault_corpus.chunker.chunk_note`.
        translate: Override for ``(ko_chunk, client) -> en_chunk``.
        embed: Override for ``(text, client) -> list[float]``.

    Returns:
        :class:`BuildReport` with counts of files scanned, chunks seen,
        chunks translated, chunks embedded, chunks upserted, chunks
        skipped because already present in the DB, and any files that
        failed.

    The function never writes to ``vault_path`` — every filesystem touch is
    a read. The vault is treated as immutable source-of-truth.
    """
    report = BuildReport()
    files = list(scan(vault_path))

    existing = _existing_en_chunk_ids(db)

    for path, fp, ko_chunks in _iter_file_chunks(
        files, read=read, chunker=chunker, report=report
    ):
        for ko_chunk in ko_chunks:
            report.chunks_seen += 1

            if ko_chunk.chunk_id in existing:
                # Re-upsert is intentionally skipped on a warm hit: the row
                # is byte-identical to what's already stored, and we want
                # delta builds to be zero-API AND zero-write where possible.
                report.skipped_existing += 1
                continue

            try:
                en_chunk = translate(ko_chunk, translate_client)
            except Exception as exc:  # noqa: BLE001
                log.warning("translate failed for %s: %s", ko_chunk.chunk_id, exc)
                report.failed_files.append(path)
                continue
            report.chunks_translated += 1
            report.cost.record_translate()

            try:
                vector = embed(en_chunk.body, embed_client)
            except Exception as exc:  # noqa: BLE001
                log.warning("embed failed for %s: %s", ko_chunk.chunk_id, exc)
                report.failed_files.append(path)
                continue
            report.chunks_embedded += 1
            report.cost.record_embed()

            upsert_chunk(db, en_chunk, vector, file_fingerprint=fp)
            report.chunks_upserted += 1
            existing.add(en_chunk.chunk_id)

    log.info(
        "build complete: scanned=%d chunks=%d translated=%d embedded=%d upserted=%d skipped=%d failed=%d",
        report.files_scanned,
        report.chunks_seen,
        report.chunks_translated,
        report.chunks_embedded,
        report.chunks_upserted,
        report.skipped_existing,
        len(report.failed_files),
    )
    return report


def rechunk_changed_file(
    path: Path,
    *,
    read: ReadFn = _default_read,
    chunker: ChunkFn = chunk_note,
) -> list[Chunk]:
    """Re-parse a single changed vault note and return its current chunks.

    Delta-build helper. Given a path that ``git diff`` flagged as modified
    (or newly added), reads the file from disk and runs the same chunker
    used by :func:`build_pipeline`, returning the freshly-computed Korean
    chunk list. The returned chunks carry the same content-derived
    ``chunk_id`` values that a full build would produce, so callers can
    diff the new id-set against ``_existing_en_chunk_ids`` to decide which
    rows need translation and which can be skipped.

    The function is **read-only** with respect to the vault: it never
    writes to ``path``, never modifies its mtime, and never touches sibling
    files. It is also stateless — no DB connection, no OpenAI client — so
    it is safe to call from any context (CLI, tests, future MCP adapter).

    Args:
        path: Absolute path to the changed markdown file. Caller is
            expected to have already filtered the path against the scoped
            vault directories (see :func:`vault_corpus.scanner.list_scoped_files`).
        read: Override for the per-file reader. Defaults to a UTF-8
            ``read_text`` call; tests inject this to stub disk access.
        chunker: Override for ``(path, text) -> list[Chunk]``. Defaults to
            :func:`vault_corpus.chunker.chunk_note`.

    Returns:
        Ordered list of :class:`vault_corpus.chunker.Chunk` for the file.
        Empty list when the note contains only front-matter (matches
        :func:`chunk_note` semantics — there is nothing chunkable, so we
        do not emit a placeholder row).

    Raises:
        OSError: When ``path`` cannot be read (missing, permission denied,
            invalid UTF-8). Callers handle deletes upstream by NOT calling
            this function for removed paths.
    """
    text = read(path)
    return chunker(path, text)


def _is_in_scope(vault_path: Path, rel_path: str) -> bool:
    """True when ``rel_path`` is a .md file under one of SCOPED_DIRS, no hidden segments."""
    if not rel_path.endswith(".md"):
        return False
    parts = Path(rel_path).parts
    if not parts:
        return False
    if parts[0] not in SCOPED_DIRS:
        return False
    if _has_hidden_segment(parts):
        return False
    return True


def git_diff_changed_files(
    vault_path: Path,
    ref: str = "HEAD~1",
    *,
    runner: Callable[[list[str], Path], str] | None = None,
) -> list[Path]:
    """Return scoped .md vault paths changed since ``ref`` (default HEAD~1).

    Runs ``git diff --name-only --diff-filter=ACMRT <ref>`` inside
    ``vault_path``. ``D`` (deleted) entries are intentionally filtered out:
    delete cleanup is handled by ``delete_obsolete_chunks`` keyed on
    ``source_path``, which only needs to know the surviving chunk-id set;
    a path that no longer exists on disk has nothing to re-chunk.

    Only files that pass :func:`_is_in_scope` are returned, so callers
    receive paths that are safe to feed straight into
    :func:`rechunk_changed_file` and the build pipeline.

    Args:
        vault_path: Vault repo root. Must be a git working tree — the
            helper raises ``RuntimeError`` otherwise so the CLI can print a
            clear "run delta only against a git-tracked vault" message.
        ref: Git ref to diff against. Defaults to ``HEAD~1`` to match the
            seed contract ("git-diff-driven incremental rebuild").
        runner: Override hook for tests — receives the argv list and
            cwd, returns stdout as a string. Defaults to a
            :mod:`subprocess` runner.

    Returns:
        Absolute :class:`Path` objects, one per in-scope changed markdown
        file. Empty list when nothing in scope changed.
    """
    if not isinstance(vault_path, Path):
        vault_path = Path(vault_path)
    vault_path = vault_path.expanduser()
    if not vault_path.is_dir():
        raise NotADirectoryError(f"vault_path not a directory: {vault_path}")

    if runner is None:
        if shutil.which("git") is None:
            raise RuntimeError("git executable not found on PATH")
        runner = _default_git_runner

    argv = [
        "git",
        "diff",
        "--name-only",
        "--diff-filter=ACMRT",
        ref,
    ]
    stdout = runner(argv, vault_path)

    out: list[Path] = []
    for line in stdout.splitlines():
        rel = line.strip()
        if not rel:
            continue
        if not _is_in_scope(vault_path, rel):
            continue
        out.append(vault_path / rel)
    return out


def _default_git_runner(argv: list[str], cwd: Path) -> str:
    """Subprocess runner used by :func:`git_diff_changed_files` in production.

    Raises ``RuntimeError`` with stderr appended when git exits non-zero,
    so the CLI can surface a meaningful message rather than an opaque
    ``CalledProcessError`` traceback.
    """
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git diff failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def process_delta(
    db: sqlite3.Connection,
    changed_paths: Iterable[Path],
    translate_client: Any,
    embed_client: Any,
    *,
    read: ReadFn = _default_read,
    chunker: ChunkFn = chunk_note,
    translate: TranslateFn = _default_translate,
    embed: EmbedFn = _default_embed,
) -> BuildReport:
    """Re-process exactly the supplied vault paths.

    For each path:

    1. Re-chunk the file via :func:`rechunk_changed_file`.
    2. Call :func:`delete_obsolete_chunks` to evict DB rows whose
       ``chunk_id`` is no longer produced.
    3. For each surviving chunk, skip translation+embedding when an
       English mirror already exists (same delta semantics as
       :func:`build_pipeline`), otherwise translate → embed → upsert.

    Files that fail to read or chunk are recorded in
    ``report.failed_files`` and skipped; the orchestrator never aborts on
    a single bad note.

    The vault is read-only in every code path. The git-diff lookup is the
    caller's responsibility — pass paths from
    :func:`git_diff_changed_files`.
    """
    report = BuildReport()
    existing = _existing_en_chunk_ids(db)

    for path in changed_paths:
        report.files_scanned += 1
        try:
            chunks = rechunk_changed_file(path, read=read, chunker=chunker)
        except OSError as exc:
            log.warning("delta read failed for %s: %s", path, exc)
            report.failed_files.append(path)
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning("delta chunk failed for %s: %s", path, exc)
            report.failed_files.append(path)
            continue

        try:
            fp = file_fingerprint(path)
        except OSError as exc:
            log.warning("delta fingerprint failed for %s: %s", path, exc)
            fp = ""

        current_ids = [c.chunk_id for c in chunks]
        # Use the chunker's view of source_path (matches what build_pipeline
        # wrote) — every Chunk carries its own source_path, derived from the
        # path argument inside chunk_note. Use the first chunk's value when
        # the file produced at least one chunk; otherwise fall back to
        # ``path`` so an emptied note still has its stale rows reaped.
        src_for_delete = chunks[0].source_path if chunks else path
        deleted = delete_obsolete_chunks(db, src_for_delete, current_ids)
        report.chunks_deleted += deleted

        for ko_chunk in chunks:
            report.chunks_seen += 1
            if ko_chunk.chunk_id in existing:
                report.skipped_existing += 1
                continue

            try:
                en_chunk = translate(ko_chunk, translate_client)
            except Exception as exc:  # noqa: BLE001
                log.warning("delta translate failed for %s: %s", ko_chunk.chunk_id, exc)
                report.failed_files.append(path)
                continue
            report.chunks_translated += 1
            report.cost.record_translate()

            try:
                vector = embed(en_chunk.body, embed_client)
            except Exception as exc:  # noqa: BLE001
                log.warning("delta embed failed for %s: %s", ko_chunk.chunk_id, exc)
                report.failed_files.append(path)
                continue
            report.chunks_embedded += 1
            report.cost.record_embed()

            upsert_chunk(db, en_chunk, vector, file_fingerprint=fp)
            report.chunks_upserted += 1
            existing.add(en_chunk.chunk_id)

    log.info(
        "delta complete: scanned=%d chunks=%d translated=%d embedded=%d "
        "upserted=%d skipped=%d deleted=%d failed=%d",
        report.files_scanned,
        report.chunks_seen,
        report.chunks_translated,
        report.chunks_embedded,
        report.chunks_upserted,
        report.skipped_existing,
        report.chunks_deleted,
        len(report.failed_files),
    )
    return report


__all__ = [
    "build_pipeline",
    "BuildReport",
    "rechunk_changed_file",
    "should_skip_chunk",
    "git_diff_changed_files",
    "process_delta",
]
