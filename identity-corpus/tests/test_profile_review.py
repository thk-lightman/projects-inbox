from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from identity_corpus.profile import export_review_tsv, generate_profile, import_review_tsv
from identity_corpus.store import init_db, update_sentence_status, upsert_sentence


def _seed(db):
    for sid, status in (("locked", "locked"), ("draft", "draft"), ("archived", "archived")):
        upsert_sentence(
            db,
            sentence_id=sid,
            source_path=f"{sid}.md",
            origin_lang="en",
            text=f"This is the {sid} sentence that is long enough.",
            file_fingerprint="fp",
        )
        db.execute(
            "UPDATE sentences SET tags_json=? WHERE sentence_id=?",
            (
                json.dumps(
                    {
                        "I_structural_frames": "Direct",
                        "V_epistemic_register": "Measured",
                    }
                ),
                sid,
            ),
        )
        if status != "draft":
            update_sentence_status(db, sid, status)
    db.commit()


def test_profile_includes_only_locked_sentences(tmp_path: Path) -> None:
    db = init_db(":memory:")
    _seed(db)
    out = tmp_path / "voice_profile.md"

    generate_profile(db, out)
    text = out.read_text(encoding="utf-8")

    assert "locked sentence" in text
    assert "draft sentence" not in text
    assert "archived sentence" not in text


def test_review_tsv_round_trip_and_unknown_action_rejected(tmp_path: Path) -> None:
    db = init_db(":memory:")
    _seed(db)
    out = tmp_path / "review.tsv"

    export_review_tsv(db, out)
    rows = list(csv.DictReader(out.open(encoding="utf-8"), delimiter="\t"))
    assert {row["sentence_id"] for row in rows} == {"locked", "draft", "archived"}
    rows[1]["suggested_action"] = "lock"
    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=rows[0].keys(), delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    import_review_tsv(db, out)
    assert db.execute("SELECT status FROM sentences WHERE sentence_id='draft'").fetchone()[0] == "locked"

    rows[0]["suggested_action"] = "explode"
    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=rows[0].keys(), delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(ValueError, match="unknown review action"):
        import_review_tsv(db, out)
