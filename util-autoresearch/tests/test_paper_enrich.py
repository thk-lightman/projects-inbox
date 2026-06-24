"""PDF-url capture, HTML title strip, and filename-collision disambiguation."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fetch_papers import (Paper, _paper_from_openalex, _paper_from_s2,  # noqa: E402
                          _render_paper_md, _strip_html, write_paper_md)


def test_strip_html_removes_tags():
    assert _strip_html("<b>lme4</b> models") == "lme4 models"
    assert _strip_html("plain") == "plain"


def test_openalex_captures_pdf_url_and_strips_title():
    w = {
        "title": "<b>lme4</b> Mixed Models",
        "authorships": [{"author": {"display_name": "A B"}}],
        "doi": "https://doi.org/10.1/x",
        "cited_by_count": 10,
        "publication_date": "2020-01-01",
        "abstract_inverted_index": {},
        "best_oa_location": {"pdf_url": "http://oa.example/x.pdf"},
    }
    p = _paper_from_openalex(w)
    assert p.title == "lme4 Mixed Models"
    assert p.pdf_url == "http://oa.example/x.pdf"


def test_openalex_pdf_url_falls_back_to_oa_url():
    w = {"title": "t", "authorships": [], "cited_by_count": 0,
         "open_access": {"oa_url": "http://oa.example/fallback.pdf"}}
    assert _paper_from_openalex(w).pdf_url == "http://oa.example/fallback.pdf"


def test_s2_captures_open_access_pdf():
    p = _paper_from_s2({"title": "t", "authors": [],
                        "openAccessPdf": {"url": "http://oa.example/s2.pdf"}})
    assert p.pdf_url == "http://oa.example/s2.pdf"


def test_render_includes_pdf_url_when_present():
    md = _render_paper_md(Paper(title="t", doi="10.1/x", pdf_url="http://oa/x.pdf"))
    assert 'pdf_url: "http://oa/x.pdf"' in md
    assert "pdf_url" not in _render_paper_md(Paper(title="t", doi="10.1/x"))


def test_write_paper_md_disambiguates_slug_collision():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        a = write_paper_md(Paper(title="Same Long Title", doi="10.1/a"), d)
        b = write_paper_md(Paper(title="Same Long Title", doi="10.2/b"), d)
        assert a != b, "distinct papers must not share a filename"
        assert a.exists() and b.exists()
