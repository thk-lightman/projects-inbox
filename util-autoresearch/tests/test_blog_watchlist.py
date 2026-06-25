"""Blog watchlist parser + scrap-append + pipeline tests (discovery-only)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fetch_blog import (  # noqa: E402
    BlogPost, BlogRow, WrittenBlog,
    append_to_scrap, parse_blog_watchlist, run_pipeline,
)


WATCHLIST_FIXTURE = """---
tags: [topic/research/curation]
---

# docs-watchlist-blog

ignore preamble.

## 표

| 블로그 | RSS URL | status | 분야 | 메모 |
|---|---|---|---|---|
| _예: Lil'Log_ | _https://lilianweng.github.io/index.xml_ | _active_ | _ML_ | _ex_ |
| Lil'Log | https://lilianweng.github.io/index.xml | active | ML | weng |
| No URL blog |  | active | x | drop me |
| Paused blog | https://example.com/feed.xml | paused | x | skip |
| Default status | https://example.org/rss | | infra |  |
"""


def test_parser_skips_example_blank_url_and_inactive():
    rows = parse_blog_watchlist(WATCHLIST_FIXTURE)
    names = [r.name for r in rows]
    # example row, blank-URL row, and paused row all dropped; blank status → active
    assert names == ["Lil'Log", "Default status"], names


def test_parser_fields_and_slug():
    rows = parse_blog_watchlist(WATCHLIST_FIXTURE)
    by_name = {r.name: r for r in rows}
    assert by_name["Lil'Log"].url == "https://lilianweng.github.io/index.xml"
    assert by_name["Lil'Log"].field_tag == "ML"
    assert by_name["Lil'Log"].slug() == "lil-log"
    assert by_name["Default status"].status == "active"


def test_canonical_id_prefers_guid_then_link():
    a = BlogPost(title="T", link="https://x/post", guid="guid-1")
    b = BlogPost(title="T", link="https://x/post", guid="guid-2")
    # same link, different guid → distinct ids; guid wins
    assert a.canonical_id() != b.canonical_id()
    assert a.canonical_id().startswith("blog-")


def test_append_to_scrap_dedup_and_format(tmp_path):
    scrap = tmp_path / "01Inbox-scrap.md"
    w = WrittenBlog(post=BlogPost(title="Post A", link="https://x/a"), bucket="lil-log")
    n = append_to_scrap([w], scrap_file=scrap, today_yyyymmdd="20260624")
    assert n == 1
    line = scrap.read_text().strip()
    # identical format to the dev layer's scrap lines → absorb-articles consumes both
    assert line == "- 20260624 - [auto-research-blog/lil-log] Post A https://x/a"
    # re-append same post → skipped (url already present)
    n2 = append_to_scrap([w], scrap_file=scrap, today_yyyymmdd="20260625")
    assert n2 == 0


def test_append_to_scrap_preserves_existing_dev_lines(tmp_path):
    scrap = tmp_path / "01Inbox-scrap.md"
    scrap.write_text("- 20260620 - [auto-research-dev/agents] Foo https://x/dev\n",
                     encoding="utf-8")
    w = WrittenBlog(post=BlogPost(title="Bar", link="https://x/blog"), bucket="feed")
    append_to_scrap([w], scrap_file=scrap, today_yyyymmdd="20260624")
    text = scrap.read_text()
    assert "auto-research-dev/agents" in text     # dev line untouched
    assert "auto-research-blog/feed" in text       # blog line appended


class _FakeDedup:
    def __init__(self):
        self.seen = set()

    def has(self, c):
        return c in self.seen

    def mark(self, c, s, t):
        self.seen.add(c)


def test_run_pipeline_dedups_and_collects():
    feeds = [BlogRow(name="Lil'Log", url="x", status="active")]
    posts = [BlogPost(title="P1", link="u1", guid="g1"),
             BlogPost(title="P2", link="u2", guid="g2")]
    d = _FakeDedup()
    counts = run_pipeline(feeds=feeds, dedup=d, dry_run=False,
                          feed_client=lambda url, max_posts: posts)
    assert counts["new_count"] == 2 and len(counts["written"]) == 2
    assert counts["written"][0].bucket == "lil-log"
    # rerun → all dedup'd, 0 new
    counts2 = run_pipeline(feeds=feeds, dedup=d, dry_run=False,
                           feed_client=lambda url, max_posts: posts)
    assert counts2["new_count"] == 0
