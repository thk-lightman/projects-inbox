"""Sub-AC 4.2: docs/architecture.md exists and covers required topics."""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHITECTURE_DOC = REPO_ROOT / "docs" / "architecture.md"

REQUIRED_SECTION_HEADERS = [
    "## Storage",
    "## Chunking",
    "## Cluster Threshold",
    "## Taxonomy Evolution",
    "## pgvector Migration",
]


def test_architecture_doc_exists():
    assert ARCHITECTURE_DOC.is_file(), (
        f"docs/architecture.md missing at {ARCHITECTURE_DOC}"
    )


def test_architecture_doc_non_empty():
    text = ARCHITECTURE_DOC.read_text(encoding="utf-8")
    assert text.strip(), "docs/architecture.md is empty"


@pytest.mark.parametrize("header", REQUIRED_SECTION_HEADERS)
def test_architecture_doc_has_required_section_header(header: str):
    text = ARCHITECTURE_DOC.read_text(encoding="utf-8")
    lines = {line.rstrip() for line in text.splitlines()}
    assert header in lines, (
        f"Required section header missing in docs/architecture.md: {header!r}"
    )
