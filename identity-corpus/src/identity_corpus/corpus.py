"""Canonical corpus dataclasses and serialization helpers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class TaggedNote:
    """A note with assigned taxonomy leaves by axis."""

    note_id: str
    title: str
    source_path: str
    tags: dict[str, str | None]
    text: str


@dataclass(frozen=True)
class Corpus:
    """Serializable identity corpus with metadata and tagged notes."""

    schema_version: str
    version: str
    created_at: str
    note_count: int
    axis_coverage: dict[str, int]
    notes: list[TaggedNote] = field(default_factory=list)


def assemble_corpus(notes: list[TaggedNote], version: str = "0.1.0") -> Corpus:
    """Assemble tagged notes into a Corpus with coverage statistics."""

    coverage: dict[str, int] = {}
    for note in notes:
        for axis, leaf in note.tags.items():
            if leaf:
                coverage[axis] = coverage.get(axis, 0) + 1
    return Corpus(
        schema_version=SCHEMA_VERSION,
        version=version,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        note_count=len(notes),
        axis_coverage=dict(sorted(coverage.items())),
        notes=notes,
    )


def corpus_to_dict(corpus: Corpus) -> dict[str, Any]:
    """Convert a Corpus to plain JSON/YAML-compatible data."""

    return asdict(corpus)


def corpus_from_dict(data: dict[str, Any]) -> Corpus:
    """Load a Corpus from plain decoded data."""

    notes = [TaggedNote(**note) for note in data.get("notes", [])]
    return Corpus(
        schema_version=data["schema_version"],
        version=data["version"],
        created_at=data["created_at"],
        note_count=int(data["note_count"]),
        axis_coverage=dict(data.get("axis_coverage", {})),
        notes=notes,
    )


def write_corpus_json(corpus: Corpus, path: Path) -> None:
    """Write canonical JSON for a Corpus."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(corpus_to_dict(corpus), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def read_corpus_json(path: Path) -> Corpus:
    """Read canonical JSON into a Corpus."""

    return corpus_from_dict(json.loads(path.read_text(encoding="utf-8")))


def write_corpus_yaml(corpus: Corpus, path: Path) -> None:
    """Write optional YAML for a Corpus."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(corpus_to_dict(corpus), allow_unicode=True, sort_keys=True),
        encoding="utf-8",
    )


def read_corpus_yaml(path: Path) -> Corpus:
    """Read YAML into a Corpus."""

    return corpus_from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))
