"""zotero_save unit tests (no real API calls)."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import zotero_save  # noqa: E402
from zotero_save import (build_zotero_item, split_frontmatter,  # noqa: E402
                          write_frontmatter, push_to_zotero, fetch_pdf,
                          _extract_abstract_from_body, _unpaywall_pdf_url)


FM_FIXTURE_PAPER = """\
---
source: arxiv-paper
title: Test Paper
authors: ["Alice Smith", "Bob Jones"]
published_date: 2024-08-01
venue: JASA
arxiv_id: "2403.12345"
doi: "10.1234/abc"
tags: [topic/research/causal-inference]
abstract: This is the abstract.
status_file: False
---

## Abstract

This is the abstract.
"""


def test_split_frontmatter_extracts_dict():
    fm, fm_text, body = split_frontmatter(FM_FIXTURE_PAPER)
    assert fm["title"] == "Test Paper"
    assert fm["arxiv_id"] == "2403.12345"
    assert "## Abstract" in body


def test_split_frontmatter_no_fences_returns_empty():
    fm, fm_text, body = split_frontmatter("no fences here\n## body")
    assert fm == {}
    assert body == "no fences here\n## body"


def test_build_zotero_item_journal_when_doi():
    fm = {
        "title": "Paper",
        "authors": ["First Last"],
        "doi": "10.1/x",
        "venue": "JASA",
        "published_date": "2024-01-01",
        "abstract": "Some abstract.",
        "tags": ["topic/research/causal"],
    }
    item = build_zotero_item(fm)
    assert item["itemType"] == "journalArticle"
    assert item["DOI"] == "10.1/x"
    assert item["publicationTitle"] == "JASA"
    assert any(t["tag"] == "from-vault" for t in item["tags"])
    assert item["creators"][0]["lastName"] == "Last"


def test_build_zotero_item_preprint_when_arxiv_only():
    fm = {
        "title": "Preprint",
        "authors": ["Solo Author"],
        "arxiv_id": "2403.12345",
        "published_date": "2024-03-15",
    }
    item = build_zotero_item(fm)
    assert item["itemType"] == "preprint"
    assert item["repository"] == "arXiv"
    assert item["archiveID"] == "2403.12345"


def test_build_item_date_object_is_json_serializable():
    # YAML turns unquoted `published_date: 2009-01-01` into a datetime.date;
    # the built item must still json-encode for the Zotero API (regression).
    import datetime
    import json
    item = build_zotero_item({
        "title": "P", "authors": ["First Last"], "doi": "10.1/x",
        "published_date": datetime.date(2009, 1, 1),
    })
    assert item["date"] == "2009-01-01"
    json.dumps(item)  # must not raise


class _FakeResp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status = status
    def read(self):
        return self._p
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def test_unpaywall_returns_pdf_url(monkeypatch):
    import json
    payload = json.dumps({"best_oa_location": {"url_for_pdf": "http://oa/x.pdf"}}).encode()
    monkeypatch.setattr(zotero_save.urllib.request, "urlopen",
                        lambda *a, **k: _FakeResp(payload))
    assert _unpaywall_pdf_url("10.1/x", "me@example.com") == "http://oa/x.pdf"


def test_unpaywall_none_on_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("net down")
    monkeypatch.setattr(zotero_save.urllib.request, "urlopen", boom)
    assert _unpaywall_pdf_url("10.1/x", "me@example.com") is None


def test_fetch_pdf_no_sources_returns_none():
    # no arxiv, no pdf_url, no doi+email → no candidates → None (no network)
    assert fetch_pdf(None, None, None) is None


def test_fetch_pdf_prefers_pdf_url(monkeypatch):
    grabbed = {}
    def fake_urlopen(req, timeout=60):
        grabbed["url"] = req.full_url
        return _FakeResp(b"x" * 2048)
    monkeypatch.setattr(zotero_save.urllib.request, "urlopen", fake_urlopen)
    out = fetch_pdf(None, "10.1/x", "http://oa/direct.pdf")
    assert out is not None and out.exists()
    assert grabbed["url"] == "http://oa/direct.pdf"  # used pdf_url, skipped unpaywall
    out.unlink(missing_ok=True)


def test_extract_abstract_prefers_frontmatter_field():
    assert _extract_abstract_from_body({"abstract": "fm"}, "## Abstract\n\nbody") == "fm"


def test_extract_abstract_falls_back_to_body():
    assert _extract_abstract_from_body({}, "## Abstract\n\nfrom body\n\n## Next") == "from body"


def test_push_to_zotero_idempotent_when_key_present():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "paper-x.md"
        p.write_text("---\ntitle: x\nzotero_key: ABC12345\n---\n\nbody", encoding="utf-8")
        with patch("zotero_save.zotero.Zotero") as mock_zot:
            result = push_to_zotero(p, api_key="k", user_id="1")
        assert result["status"] == "skipped"
        assert result["key"] == "ABC12345"
        mock_zot.assert_not_called()


def test_push_to_zotero_creates_item_and_writes_key():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "paper-y.md"
        p.write_text(FM_FIXTURE_PAPER, encoding="utf-8")
        mock_client = MagicMock()
        mock_client.create_items.return_value = {"successful": {"0": {"key": "ZK123456"}}}
        with patch("zotero_save.zotero.Zotero", return_value=mock_client):
            result = push_to_zotero(p, api_key="k", user_id="1",
                                     pdf_fetcher=lambda *a, **kw: None)
        assert result["status"] == "created"
        assert result["key"] == "ZK123456"
        assert result["pdf_attached"] is False
        new_text = p.read_text(encoding="utf-8")
        assert "zotero_key: ZK123456" in new_text


def test_push_to_zotero_attach_pdf_when_fetcher_returns_path():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "paper-z.md"
        p.write_text(FM_FIXTURE_PAPER, encoding="utf-8")
        fake_pdf = Path(td) / "fake.pdf"
        fake_pdf.write_bytes(b"x" * 2048)
        mock_client = MagicMock()
        mock_client.create_items.return_value = {"successful": {"0": {"key": "PDFKEY01"}}}
        with patch("zotero_save.zotero.Zotero", return_value=mock_client):
            result = push_to_zotero(p, api_key="k", user_id="1",
                                     pdf_fetcher=lambda *a, **kw: fake_pdf)
        assert result["status"] == "created"
        assert result["pdf_attached"] is True
        mock_client.attachment_simple.assert_called_once()
