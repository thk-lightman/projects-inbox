"""01Inbox-paper.md link-list builder tests."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fetch_papers import (Paper, WrittenPaper,  # noqa: E402
                          append_links_to_paper_inbox)


def _written(title, doi="", arxiv="", bucket="topic/x", key=""):
    p = Paper(title=title, doi=doi, arxiv_id=arxiv)
    return WrittenPaper(paper=p, path=Path("/tmp/x.md"), bucket=bucket, zotero_key=key)


def test_appends_line_with_bucket_url_and_zotero_key():
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "01Inbox-paper.md"
        n = append_links_to_paper_inbox(
            [_written("Bayes Paper", doi="10.1/x", bucket="topic/bayes", key="ZK1")],
            inbox_file=f, today_yyyymmdd="20260622")
        assert n == 1
        line = f.read_text(encoding="utf-8").strip()
        assert line == ("- 20260622 - [auto-research-paper/topic/bayes] "
                        "Bayes Paper https://doi.org/10.1/x (zotero:ZK1)")


def test_arxiv_url_and_no_url_fallback():
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "p.md"
        append_links_to_paper_inbox(
            [_written("A", arxiv="2403.111"), _written("B")],
            inbox_file=f, today_yyyymmdd="20260622")
        body = f.read_text(encoding="utf-8")
        assert "https://arxiv.org/abs/2403.111" in body
        assert "B (no-url)" in body


def test_skips_url_already_present():
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "p.md"
        f.write_text("- old - existing https://doi.org/10.1/x\n", encoding="utf-8")
        n = append_links_to_paper_inbox(
            [_written("dup", doi="10.1/x"), _written("new", doi="10.2/y")],
            inbox_file=f, today_yyyymmdd="20260622")
        assert n == 1
        assert "10.2/y" in f.read_text(encoding="utf-8")


def test_inserts_separator_when_existing_lacks_trailing_newline():
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "p.md"
        f.write_text("- prior line no newline", encoding="utf-8")
        append_links_to_paper_inbox([_written("X", doi="10.9/z")],
                                    inbox_file=f, today_yyyymmdd="20260622")
        lines = f.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "- prior line no newline"
        assert lines[1].endswith("https://doi.org/10.9/z")


def test_empty_written_is_noop():
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "p.md"
        assert append_links_to_paper_inbox([], inbox_file=f, today_yyyymmdd="20260622") == 0
        assert not f.exists()
