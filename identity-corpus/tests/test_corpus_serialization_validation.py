from __future__ import annotations

from pathlib import Path

from identity_corpus.corpus import (
    TaggedNote,
    assemble_corpus,
    read_corpus_json,
    read_corpus_yaml,
    write_corpus_json,
    write_corpus_yaml,
)
from identity_corpus.validation import validate_corpus_json


def _note(tags: dict[str, str | None]) -> TaggedNote:
    return TaggedNote("n1", "Title", "note.md", tags, "body")


def test_corpus_json_and_yaml_round_trip(tmp_path: Path) -> None:
    corpus = assemble_corpus([_note({"I_structural_frames": "Direct"})])
    json_path = tmp_path / "corpus.json"
    yaml_path = tmp_path / "corpus.yaml"

    write_corpus_json(corpus, json_path)
    write_corpus_yaml(corpus, yaml_path)

    assert read_corpus_json(json_path) == corpus
    assert read_corpus_yaml(yaml_path) == corpus


def test_validate_corpus_json_reports_invalid_leaf(tmp_path: Path) -> None:
    taxonomy = tmp_path / "taxonomy.yaml"
    taxonomy.write_text(
        """
version: 1
axes:
  I_structural_frames:
    groups:
      Opening: [Direct]
""",
        encoding="utf-8",
    )
    corpus = assemble_corpus([_note({"I_structural_frames": "Bogus"})])
    path = tmp_path / "corpus.json"
    write_corpus_json(corpus, path)

    errors = validate_corpus_json(path, taxonomy)

    assert errors == ["n1: invalid leaf I_structural_frames:Bogus", "axis coverage incomplete: I_structural_frames"]
