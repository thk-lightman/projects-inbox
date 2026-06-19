#!/usr/bin/env python3
"""Autoresearch weekly dev fetch — URL supplier for material-absorb.

Reads vault/00 GTD/03Inbox/auto-research/docs/docs-watchlist-topics.md and
invokes `last30days-skill` via `claude -p` once per active topic. Two outputs:

1. **briefings/dev-W<주>-<topic>.md** — full synthesis (last30days output + a
   Korean TL;DR paragraph at the top). Kept for human curation reference; the
   material-absorb pipeline does NOT scan this folder.
2. **01Inbox-scrap.md** — every cited URL inside the synthesis is appended as
   one line per URL, in the user's existing paste format:
       `- YYYYMMDD - <title> <URL>`
   material-absorb later turns these lines into a propose table and routes them
   through fetch → judge → file.

Runs on the HOST (not the util-autoresearch Docker container) because the
Claude Code CLI and the installed plugin marketplace live on the host.

Usage:
    python3 fetch_dev.py
        [--watchlist-topics PATH] [--briefings-dir PATH] [--scrap-file PATH]
        [--dedup-db PATH] [--claude BIN] [--dry-run] [--days N]
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_VAULT = Path(os.environ.get("VAULT_ROOT", str(Path.home() / "Obsidian/Obsidian_Master_v2")))
DEFAULT_WATCHLIST_TOPICS = DEFAULT_VAULT / "00 Get Things Done/03Inbox/auto-research/docs/docs-watchlist-topics.md"
DEFAULT_BRIEFINGS_DIR = DEFAULT_VAULT / "00 Get Things Done/03Inbox/auto-research/briefings"
DEFAULT_SCRAP_FILE = DEFAULT_VAULT / "00 Get Things Done/03Inbox/01Inbox-scrap.md"
DEFAULT_DEDUP_DB = Path.home() / ".cache/autoresearch/dedup.sqlite"
DEFAULT_DAYS = 7

CLAUDE_DEFAULT_BIN = "claude"
"""Claude Code CLI. Override with --claude when installed elsewhere.

We invoke `claude -p --permission-mode bypassPermissions "/last30days ..."`.
This is the intended path per the skill's SKILL.md (LAW 7: "YOU ARE the
planner — the deterministic CLI fallback is the headless/cron path only").
Claude.ai Pro/Max subscriptions cover the quota; API-key installs bill per
call at Sonnet rates.
"""


# --------------------------- Data shapes -----------------------------------

@dataclass
class TopicRow:
    topic: str
    why_pick: str = ""
    status: str = "active"
    note: str = ""

    def slug(self) -> str:
        return _slugify(self.topic)


# --------------------------- Watchlist parser ------------------------------

WATCHLIST_TABLE_HEADERS = ("topic", "why_pick", "status")


def parse_topics_watchlist(text: str) -> list[TopicRow]:
    """Extract active TopicRow entries from a markdown watchlist file."""
    rows: list[TopicRow] = []
    for cells in _iter_markdown_table_rows(text, WATCHLIST_TABLE_HEADERS):
        topic = cells.get("topic", "").strip().strip("_")
        if not topic or topic.startswith("예:") or "_예:" in topic:
            continue
        status = (cells.get("status", "").strip().strip("_") or "active").lower()
        if status != "active":
            continue
        rows.append(TopicRow(
            topic=topic,
            why_pick=cells.get("why_pick", "").strip().strip("_"),
            status=status,
            note=cells.get("notes", "").strip().strip("_"),
        ))
    return rows


def _iter_markdown_table_rows(text: str, header_keys):
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
            continue
        yield {h: cells[i] if i < len(cells) else "" for i, h in enumerate(headers)}


def _slugify(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "topic"


# --------------------------- Dedup store -----------------------------------

class DedupStore:
    """sqlite-backed canonical_id store (shared with fetch_papers.py).

    Schema extended on 2026-06-10 with source_kind column (default 'paper'
    for backfilled rows). dev rows get source_kind='article'.
    """

    SOURCE_KIND = "article"

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
        self.conn.commit()

    def has(self, canonical_id: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM seen WHERE canonical_id = ?", (canonical_id,)
        )
        return cur.fetchone() is not None

    def mark(self, canonical_id: str, source: str, title: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO seen("
            "canonical_id, first_seen, source, title, source_kind) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                canonical_id,
                datetime.now(timezone.utc).isoformat(),
                source,
                title,
                self.SOURCE_KIND,
            ),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


# --------------------------- last30days runner -----------------------------

class Last30DaysError(RuntimeError):
    """Raised when claude -p exits non-zero or its output is unparseable."""


def run_last30days(
    topic: str,
    *,
    bin_path: str = CLAUDE_DEFAULT_BIN,
    days: int = DEFAULT_DAYS,
    timeout_s: int = 900,
) -> str:
    """Invoke /last30days via `claude -p` and return synthesis text.

    The prompt names the skill, the topic, and an additional instruction:
    prepend a Korean TL;DR paragraph at the very top of the output. The host
    LLM resolves the skill from the plugin marketplace and runs it end-to-end
    including LLM-driven planner + synthesis (SKILL.md LAW 7).

    bypassPermissions is required because cron has no interactive approver.
    Scope is narrowed by the prompt to a single slash command + one extra
    instruction; the host LLM cannot drift into other tool use.
    """
    prompt = (
        f"/last30days {topic} --emit md --days {days} --store\n\n"
        "Additional output requirement: at the very top of your synthesis "
        "(before the badge or any other content), insert a short Korean "
        "summary paragraph titled exactly `## 한국어 요약` containing 2-3 "
        "sentences in Korean that capture the dominant signal of the week. "
        "Then continue with the canonical last30days output as normal."
    )
    cmd = [bin_path, "-p", "--permission-mode", "bypassPermissions", prompt]
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except FileNotFoundError as exc:
        raise Last30DaysError(
            f"claude CLI not found at {bin_path!r}. Install Claude Code "
            "and add the last30days plugin via "
            "`/plugin marketplace add mvanhorn/last30days-skill`."
        ) from exc

    if proc.returncode != 0:
        raise Last30DaysError(
            f"claude -p exited {proc.returncode} for topic {topic!r}:\n"
            f"stdout: {proc.stdout[-500:]}\nstderr: {proc.stderr[-500:]}"
        )

    body = proc.stdout.strip()
    if not body:
        raise Last30DaysError(
            f"claude -p produced empty stdout for topic {topic!r}.\n"
            f"stderr: {proc.stderr[-500:]}"
        )
    return body


# --------------------------- URL extraction --------------------------------

# Markdown link [title](url) — title can contain anything except ']' on the
# same line; url is anything up to ')' that doesn't itself contain ')'.
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")

# Bare URLs that aren't already inside a markdown link parenthesis.
# We rely on _MD_LINK_RE matching first and excluding matched URLs from the
# bare pass via the seen-set in extract_urls.
_BARE_URL_RE = re.compile(r"https?://[^\s)\]]+")


def extract_urls(synthesis: str) -> list[tuple[str, str]]:
    """Pull (title, url) pairs from a last30days synthesis blob.

    Markdown links are preferred (title carries human context). Bare URLs are
    appended after with an empty title. Same URL appearing twice keeps only
    the first occurrence.
    """
    seen: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for m in _MD_LINK_RE.finditer(synthesis):
        title, url = m.group(1).strip(), _strip_url(m.group(2))
        if url in seen:
            continue
        seen.add(url)
        pairs.append((title, url))
    for m in _BARE_URL_RE.finditer(synthesis):
        url = _strip_url(m.group(0))
        if url in seen:
            continue
        seen.add(url)
        pairs.append(("", url))
    return pairs


def _strip_url(url: str) -> str:
    return url.rstrip(".,;:!?)\"'")


# --------------------------- Scrap file appender ---------------------------

def append_urls_to_scrap(
    pairs: list[tuple[str, str]],
    *,
    scrap_file: Path,
    topic: TopicRow,
    today_yyyymmdd: str,
) -> int:
    """Append URL lines to 01Inbox-scrap.md in the user's paste format.

    Format mirrors lines already in the file:
        `- YYYYMMDD - <title> <URL>`
    The topic name is prepended in brackets so material-absorb can later
    cluster lines by origin if it wants to. URLs already present in the
    file are NOT re-added (silent dedup).
    """
    if not pairs:
        return 0

    scrap_file.parent.mkdir(parents=True, exist_ok=True)
    existing = scrap_file.read_text(encoding="utf-8") if scrap_file.is_file() else ""

    new_lines: list[str] = []
    for title, url in pairs:
        if url in existing:
            continue
        if title:
            line = f"- {today_yyyymmdd} - [auto-research-dev/{topic.slug()}] {title} {url}"
        else:
            line = f"- {today_yyyymmdd} - [auto-research-dev/{topic.slug()}] {url}"
        new_lines.append(line)

    if not new_lines:
        return 0

    suffix = "\n" + "\n".join(new_lines) + "\n"
    with scrap_file.open("a", encoding="utf-8") as f:
        f.write(suffix if existing.endswith("\n") else "\n" + suffix.lstrip("\n"))
    return len(new_lines)


# --------------------------- Briefing writer -------------------------------

def render_briefing_note(
    *,
    topic: TopicRow,
    week_iso: str,
    synthesis: str,
    fetched_at: str,
) -> str:
    """Wrap last30days synthesis with our frontmatter for the briefings/ folder."""
    return "\n".join([
        "---",
        "source: last30days",
        "source_kind: article",
        "track: dev",
        f"topic: {_yaml_escape(topic.topic)}",
        f"week: {week_iso}",
        f"fetched_at: {fetched_at}",
        f"why_pick: {_yaml_escape(topic.why_pick)}",
        "status_file: False",
        "tags:",
        "  - topic/research/dev-briefing",
        "---",
        "",
        f"# {topic.topic} ({week_iso})",
        "",
        f"> {topic.why_pick}" if topic.why_pick else "",
        "",
        synthesis.strip(),
        "",
    ])


def _yaml_escape(value: str) -> str:
    if not value:
        return '""'
    if any(c in value for c in ":#\"'\\\n"):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


# --------------------------- Main orchestrator -----------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watchlist-topics", type=Path, default=DEFAULT_WATCHLIST_TOPICS)
    parser.add_argument("--briefings-dir", type=Path, default=DEFAULT_BRIEFINGS_DIR)
    parser.add_argument("--scrap-file", type=Path, default=DEFAULT_SCRAP_FILE)
    parser.add_argument("--dedup-db", type=Path, default=DEFAULT_DEDUP_DB)
    parser.add_argument("--claude", default=CLAUDE_DEFAULT_BIN,
                        help="claude CLI binary path (default: PATH lookup).")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help="Lookback window in days (default: 7).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse watchlist and print plan without calling claude.")
    args = parser.parse_args(argv)

    if not args.watchlist_topics.is_file():
        print(f"error: watchlist not found at {args.watchlist_topics}", file=sys.stderr)
        return 2

    text = args.watchlist_topics.read_text(encoding="utf-8")
    topics = parse_topics_watchlist(text)
    if not topics:
        print("no active topics in watchlist; nothing to do.")
        return 0

    print(f"dev layer: {len(topics)} active topic(s).")
    if args.dry_run:
        for t in topics:
            print(f"  · {t.topic} → would call: {args.claude} -p "
                  f"(prompt: /last30days {t.topic!r} --emit md --days {args.days})")
        return 0

    week_iso = datetime.now().strftime("%G-W%V")
    today_yyyymmdd = datetime.now().strftime("%Y%m%d")
    fetched_at = datetime.now(timezone.utc).isoformat()
    args.briefings_dir.mkdir(parents=True, exist_ok=True)

    dedup = DedupStore(args.dedup_db)
    written: list[TopicRow] = []
    url_counts: dict[str, int] = {}
    failures: list[tuple[TopicRow, str]] = []

    try:
        for topic in topics:
            canonical_id = f"dev-topic:{topic.slug()}:{week_iso}"
            if dedup.has(canonical_id):
                print(f"  - skip {topic.topic!r}: already fetched this week.")
                continue
            try:
                synthesis = run_last30days(
                    topic.topic,
                    bin_path=args.claude,
                    days=args.days,
                )
            except Last30DaysError as exc:
                print(f"  ! fail {topic.topic!r}: {exc}", file=sys.stderr)
                failures.append((topic, str(exc)))
                continue

            briefing_path = args.briefings_dir / f"dev-{week_iso}-{topic.slug()}.md"
            briefing_path.write_text(
                render_briefing_note(
                    topic=topic,
                    week_iso=week_iso,
                    synthesis=synthesis,
                    fetched_at=fetched_at,
                ),
                encoding="utf-8",
            )
            print(f"  · briefing wrote {briefing_path}")

            url_pairs = extract_urls(synthesis)
            added = append_urls_to_scrap(
                url_pairs,
                scrap_file=args.scrap_file,
                topic=topic,
                today_yyyymmdd=today_yyyymmdd,
            )
            url_counts[topic.slug()] = added
            print(f"  · {added} URL(s) appended to {args.scrap_file.name}")

            dedup.mark(canonical_id, source=f"last30days:{topic.slug()}",
                       title=topic.topic)
            written.append(topic)
    finally:
        dedup.close()

    total_urls = sum(url_counts.values())
    print(f"dev layer: {len(written)} topic(s) processed, "
          f"{total_urls} URL(s) appended to scrap; "
          f"{len(failures)} failure(s).")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
