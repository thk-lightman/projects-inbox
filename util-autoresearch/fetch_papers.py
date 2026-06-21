#!/usr/bin/env python3
"""Autoresearch weekly paper fetch.

Reads vault/01 Command Center/prod-autoresearch/docs-watchlist-{labs,journals}.md
tables, calls OpenAlex + Semantic Scholar for each PI/venue, applies citation
filter, dedups via DOI → arXiv id → title+author hash cascade, then drops new
papers into vault/00 GTD/03Inbox/auto/paper-<canonical-id>.md with M1 schema.

Usage:
    python3 fetch_papers.py
        [--watchlist-labs PATH] [--watchlist-journals PATH]
        [--inbox-dir PATH] [--dedup-db PATH]
        [--dry-run]

Defaults match the dotfiles + vault convention; the launchd plist passes
absolute paths so the script needs no environment beyond OPENALEX_EMAIL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_VAULT = Path(os.environ.get("VAULT_ROOT", str(Path.home() / "Obsidian/Obsidian_Master_v2")))
# Vault-relative paths; override per-file via AR_PAPER_* env (see .env.example).
DEFAULT_WATCHLIST_LABS = DEFAULT_VAULT / (os.environ.get("AR_PAPER_WATCHLIST_LABS_REL")
    or "00 Get Things Done/03Inbox/auto-research/docs/docs-watchlist-labs.md")
DEFAULT_WATCHLIST_JOURNALS = DEFAULT_VAULT / (os.environ.get("AR_PAPER_WATCHLIST_JOURNALS_REL")
    or "00 Get Things Done/03Inbox/auto-research/docs/docs-watchlist-journals.md")
DEFAULT_INBOX_DIR = DEFAULT_VAULT / (os.environ.get("AR_PAPER_INBOX_REL")
    or "00 Get Things Done/03Inbox/auto-research/raws")
DEFAULT_WATCHLIST_TOPICS = DEFAULT_VAULT / (os.environ.get("AR_PAPER_WATCHLIST_TOPICS_REL")
    or "00 Get Things Done/03Inbox/auto-research/docs/docs-watchlist-paper-topics.md")
DEFAULT_INBOX_LIST = DEFAULT_VAULT / (os.environ.get("AR_PAPER_INBOX_LIST_REL")
    or "00 Get Things Done/03Inbox/01Inbox-paper.md")
DEFAULT_DEDUP_DB = Path.home() / ".cache/autoresearch/dedup.sqlite"

DEFAULT_CITATION_MIN = 20
DEFAULT_LOOKBACK_MONTHS = 12

OPENALEX_WORKS_URL = "https://api.openalex.org/works"
S2_AUTHOR_PAPERS_URL = "https://api.semanticscholar.org/graph/v1/author/{author_id}/papers"
S2_PAPER_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"

OPENALEX_RATE_DELAY = 0.1  # 10/s polite pool
S2_RATE_DELAY = 3.1  # ≤ 100 / 5min when unauthed (extra margin)

USER_AGENT = "autoresearch/0.1 (https://github.com/openclaw)"  # placeholder UA


# --------------------------- Data shapes -----------------------------------

@dataclass
class LabRow:
    pi_name: str
    openalex_author_id: str = ""
    s2_author_id: str = ""
    position_filter: str = "any"  # last|first|any|first_or_last
    citation_min: int = 0  # 0 means use global default
    field_tag: str = ""
    note: str = ""

    def effective_citation_min(self) -> int:
        return self.citation_min or DEFAULT_CITATION_MIN


@dataclass
class JournalRow:
    venue_name: str
    openalex_source_id: str = ""
    s2_venue: str = ""
    citation_min: int = 0
    lookback_months: int = 0
    field_tag: str = ""
    note: str = ""

    def effective_citation_min(self) -> int:
        return self.citation_min or DEFAULT_CITATION_MIN

    def effective_lookback_months(self) -> int:
        return self.lookback_months or DEFAULT_LOOKBACK_MONTHS


TOPIC_STRATEGIES = ("classic", "recent", "keyword")


@dataclass
class TopicRow:
    """A keyword-driven fetch target. strategy picks the OpenAlex sort/filter."""
    topic: str
    strategy: str = "keyword"  # classic | recent | keyword
    query: str = ""            # search term; falls back to topic when empty
    citation_min: int = 0
    status: str = "active"
    note: str = ""

    def slug(self) -> str:
        s = re.sub(r"[^a-z0-9]+", "-", self.topic.strip().lower()).strip("-")
        return s or "topic"

    def effective_query(self) -> str:
        return self.query or self.topic

    def effective_citation_min(self) -> int:
        return self.citation_min or DEFAULT_CITATION_MIN


@dataclass
class Paper:
    title: str
    authors: list[str] = field(default_factory=list)
    doi: str = ""
    arxiv_id: str = ""
    venue: str = ""
    published_date: str = ""
    abstract: str = ""
    citation_count: int = 0
    raw_source: str = ""  # "openalex" or "s2"

    def url(self) -> str:
        """Best stable URL: DOI resolver, else arXiv abstract page."""
        if self.doi:
            return f"https://doi.org/{self.doi}"
        if self.arxiv_id:
            return f"https://arxiv.org/abs/{self.arxiv_id}"
        return ""

    def canonical_id(self) -> str:
        """DOI → arXiv id → title+first-author hash."""
        if self.doi:
            return _normalize_doi(self.doi)
        if self.arxiv_id:
            return f"arxiv-{self.arxiv_id}"
        first_author = self.authors[0] if self.authors else ""
        hash_src = f"{self.title.strip().lower()}|{first_author.strip().lower()}"
        h = hashlib.sha256(hash_src.encode("utf-8")).hexdigest()[:16]
        return f"hash-{h}"


def _normalize_doi(doi: str) -> str:
    """Convert DOI to filename-safe id. 10.1234/abc.def → 10-1234-abc-def."""
    d = doi.strip().lower()
    d = d.replace("https://doi.org/", "").replace("doi:", "")
    return re.sub(r"[^a-z0-9]+", "-", d).strip("-")


@dataclass
class WrittenPaper:
    """A paper newly written this run, tagged with its origin bucket."""
    paper: Paper
    path: Path
    bucket: str           # e.g. "topic/<slug>", "pi/<name>", "venue/<name>"
    zotero_key: str = ""  # filled when the zotero push step runs


# --------------------------- Watchlist parser ------------------------------

WATCHLIST_TABLE_HEADER_LABS_KEYS = ("PI 이름", "OpenAlex authorId", "S2 authorId")
WATCHLIST_TABLE_HEADER_JOURNALS_KEYS = ("저널", "OpenAlex source id", "S2 venue 이름")
WATCHLIST_TABLE_HEADER_TOPICS_KEYS = ("주제", "전략", "쿼리")


def parse_labs_watchlist(text: str) -> list[LabRow]:
    """Extract LabRow entries from a markdown watchlist file."""
    rows: list[LabRow] = []
    for cells in _iter_markdown_table_rows(text, WATCHLIST_TABLE_HEADER_LABS_KEYS):
        pi = cells.get("PI 이름", "").strip().strip("_")
        if not pi or pi.startswith("예:") or "_예:" in pi:
            continue
        rows.append(LabRow(
            pi_name=pi,
            openalex_author_id=cells.get("OpenAlex authorId", "").strip().strip("_"),
            s2_author_id=cells.get("S2 authorId", "").strip().strip("_"),
            position_filter=(cells.get("위치 필터", "").strip().strip("_") or "any"),
            citation_min=_parse_int(cells.get("citation_min", "")),
            field_tag=cells.get("분야", "").strip().strip("_"),
            note=cells.get("메모", "").strip().strip("_"),
        ))
    return rows


def parse_journals_watchlist(text: str) -> list[JournalRow]:
    rows: list[JournalRow] = []
    for cells in _iter_markdown_table_rows(text, WATCHLIST_TABLE_HEADER_JOURNALS_KEYS):
        name = cells.get("저널·컨퍼런스", "").strip().strip("_")
        if not name or name.startswith("예:") or "_예:" in name:
            continue
        rows.append(JournalRow(
            venue_name=name,
            openalex_source_id=cells.get("OpenAlex source id", "").strip().strip("_"),
            s2_venue=cells.get("S2 venue 이름", "").strip().strip("_"),
            citation_min=_parse_int(cells.get("citation_min", "")),
            lookback_months=_parse_int(cells.get("lookback_months", "")),
            field_tag=cells.get("분야", "").strip().strip("_"),
            note=cells.get("메모", "").strip().strip("_"),
        ))
    return rows


def parse_paper_topics_watchlist(text: str) -> list[TopicRow]:
    """Extract active TopicRow entries from a paper-topics markdown watchlist."""
    rows: list[TopicRow] = []
    for cells in _iter_markdown_table_rows(text, WATCHLIST_TABLE_HEADER_TOPICS_KEYS):
        topic = cells.get("주제", "").strip().strip("_")
        if not topic or topic.startswith("예:") or "_예:" in topic:
            continue
        status = (cells.get("status", "").strip().strip("_") or "active").lower()
        if status != "active":
            continue
        strategy = (cells.get("전략", "").strip().strip("_") or "keyword").lower()
        if strategy not in TOPIC_STRATEGIES:
            strategy = "keyword"
        rows.append(TopicRow(
            topic=topic,
            strategy=strategy,
            query=cells.get("쿼리", "").strip().strip("_"),
            citation_min=_parse_int(cells.get("citation_min", "")),
            status=status,
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


def _parse_int(s: str) -> int:
    s = s.strip().strip("_")
    if not s or not re.fullmatch(r"\d+", s):
        return 0
    return int(s)


# --------------------------- Dedup store -----------------------------------

class DedupStore:
    """sqlite-backed canonical_id store, shared with fetch_dev.py.

    The `seen` table carries source_kind so paper rows and the dev layer's
    article rows coexist in one db. Legacy column-less tables are migrated in
    place so whichever layer opens the db first repairs the schema.
    """

    SOURCE_KIND = "paper"

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
            "SELECT 1 FROM seen WHERE canonical_id = ?", (canonical_id,)
        )
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


# --------------------------- API clients -----------------------------------

def _http_get_json(url: str, params: dict | None = None, *, timeout: float = 30.0) -> dict:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} for {url}")
        return json.loads(resp.read().decode("utf-8"))


def fetch_openalex_author_papers(author_id: str, lookback_months: int, citation_min: int,
                                  http_get=_http_get_json) -> list[Paper]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30 * lookback_months)).date().isoformat()
    params = {
        "filter": f"author.id:{author_id},from_publication_date:{cutoff},cited_by_count:>{citation_min - 1}",
        "per-page": 50,
    }
    email = os.environ.get("OPENALEX_EMAIL")
    if email:
        params["mailto"] = email
    data = http_get(OPENALEX_WORKS_URL, params)
    return [_paper_from_openalex(w) for w in data.get("results", [])]


def fetch_openalex_venue_papers(source_id: str, lookback_months: int, citation_min: int,
                                 http_get=_http_get_json) -> list[Paper]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30 * lookback_months)).date().isoformat()
    params = {
        "filter": f"primary_location.source.id:{source_id},from_publication_date:{cutoff},cited_by_count:>{citation_min - 1}",
        "per-page": 50,
    }
    email = os.environ.get("OPENALEX_EMAIL")
    if email:
        params["mailto"] = email
    data = http_get(OPENALEX_WORKS_URL, params)
    return [_paper_from_openalex(w) for w in data.get("results", [])]


def fetch_openalex_topic_papers(query: str, strategy: str, lookback_months: int,
                                 citation_min: int, http_get=_http_get_json) -> list[Paper]:
    """Fetch by free-text query. strategy selects the sort + date/citation policy:
    keyword = relevance within the lookback window; classic = most-cited, no date
    cutoff (foundational); recent = newest first, no citation floor (new papers
    have few cites yet)."""
    params: dict = {"search": query, "per-page": 50}
    filters: list[str] = []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30 * lookback_months)).date().isoformat()
    if strategy == "classic":
        params["sort"] = "cited_by_count:desc"
        filters.append(f"cited_by_count:>{citation_min - 1}")
    elif strategy == "recent":
        params["sort"] = "publication_date:desc"
        filters.append(f"from_publication_date:{cutoff}")
    else:  # keyword
        filters.append(f"from_publication_date:{cutoff}")
        filters.append(f"cited_by_count:>{citation_min - 1}")
    params["filter"] = ",".join(filters)
    email = os.environ.get("OPENALEX_EMAIL")
    if email:
        params["mailto"] = email
    data = http_get(OPENALEX_WORKS_URL, params)
    return [_paper_from_openalex(w) for w in data.get("results", [])]


def _paper_from_openalex(w: dict) -> Paper:
    title = w.get("title") or w.get("display_name") or ""
    authors = [a.get("author", {}).get("display_name", "") for a in w.get("authorships", [])]
    doi = (w.get("doi") or "").replace("https://doi.org/", "")
    venue = ""
    primary = (w.get("primary_location") or {}).get("source") or {}
    venue = primary.get("display_name", "")
    published = w.get("publication_date") or ""
    abstract = _reconstruct_openalex_abstract(w.get("abstract_inverted_index") or {})
    arxiv_id = ""
    for loc in (w.get("locations") or []):
        src = (loc.get("source") or {})
        if src.get("display_name") == "arXiv" and loc.get("landing_page_url"):
            m = re.search(r"arxiv\.org/abs/([\w./-]+)", loc["landing_page_url"])
            if m:
                arxiv_id = m.group(1)
                break
    return Paper(
        title=title,
        authors=[a for a in authors if a],
        doi=doi,
        arxiv_id=arxiv_id,
        venue=venue,
        published_date=published,
        abstract=abstract,
        citation_count=int(w.get("cited_by_count") or 0),
        raw_source="openalex",
    )


def _reconstruct_openalex_abstract(inverted: dict) -> str:
    if not inverted:
        return ""
    positions: list[tuple[int, str]] = []
    for word, idxs in inverted.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort()
    return " ".join(w for _, w in positions)


def fetch_s2_author_papers(author_id: str, lookback_months: int, citation_min: int,
                            http_get=_http_get_json) -> list[Paper]:
    cutoff_year = (datetime.now(timezone.utc) - timedelta(days=30 * lookback_months)).year
    params = {
        "fields": "title,authors,externalIds,year,publicationDate,venue,abstract,citationCount",
        "limit": 50,
    }
    url = S2_AUTHOR_PAPERS_URL.format(author_id=author_id)
    data = http_get(url, params)
    out: list[Paper] = []
    for p in data.get("data", []):
        if int(p.get("citationCount") or 0) < citation_min:
            continue
        if (p.get("year") or 0) < cutoff_year:
            continue
        out.append(_paper_from_s2(p))
    return out


def fetch_s2_venue_papers(venue_name: str, lookback_months: int, citation_min: int,
                           http_get=_http_get_json) -> list[Paper]:
    cutoff_year = (datetime.now(timezone.utc) - timedelta(days=30 * lookback_months)).year
    now_year = datetime.now(timezone.utc).year
    params = {
        "venue": venue_name,
        "year": f"{cutoff_year}-{now_year}",
        "minCitationCount": citation_min,
        "fields": "title,authors,externalIds,year,publicationDate,venue,abstract,citationCount",
        "limit": 50,
    }
    data = http_get(S2_PAPER_SEARCH_URL, params)
    return [_paper_from_s2(p) for p in data.get("data", [])]


def _paper_from_s2(p: dict) -> Paper:
    ext = p.get("externalIds") or {}
    return Paper(
        title=p.get("title") or "",
        authors=[a.get("name", "") for a in (p.get("authors") or [])],
        doi=(ext.get("DOI") or "").lower(),
        arxiv_id=ext.get("ArXiv") or "",
        venue=p.get("venue") or "",
        published_date=p.get("publicationDate") or (str(p.get("year")) + "-01-01" if p.get("year") else ""),
        abstract=p.get("abstract") or "",
        citation_count=int(p.get("citationCount") or 0),
        raw_source="s2",
    )


# --------------------------- Filtering -------------------------------------

def matches_author_position(paper: Paper, pi_name: str, position: str) -> bool:
    """Check pi_name match against paper.authors[position]."""
    if not paper.authors:
        return False
    pi_lower = pi_name.strip().lower()
    last_name = pi_lower.split()[-1] if pi_lower else ""

    def _author_matches(name: str) -> bool:
        nl = name.strip().lower()
        if not nl:
            return False
        # name match: full name or last-name substring
        return pi_lower in nl or (last_name and last_name in nl.split()[-1:])

    if position == "any" or not position:
        return any(_author_matches(a) for a in paper.authors)
    if position == "first":
        return _author_matches(paper.authors[0])
    if position == "last":
        return _author_matches(paper.authors[-1])
    if position == "first_or_last":
        return _author_matches(paper.authors[0]) or _author_matches(paper.authors[-1])
    return False


# --------------------------- Frontmatter writer ----------------------------

def write_paper_md(paper: Paper, inbox_dir: Path, *, dry_run: bool = False) -> Path:
    """Write or dry-run paper-W<isoweek>-<title-slug>.md to inbox_dir. Returns path."""
    canonical = paper.canonical_id()
    week_short = "W" + datetime.now().strftime("%V")
    title_slug = re.sub(r"[^a-z0-9]+", "-", paper.title.lower())[:60].strip("-") or canonical
    target = inbox_dir / f"paper-{week_short}-{title_slug}.md"
    if target.exists():
        return target  # idempotent — leave existing alone
    body = _render_paper_md(paper)
    if not dry_run:
        inbox_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return target


def _render_paper_md(paper: Paper) -> str:
    authors_yaml = "[" + ", ".join(f'"{a}"' for a in paper.authors) + "]" if paper.authors else "[]"
    arxiv_line = f'arxiv_id: "{paper.arxiv_id}"\n' if paper.arxiv_id else ""
    doi_line = f'doi: "{paper.doi}"\n' if paper.doi else ""
    return (
        "---\n"
        "source: arxiv-paper\n"
        f"title: {_yaml_escape(paper.title)}\n"
        f"authors: {authors_yaml}\n"
        f"published_date: {paper.published_date}\n"
        f"venue: {_yaml_escape(paper.venue)}\n"
        f"citation_count: {paper.citation_count}\n"
        f"{arxiv_line}"
        f"{doi_line}"
        "status_file: False\n"
        "---\n\n"
        "## Abstract\n\n"
        f"{paper.abstract.strip() or '(abstract unavailable)'}\n"
    )


def _yaml_escape(s: str) -> str:
    s = (s or "").replace("\n", " ").strip()
    if any(c in s for c in ':"'):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def append_links_to_paper_inbox(written: list[WrittenPaper], *,
                                 inbox_file: Path, today_yyyymmdd: str) -> int:
    """Append one line per newly-written paper to 01Inbox-paper.md (paper-absorb
    input), mirroring the dev scrap line format:

        - YYYYMMDD - [auto-research-paper/<bucket>] <title> <url> (zotero:<KEY>)

    A paper whose url (or title, when url is empty) already appears is skipped.
    Returns the number of lines appended."""
    if not written:
        return 0
    inbox_file.parent.mkdir(parents=True, exist_ok=True)
    existing = inbox_file.read_text(encoding="utf-8") if inbox_file.is_file() else ""
    new_lines: list[str] = []
    for w in written:
        url = w.paper.url()
        dedup_key = url or w.paper.title
        if dedup_key and dedup_key in existing:
            continue
        zot = f" (zotero:{w.zotero_key})" if w.zotero_key else ""
        new_lines.append(
            f"- {today_yyyymmdd} - [auto-research-paper/{w.bucket}] "
            f"{w.paper.title} {url or '(no-url)'}{zot}")
    if not new_lines:
        return 0
    sep = "" if (not existing or existing.endswith("\n")) else "\n"
    with inbox_file.open("a", encoding="utf-8") as f:
        f.write(sep + "\n".join(new_lines) + "\n")
    return len(new_lines)


# --------------------------- Pipeline orchestration ------------------------

def run_pipeline(*,
                 labs: list[LabRow],
                 journals: list[JournalRow],
                 inbox_dir: Path,
                 dedup: DedupStore,
                 dry_run: bool,
                 topics: list[TopicRow] | None = None,
                 openalex_author=fetch_openalex_author_papers,
                 openalex_venue=fetch_openalex_venue_papers,
                 openalex_topic=fetch_openalex_topic_papers,
                 s2_author=fetch_s2_author_papers,
                 s2_venue=fetch_s2_venue_papers) -> dict:
    """Returns counts: {seen_total, new_written, by_source}."""
    counts = {"seen_total": 0, "new_written": 0, "by_source": {"openalex": 0, "s2": 0}}
    written: list[WrittenPaper] = []
    counts["written"] = written

    for topic in topics or []:
        try:
            papers = openalex_topic(topic.effective_query(), topic.strategy,
                                    DEFAULT_LOOKBACK_MONTHS, topic.effective_citation_min())
        except Exception as e:  # surface, do not crash whole run
            print(f"WARN openalex/topic:{topic.slug()}: {e}", file=sys.stderr)
            continue
        for paper in papers:
            _maybe_write(paper, "openalex", f"topic/{topic.slug()}",
                         inbox_dir, dedup, dry_run, counts, written)
        _rate_sleep("openalex")

    for lab in labs:
        for fetch_fn, src_label, author_id in (
            (openalex_author, "openalex", lab.openalex_author_id),
            (s2_author, "s2", lab.s2_author_id),
        ):
            if not author_id:
                continue
            try:
                papers = fetch_fn(author_id, DEFAULT_LOOKBACK_MONTHS, lab.effective_citation_min())
            except Exception as e:  # surface, do not crash whole run
                print(f"WARN {src_label}/{lab.pi_name}: {e}", file=sys.stderr)
                continue
            for paper in papers:
                if not matches_author_position(paper, lab.pi_name, lab.position_filter):
                    continue
                _maybe_write(paper, src_label, f"pi/{lab.pi_name}",
                             inbox_dir, dedup, dry_run, counts, written)
            _rate_sleep(src_label)

    for journal in journals:
        for fetch_fn, src_label, identifier in (
            (openalex_venue, "openalex", journal.openalex_source_id),
            (s2_venue, "s2", journal.s2_venue),
        ):
            if not identifier:
                continue
            try:
                papers = fetch_fn(identifier, journal.effective_lookback_months(),
                                  journal.effective_citation_min())
            except Exception as e:
                print(f"WARN {src_label}/{journal.venue_name}: {e}", file=sys.stderr)
                continue
            for paper in papers:
                _maybe_write(paper, src_label, f"venue/{journal.venue_name}",
                             inbox_dir, dedup, dry_run, counts, written)
            _rate_sleep(src_label)

    return counts


def _maybe_write(paper: Paper, src_label: str, bucket: str, inbox_dir: Path,
                  dedup: DedupStore, dry_run: bool, counts: dict,
                  written: list[WrittenPaper]) -> None:
    canonical = paper.canonical_id()
    counts["seen_total"] += 1
    if dedup.has(canonical):
        return
    path = write_paper_md(paper, inbox_dir, dry_run=dry_run)
    if not dry_run:
        dedup.mark(canonical, src_label, paper.title)
    counts["new_written"] += 1
    counts["by_source"][src_label] = counts["by_source"].get(src_label, 0) + 1
    written.append(WrittenPaper(paper=paper, path=path, bucket=bucket))


def _rate_sleep(src_label: str) -> None:
    delay = OPENALEX_RATE_DELAY if src_label == "openalex" else S2_RATE_DELAY
    time.sleep(delay)


# --------------------------- CLI -------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watchlist-labs", default=str(DEFAULT_WATCHLIST_LABS))
    ap.add_argument("--watchlist-journals", default=str(DEFAULT_WATCHLIST_JOURNALS))
    ap.add_argument("--watchlist-topics", default=str(DEFAULT_WATCHLIST_TOPICS))
    ap.add_argument("--inbox-dir", default=str(DEFAULT_INBOX_DIR))
    ap.add_argument("--inbox-list", default=str(DEFAULT_INBOX_LIST))
    ap.add_argument("--dedup-db", default=str(DEFAULT_DEDUP_DB))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    labs_path = Path(args.watchlist_labs)
    journals_path = Path(args.watchlist_journals)
    if not labs_path.exists():
        print(f"ERROR: labs watchlist missing: {labs_path}", file=sys.stderr)
        return 2
    if not journals_path.exists():
        print(f"ERROR: journals watchlist missing: {journals_path}", file=sys.stderr)
        return 2

    labs = parse_labs_watchlist(labs_path.read_text(encoding="utf-8"))
    journals = parse_journals_watchlist(journals_path.read_text(encoding="utf-8"))

    # Topics watchlist is optional — a missing file just means no topic fetches.
    topics_path = Path(args.watchlist_topics)
    topics = (parse_paper_topics_watchlist(topics_path.read_text(encoding="utf-8"))
              if topics_path.exists() else [])

    dedup = DedupStore(Path(args.dedup_db))
    try:
        counts = run_pipeline(
            labs=labs,
            journals=journals,
            topics=topics,
            inbox_dir=Path(args.inbox_dir),
            dedup=dedup,
            dry_run=args.dry_run,
        )
    finally:
        dedup.close()

    linked = 0
    if not args.dry_run:
        linked = append_links_to_paper_inbox(
            counts["written"],
            inbox_file=Path(args.inbox_list),
            today_yyyymmdd=datetime.now().strftime("%Y%m%d"),
        )

    print(f"OK. seen={counts['seen_total']} new={counts['new_written']} "
          f"openalex={counts['by_source'].get('openalex', 0)} "
          f"s2={counts['by_source'].get('s2', 0)} "
          f"linked={linked} dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
