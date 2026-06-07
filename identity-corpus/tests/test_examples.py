from __future__ import annotations

import json

from identity_corpus.examples import build_examples_prompt, generate_examples
from identity_corpus.store import init_db, upsert_sentence


class ExampleStub:
    def __init__(self) -> None:
        self.calls = 0

    def examples(self, prompt: str) -> dict:
        self.calls += 1
        return {"examples": ["First demo.", "Second demo.", "Third demo.", "Extra."]}


def _tagged_sentence(db, tags: dict[str, str]) -> str:
    upsert_sentence(
        db,
        sentence_id="s1",
        source_path="a.md",
        origin_lang="kr",
        text="충분히 긴 한국어 문장입니다 예제 생성용.",
        file_fingerprint="fp",
    )
    db.execute(
        "UPDATE sentences SET tags_json=?, en_translation=? WHERE sentence_id=?",
        (json.dumps(tags), "A sufficiently long English meaning.", "s1"),
    )
    db.commit()
    return "s1"


def test_examples_prompt_is_deterministic_and_caps_count() -> None:
    first = build_examples_prompt({"I_structural_frames": "Direct"}, "ref", "meaning", 3)
    second = build_examples_prompt({"I_structural_frames": "Direct"}, "ref", "meaning", 3)
    assert first == second
    assert json.loads(first)["count"] == 3


def test_tagged_sentence_generates_k_examples_once_idempotent() -> None:
    db = init_db(":memory:")
    sid = _tagged_sentence(db, {"I_structural_frames": "Direct"})
    client = ExampleStub()

    first = generate_examples(db, sid, client, k=3)
    second = generate_examples(db, sid, client, k=3)

    assert first == ["First demo.", "Second demo.", "Third demo."]  # capped at k
    assert second == first  # cached, no second call
    assert client.calls == 1


def test_untagged_sentence_is_skipped() -> None:
    db = init_db(":memory:")
    sid = _tagged_sentence(db, {})
    client = ExampleStub()

    assert generate_examples(db, sid, client) == []
    assert client.calls == 0
