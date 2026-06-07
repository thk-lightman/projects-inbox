from __future__ import annotations

import csv
import json

from identity_corpus.sheets import (
    STAGING_COLUMNS,
    collect_staging_rows,
    write_staging_csv,
)
from identity_corpus.store import init_db, upsert_sentence


def _sentence_with_examples(db, examples: list[str]) -> str:
    upsert_sentence(
        db,
        sentence_id="s1",
        source_path="a.md",
        origin_lang="kr",
        text="충분히 긴 한국어 문장입니다 시트 출력용.",
        file_fingerprint="fp",
    )
    db.execute(
        "UPDATE sentences SET tags_json=?, en_translation=?, examples_json=? WHERE sentence_id=?",
        (
            json.dumps({"I_structural_frames": "Direct"}),
            "A meaning.",
            json.dumps(examples),
            "s1",
        ),
    )
    db.commit()
    return "s1"


def test_collect_one_row_per_example() -> None:
    db = init_db(":memory:")
    _sentence_with_examples(db, ["one", "two"])

    rows = collect_staging_rows(db)

    assert [r["en_example"] for r in rows] == ["one", "two"]
    assert rows[0]["tags"] == "I_structural_frames:Direct"
    assert rows[0]["approved"] == ""


def test_sentences_with_no_examples_are_excluded() -> None:
    db = init_db(":memory:")
    _sentence_with_examples(db, [])

    assert collect_staging_rows(db) == []


def test_write_staging_csv_has_header_and_rows(tmp_path) -> None:
    db = init_db(":memory:")
    _sentence_with_examples(db, ["one", "two"])
    out = tmp_path / "staging.csv"

    count = write_staging_csv(db, out)

    with out.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == STAGING_COLUMNS
        assert len(list(reader)) == 2
    assert count == 2
