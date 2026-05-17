"""SQLite persistence — WAL mode, sentence state machine, file-level checkpoint."""
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

STATUS_PENDING = "pending"
STATUS_CURATED = "curated"
STATUS_MERGED = "merged"
STATUS_LOCKED = "locked"
STATUS_TRANSLATED = "translated"
STATUS_SYNCED = "synced"
STATUS_DELETED = "deleted"
STATUS_ERROR = "error"

LANG_KR = "kr"
LANG_EN = "en"

_SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS sentences (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kr_hash     TEXT    NOT NULL UNIQUE,
    kr_text     TEXT    NOT NULL,
    en_text     TEXT,
    source_file TEXT    NOT NULL,
    folder_key  TEXT    NOT NULL,
    lang        TEXT    NOT NULL DEFAULT 'kr',
    status      TEXT    NOT NULL DEFAULT 'pending',
    embedding   BLOB,
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS file_state (
    file_path    TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    processed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS expressions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kr_hash     TEXT    NOT NULL UNIQUE,
    kr_expr     TEXT    NOT NULL,
    en_expr     TEXT,
    gloss       TEXT,
    freq        INTEGER NOT NULL DEFAULT 0,
    lang        TEXT    NOT NULL DEFAULT 'kr',
    status      TEXT    NOT NULL DEFAULT 'pending',
    centroid    BLOB,
    member_count INTEGER NOT NULL DEFAULT 0,
    label_dirty INTEGER NOT NULL DEFAULT 1,
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS expression_instances (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    expression_id  INTEGER NOT NULL REFERENCES expressions(id) ON DELETE CASCADE,
    sentence_id    INTEGER NOT NULL REFERENCES sentences(id) ON DELETE CASCADE,
    created_at     REAL    NOT NULL,
    UNIQUE(expression_id, sentence_id)
);
"""

_SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_status ON sentences(status);
CREATE INDEX IF NOT EXISTS idx_source ON sentences(source_file);
CREATE INDEX IF NOT EXISTS idx_sentence_lang ON sentences(lang);
CREATE INDEX IF NOT EXISTS idx_expr_status ON expressions(status);
CREATE INDEX IF NOT EXISTS idx_expr_lang ON expressions(lang);
CREATE INDEX IF NOT EXISTS idx_instance_expr ON expression_instances(expression_id);
CREATE INDEX IF NOT EXISTS idx_instance_sentence ON expression_instances(sentence_id);
"""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_expr(text: str) -> str:
    return text.strip().lower()


def _expr_hash(text: str, lang: str) -> str:
    """Lang-scoped hash so same text in different langs becomes different rows."""
    return _sha256(f"{lang}::{_normalize_expr(text)}")


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent migration. Adds new columns to old DBs."""
    s_cols = {r["name"] for r in conn.execute("PRAGMA table_info(sentences)").fetchall()}
    if "lang" not in s_cols:
        conn.execute("ALTER TABLE sentences ADD COLUMN lang TEXT NOT NULL DEFAULT 'kr'")
    if "embedding" not in s_cols:
        conn.execute("ALTER TABLE sentences ADD COLUMN embedding BLOB")

    e_cols = {r["name"] for r in conn.execute("PRAGMA table_info(expressions)").fetchall()}
    if e_cols and "lang" not in e_cols:
        conn.execute("ALTER TABLE expressions ADD COLUMN lang TEXT NOT NULL DEFAULT 'kr'")
    if e_cols and "centroid" not in e_cols:
        conn.execute("ALTER TABLE expressions ADD COLUMN centroid BLOB")
    if e_cols and "member_count" not in e_cols:
        conn.execute("ALTER TABLE expressions ADD COLUMN member_count INTEGER NOT NULL DEFAULT 0")
    if e_cols and "label_dirty" not in e_cols:
        conn.execute("ALTER TABLE expressions ADD COLUMN label_dirty INTEGER NOT NULL DEFAULT 1")
    conn.commit()


class Database:
    def __init__(self, db_path: Path):
        self._path = db_path
        self._checkpoint_path = db_path.parent / ".identity_checkpoint.json"
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self) -> None:
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA_TABLES)
        _migrate(self._conn)
        self._conn.executescript(_SCHEMA_INDEXES)
        self._conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.commit()
            self._conn.close()
            self._conn = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()

    # ── file state ────────────────────────────────────────────────────────────

    def get_file_hash(self, file_path: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT content_hash FROM file_state WHERE file_path = ?", (file_path,)
        ).fetchone()
        return row["content_hash"] if row else None

    def upsert_file_hash(self, file_path: str, content_hash: str) -> None:
        self._conn.execute(
            """INSERT INTO file_state(file_path, content_hash, processed_at)
               VALUES(?, ?, ?)
               ON CONFLICT(file_path) DO UPDATE SET
                   content_hash = excluded.content_hash,
                   processed_at = excluded.processed_at""",
            (file_path, content_hash, time.time()),
        )
        self._conn.commit()

    # ── sentences ─────────────────────────────────────────────────────────────

    def upsert_sentences_batch(self, rows: list[dict]) -> int:
        """Insert new sentences; skip duplicates. Returns count inserted.

        Each row dict must include `lang` ('kr' or 'en').
        """
        now = time.time()
        cur = self._conn.executemany(
            """INSERT INTO sentences(kr_hash, kr_text, source_file, folder_key, lang, status, created_at, updated_at)
               VALUES(:kr_hash, :kr_text, :source_file, :folder_key, :lang, 'pending', :now, :now)
               ON CONFLICT(kr_hash) DO NOTHING""",
            [
                {
                    **r,
                    "now": now,
                    "lang": r.get("lang", LANG_KR),
                    "kr_hash": _sha256(r["kr_text"]),
                }
                for r in rows
            ],
        )
        self._conn.commit()
        return cur.rowcount

    def get_sentences_by_status(self, *statuses: str) -> list[sqlite3.Row]:
        placeholders = ",".join("?" * len(statuses))
        return self._conn.execute(
            f"SELECT * FROM sentences WHERE status IN ({placeholders}) ORDER BY id",
            list(statuses),
        ).fetchall()

    def set_translation(self, kr_hash: str, en_text: str) -> None:
        self._conn.execute(
            "UPDATE sentences SET en_text=?, status=?, updated_at=? WHERE kr_hash=?",
            (en_text, STATUS_TRANSLATED, time.time(), kr_hash),
        )
        self._conn.commit()

    def set_synced(self, kr_hash: str) -> None:
        self._conn.execute(
            "UPDATE sentences SET status=?, updated_at=? WHERE kr_hash=?",
            (STATUS_SYNCED, time.time(), kr_hash),
        )
        self._conn.commit()

    def set_error(self, kr_hash: str) -> None:
        self._conn.execute(
            "UPDATE sentences SET status=?, updated_at=? WHERE kr_hash=?",
            (STATUS_ERROR, time.time(), kr_hash),
        )
        self._conn.commit()

    def set_sentence_status(self, kr_hash: str, status: str) -> None:
        self._conn.execute(
            "UPDATE sentences SET status=?, updated_at=? WHERE kr_hash=?",
            (status, time.time(), kr_hash),
        )
        self._conn.commit()

    def set_sentence_translation(self, sentence_id: int, en_text: str) -> None:
        self._conn.execute(
            "UPDATE sentences SET en_text=?, updated_at=? WHERE id=?",
            (en_text, time.time(), sentence_id),
        )
        self._conn.commit()

    def stats(self) -> dict:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM sentences GROUP BY status"
        ).fetchall()
        return {r["status"]: r["cnt"] for r in rows}

    def expression_stats(self) -> dict:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM expressions GROUP BY status"
        ).fetchall()
        return {r["status"]: r["cnt"] for r in rows}

    # ── expressions ──────────────────────────────────────────────────────────

    def upsert_expression(self, kr_expr: str, lang: str = LANG_KR) -> int:
        """Insert expression by normalized text + lang. Returns expression id.

        For en mode, kr_expr column holds the EN expression text. The column
        name is preserved for backwards compat — treat it as 'source expression'.
        """
        now = time.time()
        expr_hash = _expr_hash(kr_expr, lang)
        # For EN mode, also seed en_expr with the source text immediately so
        # the translate stage can be skipped entirely.
        en_seed = kr_expr if lang == LANG_EN else None
        self._conn.execute(
            """INSERT INTO expressions(kr_hash, kr_expr, en_expr, freq, lang, status, created_at, updated_at)
               VALUES(?, ?, ?, 0, ?, 'pending', ?, ?)
               ON CONFLICT(kr_hash) DO NOTHING""",
            (expr_hash, kr_expr, en_seed, lang, now, now),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id FROM expressions WHERE kr_hash=?", (expr_hash,)
        ).fetchone()
        return row["id"]

    def link_instance(self, expression_id: int, sentence_id: int) -> bool:
        """Link sentence as an instance of expression. Returns True if newly linked."""
        cur = self._conn.execute(
            """INSERT INTO expression_instances(expression_id, sentence_id, created_at)
               VALUES(?, ?, ?)
               ON CONFLICT(expression_id, sentence_id) DO NOTHING""",
            (expression_id, sentence_id, time.time()),
        )
        if cur.rowcount > 0:
            self._conn.execute(
                "UPDATE expressions SET freq = freq + 1, updated_at=? WHERE id=?",
                (time.time(), expression_id),
            )
            self._conn.commit()
            return True
        self._conn.commit()
        return False

    def get_sentence_by_hash(self, kr_hash: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM sentences WHERE kr_hash=?", (kr_hash,)
        ).fetchone()

    def get_expressions_by_status(self, *statuses: str) -> list[sqlite3.Row]:
        placeholders = ",".join("?" * len(statuses))
        return self._conn.execute(
            f"SELECT * FROM expressions WHERE status IN ({placeholders}) ORDER BY freq DESC, id",
            list(statuses),
        ).fetchall()

    def get_expressions_with_min_freq(self, min_freq: int, *statuses: str) -> list[sqlite3.Row]:
        placeholders = ",".join("?" * len(statuses))
        return self._conn.execute(
            f"SELECT * FROM expressions WHERE status IN ({placeholders}) AND freq >= ? "
            "ORDER BY freq DESC, id",
            [*statuses, min_freq],
        ).fetchall()

    def set_expression_translation(self, expr_id: int, en_expr: str, gloss: Optional[str] = None) -> None:
        self._conn.execute(
            """UPDATE expressions SET en_expr=?, gloss=?, status=?, updated_at=?
               WHERE id=?""",
            (en_expr, gloss, STATUS_TRANSLATED, time.time(), expr_id),
        )
        self._conn.commit()

    def set_expression_status(self, expr_id: int, status: str) -> None:
        self._conn.execute(
            "UPDATE expressions SET status=?, updated_at=? WHERE id=?",
            (status, time.time(), expr_id),
        )
        self._conn.commit()

    def get_instances_for_expression(self, expression_id: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            """SELECT s.* FROM expression_instances ei
               JOIN sentences s ON s.id = ei.sentence_id
               WHERE ei.expression_id = ?
               ORDER BY s.id""",
            (expression_id,),
        ).fetchall()

    def get_pending_instance_sentences(self, expression_ids: list[int]) -> list[sqlite3.Row]:
        """Return distinct sentences linked to given expressions that lack en_text."""
        if not expression_ids:
            return []
        placeholders = ",".join("?" * len(expression_ids))
        return self._conn.execute(
            f"""SELECT DISTINCT s.* FROM expression_instances ei
                JOIN sentences s ON s.id = ei.sentence_id
                WHERE ei.expression_id IN ({placeholders})
                  AND (s.en_text IS NULL OR s.en_text = '')
                ORDER BY s.id""",
            expression_ids,
        ).fetchall()

    def get_expressions_filtered(
        self,
        lang: Optional[str] = None,
        statuses: Optional[list[str]] = None,
        min_freq: int = 0,
    ) -> list[sqlite3.Row]:
        clauses, params = ["freq >= ?"], [min_freq]
        if lang:
            clauses.append("lang = ?")
            params.append(lang)
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        where = " AND ".join(clauses)
        return self._conn.execute(
            f"SELECT * FROM expressions WHERE {where} ORDER BY freq DESC, id",
            params,
        ).fetchall()

    def merge_expressions(self, canonical_id: int, duplicate_ids: list[int]) -> int:
        """Merge duplicate expression rows into the canonical row.

        - Re-links all instances of duplicates to the canonical row (ignoring
          UNIQUE conflicts so each sentence appears at most once).
        - Recomputes canonical freq from distinct instance count.
        - Deletes duplicate rows.
        Returns number of duplicates merged.
        """
        if not duplicate_ids:
            return 0
        dup_ids = [d for d in duplicate_ids if d != canonical_id]
        if not dup_ids:
            return 0
        placeholders = ",".join("?" * len(dup_ids))

        # 1. Reassign instance links (best-effort; UNIQUE may block dups)
        self._conn.execute(
            f"""UPDATE OR IGNORE expression_instances
                SET expression_id = ?
                WHERE expression_id IN ({placeholders})""",
            [canonical_id, *dup_ids],
        )
        # 2. Drop any leftover dup instances that conflicted
        self._conn.execute(
            f"DELETE FROM expression_instances WHERE expression_id IN ({placeholders})",
            dup_ids,
        )
        # 3. Recompute freq
        self._conn.execute(
            """UPDATE expressions SET freq = (
                   SELECT COUNT(*) FROM expression_instances WHERE expression_id = ?
               ), updated_at = ?
               WHERE id = ?""",
            (canonical_id, time.time(), canonical_id),
        )
        # 4. Remove duplicates
        cur = self._conn.execute(
            f"DELETE FROM expressions WHERE id IN ({placeholders})",
            dup_ids,
        )
        self._conn.commit()
        return cur.rowcount

    def update_expression_text(self, expr_id: int, new_text: str) -> None:
        """Update kr_expr (source text) and recompute kr_hash. Used by review import."""
        row = self._conn.execute(
            "SELECT lang FROM expressions WHERE id=?", (expr_id,)
        ).fetchone()
        if not row:
            return
        new_hash = _expr_hash(new_text, row["lang"])
        self._conn.execute(
            "UPDATE expressions SET kr_expr=?, kr_hash=?, updated_at=? WHERE id=?",
            (new_text, new_hash, time.time(), expr_id),
        )
        self._conn.commit()

    def delete_expression(self, expr_id: int) -> None:
        self._conn.execute("DELETE FROM expressions WHERE id=?", (expr_id,))
        self._conn.commit()

    # ── embeddings ───────────────────────────────────────────────────────────

    def set_sentence_embedding(self, sentence_id: int, blob: bytes) -> None:
        self._conn.execute(
            "UPDATE sentences SET embedding=?, updated_at=? WHERE id=?",
            (blob, time.time(), sentence_id),
        )
        self._conn.commit()

    def set_sentence_embeddings_batch(self, items: list[tuple[int, bytes]]) -> None:
        now = time.time()
        self._conn.executemany(
            "UPDATE sentences SET embedding=?, updated_at=? WHERE id=?",
            [(blob, now, sid) for sid, blob in items],
        )
        self._conn.commit()

    def get_sentences_without_embedding(
        self, lang: Optional[str] = None, limit: Optional[int] = None,
    ) -> list[sqlite3.Row]:
        clauses = ["embedding IS NULL"]
        params: list = []
        if lang:
            clauses.append("lang = ?")
            params.append(lang)
        sql = f"SELECT * FROM sentences WHERE {' AND '.join(clauses)} ORDER BY id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self._conn.execute(sql, params).fetchall()

    def get_sentences_with_embedding(
        self,
        lang: Optional[str] = None,
        statuses: Optional[list[str]] = None,
    ) -> list[sqlite3.Row]:
        clauses = ["embedding IS NOT NULL"]
        params: list = []
        if lang:
            clauses.append("lang = ?")
            params.append(lang)
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        return self._conn.execute(
            f"SELECT * FROM sentences WHERE {' AND '.join(clauses)} ORDER BY id",
            params,
        ).fetchall()

    # ── expression centroids ────────────────────────────────────────────────

    def get_expressions_with_centroid(self, lang: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT id, kr_expr, en_expr, lang, status, freq, member_count, centroid, label_dirty "
            "FROM expressions WHERE lang=? AND centroid IS NOT NULL",
            (lang,),
        ).fetchall()

    def create_expression_with_centroid(
        self,
        seed_text: str,
        lang: str,
        centroid_blob: bytes,
    ) -> int:
        """Create a new expression seeded with the given centroid.

        kr_expr starts as seed_text; label step will rename it later. label_dirty=1.
        """
        now = time.time()
        kr_hash = _expr_hash(f"__seed_{now}_{seed_text[:50]}", lang)  # avoid collision pre-label
        en_seed = seed_text if lang == LANG_EN else None
        cur = self._conn.execute(
            """INSERT INTO expressions(kr_hash, kr_expr, en_expr, freq, member_count,
                                       lang, status, centroid, label_dirty,
                                       created_at, updated_at)
               VALUES(?, ?, ?, 0, 0, ?, 'pending', ?, 1, ?, ?)""",
            (kr_hash, seed_text, en_seed, lang, centroid_blob, now, now),
        )
        self._conn.commit()
        return cur.lastrowid

    def update_expression_centroid(
        self,
        expr_id: int,
        new_centroid_blob: bytes,
        new_member_count: int,
    ) -> None:
        self._conn.execute(
            """UPDATE expressions
               SET centroid=?, member_count=?, label_dirty=1, updated_at=?
               WHERE id=?""",
            (new_centroid_blob, new_member_count, time.time(), expr_id),
        )
        self._conn.commit()

    def get_dirty_expressions(
        self, lang: str, statuses: Optional[list[str]] = None,
    ) -> list[sqlite3.Row]:
        clauses = ["lang=?", "label_dirty=1"]
        params: list = [lang]
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        return self._conn.execute(
            f"SELECT * FROM expressions WHERE {' AND '.join(clauses)} ORDER BY freq DESC, id",
            params,
        ).fetchall()

    def set_expression_gloss(self, expr_id: int, gloss: str) -> None:
        self._conn.execute(
            "UPDATE expressions SET gloss=?, updated_at=? WHERE id=?",
            (gloss, time.time(), expr_id),
        )
        self._conn.commit()

    def set_expression_label(self, expr_id: int, new_text: str) -> None:
        """Set canonical text after LLM labeling. Recompute kr_hash, clear dirty."""
        row = self._conn.execute(
            "SELECT lang FROM expressions WHERE id=?", (expr_id,)
        ).fetchone()
        if not row:
            return
        new_hash = _expr_hash(new_text, row["lang"])
        en_seed_clause = ""
        params: list = [new_text, new_hash]
        if row["lang"] == LANG_EN:
            en_seed_clause = ", en_expr=?"
            params.append(new_text)
        params.extend([time.time(), expr_id])
        self._conn.execute(
            f"""UPDATE expressions
                SET kr_expr=?, kr_hash=?{en_seed_clause},
                    label_dirty=0, updated_at=?
                WHERE id=?""",
            params,
        )
        self._conn.commit()

    # ── checkpoint ────────────────────────────────────────────────────────────

    def save_checkpoint(self, last_file: str, sentence_index: int) -> None:
        data = {"last_file": last_file, "sentence_index": sentence_index, "ts": time.time()}
        self._checkpoint_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def load_checkpoint(self) -> Optional[dict]:
        if not self._checkpoint_path.exists():
            return None
        return json.loads(self._checkpoint_path.read_text(encoding="utf-8"))

    def clear_checkpoint(self) -> None:
        if self._checkpoint_path.exists():
            self._checkpoint_path.unlink()

    @staticmethod
    def hash_text(text: str) -> str:
        return _sha256(text)
