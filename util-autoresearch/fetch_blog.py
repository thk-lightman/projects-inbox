#!/usr/bin/env python3
"""Autoresearch weekly blog discovery.

Reads a markdown watchlist of blog RSS/Atom feeds and appends new posts as link
lines to 01Inbox-scrap.md — the SAME inbox the dev layer feeds. The blog layer
is pure *discovery* (find URLs); *comprehension* (fetch fulltext + judge) is the
absorb stage's job (learning-absorb-articles, which lazily fetches each scrap
URL's fulltext). So blog and dev are isomorphic: both discover → scrap → absorb.
Dedup is shared with the paper/dev layers via the sqlite store (source_kind=blog).

Usage:
    python3 fetch_blog.py
        [--watchlist PATH] [--scrap-file PATH] [--dedup-db PATH]
        [--max-posts N] [--dry-run]

Defaults match the dotfiles + vault convention; override per-file via AR_BLOG_*
env (see .env.example).
"""

from __future__ import annotations

import argparse
import hashlib
import html
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_VAULT = Path(os.environ.get("VAULT_ROOT", str(Path.home() / "Obsidian/Obsidian_Master_v2")))
# Vault-relative paths; override per-file via AR_BLOG_* env (see .env.example).
DEFAULT_WATCHLIST = DEFAULT_VAULT / (os.environ.get("AR_BLOG_WATCHLIST_REL")
    or "00 Get Things Done/03Inbox/auto-research/docs/docs-watchlist-blog.md")
# Blog discovery lands in the shared scrap inbox (same as the dev layer).
DEFAULT_SCRAP = DEFAULT_VAULT / (os.environ.get("AR_BLOG_SCRAP_REL")
    or "00 Get Things Done/03Inbox/01Inbox-scrap.md")
DEFAULT_DEDUP_DB = Path.home() / ".cache/autoresearch/dedup.sqlite"

# A fresh feed can carry a long backlog; cap newest-N per feed so a first run
# doesn't dump the whole archive. Older entries get picked up only if the feed
# still lists them on a later run — acceptable for a weekly cadence.
DEFAULT_MAX_POSTS = 20

USER_AGENT = "autoresearch/0.1 (https://github.com/openclaw)"  # placeholder UA


# --------------------------- Data shapes -----------------------------------

@dataclass
class BlogRow:
    """A feed fetch target from the watchlist."""
    name: str
    url: str = ""
    status: str = "active"
    field_tag: str = ""
    note: str = ""

    def slug(self) -> str:
        s = re.sub(r"[^a-z0-9]+", "-", self.name.strip().lower()).strip("-")
        return s or "blog"


@dataclass
class BlogPost:
    title: str
    link: str = ""
    guid: str = ""
    feed_name: str = ""

    def canonical_id(self) -> str:
        """guid → link hash. Prefix isolates blog rows from paper/dev ids."""
        src = (self.guid or self.link or self.title).strip()
        h = hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]
        return f"blog-{h}"


@dataclass
class WrittenBlog:
    post: BlogPost
    bucket: str  # feed slug


def _strip_html(s: str) -> str:
    """Drop HTML tags, decode entities (&rsquo; etc.), collapse whitespace."""
    text = re.sub(r"<[^>]+>", "", s or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


# --------------------------- Watchlist parser ------------------------------

WATCHLIST_TABLE_HEADER_KEYS = ("블로그", "RSS URL", "status")


def parse_blog_watchlist(text: str) -> list[BlogRow]:
    """Extract active BlogRow entries from a markdown watchlist file."""
    rows: list[BlogRow] = []
    for cells in _iter_markdown_table_rows(text, WATCHLIST_TABLE_HEADER_KEYS):
        name = cells.get("블로그", "").strip().strip("_")
        if not name or name.startswith("예:") or "_예:" in name:
            continue
        status = (cells.get("status", "").strip().strip("_") or "active").lower()
        if status != "active":
            continue
        url = cells.get("RSS URL", "").strip().strip("_")
        if not url:
            continue
        rows.append(BlogRow(
            name=name,
            url=url,
            status=status,
            field_tag=cells.get("분야", "").strip().strip("_"),
            note=cells.get("메모", "").strip().strip("_"),
        ))
    return rows


def _iter_markdown_table_rows(text: str, header_keys):
    """Yield dict-rows from markdown tables that include the given headers."""
    in_table = False
    headers: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            in_table = False
            headers = []
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if not in_table:
            if any(k in cells for k in header_keys):
                headers = cells
                in_table = True
            continue
        if all(re.fullmatch(r":?-+:?", c or "") for c in cells):
            continue  # separator
        yield {h: cells[i] if i < len(cells) else "" for i, h in enumerate(headers)}


# --------------------------- Dedup store -----------------------------------

class DedupStore:
    """sqlite-backed canonical_id store, shared with fetch_papers.py/fetch_dev.py.

    Blog rows carry source_kind='blog' so they coexist with paper/article rows
    in the one `seen` table. Schema is created/migrated identically to the other
    layers so whichever opens the db first repairs it."""

    SOURCE_KIND = "blog"

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS seen ("
            "canonical_id TEXT PRIMARY KEY, "
            "first_seen TEXT NOT NULL, "
            "source TEXT, title TEXT, "
            "source_kind TEXT NOT NULL DEFAULT 'paper')"
        )
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(seen)")}
        if "source_kind" not in cols:
            self.conn.execute(
                "ALTER TABLE seen ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'paper'")
        self.conn.commit()

    def has(self, canonical_id: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM seen WHERE canonical_id = ?", (canonical_id,))
        return cur.fetchone() is not None

    def mark(self, canonical_id: str, source: str, title: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO seen("
            "canonical_id, first_seen, source, title, source_kind) "
            "VALUES (?, ?, ?, ?, ?)",
            (canonical_id, datetime.now(timezone.utc).isoformat(), source, title,
             self.SOURCE_KIND),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


# --------------------------- Feed client -----------------------------------

def fetch_feed(url: str, *, max_posts: int = DEFAULT_MAX_POSTS) -> list[BlogPost]:
    """Parse an RSS/Atom feed into newest-first BlogPost list (≤ max_posts)."""
    import feedparser  # local import: keeps watchlist-parser tests dependency-free
    parsed = feedparser.parse(url, agent=USER_AGENT)
    feed_name = _strip_html((parsed.feed or {}).get("title", "")) if parsed.feed else ""
    posts: list[BlogPost] = []
    for e in parsed.entries[:max_posts]:
        posts.append(BlogPost(
            title=_strip_html(e.get("title", "")),
            link=(e.get("link") or "").strip(),
            guid=(e.get("id") or "").strip(),
            feed_name=feed_name,
        ))
    return posts


# --------------------------- Scrap append ----------------------------------

def append_to_scrap(written: list[WrittenBlog], *,
                    scrap_file: Path, today_yyyymmdd: str) -> int:
    """Append one discovery line per new post to 01Inbox-scrap.md, mirroring the
    dev layer's format (absorb-articles consumes this same inbox):

        - YYYYMMDD - [auto-research-blog/<feed>] <title> <url>

    A post whose url (or title, when url is empty) already appears is skipped.
    Returns the number of lines appended."""
    if not written:
        return 0
    scrap_file.parent.mkdir(parents=True, exist_ok=True)
    existing = scrap_file.read_text(encoding="utf-8") if scrap_file.is_file() else ""
    new_lines: list[str] = []
    for w in written:
        url = w.post.link
        dedup_key = url or w.post.title
        if dedup_key and dedup_key in existing:
            continue
        new_lines.append(
            f"- {today_yyyymmdd} - [auto-research-blog/{w.bucket}] "
            f"{w.post.title} {url or '(no-url)'}")
    if not new_lines:
        return 0
    sep = "" if (not existing or existing.endswith("\n")) else "\n"
    with scrap_file.open("a", encoding="utf-8") as f:
        f.write(sep + "\n".join(new_lines) + "\n")
    return len(new_lines)


# --------------------------- Pipeline orchestration ------------------------

def run_pipeline(*, feeds: list[BlogRow], dedup: DedupStore, dry_run: bool,
                 max_posts: int = DEFAULT_MAX_POSTS, feed_client=fetch_feed) -> dict:
    """Returns {seen_total, new_count, written}."""
    counts = {"seen_total": 0, "new_count": 0}
    written: list[WrittenBlog] = []
    counts["written"] = written

    for feed in feeds:
        try:
            posts = feed_client(feed.url, max_posts=max_posts)
        except Exception as e:  # one feed's failure must not abort the rest
            print(f"WARN feed:{feed.slug()}: {e}", file=sys.stderr)
            continue
        for post in posts:
            if not post.feed_name:
                post.feed_name = feed.name  # fall back to watchlist name
            canonical = post.canonical_id()
            counts["seen_total"] += 1
            if dedup.has(canonical):
                continue
            if not dry_run:
                dedup.mark(canonical, "blog", post.title)
            counts["new_count"] += 1
            written.append(WrittenBlog(post=post, bucket=feed.slug()))

    return counts


# --------------------------- CLI -------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST))
    ap.add_argument("--scrap-file", default=str(DEFAULT_SCRAP))
    ap.add_argument("--dedup-db", default=str(DEFAULT_DEDUP_DB))
    ap.add_argument("--max-posts", type=int, default=DEFAULT_MAX_POSTS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    watchlist_path = Path(args.watchlist)
    if not watchlist_path.exists():
        print(f"ERROR: blog watchlist missing: {watchlist_path}", file=sys.stderr)
        return 2

    feeds = parse_blog_watchlist(watchlist_path.read_text(encoding="utf-8"))

    dedup = DedupStore(Path(args.dedup_db))
    try:
        counts = run_pipeline(
            feeds=feeds,
            dedup=dedup,
            dry_run=args.dry_run,
            max_posts=args.max_posts,
        )
    finally:
        dedup.close()

    linked = 0
    if not args.dry_run:
        linked = append_to_scrap(
            counts["written"],
            scrap_file=Path(args.scrap_file),
            today_yyyymmdd=datetime.now().strftime("%Y%m%d"),
        )

    print(f"OK. feeds={len(feeds)} seen={counts['seen_total']} "
          f"new={counts['new_count']} linked={linked} dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
