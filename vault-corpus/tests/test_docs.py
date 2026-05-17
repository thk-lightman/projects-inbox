"""Verify README.md and docs/architecture.md contain required heading anchors.

Sub-AC 5 gate: docs must expose specific sections so downstream tooling
(and humans) can find usage, the API walkthrough, the storage/access split,
and the extension points without grepping.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
ARCH = REPO_ROOT / "docs" / "architecture.md"


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


def _headings(md_path: Path) -> list[str]:
    """Return the visible heading titles (any level) of a markdown file.

    Lines inside fenced code blocks are excluded so that a `## Goal` example
    inside a ```python ... ``` block is not mistaken for a real heading.
    """
    text = md_path.read_text(encoding="utf-8")
    in_fence = False
    headings: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if m:
            headings.append(m.group(2).strip())
    return headings


def test_readme_exists():
    assert README.is_file(), f"README.md missing at {README}"


def test_architecture_exists():
    assert ARCH.is_file(), f"docs/architecture.md missing at {ARCH}"


def test_readme_required_sections():
    headings = _headings(README)
    required = [
        "vault-corpus",
        "Install",
        "Usage",
        "API Integration — Step-by-Step",
        "Step 1: Auth (env var loading)",
        "Step 2: Single-text embedding call",
        "Step 3: Single translation call",
        "Step 4: End-to-end search call",
        "Architecture",
    ]
    missing = [h for h in required if h not in headings]
    assert not missing, f"README missing required headings: {missing}"


def test_architecture_required_sections():
    headings = _headings(ARCH)
    required = [
        "Architecture",
        "Storage Layer",
        "Access Layer",
        "Extension Points",
        "MCP server",
        "CLI search UI",
        "LLM Wiki publisher",
        "Migration to pgvector",
        "Reproducibility & Vault Immutability",
    ]
    missing = [h for h in required if h not in headings]
    assert not missing, f"architecture.md missing required headings: {missing}"


def test_readme_mentions_each_openai_touchpoint():
    """The API walkthrough must reference the four OpenAI integration boundaries."""
    text = README.read_text(encoding="utf-8").lower()
    for token in ("openai_api_key", "embeddings.create", "chat.completions.create", "text-embedding-3-large"):
        assert token in text, f"README walkthrough missing required token: {token!r}"


def test_architecture_documents_pgvector_path():
    text = ARCH.read_text(encoding="utf-8").lower()
    assert "pgvector" in text
    assert "chunk_id" in text
    assert "no re-embedding" in text or "no re-embed" in text
