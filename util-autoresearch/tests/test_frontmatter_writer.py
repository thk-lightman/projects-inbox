"""Frontmatter writer + author-position filter unit tests."""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fetch_papers import (Paper, matches_author_position, write_paper_md,  # noqa: E402
                          _paper_from_openalex, _paper_from_s2,
                          _reconstruct_openalex_abstract)


def test_write_paper_md_creates_file_with_required_frontmatter_keys():
    p = Paper(
        title="Bayesian Causal Forests",
        authors=["Andrew Gelman", "Other Author"],
        doi="10.1234/abc.def",
        venue="JASA",
        published_date="2025-03-15",
        abstract="We propose a Bayesian method.",
        citation_count=42,
        raw_source="openalex",
    )
    with tempfile.TemporaryDirectory() as td:
        out = write_paper_md(p, Path(td))
        body = out.read_text(encoding="utf-8")
    assert re.fullmatch(r"paper-W\d{2}-bayesian-causal-forests\.md", out.name), out.name
    assert "source: arxiv-paper" in body
    assert "title:" in body
    assert "authors:" in body
    assert "published_date: 2025-03-15" in body
    assert "venue: JASA" in body
    assert "citation_count: 42" in body
    assert "## Abstract" in body
    assert "Bayesian method" in body
    assert "status_file: False" in body


def test_write_paper_md_idempotent_skips_existing():
    p = Paper(title="t", authors=["a"], doi="10.x/y")
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        first = write_paper_md(p, td_path)
        first.write_text("preexisting", encoding="utf-8")
        second = write_paper_md(p, td_path)
        assert second == first
        assert second.read_text(encoding="utf-8") == "preexisting"


def test_write_paper_md_dry_run_no_file():
    p = Paper(title="t", authors=["a"], doi="10.x/y")
    with tempfile.TemporaryDirectory() as td:
        out = write_paper_md(p, Path(td), dry_run=True)
        assert not out.exists()


def test_matches_author_position_last():
    p = Paper(title="t", authors=["Student", "Andrew Gelman"])
    assert matches_author_position(p, "Andrew Gelman", "last") is True
    assert matches_author_position(p, "Andrew Gelman", "first") is False


def test_matches_author_position_any():
    p = Paper(title="t", authors=["A", "Andrew Gelman", "B"])
    assert matches_author_position(p, "Andrew Gelman", "any") is True
    assert matches_author_position(p, "Andrew Gelman", "") is True  # blank = any


def test_matches_author_position_first_or_last():
    p = Paper(title="t", authors=["First Author", "Middle", "Last Author"])
    assert matches_author_position(p, "first author", "first_or_last") is True
    assert matches_author_position(p, "last author", "first_or_last") is True
    assert matches_author_position(p, "middle", "first_or_last") is False


def test_paper_from_openalex_extracts_required_fields():
    work = {
        "title": "Foo",
        "authorships": [{"author": {"display_name": "Alice"}},
                          {"author": {"display_name": "Bob"}}],
        "doi": "https://doi.org/10.1234/foo",
        "primary_location": {"source": {"display_name": "JASA"}},
        "publication_date": "2025-01-02",
        "abstract_inverted_index": {"hello": [0], "world": [1]},
        "cited_by_count": 25,
        "locations": [],
    }
    p = _paper_from_openalex(work)
    assert p.title == "Foo"
    assert p.authors == ["Alice", "Bob"]
    assert p.doi == "10.1234/foo"
    assert p.venue == "JASA"
    assert p.published_date == "2025-01-02"
    assert p.abstract == "hello world"
    assert p.citation_count == 25


def test_paper_from_s2_extracts_arxiv_id():
    p = _paper_from_s2({
        "title": "Bar",
        "authors": [{"name": "X"}],
        "externalIds": {"DOI": "10.5/y", "ArXiv": "2403.12345"},
        "year": 2024,
        "publicationDate": "2024-08-01",
        "venue": "ICML",
        "abstract": "abs",
        "citationCount": 100,
    })
    assert p.arxiv_id == "2403.12345"
    assert p.doi == "10.5/y"
    assert p.published_date == "2024-08-01"


def test_reconstruct_openalex_abstract_preserves_order():
    inv = {"world": [1], "hello": [0], "foo": [2]}
    assert _reconstruct_openalex_abstract(inv) == "hello world foo"


def test_reconstruct_openalex_abstract_empty():
    assert _reconstruct_openalex_abstract({}) == ""
