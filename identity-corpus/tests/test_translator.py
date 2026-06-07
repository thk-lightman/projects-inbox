from __future__ import annotations

from identity_corpus.store import init_db, upsert_sentence
from identity_corpus.translator import translate_sentence


class TranslateStub:
    def __init__(self) -> None:
        self.calls = 0

    def translate(self, text: str) -> str:
        self.calls += 1
        return f"translated: {text}"


def _sentence(db, sid: str, lang: str, text: str) -> None:
    upsert_sentence(
        db,
        sentence_id=sid,
        source_path=f"{sid}.md",
        origin_lang=lang,
        text=text,
        file_fingerprint="fp",
    )


def test_kr_sentence_translates_once_and_is_idempotent() -> None:
    db = init_db(":memory:")
    _sentence(db, "kr1", "kr", "충분히 긴 한국어 문장입니다.")
    client = TranslateStub()

    assert translate_sentence(db, "kr1", client).startswith("translated:")
    assert translate_sentence(db, "kr1", client).startswith("translated:")
    assert client.calls == 1


def test_en_sentence_never_enters_translator_path() -> None:
    db = init_db(":memory:")
    _sentence(db, "en1", "en", "This English sentence already needs no translation.")
    client = TranslateStub()

    assert translate_sentence(db, "en1", client) is None
    assert client.calls == 0
