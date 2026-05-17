"""SQLite storage layer for the vault-corpus chunk + embedding index.

Schema is intentionally portable: a single ``chunks`` table holds chunk
metadata plus the OpenAI text-embedding-3-large vector as a BLOB column.
sqlite-vss is loaded opportunistically (best-effort) so that nearest-neighbor
search can use the ``vss0`` virtual table when the host Python build supports
``enable_load_extension``; absence of the extension does not block ingestion
or block this module from initializing the schema.

pgvector migration
------------------
The schema is designed so that migrating to Postgres + pgvector requires
**no re-embedding**:

1. Create an identical ``chunks`` table in Postgres, swapping ``embedding BLOB``
   for ``embedding vector(3072)`` and ``heading_chain``/``front_matter`` for
   ``jsonb``.
2. Stream rows from SQLite to Postgres. ``chunk_id`` is a content-derived
   SHA-256 (see :func:`vault_corpus.chunker.compute_chunk_id`) so it is stable
   across engines — the same chunk on either side carries the same id.
3. Decode the SQLite ``embedding`` BLOB (float32 little-endian, length 3072)
   straight into a pgvector ``vector`` value. The embeddings themselves do
   not change.

Because the row payload, ids, and vector bytes are all engine-neutral, no
chunk needs to be re-translated and no embedding needs to be re-requested
from OpenAI during the migration.
"""

from __future__ import annotations

import json
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol

import numpy as np

from vault_corpus.chunker import Chunk


# OpenAI embedding model + vector dimensionality. Pinned here so callers (and
# tests) reference a single source of truth — changing the model is a one-line
# edit and any downstream length assertion picks it up automatically.
EMBEDDING_MODEL = "text-embedding-3-large"
EMBEDDING_DIM = 3072


# DDL kept as module constants so the test (and a future migration script)
# can reference them directly without re-deriving the schema.
_CREATE_CHUNKS_SQL = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id         TEXT PRIMARY KEY,
    source_path      TEXT NOT NULL,
    heading_chain    TEXT NOT NULL,
    lang             TEXT NOT NULL,
    body             TEXT NOT NULL,
    front_matter     TEXT NOT NULL,
    build_ts         TEXT NOT NULL,
    file_fingerprint TEXT NOT NULL,
    embedding        BLOB
)
""".strip()

_CREATE_SOURCE_PATH_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_chunks_source_path "
    "ON chunks(source_path)"
)


def _try_load_sqlite_vss(conn: sqlite3.Connection) -> bool:
    """Best-effort load of the sqlite-vss extension.

    Returns ``True`` on success, ``False`` otherwise. Failure is silent on
    purpose: the host Python may be built without
    ``--enable-loadable-sqlite-extensions`` (common on pyenv/macOS), and the
    main ``chunks`` table works without the extension. ANN search paths that
    require sqlite-vss should re-check availability and either load it or
    fall back to brute-force cosine.
    """
    try:
        import sqlite_vss  # type: ignore[import-not-found]
    except ImportError:
        return False
    try:
        conn.enable_load_extension(True)
    except AttributeError:
        return False
    try:
        sqlite_vss.load(conn)
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        try:
            conn.enable_load_extension(False)
        except (AttributeError, sqlite3.OperationalError):
            pass


def init_db(db_path: Path) -> sqlite3.Connection:
    """Create (if needed) and open the chunk store at ``db_path``.

    Ensures the parent directory exists, opens a connection, attempts to load
    sqlite-vss (best-effort — see :func:`_try_load_sqlite_vss`), and creates
    the ``chunks`` table plus the ``idx_chunks_source_path`` index if they do
    not already exist. The returned connection is left open for the caller.

    The ``chunks`` table has exactly nine columns:

    ``chunk_id`` (TEXT PRIMARY KEY) — content-derived SHA-256.
    ``source_path`` (TEXT) — vault-relative path of the originating note.
    ``heading_chain`` (TEXT) — JSON-encoded list of ``##``/``###`` titles.
    ``lang`` (TEXT) — ``"ko"`` for source chunks, ``"en"`` for translations.
    ``body`` (TEXT) — markdown chunk body, front-matter stripped.
    ``front_matter`` (TEXT) — JSON-encoded YAML front-matter mapping.
    ``build_ts`` (TEXT) — ISO-8601 timestamp of the build that wrote the row.
    ``file_fingerprint`` (TEXT) — hash of the source file for delta builds.
    ``embedding`` (BLOB) — float32 little-endian vector (3072 dims for
    text-embedding-3-large); ``NULL`` until the embedding pass runs.

    See module docstring for the pgvector migration path.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    _try_load_sqlite_vss(conn)

    with conn:
        conn.execute(_CREATE_CHUNKS_SQL)
        conn.execute(_CREATE_SOURCE_PATH_INDEX_SQL)

    return conn


class _EmbeddingClient(Protocol):
    """Minimal structural type for the OpenAI client surface we use.

    Anything exposing ``client.embeddings.create(model=..., input=...)`` and
    returning an object with ``.data[0].embedding`` satisfies this protocol —
    keeps the real ``openai.OpenAI`` client and a unit-test fake interchangeable
    without importing the SDK at module load time.
    """

    embeddings: Any


def _default_client() -> Any:
    """Construct the default OpenAI client.

    Isolated in its own function so tests can monkey-patch ``_default_client``
    (or pass an explicit ``client=`` to :func:`embed`) without ever importing
    the real SDK or touching the network. Import is lazy so ``store.py`` stays
    importable in environments where the ``openai`` package is unavailable.
    """
    from openai import OpenAI  # local import: keeps module import side-effect-free

    return OpenAI()


def embed(
    text: str,
    *,
    client: _EmbeddingClient | None = None,
    model: str = EMBEDDING_MODEL,
) -> list[float]:
    """Embed ``text`` via OpenAI ``text-embedding-3-large``.

    Single-shot embedding call. Constructs a default ``OpenAI`` client on
    demand (lazy import) unless an explicit ``client`` is injected — which
    is the path unit tests take to avoid any real network I/O.

    Args:
        text: Raw English chunk text to embed. Sent verbatim as ``input``.
        client: Optional pre-built OpenAI-compatible client. When ``None``,
            a fresh client is created via :func:`_default_client`.
        model: Embedding model id. Defaults to :data:`EMBEDDING_MODEL`
            (``text-embedding-3-large``); override only for tests.

    Returns:
        ``list[float]`` of length :data:`EMBEDDING_DIM` (3072 for the default
        model). The list is materialized eagerly so callers can pickle / write
        the vector to the ``embedding`` BLOB column without holding an open
        SDK response object.

    Raises:
        ValueError: If the embedding returned by the API is not the expected
            :data:`EMBEDDING_DIM` length — guards against silent model swaps
            (e.g. accidentally embedding with ``text-embedding-3-small``).
    """
    if client is None:
        client = _default_client()

    response = client.embeddings.create(model=model, input=text)
    vector = list(response.data[0].embedding)

    if model == EMBEDDING_MODEL and len(vector) != EMBEDDING_DIM:
        raise ValueError(
            f"expected {EMBEDDING_DIM}-dim embedding for {EMBEDDING_MODEL}, "
            f"got {len(vector)}"
        )

    return vector


def _encode_embedding(vector: list[float] | None) -> bytes | None:
    """Serialize an embedding vector to the on-disk BLOB representation.

    Uses float32 little-endian — the format documented in the module docstring
    and the format pgvector reads natively. ``None`` round-trips as ``None`` so
    rows can be written before the embedding pass has run.

    Raises:
        ValueError: If the vector is non-empty but not the expected
            :data:`EMBEDDING_DIM` length. Catches accidental dimension swaps
            (e.g. someone wiring up ``text-embedding-3-small`` (1536)) before
            the wrong-shape bytes hit the DB.
    """
    if vector is None:
        return None
    if len(vector) != EMBEDDING_DIM:
        raise ValueError(
            f"expected {EMBEDDING_DIM}-dim embedding, got {len(vector)}"
        )
    return struct.pack(f"<{EMBEDDING_DIM}f", *vector)


def _utc_now_iso() -> str:
    """Return current UTC time as an ISO-8601 string (seconds precision)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


_UPSERT_CHUNK_SQL = """
INSERT INTO chunks (
    chunk_id, source_path, heading_chain, lang, body,
    front_matter, build_ts, file_fingerprint, embedding
) VALUES (
    :chunk_id, :source_path, :heading_chain, :lang, :body,
    :front_matter, :build_ts, :file_fingerprint, :embedding
)
ON CONFLICT(chunk_id) DO UPDATE SET
    source_path      = excluded.source_path,
    heading_chain    = excluded.heading_chain,
    lang             = excluded.lang,
    body             = excluded.body,
    front_matter     = excluded.front_matter,
    build_ts         = excluded.build_ts,
    file_fingerprint = excluded.file_fingerprint,
    embedding        = excluded.embedding
""".strip()


def upsert_chunk(
    conn: sqlite3.Connection,
    chunk: Chunk,
    embedding: list[float] | None,
    *,
    file_fingerprint: str = "",
    build_ts: str | None = None,
) -> None:
    """Idempotently insert or update a single chunk row keyed by ``chunk_id``.

    The write is performed in a single ``INSERT ... ON CONFLICT(chunk_id) DO
    UPDATE`` statement so re-running the build with the same ``chunk_id`` but
    different body / embedding overwrites every mutable column in place — the
    table will hold exactly one row for that id, never two.

    Args:
        conn: Open SQLite connection produced by :func:`init_db`.
        chunk: The :class:`vault_corpus.chunker.Chunk` to persist. Its
            ``chunk_id``, ``source_path``, ``heading_chain``, ``lang``,
            ``body``, and ``frontmatter`` are written to the matching columns.
        embedding: 3072-float vector for the chunk, or ``None`` if the
            embedding pass has not yet run. Encoded as float32 little-endian
            bytes (see :func:`_encode_embedding`) — the same layout that
            pgvector's ``vector`` type ingests on migration.
        file_fingerprint: Hash of the source file used by delta builds to
            detect changed notes. Stored verbatim; supply an empty string when
            unknown.
        build_ts: ISO-8601 timestamp of the build that produced the row.
            Defaults to the current UTC time. Always overwritten on update so
            the column reflects the *latest* write, not the first.
    """
    if build_ts is None:
        build_ts = _utc_now_iso()

    params = {
        "chunk_id": chunk.chunk_id,
        "source_path": str(chunk.source_path),
        "heading_chain": json.dumps(chunk.heading_chain, ensure_ascii=False),
        "lang": chunk.lang,
        "body": chunk.body,
        "front_matter": json.dumps(chunk.frontmatter, ensure_ascii=False, default=str),
        "build_ts": build_ts,
        "file_fingerprint": file_fingerprint,
        "embedding": _encode_embedding(embedding),
    }

    with conn:
        conn.execute(_UPSERT_CHUNK_SQL, params)


def delete_obsolete_chunks(
    db: sqlite3.Connection,
    source_path: str | Path,
    current_chunk_ids: Iterable[str],
) -> int:
    """Delete DB rows for ``source_path`` whose ``chunk_id`` is not current.

    Delta-build cleanup primitive: when a vault note is re-chunked, headings
    may have been renamed, merged, split, or removed. Each surviving chunk
    re-derives the same content-addressed ``chunk_id`` (see
    :func:`vault_corpus.chunker.compute_chunk_id`) and is rewritten via
    :func:`upsert_chunk`, but the rows for *removed* headings would otherwise
    linger forever — silently inflating the index and polluting search
    results with stale English mirrors of content the user has deleted.

    This function removes exactly those stale rows. The match is scoped to
    ``source_path`` so chunks from other notes are never touched, and the
    filter is "row's ``chunk_id`` is not in the caller-supplied current set"
    so any chunk that still exists in the freshly-chunked note is preserved
    in place — no churn, no re-embedding, no re-translation.

    Behavior contract:

    * Rows where ``source_path`` matches and ``chunk_id`` is **in**
      ``current_chunk_ids`` are left untouched.
    * Rows where ``source_path`` matches and ``chunk_id`` is **not in**
      ``current_chunk_ids`` are deleted (both ``lang='ko'`` and ``lang='en'``
      mirrors for the same id are removed together — they share the id).
    * Rows from other source paths are never inspected or deleted.
    * Passing an empty ``current_chunk_ids`` deletes *every* row for that
      source path — the correct semantics when a vault note's entire body
      has been removed but the file still exists (e.g. front-matter only).

    Args:
        db: Open SQLite connection produced by :func:`init_db`.
        source_path: Vault-relative path of the note whose stale chunks
            should be reaped. Coerced to ``str`` so callers can pass either
            a :class:`pathlib.Path` or a raw string — both round-trip
            against the value written by :func:`upsert_chunk`.
        current_chunk_ids: Iterable of ``chunk_id`` strings that the
            freshly-chunked note still produces. Materialized to a list
            internally so generators are safe to pass.

    Returns:
        ``int`` — number of rows deleted. ``0`` when the note's current
        chunk set already matches the stored set (the common warm-build
        case). Useful for the delta-build debug log and for assertions in
        the integration test.
    """
    src = str(source_path)
    current = list(current_chunk_ids)

    with db:
        if not current:
            cur = db.execute(
                "DELETE FROM chunks WHERE source_path = ?",
                (src,),
            )
            return cur.rowcount or 0

        # Build an in-clause sized to the current set. SQLite's default
        # parameter limit (999 / 32766 depending on build) is comfortably
        # above any plausible per-file chunk count, so a single statement
        # is safe and avoids a second round-trip.
        placeholders = ",".join("?" * len(current))
        sql = (
            f"DELETE FROM chunks "
            f"WHERE source_path = ? AND chunk_id NOT IN ({placeholders})"
        )
        cur = db.execute(sql, (src, *current))
        return cur.rowcount or 0


def _decode_embedding_blob(blob: bytes) -> np.ndarray:
    """Decode the on-disk float32 little-endian BLOB back into a numpy array.

    Mirror of :func:`_encode_embedding`. Used by :func:`search` to brute-force
    cosine similarity against stored chunk vectors. Returned dtype is
    ``float32`` to match the encoded payload exactly — no widening copy.
    """
    return np.frombuffer(blob, dtype="<f4")


_SELECT_EN_EMBEDDED_CHUNKS_SQL = """
SELECT chunk_id, source_path, heading_chain, lang, body, front_matter, embedding
FROM chunks
WHERE lang = 'en' AND embedding IS NOT NULL
""".strip()


def search_with_scores(
    conn: sqlite3.Connection,
    query: str,
    k: int,
    *,
    client: _EmbeddingClient | None = None,
    model: str = EMBEDDING_MODEL,
) -> list[tuple[Chunk, float]]:
    """Return the top-``k`` English chunks paired with their cosine similarity.

    Same ranking logic as :func:`search`, but the similarity score for each
    returned chunk is exposed alongside the chunk. Used by the smoke-test
    gate (``vault_corpus.smoke.run_query``), which needs the score to apply
    the "at least 3 results above the similarity floor" check.

    See :func:`search` for the full argument and ranking contract — this
    function shares its implementation and only differs in return shape.

    Returns:
        ``list[tuple[Chunk, float]]`` of length ``min(k, n_english_chunks)``,
        sorted by cosine similarity descending. The second element of each
        tuple is the cosine similarity in ``[-1.0, 1.0]``.
    """
    if k <= 0:
        return []

    rows = conn.execute(_SELECT_EN_EMBEDDED_CHUNKS_SQL).fetchall()
    if not rows:
        return []

    query_vec = np.asarray(
        embed(query, client=client, model=model), dtype=np.float32
    )
    query_norm = float(np.linalg.norm(query_vec))
    if query_norm == 0.0:
        return []

    scored: list[tuple[float, tuple]] = []
    for row in rows:
        vec = _decode_embedding_blob(row[6])
        denom = query_norm * float(np.linalg.norm(vec))
        if denom == 0.0:
            continue
        sim = float(np.dot(query_vec, vec) / denom)
        scored.append((sim, row))

    scored.sort(key=lambda pair: pair[0], reverse=True)

    results: list[tuple[Chunk, float]] = []
    for sim, row in scored[:k]:
        chunk = Chunk(
            source_path=Path(row[1]),
            heading_chain=list(json.loads(row[2])),
            body=row[4],
            chunk_id=row[0],
            lang=row[3],
            frontmatter=dict(json.loads(row[5])),
        )
        results.append((chunk, sim))
    return results


def search(
    conn: sqlite3.Connection,
    query: str,
    k: int,
    *,
    client: _EmbeddingClient | None = None,
    model: str = EMBEDDING_MODEL,
) -> list[Chunk]:
    """Return the top-``k`` English chunks most similar to ``query``.

    Embeds ``query`` via :func:`embed` and ranks every English chunk
    (``lang = 'en'`` with a non-NULL embedding) by cosine similarity to that
    query vector. Korean source rows are excluded by the SQL filter, so they
    cannot appear in the result even when their vectors are closer.

    The ranking is a brute-force pass in Python over the stored float32 BLOBs.
    sqlite-vss is not required (and is intentionally not used here) so the
    function works on every host regardless of whether the loadable extension
    is available. For 1k–10k chunks this is fast enough; if the corpus grows
    past that, swap this body for a ``vss_search`` query against a vss virtual
    table — the schema is already set up for it and the cosine math stays the
    same so results are bit-identical.

    Args:
        conn: Open SQLite connection produced by :func:`init_db`.
        query: Natural-language query string. Embedded via the same OpenAI
            model as the corpus (``text-embedding-3-large`` by default) so the
            query vector lives in the same space as the chunk vectors.
        k: Maximum number of results to return. Values ``<= 0`` produce an
            empty list. Fewer results are returned when the index contains
            fewer than ``k`` English chunks.
        client: Optional OpenAI-compatible client used to embed the query.
            ``None`` triggers :func:`_default_client` — tests inject a fake to
            avoid network I/O and to control the query vector deterministically.
        model: Embedding model id. Defaults to :data:`EMBEDDING_MODEL`. Must
            match the model used to embed the stored chunks; otherwise the
            cosine scores are meaningless.

    Returns:
        ``list[Chunk]`` of length ``min(k, n_english_chunks)`` sorted by
        cosine similarity (highest first). Each returned :class:`Chunk` is
        reconstructed from the stored row — ``source_path`` is a
        :class:`pathlib.Path`, ``heading_chain`` and ``frontmatter`` are
        decoded from their JSON columns, and ``lang`` is always ``"en"``.
    """
    return [
        chunk
        for chunk, _sim in search_with_scores(
            conn, query, k, client=client, model=model
        )
    ]


__all__ = [
    "init_db",
    "embed",
    "upsert_chunk",
    "delete_obsolete_chunks",
    "search",
    "search_with_scores",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIM",
]
