"""Push generated examples to a Google Sheets staging tab for operator review.

Raw generator output lands in the `staging` tab; the operator eyeballs it, checks
`approved`, and only approved rows move to the live tab that feeds Obsidian_to_Anki.
Sheet name, tab, and service-account path are parameterized (env / CLI) — no
hardcoded spreadsheet identity.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from identity_corpus.store import utc_now

STAGING_COLUMNS = [
    "sentence_id",
    "example_idx",
    "en_example",
    "tags",
    "en_meaning",
    "approved",
]


def _tag_str(tags_json: str | None) -> str:
    tags = json.loads(tags_json or "{}")
    return "; ".join(f"{axis}:{leaf}" for axis, leaf in sorted(tags.items()))


def collect_staging_rows(db: sqlite3.Connection, only_unstaged: bool = True) -> list[dict]:
    """Flatten sentences with examples into one row per (sentence, example)."""

    where = "WHERE examples_json NOT IN ('[]', '')"
    if only_unstaged:
        where += " AND staged_ts IS NULL"
    rows = db.execute(
        f"""
        SELECT sentence_id, tags_json, en_translation, examples_json
        FROM sentences {where} ORDER BY sentence_id
        """
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        examples = json.loads(row["examples_json"] or "[]")
        tag_str = _tag_str(row["tags_json"])
        for idx, example in enumerate(examples):
            out.append(
                {
                    "sentence_id": row["sentence_id"],
                    "example_idx": idx,
                    "en_example": example,
                    "tags": tag_str,
                    "en_meaning": row["en_translation"] or "",
                    "approved": "",
                }
            )
    return out


def _mark_staged(db: sqlite3.Connection, sentence_ids: set[str]) -> None:
    ts = utc_now()
    db.executemany(
        "UPDATE sentences SET staged_ts=? WHERE sentence_id=?",
        [(ts, sid) for sid in sentence_ids],
    )
    db.commit()


def write_staging_csv(db: sqlite3.Connection, out_path: Path, only_unstaged: bool = True) -> int:
    """Write staging rows to a CSV artifact. Returns row count. Does not mark staged."""

    rows = collect_staging_rows(db, only_unstaged=only_unstaged)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=STAGING_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def push_to_sheet(
    db: sqlite3.Connection,
    *,
    sheet_name: str,
    service_account_file: str,
    worksheet: str = "staging",
    only_unstaged: bool = True,
) -> int:
    """Append unstaged example rows to the GS staging tab. Returns rows appended.

    Marks pushed sentences with staged_ts so a re-run never double-appends.
    """

    import gspread

    rows = collect_staging_rows(db, only_unstaged=only_unstaged)
    if not rows:
        return 0

    gc = gspread.service_account(filename=service_account_file)
    sh = gc.open(sheet_name)
    try:
        ws = sh.worksheet(worksheet)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=worksheet, rows=len(rows) + 1, cols=len(STAGING_COLUMNS))
        ws.update([STAGING_COLUMNS], "A1")
    if not ws.get_all_values():
        ws.update([STAGING_COLUMNS], "A1")

    payload = [[str(row[col]) for col in STAGING_COLUMNS] for row in rows]
    ws.append_rows(payload, value_input_option="RAW")

    _mark_staged(db, {row["sentence_id"] for row in rows})
    return len(rows)
