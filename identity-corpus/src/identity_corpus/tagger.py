"""Hybrid pragmatic taxonomy loading and per-sentence tagging."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import yaml

from identity_corpus.store import utc_now


def load_taxonomy(path: Path) -> dict[str, Any]:
    """Load taxonomy.yaml as the source of truth for known tags."""

    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    axes = data.get("axes")
    if not isinstance(axes, dict):
        raise ValueError("taxonomy.yaml must contain an axes mapping")
    return data


def taxonomy_leaf_map(taxonomy: dict[str, Any]) -> dict[str, set[str]]:
    """Return valid leaf tags by axis id."""

    result: dict[str, set[str]] = {}
    for axis, body in taxonomy.get("axes", {}).items():
        leaves: set[str] = set()
        for group_leaves in body.get("groups", {}).values():
            leaves.update(str(leaf) for leaf in group_leaves)
        result[axis] = leaves
    return result


def build_tagger_prompt(
    sentences: list[str], taxonomy: dict[str, Any], model: str = "gpt-4o-mini"
) -> str:
    """Build the deterministic prompt sent to the cluster labeler."""

    payload = {
        "instruction": (
            "Assign at most one known leaf per taxonomy axis, or null. "
            "Return JSON only with axis ids and suggested_new_tags."
        ),
        "model_contract": model,
        "sentences": sentences,
        "taxonomy_axes": taxonomy.get("axes", {}),
        "response_shape": {
            "<axis_id>": "<leaf or null>",
            "suggested_new_tags": [["<axis_id>", "<group>", "<leaf>"]],
        },
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _call_chat(client, prompt: str, model: str) -> dict[str, Any]:
    if hasattr(client, "tag"):
        return dict(client.tag(prompt))
    if hasattr(client, "chat"):
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a deterministic taxonomy labeler."},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        return json.loads(response.choices[0].message.content)
    raise ValueError("client must provide tag(prompt) or chat.completions.create(...)")


def tag_sentence(
    db: sqlite3.Connection,
    sentence_id: str,
    client,
    taxonomy: dict[str, Any],
    model: str = "gpt-4o-mini",
) -> dict[str, Any]:
    """Run one chat-completion call for one sentence and return parsed tags."""

    row = db.execute(
        "SELECT kr_text, en_text FROM sentences WHERE sentence_id=?", (sentence_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown sentence_id: {sentence_id}")
    text = row["en_text"] or row["kr_text"] or ""
    prompt = build_tagger_prompt([text], taxonomy, model=model)
    return _call_chat(client, prompt, model)


def apply_tags(db: sqlite3.Connection, sentence_id: str, llm_response: dict[str, Any]) -> None:
    """Persist known axis tags and suggested new tags for a sentence."""

    known_tags = {
        key: value
        for key, value in llm_response.items()
        if key != "suggested_new_tags" and value is not None
    }
    db.execute(
        "UPDATE sentences SET tags_json=? WHERE sentence_id=?",
        (json.dumps(known_tags, ensure_ascii=False, sort_keys=True), sentence_id),
    )
    for suggestion in llm_response.get("suggested_new_tags", []) or []:
        if isinstance(suggestion, dict):
            dimension = str(suggestion.get("axis_id") or suggestion.get("dimension") or "")
            tag = str(suggestion.get("leaf") or suggestion.get("tag") or "")
        else:
            dimension = str(suggestion[0]) if len(suggestion) >= 1 else ""
            tag = str(suggestion[2] if len(suggestion) >= 3 else suggestion[-1])
        if not dimension or not tag:
            continue
        db.execute(
            """
            INSERT INTO suggested_tags(dimension, tag, first_seen_sentence_id, first_seen_ts, count)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(dimension, tag) DO UPDATE SET count=count+1
            """,
            (dimension, tag, sentence_id, utc_now()),
        )
    db.commit()


def promote_suggested_tag(
    db: sqlite3.Connection,
    dimension: str,
    tag: str,
    taxonomy_path: Path = Path("taxonomy.yaml"),
    group: str = "Promoted",
) -> None:
    """Promote a pending suggestion into taxonomy.yaml and log the decision."""

    data = load_taxonomy(taxonomy_path)
    axes = data["axes"]
    if dimension not in axes:
        raise ValueError(f"unknown taxonomy dimension: {dimension}")
    groups = axes[dimension].setdefault("groups", {})
    leaves = groups.setdefault(group, [])
    if tag not in leaves:
        leaves.append(tag)
    with taxonomy_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
    db.execute("DELETE FROM suggested_tags WHERE dimension=? AND tag=?", (dimension, tag))
    db.execute(
        "INSERT INTO taxonomy_log(ts, action, dimension, tag, note) VALUES (?, 'promote', ?, ?, ?)",
        (utc_now(), dimension, tag, f"group={group}"),
    )
    db.commit()


def reject_suggested_tag(db: sqlite3.Connection, dimension: str, tag: str) -> None:
    """Reject a pending suggestion and log the operator decision."""

    db.execute("DELETE FROM suggested_tags WHERE dimension=? AND tag=?", (dimension, tag))
    db.execute(
        "INSERT INTO taxonomy_log(ts, action, dimension, tag, note) VALUES (?, 'reject', ?, ?, NULL)",
        (utc_now(), dimension, tag),
    )
    db.commit()
