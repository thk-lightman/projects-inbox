from __future__ import annotations

import json
from pathlib import Path

import yaml

from identity_corpus.store import init_db, upsert_sentence
from identity_corpus.tagger import (
    apply_tags,
    build_tagger_prompt,
    load_taxonomy,
    promote_suggested_tag,
    tag_sentence,
)


class TagStub:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def tag(self, prompt: str) -> dict:
        self.prompts.append(prompt)
        return {
            "I_structural_frames": "Direct",
            "suggested_new_tags": [["V_epistemic_register", "Register", "Crisp"]],
        }


def _seeded_db():
    db = init_db(":memory:")
    upsert_sentence(
        db,
        sentence_id="s1",
        source_path="a.md",
        origin_lang="en",
        text="This is a sufficiently long English sentence for tagging.",
        file_fingerprint="fp",
    )
    return db, "s1"


def test_tagger_prompt_is_deterministic() -> None:
    taxonomy = {"axes": {"I_structural_frames": {"groups": {"Opening": ["Direct"]}}}}

    first = build_tagger_prompt(["same"], taxonomy)
    second = build_tagger_prompt(["same"], taxonomy)

    assert first == second
    assert json.loads(first)["sentences"] == ["same"]


def test_apply_tags_stores_known_and_suggested_tags() -> None:
    db, sid = _seeded_db()
    response = {
        "I_structural_frames": "Direct",
        "suggested_new_tags": [["V_epistemic_register", "Register", "Crisp"]],
    }

    apply_tags(db, sid, response)

    row = db.execute("SELECT tags_json FROM sentences WHERE sentence_id=?", (sid,)).fetchone()
    assert json.loads(row["tags_json"]) == {"I_structural_frames": "Direct"}
    assert db.execute("SELECT count FROM suggested_tags").fetchone()["count"] == 1


def test_tag_sentence_uses_one_client_call_and_promote_rewrites_taxonomy(tmp_path: Path) -> None:
    db, sid = _seeded_db()
    taxonomy_path = tmp_path / "taxonomy.yaml"
    taxonomy_path.write_text(
        yaml.safe_dump(
            {"version": 1, "axes": {"V_epistemic_register": {"groups": {"Register": []}}}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    client = TagStub()

    response = tag_sentence(db, sid, client, load_taxonomy(taxonomy_path))
    apply_tags(db, sid, response)
    promote_suggested_tag(
        db, "V_epistemic_register", "Crisp", taxonomy_path=taxonomy_path, group="Register"
    )

    assert len(client.prompts) == 1
    assert "Crisp" in load_taxonomy(taxonomy_path)["axes"]["V_epistemic_register"]["groups"]["Register"]
    assert db.execute("SELECT COUNT(*) AS n FROM suggested_tags").fetchone()["n"] == 0
