"""Paper briefing renderer/writer tests."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fetch_papers import (Paper, WrittenPaper, render_paper_briefing,  # noqa: E402
                          write_paper_briefings)


def _wp(title, cites, bucket, doi="10.1/x"):
    return WrittenPaper(paper=Paper(title=title, doi=doi, citation_count=cites,
                                    authors=["A", "B"], venue="JASA"),
                        path=Path("/tmp/x.md"), bucket=bucket)


def test_briefing_frontmatter_and_citation_ordering():
    papers = [Paper(title="low", citation_count=5, doi="10.1/a"),
              Paper(title="high", citation_count=99, doi="10.1/b")]
    md = render_paper_briefing(bucket="topic/bayes", week_iso="2026-W25",
                               papers=papers, fetched_at="2026-06-22T00:00:00Z")
    assert "source_kind: paper" in md
    assert "paper_count: 2" in md
    assert "topic/research/paper-briefing" in md
    # highest-cited first
    assert md.index("## high") < md.index("## low")


def test_write_one_file_per_bucket():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "briefings"
        written = [_wp("p1", 10, "topic/bayes"), _wp("p2", 20, "topic/bayes"),
                   _wp("p3", 5, "pi/Andrew Gelman")]
        n = write_paper_briefings(written, briefings_dir=d, week_iso="2026-W25",
                                  fetched_at="2026-06-22T00:00:00Z")
        assert n == 2
        names = sorted(p.name for p in d.glob("*.md"))
        assert names == ["paper-2026-W25-pi-andrew-gelman.md",
                         "paper-2026-W25-topic-bayes.md"], names


def test_dry_run_writes_nothing():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "briefings"
        write_paper_briefings([_wp("p1", 10, "topic/x")], briefings_dir=d,
                              week_iso="2026-W25", fetched_at="t", dry_run=True)
        assert not d.exists()
