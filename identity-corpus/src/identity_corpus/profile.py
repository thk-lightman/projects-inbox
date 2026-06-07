"""Voice profile generation and review TSV import/export."""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from identity_corpus.store import update_sentence_status

PROFILE_SECTIONS = [
    "Voice Summary",
    "Signature Phrases",
    "Structural Frames in My Voice",
    "Narrative Tools I Use",
    "Interview Moves",
    "Calibration Inventory",
]


def _locked_rows(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT sentence_id, origin_lang, status, kr_text,
               COALESCE(en_text, en_translation) AS en_text, tags_json
        FROM sentences
        WHERE status='locked'
        ORDER BY sentence_id
        """
    ).fetchall()


def _tags(row: sqlite3.Row) -> dict[str, str]:
    return json.loads(row["tags_json"] or "{}")


def generate_profile(db: sqlite3.Connection, out_path: Path) -> None:
    """Generate voice_profile.md from locked sentences only."""

    rows = _locked_rows(db)
    lines = ["# Voice Profile", ""]
    for section in PROFILE_SECTIONS:
        lines.extend([f"## {section}", ""])
        if section == "Voice Summary":
            v_tags = Counter(
                leaf
                for row in rows
                for axis, leaf in _tags(row).items()
                if axis == "V_epistemic_register"
            )
            top = ", ".join(tag for tag, _ in v_tags.most_common(3)) or "No locked calibration samples yet."
            lines.extend([f"Default tone signals currently visible: {top}.", ""])
        elif section == "Signature Phrases":
            for row in rows[:10]:
                tags = ", ".join(f"{axis}:{leaf}" for axis, leaf in _tags(row).items())
                text = row["en_text"] or row["kr_text"] or ""
                lines.append(f"- {text} ({tags})")
            lines.append("")
        else:
            axis_prefix = {
                "Structural Frames in My Voice": "I_structural_frames",
                "Narrative Tools I Use": "II_narrative_tools",
                "Interview Moves": "IV_interview_moves",
                "Calibration Inventory": "V_epistemic_register",
            }[section]
            grouped: dict[str, list[str]] = defaultdict(list)
            for row in rows:
                leaf = _tags(row).get(axis_prefix)
                if leaf:
                    grouped[leaf].append(row["en_text"] or row["kr_text"] or "")
            if not grouped:
                lines.append("- No locked samples yet.")
            for leaf, samples in sorted(grouped.items()):
                lines.append(f"- {leaf}: {samples[0]}")
            lines.append("")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def export_review_tsv(db: sqlite3.Connection, out_path: Path) -> None:
    """Export sentence review rows to a TSV file for operator editing."""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = db.execute(
        """
        SELECT sentence_id, origin_lang, status, kr_text,
               COALESCE(en_text, en_translation) AS en_text, tags_json
        FROM sentences
        ORDER BY sentence_id
        """
    ).fetchall()
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "sentence_id",
                "origin_lang",
                "status",
                "kr_text",
                "en_text",
                "tags",
                "suggested_action",
            ],
            delimiter="\t",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "sentence_id": row["sentence_id"],
                    "origin_lang": row["origin_lang"],
                    "status": row["status"],
                    "kr_text": row["kr_text"] or "",
                    "en_text": row["en_text"] or "",
                    "tags": row["tags_json"] or "{}",
                    "suggested_action": "",
                }
            )


def import_review_tsv(db: sqlite3.Connection, in_path: Path) -> None:
    """Import operator review actions from TSV and apply status transitions."""

    action_to_status = {"lock": "locked", "archive": "archived", "skip": None, "": None}
    with in_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        for row in reader:
            action = (row.get("suggested_action") or "").strip().lower()
            if action not in action_to_status:
                raise ValueError(f"unknown review action: {action}")
            status = action_to_status[action]
            if status:
                update_sentence_status(db, row["sentence_id"], status)
