"""Tests for vault_corpus.chunker.parse_frontmatter and split_by_headings."""

from __future__ import annotations

import hashlib
from pathlib import Path

from vault_corpus.chunker import (
    Chunk,
    chunk_note,
    compute_chunk_id,
    parse_frontmatter,
    split_by_headings,
)


def test_parse_frontmatter_with_metadata_and_body():
    text = (
        "---\n"
        "title: Hello\n"
        "tags: [a, b]\n"
        "---\n"
        "# Heading\n"
        "\n"
        "Body paragraph.\n"
    )
    meta, body = parse_frontmatter(text)
    assert meta == {"title": "Hello", "tags": ["a", "b"]}
    assert body == "# Heading\n\nBody paragraph.\n"


def test_parse_frontmatter_without_frontmatter_returns_empty_meta():
    text = "# Heading\n\nNo front-matter here.\n"
    meta, body = parse_frontmatter(text)
    assert meta == {}
    assert body == text


def test_parse_frontmatter_only_frontmatter_yields_empty_body():
    text = "---\ntitle: Only\nstatus: draft\n---\n"
    meta, body = parse_frontmatter(text)
    assert meta == {"title": "Only", "status": "draft"}
    assert body == ""


def test_parse_frontmatter_empty_string_is_safe():
    meta, body = parse_frontmatter("")
    assert meta == {}
    assert body == ""


def test_parse_frontmatter_unterminated_block_is_not_treated_as_frontmatter():
    text = "---\ntitle: Unterminated\n\nbody continues\n"
    meta, body = parse_frontmatter(text)
    assert meta == {}
    assert body == text


def test_parse_frontmatter_malformed_yaml_returns_empty_meta_and_strips_block():
    text = "---\n: : not valid yaml :\n---\nbody\n"
    meta, body = parse_frontmatter(text)
    assert meta == {}
    assert body == "body\n"


def test_split_by_headings_no_heading_returns_single_chunk():
    body = "Just a paragraph.\n\nAnother paragraph.\n"
    chunks = split_by_headings(body)
    assert chunks == [([], body)]


def test_split_by_headings_empty_body_returns_single_empty_chunk():
    assert split_by_headings("") == [([], "")]


def test_split_by_headings_multi_heading_splits_at_each_h2():
    body = (
        "## First\n"
        "alpha line\n"
        "## Second\n"
        "beta line\n"
        "## Third\n"
        "gamma line\n"
    )
    chunks = split_by_headings(body)
    assert [c[0] for c in chunks] == [["First"], ["Second"], ["Third"]]
    assert chunks[0][1] == "## First\nalpha line\n"
    assert chunks[1][1] == "## Second\nbeta line\n"
    assert chunks[2][1] == "## Third\ngamma line\n"


def test_split_by_headings_nested_h3_inherits_parent_h2():
    body = (
        "## Parent\n"
        "intro\n"
        "### ChildA\n"
        "child a body\n"
        "### ChildB\n"
        "child b body\n"
        "## Sibling\n"
        "sibling body\n"
        "### Nested\n"
        "nested body\n"
    )
    chunks = split_by_headings(body)
    chains = [c[0] for c in chunks]
    assert chains == [
        ["Parent"],
        ["Parent", "ChildA"],
        ["Parent", "ChildB"],
        ["Sibling"],
        ["Sibling", "Nested"],
    ]
    assert chunks[0][1] == "## Parent\nintro\n"
    assert chunks[1][1] == "### ChildA\nchild a body\n"
    assert chunks[3][1] == "## Sibling\nsibling body\n"


def test_split_by_headings_ignores_headings_inside_fenced_code_block():
    body = (
        "## Real Heading\n"
        "before code\n"
        "```python\n"
        "## not a heading\n"
        "### also not a heading\n"
        "```\n"
        "after code\n"
        "## Another Real\n"
        "tail\n"
    )
    chunks = split_by_headings(body)
    assert [c[0] for c in chunks] == [["Real Heading"], ["Another Real"]]
    assert "## not a heading" in chunks[0][1]
    assert "### also not a heading" in chunks[0][1]
    assert chunks[1][1] == "## Another Real\ntail\n"


def test_split_by_headings_ignores_headings_inside_tilde_fence():
    body = (
        "## Real\n"
        "~~~\n"
        "## fake\n"
        "~~~\n"
        "## RealTwo\n"
    )
    chunks = split_by_headings(body)
    assert [c[0] for c in chunks] == [["Real"], ["RealTwo"]]


def test_split_by_headings_preamble_before_first_heading_is_its_own_chunk():
    body = (
        "preamble line one\n"
        "preamble line two\n"
        "## First\n"
        "body\n"
    )
    chunks = split_by_headings(body)
    assert chunks[0] == ([], "preamble line one\npreamble line two\n")
    assert chunks[1] == (["First"], "## First\nbody\n")


def test_split_by_headings_h1_and_h4_not_boundaries():
    body = (
        "# Title H1\n"
        "intro under h1\n"
        "## Real\n"
        "real body\n"
        "#### Deep\n"
        "deep body still in real\n"
    )
    chunks = split_by_headings(body)
    assert [c[0] for c in chunks] == [[], ["Real"]]
    assert chunks[0][1] == "# Title H1\nintro under h1\n"
    assert "#### Deep" in chunks[1][1]


def test_split_by_headings_leading_h3_without_parent_h2():
    body = "### Orphan\nbody\n## Parent\np body\n### Child\nc body\n"
    chunks = split_by_headings(body)
    assert [c[0] for c in chunks] == [
        ["Orphan"],
        ["Parent"],
        ["Parent", "Child"],
    ]


def test_compute_chunk_id_is_deterministic():
    path = Path("/vault/note.md")
    chain = ["H2", "H3"]
    body = "some chunk body\n"
    a = compute_chunk_id(path, chain, body)
    b = compute_chunk_id(path, chain, body)
    assert a == b
    assert len(a) == 64
    assert all(c in "0123456789abcdef" for c in a)


def test_compute_chunk_id_matches_documented_sha256_formula():
    path = Path("/vault/note.md")
    chain = ["H2", "H3"]
    body = "some chunk body\n"
    expected = hashlib.sha256(
        f"{path}\n{chain}\n{body}".encode("utf-8")
    ).hexdigest()
    assert compute_chunk_id(path, chain, body) == expected


def test_compute_chunk_id_changes_when_source_path_changes():
    chain = ["H"]
    body = "b"
    a = compute_chunk_id(Path("/vault/one.md"), chain, body)
    b = compute_chunk_id(Path("/vault/two.md"), chain, body)
    assert a != b


def test_compute_chunk_id_changes_when_heading_chain_changes():
    path = Path("/vault/note.md")
    body = "b"
    a = compute_chunk_id(path, ["H"], body)
    b = compute_chunk_id(path, ["H", "Sub"], body)
    c = compute_chunk_id(path, ["Other"], body)
    assert a != b
    assert a != c
    assert b != c


def test_compute_chunk_id_changes_when_body_changes():
    path = Path("/vault/note.md")
    chain = ["H"]
    a = compute_chunk_id(path, chain, "body one")
    b = compute_chunk_id(path, chain, "body two")
    assert a != b


# ---------------------------------------------------------------------------
# chunk_note() — integration tests for the five required AC3 scenarios.
# ---------------------------------------------------------------------------


def test_chunk_note_multi_heading_note_yields_one_chunk_per_h2():
    """Scenario 1 of AC3: multi-heading note."""
    path = Path("/vault/multi.md")
    text = (
        "---\n"
        "title: Multi\n"
        "tags: [a]\n"
        "---\n"
        "## Alpha\n"
        "alpha body\n"
        "## Beta\n"
        "beta body\n"
        "## Gamma\n"
        "gamma body\n"
    )
    chunks = chunk_note(path, text)

    assert len(chunks) == 3
    assert [c.heading_chain for c in chunks] == [["Alpha"], ["Beta"], ["Gamma"]]
    assert all(isinstance(c, Chunk) for c in chunks)
    assert all(c.source_path == path for c in chunks)
    assert all(c.lang == "ko" for c in chunks)
    assert all(c.frontmatter == {"title": "Multi", "tags": ["a"]} for c in chunks)
    # Front-matter excluded from chunk body.
    for c in chunks:
        assert "title: Multi" not in c.body
        assert not c.body.startswith("---")
    # chunk_id is the documented SHA-256.
    assert chunks[0].chunk_id == compute_chunk_id(
        path, ["Alpha"], "## Alpha\nalpha body\n"
    )
    # chunk_ids unique across the note.
    assert len({c.chunk_id for c in chunks}) == 3


def test_chunk_note_no_heading_note_yields_single_chunk_covering_whole_body():
    """Scenario 2 of AC3: note with no `##` headings."""
    path = Path("/vault/flat.md")
    text = (
        "---\n"
        "title: Flat\n"
        "---\n"
        "Just a paragraph.\n"
        "\n"
        "And another paragraph.\n"
    )
    chunks = chunk_note(path, text)

    assert len(chunks) == 1
    only = chunks[0]
    assert only.heading_chain == []
    assert only.body == "Just a paragraph.\n\nAnd another paragraph.\n"
    assert only.lang == "ko"
    assert only.source_path == path
    assert only.frontmatter == {"title": "Flat"}
    assert only.chunk_id == compute_chunk_id(path, [], only.body)


def test_chunk_note_no_heading_no_frontmatter_yields_one_whole_note_chunk():
    """Scenario 2 variant: bare note, no front-matter, no headings."""
    path = Path("/vault/bare.md")
    text = "Bare note body line one.\nLine two.\n"
    chunks = chunk_note(path, text)

    assert len(chunks) == 1
    assert chunks[0].heading_chain == []
    assert chunks[0].body == text
    assert chunks[0].frontmatter == {}
    assert chunks[0].lang == "ko"


def test_chunk_note_nested_h2_h3_headings_build_correct_chain():
    """Scenario 3 of AC3: nested ## / ### headings."""
    path = Path("/vault/nested.md")
    text = (
        "## Parent\n"
        "intro\n"
        "### ChildA\n"
        "a body\n"
        "### ChildB\n"
        "b body\n"
        "## Sibling\n"
        "s body\n"
        "### Nested\n"
        "n body\n"
    )
    chunks = chunk_note(path, text)

    chains = [c.heading_chain for c in chunks]
    assert chains == [
        ["Parent"],
        ["Parent", "ChildA"],
        ["Parent", "ChildB"],
        ["Sibling"],
        ["Sibling", "Nested"],
    ]
    # Empty front-matter mapping for a note without front-matter.
    assert all(c.frontmatter == {} for c in chunks)
    # Each chunk's body retains its own heading line.
    assert chunks[0].body.startswith("## Parent")
    assert chunks[1].body.startswith("### ChildA")
    assert chunks[4].body.startswith("### Nested")
    # chunk_ids are all distinct.
    assert len({c.chunk_id for c in chunks}) == len(chunks)


def test_chunk_note_only_frontmatter_yields_zero_chunks():
    """Scenario 4 of AC3: note with only front-matter (no body to chunk)."""
    path = Path("/vault/meta_only.md")
    text = "---\ntitle: Only\nstatus: draft\n---\n"
    chunks = chunk_note(path, text)
    # Front-matter is metadata, not chunkable content. No chunk should be
    # emitted so we never store an empty-body chunk in the index.
    assert chunks == []


def test_chunk_note_code_blocks_with_hashes_are_not_heading_boundaries():
    """Scenario 5 of AC3: `##` lines inside fenced code blocks are not headings."""
    path = Path("/vault/code.md")
    text = (
        "---\n"
        "lang: python\n"
        "---\n"
        "## Real Heading\n"
        "before code\n"
        "```python\n"
        "## this is a comment, not a heading\n"
        "### also still inside fence\n"
        "```\n"
        "after code\n"
        "## Another Real\n"
        "tail\n"
    )
    chunks = chunk_note(path, text)

    assert [c.heading_chain for c in chunks] == [["Real Heading"], ["Another Real"]]
    # The fake heading lines must remain inside the first chunk's body.
    assert "## this is a comment" in chunks[0].body
    assert "### also still inside fence" in chunks[0].body
    # Front-matter persists on each chunk.
    assert all(c.frontmatter == {"lang": "python"} for c in chunks)
    # Front-matter delimiter never leaks into body.
    for c in chunks:
        assert not c.body.startswith("---")


def test_chunk_note_chunk_id_is_stable_across_calls():
    """chunk_note must be deterministic — same input ⇒ same chunk_ids."""
    path = Path("/vault/stable.md")
    text = "## H\nbody\n## H2\nbody2\n"
    a = chunk_note(path, text)
    b = chunk_note(path, text)
    assert [c.chunk_id for c in a] == [c.chunk_id for c in b]


def test_chunk_note_preamble_before_first_heading_becomes_its_own_chunk():
    """Preamble before the first ## heading is preserved with empty chain."""
    path = Path("/vault/preamble.md")
    text = (
        "intro line one\n"
        "intro line two\n"
        "## First\n"
        "body\n"
    )
    chunks = chunk_note(path, text)
    assert len(chunks) == 2
    assert chunks[0].heading_chain == []
    assert chunks[0].body == "intro line one\nintro line two\n"
    assert chunks[1].heading_chain == ["First"]
    assert chunks[1].body == "## First\nbody\n"


def test_chunk_note_returns_chunk_dataclass_instances():
    """Return type contract: list[Chunk]."""
    path = Path("/vault/x.md")
    chunks = chunk_note(path, "body only\n")
    assert isinstance(chunks, list)
    assert all(isinstance(c, Chunk) for c in chunks)
    only = chunks[0]
    assert only.lang == "ko"
    assert only.source_path == path
    assert len(only.chunk_id) == 64
