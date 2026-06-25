"""Blog watchlist parser + writer/inbox unit tests."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fetch_blog import (  # noqa: E402
    BlogPost, BlogRow, WrittenBlog,
    append_links_to_blog_inbox, parse_blog_watchlist, write_blog_md,
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


def test_write_blog_md_and_idempotency(tmp_path):
    post = BlogPost(title="My First Post", link="https://x/p1", guid="g1",
                    author="Jane", published_date="2026-06-20",
                    summary="Hello <b>world</b>", feed_name="Lil'Log")
    p1 = write_blog_md(post, tmp_path)
    assert p1.name == "blog-W" + __import__("datetime").datetime.now().strftime("%V") + "-my-first-post.md"
    body = p1.read_text()
    assert "source: blog-rss" in body
    assert "canonical_id: " + post.canonical_id() in body
    assert "feed: Lil'Log" in body
    assert "## Summary" in body
    # second write of same post is a no-op returning the same path
    p2 = write_blog_md(post, tmp_path)
    assert p1 == p2


def test_write_blog_md_slug_collision_disambiguates(tmp_path):
    a = BlogPost(title="Same Title", link="https://x/a", guid="ga")
    b = BlogPost(title="Same Title", link="https://x/b", guid="gb")
    pa = write_blog_md(a, tmp_path)
    pb = write_blog_md(b, tmp_path)
    assert pa != pb  # different canonical → distinct files, neither lost


def test_append_links_dedup_and_format(tmp_path):
    inbox = tmp_path / "01Inbox-blog.md"
    w = WrittenBlog(post=BlogPost(title="Post A", link="https://x/a"),
                    path=tmp_path / "blog-x.md", bucket="lil-log")
    n = append_links_to_blog_inbox([w], inbox_file=inbox, today_yyyymmdd="20260624")
    assert n == 1
    line = inbox.read_text().strip()
    assert line == "- 20260624 - [auto-research-blog/lil-log] Post A https://x/a"
    # re-append same post → skipped (url already present)
    n2 = append_links_to_blog_inbox([w], inbox_file=inbox, today_yyyymmdd="20260625")
    assert n2 == 0
