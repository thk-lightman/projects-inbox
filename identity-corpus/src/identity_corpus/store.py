"""SQLite storage for the identity sentence bank.

Sentence-centric: each sentence carries its own tags, translation, and examples.
sentence_id is content-derived, so verbatim duplicates collapse on upsert.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

VALID_STATUSES = {"draft", "locked", "archived"}


def utc_now() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""

    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a SQLite connection with row access by column name."""

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: Path | str) -> sqlite3.Connection:
    """Create the sentence bank schema and return an open connection."""

    db_path = Path(path)
    if str(path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sentences (
            sentence_id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL,
            origin_lang TEXT NOT NULL CHECK(origin_lang IN ('kr', 'en')),
            kr_text TEXT,
            en_text TEXT,
            tags_json TEXT NOT NULL DEFAULT '{}',
            en_translation TEXT,
            examples_json TEXT NOT NULL DEFAULT '[]',
            staged_ts TEXT,
            status TEXT NOT NULL DEFAULT 'draft'
                CHECK(status IN ('draft', 'locked', 'archived')),
            build_ts TEXT NOT NULL,
            file_fingerprint TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS suggested_tags (
            dimension TEXT NOT NULL,
            tag TEXT NOT NULL,
            first_seen_sentence_id TEXT NOT NULL,
            first_seen_ts TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(dimension, tag)
        );

        CREATE TABLE IF NOT EXISTS taxonomy_log (
            ts TEXT NOT NULL,
            action TEXT NOT NULL,
            dimension TEXT NOT NULL,
            tag TEXT NOT NULL,
            note TEXT
        );
        """
    )
    conn.commit()
    return conn


def upsert_sentence(
    db: sqlite3.Connection,
    *,
    sentence_id: str,
    source_path: str,
    origin_lang: str,
    text: str,
    file_fingerprint: str,
) -> None:
    """Insert or update a sentence without changing its review status or tags."""

    kr_text = text if origin_lang == "kr" else None
    en_text = text if origin_lang == "en" else None
    db.execute(
        """
        INSERT INTO sentences (
            sentence_id, source_path, origin_lang, kr_text, en_text,
            status, build_ts, file_fingerprint
        )
        VALUES (?, ?, ?, ?, ?, 'draft', ?, ?)
        ON CONFLICT(sentence_id) DO UPDATE SET
            source_path=excluded.source_path,
            origin_lang=excluded.origin_lang,
            kr_text=excluded.kr_text,
            en_text=excluded.en_text,
            build_ts=excluded.build_ts,
            file_fingerprint=excluded.file_fingerprint
        """,
        (
            sentence_id,
            source_path,
            origin_lang,
            kr_text,
            en_text,
            utc_now(),
            file_fingerprint,
        ),
    )
    db.commit()


def update_sentence_status(
    db: sqlite3.Connection, sentence_id: str, status: str, note: str = "review import"
) -> None:
    """Apply an operator-driven review status transition."""

    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status}")
    cur = db.execute("UPDATE sentences SET status=? WHERE sentence_id=?", (status, sentence_id))
    if cur.rowcount == 0:
        raise ValueError(f"unknown sentence_id: {sentence_id}")
    db.execute(
        "INSERT INTO taxonomy_log(ts, action, dimension, tag, note) VALUES (?, ?, ?, ?, ?)",
        (utc_now(), f"status:{status}", "sentence", sentence_id, note),
    )
    db.commit()


def sentence_tags(row: sqlite3.Row) -> dict:
    """Decode a sentence row's tags_json mapping."""

    return json.loads(row["tags_json"] or "{}")
