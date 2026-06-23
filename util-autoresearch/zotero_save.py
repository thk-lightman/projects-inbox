#!/usr/bin/env python3
"""Zotero push for a vault paper-*.md or learning-*.md note.

Usage:
    python3 zotero_save.py <vault-md-path>
    python3 zotero_save.py --doi 10.1234/abc.def --vault /vault
    python3 zotero_save.py --arxiv 1510.04342 --vault /vault

Env:
    ZOTERO_API_KEY    Required.
    ZOTERO_USER_ID    Required (numeric user id from zotero.org/settings/keys).
    ZOTERO_COLLECTION Optional 8-char collection key. None = unfiled.

Behavior:
    - Reads vault md frontmatter (yaml between --- fences).
    - Creates Zotero item with metadata + tags.
    - Downloads PDF (arxiv preferred) + attaches.
    - Writes zotero_key back to vault md frontmatter.
    - Idempotent: skips push if frontmatter already has zotero_key.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import urllib.request
from pathlib import Path

import yaml

try:
    from pyzotero import zotero
except ImportError:
    zotero = None  # resolved lazily in push_to_zotero; import must stay side-effect-free


# --------------------------- Frontmatter helpers ---------------------------

def split_frontmatter(text: str):
    """Return (fm_dict, fm_text, body)."""
    if not (text.startswith("---\n") or text.startswith("---\r\n")):
        return {}, "", text
    after = text[4:]
    close = after.find("\n---")
    if close == -1:
        return {}, "", text
    fm_text = after[:close]
    body = after[close + len("\n---"):]
    try:
        data = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        data = {}
    return data, fm_text, body


def write_frontmatter(path: Path, fm: dict, body: str) -> None:
    fm_text = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).strip()
    path.write_text(f"---\n{fm_text}\n---{body}", encoding="utf-8")


# --------------------------- Zotero item shape -----------------------------

def build_zotero_item(fm: dict) -> dict:
    """Convert vault frontmatter into Zotero item template."""
    is_preprint = bool(fm.get("arxiv_id")) and not fm.get("doi")
    item_type = "preprint" if is_preprint else "journalArticle"
    creators = []
    for a in (fm.get("authors") or []):
        parts = a.strip().rsplit(" ", 1)
        if len(parts) == 2:
            creators.append({"creatorType": "author", "firstName": parts[0], "lastName": parts[1]})
        else:
            creators.append({"creatorType": "author", "name": a.strip()})

    item = {
        "itemType": item_type,
        "title": fm.get("title", ""),
        "creators": creators,
        # YAML parses an unquoted `published_date: 2009-01-01` as a date object,
        # which is not JSON-serializable for the Zotero API — coerce to str.
        "date": str(fm.get("published_date") or ""),
        "abstractNote": _extract_abstract_from_body(fm, ""),
        "tags": [{"tag": t} for t in (fm.get("tags") or []) + ["from-vault"]],
    }
    if item_type == "preprint":
        item["repository"] = "arXiv"
        item["archiveID"] = fm.get("arxiv_id", "")
        item["url"] = f"https://arxiv.org/abs/{fm['arxiv_id']}" if fm.get("arxiv_id") else ""
    else:
        item["publicationTitle"] = fm.get("venue", "")
        item["DOI"] = fm.get("doi", "")
        item["url"] = f"https://doi.org/{fm['doi']}" if fm.get("doi") else ""
    return item


def _extract_abstract_from_body(fm: dict, body: str) -> str:
    """Prefer frontmatter abstract field; fall back to body ## Abstract section."""
    if fm.get("abstract"):
        return fm["abstract"]
    m = re.search(r"##\s+Abstract\s*\n+(.*?)(?:\n##|\Z)", body, re.DOTALL)
    return m.group(1).strip() if m else ""


# --------------------------- PDF fetch -------------------------------------

def fetch_pdf(arxiv_id: str | None, doi: str | None) -> Path | None:
    """Download paper PDF to tempfile. Returns Path or None."""
    candidates = []
    if arxiv_id:
        candidates.append(f"https://arxiv.org/pdf/{arxiv_id}")
    for url in candidates:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "project-autoresearch/0.1"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                if resp.status != 200:
                    continue
                tmp = Path(tempfile.mktemp(suffix=".pdf"))
                tmp.write_bytes(resp.read())
                if tmp.stat().st_size > 1024:
                    return tmp
        except Exception as e:
            print(f"WARN pdf fetch {url}: {e}", file=sys.stderr)
    return None


# --------------------------- Main pipeline ---------------------------------

def push_to_zotero(vault_md_path: Path, *,
                    api_key: str, user_id: str,
                    collection: str | None = None,
                    pdf_fetcher=fetch_pdf) -> dict:
    """Read vault md, push to Zotero, write key back. Returns result dict."""
    if not vault_md_path.exists():
        return {"status": "error", "reason": f"vault md not found: {vault_md_path}"}

    text = vault_md_path.read_text(encoding="utf-8")
    fm, _fm_text, body = split_frontmatter(text)

    if fm.get("zotero_key"):
        return {"status": "skipped", "reason": "already has zotero_key", "key": fm["zotero_key"]}

    if zotero is None:
        print("ERROR: pyzotero not installed. pip install pyzotero>=1.5", file=sys.stderr)
        sys.exit(2)

    zot = zotero.Zotero(user_id, "user", api_key)
    item = build_zotero_item(fm)
    if collection:
        item["collections"] = [collection]

    resp = zot.create_items([item])
    successful = resp.get("successful") or {}
    if not successful:
        return {"status": "error", "reason": "create_items returned no success",
                "raw": resp}
    key = list(successful.values())[0]["key"]

    # PDF attach
    pdf = pdf_fetcher(fm.get("arxiv_id"), fm.get("doi"))
    pdf_attached = False
    if pdf:
        try:
            zot.attachment_simple([str(pdf)], parentid=key)
            pdf_attached = True
        except Exception as e:
            print(f"WARN pdf attach: {e}", file=sys.stderr)
        finally:
            pdf.unlink(missing_ok=True)

    # Write key back
    fm["zotero_key"] = key
    write_frontmatter(vault_md_path, fm, body)

    return {"status": "created", "key": key, "pdf_attached": pdf_attached}


# --------------------------- CLI -------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("vault_md", nargs="?", help="path to vault paper-*.md or learning-*.md")
    ap.add_argument("--vault", default=os.environ.get("VAULT_ROOT", "/vault"))
    ap.add_argument("--doi", help="lookup vault md by DOI")
    ap.add_argument("--arxiv", help="lookup vault md by arxiv id")
    args = ap.parse_args()

    api_key = os.environ.get("ZOTERO_API_KEY")
    user_id = os.environ.get("ZOTERO_USER_ID")
    if not api_key or not user_id:
        print("ERROR: ZOTERO_API_KEY and ZOTERO_USER_ID env required", file=sys.stderr)
        return 2

    if args.vault_md:
        md_path = Path(args.vault_md)
        if not md_path.is_absolute():
            md_path = Path(args.vault) / "00 Get Things Done/03Inbox/auto" / md_path
    elif args.doi:
        md_path = _find_by_canonical(Path(args.vault), doi=args.doi)
    elif args.arxiv:
        md_path = _find_by_canonical(Path(args.vault), arxiv=args.arxiv)
    else:
        print("ERROR: provide vault_md path, --doi, or --arxiv", file=sys.stderr)
        return 2

    if not md_path or not md_path.exists():
        print(f"ERROR: vault md not found: {md_path}", file=sys.stderr)
        return 2

    result = push_to_zotero(
        md_path,
        api_key=api_key,
        user_id=user_id,
        collection=os.environ.get("ZOTERO_COLLECTION") or None,
    )

    print(f"status={result['status']} key={result.get('key', '')} "
          f"pdf={result.get('pdf_attached', False)} "
          f"reason={result.get('reason', '')}")
    return 0 if result["status"] in ("created", "skipped") else 1


def _find_by_canonical(vault: Path, *, doi: str | None = None, arxiv: str | None = None) -> Path | None:
    """Find vault md by canonical_id pattern."""
    auto_dir = vault / "00 Get Things Done/03Inbox/auto"
    if not auto_dir.is_dir():
        return None
    if doi:
        canonical = re.sub(r"[^a-z0-9]+", "-", doi.lower().replace("https://doi.org/", "")).strip("-")
    elif arxiv:
        canonical = f"arxiv-{arxiv}"
    else:
        return None
    for prefix in ("paper-", "learning-"):
        candidate = auto_dir / f"{prefix}{canonical}.md"
        if candidate.exists():
            return candidate
    return None


if __name__ == "__main__":
    sys.exit(main())
