"""Generate English example sentences that exemplify a sentence's tagged moves.

Each tagged sentence (a discourse pattern lifted from kr-self / en-ref) yields K
English sentences that (1) carry its meaning and (2) demonstrate every tagged
taxonomy leaf. These become the fixed card material pushed to the GS staging tab
for operator review.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_K = 3


def build_examples_prompt(
    tags: dict[str, str], reference: str, meaning: str, k: int
) -> str:
    """Build the deterministic prompt sent to the example generator."""

    payload = {
        "instruction": (
            f"Write {k} natural, idiomatic English sentences. Each must (1) convey "
            "the given meaning and (2) demonstrate ALL listed discourse moves. Vary "
            "the surface wording across the sentences. Return JSON only."
        ),
        "meaning": meaning,
        "reference_sentence": reference,
        "discourse_moves": dict(sorted(tags.items())),
        "count": k,
        "response_shape": {"examples": ["<english sentence>"]},
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _call_examples(client, prompt: str, model: str) -> dict[str, Any]:
    if hasattr(client, "examples"):
        return dict(client.examples(prompt))
    if hasattr(client, "chat"):
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a precise English example writer."},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        return json.loads(response.choices[0].message.content)
    raise ValueError("client must provide examples(prompt) or chat.completions.create(...)")


def generate_examples(
    db: sqlite3.Connection,
    sentence_id: str,
    client,
    k: int = DEFAULT_K,
    model: str = DEFAULT_MODEL,
) -> list[str]:
    """Generate (once) and store K English examples for a tagged sentence.

    Idempotent: returns the stored examples if already present. Untagged sentences
    are skipped (return []), because an example with no demonstrated move is noise.
    """

    row = db.execute(
        """
        SELECT tags_json, en_translation, examples_json, kr_text, en_text
        FROM sentences WHERE sentence_id=?
        """,
        (sentence_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown sentence_id: {sentence_id}")

    stored = json.loads(row["examples_json"] or "[]")
    if stored:
        return [str(item) for item in stored]

    tags = json.loads(row["tags_json"] or "{}")
    if not tags:
        return []

    reference = row["en_text"] or row["kr_text"] or ""
    meaning = row["en_translation"] or reference
    prompt = build_examples_prompt(tags, reference, meaning, k)
    response = _call_examples(client, prompt, model)
    examples = [str(item) for item in (response.get("examples") or [])][:k]

    db.execute(
        "UPDATE sentences SET examples_json=? WHERE sentence_id=?",
        (json.dumps(examples, ensure_ascii=False), sentence_id),
    )
    db.commit()
    return examples
