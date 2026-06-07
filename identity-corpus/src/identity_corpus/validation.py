"""Validation for serialized Corpus JSON artifacts."""

from __future__ import annotations

from pathlib import Path

from identity_corpus.corpus import Corpus, read_corpus_json
from identity_corpus.tagger import load_taxonomy, taxonomy_leaf_map


def validate_corpus(corpus: Corpus, taxonomy_path: Path) -> list[str]:
    """Return validation errors for a Corpus against taxonomy.yaml."""

    errors: list[str] = []
    taxonomy = load_taxonomy(taxonomy_path)
    valid = taxonomy_leaf_map(taxonomy)
    axes = set(valid)
    for note in corpus.notes:
        if not note.note_id or not note.source_path:
            errors.append(f"orphan note: {note}")
        for axis, leaf in note.tags.items():
            if axis not in valid:
                errors.append(f"{note.note_id}: unknown axis {axis}")
            elif leaf is not None and leaf not in valid[axis]:
                errors.append(f"{note.note_id}: invalid leaf {axis}:{leaf}")
        missing_axes = axes - set(note.tags)
        if missing_axes:
            errors.append(f"{note.note_id}: missing axes {', '.join(sorted(missing_axes))}")
    covered = {
        axis
        for note in corpus.notes
        for axis, leaf in note.tags.items()
        if axis in valid and leaf in valid[axis]
    }
    missing_coverage = axes - covered
    if corpus.notes and missing_coverage:
        errors.append(f"axis coverage incomplete: {', '.join(sorted(missing_coverage))}")
    if corpus.note_count != len(corpus.notes):
        errors.append("note_count does not match notes length")
    return errors


def validate_corpus_json(path: Path, taxonomy_path: Path) -> list[str]:
    """Read and validate a Corpus JSON file."""

    return validate_corpus(read_corpus_json(path), taxonomy_path)
