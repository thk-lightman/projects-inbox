"""Scoped scanner for the two opt-in IDENTITY note folders."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

IDENTITY_RELATIVE = Path("04 PracticeMakesPerfect") / "IDENTITY"
SCOPED_FOLDERS = ("kr-self", "en-ref")


@dataclass(frozen=True)
class RawNote:
    """Markdown note read from one of the scoped IDENTITY folders."""

    path: Path
    bucket: str
    lang: str
    title: str
    tags: tuple[str, ...]
    date: str | None
    text: str
    fingerprint: str


def _has_hidden_segment(path: Path) -> bool:
    return any(part.startswith(".") for part in path.parts)


def _identity_dir(vault_root: Path, bucket: str) -> Path:
    return (vault_root / IDENTITY_RELATIVE / bucket).expanduser().resolve()


def _is_confined(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def list_identity_files(vault_root: Path) -> dict[str, list[Path]]:
    """Return markdown files under IDENTITY/kr-self and IDENTITY/en-ref only."""

    vault_root = vault_root.expanduser().resolve()
    result: dict[str, list[Path]] = {"kr-self": [], "en-ref": []}
    for bucket in SCOPED_FOLDERS:
        scoped_root = _identity_dir(vault_root, bucket)
        if not scoped_root.is_dir():
            continue
        files: list[Path] = []
        for path in scoped_root.rglob("*.md"):
            if _has_hidden_segment(path.relative_to(scoped_root)):
                continue
            if _is_confined(path, scoped_root):
                files.append(path)
        result[bucket] = sorted(files)
    return result


def assert_scoped_identity_path(path: Path, vault_root: Path) -> str:
    """Return the note bucket if path is inside an allowed IDENTITY folder."""

    resolved = path.expanduser().resolve()
    for bucket in SCOPED_FOLDERS:
        if _is_confined(resolved, _identity_dir(vault_root, bucket)):
            return bucket
    raise ValueError(f"path is outside scoped IDENTITY folders: {path}")


def file_fingerprint(path: Path) -> str:
    """Return a stable SHA-256 content fingerprint."""

    import hashlib

    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    raw = text[4:end]
    body = text[end + 4 :].lstrip("\n")
    data = yaml.safe_load(raw) or {}
    return (data if isinstance(data, dict) else {}), body


def _extract_title(body: str, path: Path, meta: dict) -> str:
    if isinstance(meta.get("title"), str) and meta["title"].strip():
        return meta["title"].strip()
    for line in body.splitlines():
        match = re.match(r"^\s*#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return path.stem


def _extract_tags(text: str, meta: dict) -> tuple[str, ...]:
    tags: set[str] = set()
    raw_tags = meta.get("tags", [])
    if isinstance(raw_tags, str):
        raw_tags = [raw_tags]
    if isinstance(raw_tags, list):
        for tag in raw_tags:
            if isinstance(tag, str):
                tags.add(tag.lstrip("#").strip())
    for tag in re.findall(r"(?<!\w)#([A-Za-z0-9_/-]+)", text):
        tags.add(tag)
    return tuple(sorted(t for t in tags if t))


def _extract_date(path: Path, meta: dict) -> str | None:
    value = meta.get("date")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    match = re.search(r"\d{4}-\d{2}-\d{2}", path.name)
    return match.group(0) if match else None


def parse_note(path: Path, bucket: str) -> RawNote:
    """Parse one markdown file into note metadata and body text."""

    text = path.read_text(encoding="utf-8")
    meta, body = _frontmatter(text)
    lang = "kr" if bucket == "kr-self" else "en"
    return RawNote(
        path=path,
        bucket=bucket,
        lang=lang,
        title=_extract_title(body, path, meta),
        tags=_extract_tags(text, meta),
        date=_extract_date(path, meta),
        text=body,
        fingerprint=file_fingerprint(path),
    )


def scan_notes(vault_root: Path) -> list[RawNote]:
    """Scan both scoped folders and return parsed RawNote objects."""

    notes: list[RawNote] = []
    for bucket, paths in list_identity_files(vault_root).items():
        notes.extend(parse_note(path, bucket) for path in paths)
    return notes
