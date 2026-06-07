"""Per-sentence KR→EN translation (direct LLM call, no external corpus dep)."""

from __future__ import annotations

import sqlite3

DEFAULT_MODEL = "gpt-4o-mini"


def _translate(client, text: str, model: str = DEFAULT_MODEL) -> str:
    if hasattr(client, "translate"):
        result = client.translate(text)
        return result if isinstance(result, str) else getattr(result, "text", str(result))
    if hasattr(client, "chat"):
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You translate Korean into natural, idiomatic English. "
                        "Return only the English translation, no preamble."
                    ),
                },
                {"role": "user", "content": text},
            ],
        )
        return response.choices[0].message.content.strip()
    raise ValueError("client must provide translate(text) or chat.completions.create(...)")


def translate_sentence(
    db: sqlite3.Connection, sentence_id: str, client, model: str = DEFAULT_MODEL
) -> str | None:
    """Translate a KR sentence once and store en_translation. EN sentences skip."""

    row = db.execute(
        "SELECT origin_lang, kr_text, en_translation FROM sentences WHERE sentence_id=?",
        (sentence_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown sentence_id: {sentence_id}")
    if row["en_translation"]:
        return str(row["en_translation"])
    if row["origin_lang"] == "en":
        return None

    translated = _translate(client, row["kr_text"] or "", model)
    db.execute(
        "UPDATE sentences SET en_translation=? WHERE sentence_id=?", (translated, sentence_id)
    )
    db.commit()
    return translated
