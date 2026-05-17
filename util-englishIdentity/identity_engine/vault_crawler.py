"""Vault Crawler — delta tracking + Korean sentence tokenizer."""
import hashlib
import re
from pathlib import Path
from typing import Generator

from .config import resolve_folder_key
from .database import Database

# Korean sentence boundary: end on .?! or Korean sentence-final particles + punctuation
_KR_SENTENCE_RE = re.compile(
    r"[^.!?\n。？！]+[.!?\n。？！]+"
    r"|[^.!?\n。？！]+$",
    re.MULTILINE,
)

# Strip markdown frontmatter, headings, code blocks, links, tags
_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)
_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`[^`]+`")
_MD_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]|\[([^\]]+)\]\([^)]+\)")
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_DATAVIEW_RE = re.compile(r"```dataview[\s\S]*?```")
_TAG_RE = re.compile(r"#\w+")
_WHITESPACE_RE = re.compile(r"\s{2,}")

# Minimum Korean character ratio to consider a line Korean
_KR_CHAR_RE = re.compile(r"[가-힣]")
_MIN_KR_LENGTH = 5

# English heuristic: line must have at least N alphabetic chars and almost no hangul
_EN_ALPHA_RE = re.compile(r"[A-Za-z]")
_MIN_EN_ALPHA = 20


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _clean_markdown(text: str) -> str:
    text = _FRONTMATTER_RE.sub("", text)
    text = _DATAVIEW_RE.sub("", text)
    text = _CODE_BLOCK_RE.sub("", text)
    text = _INLINE_CODE_RE.sub("", text)
    text = _MD_LINK_RE.sub(r"\1\2", text)
    text = _MD_HEADING_RE.sub("", text)
    text = _TAG_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


def _is_korean(sentence: str) -> bool:
    kr_chars = len(_KR_CHAR_RE.findall(sentence))
    return kr_chars >= _MIN_KR_LENGTH


def _tokenize_korean(text: str) -> list[str]:
    """Split cleaned text into Korean sentences."""
    sentences = []
    for line in text.splitlines():
        line = line.strip()
        if not line or not _is_korean(line):
            continue
        parts = re.split(r"(?<=[.!?。？！])\s+", line)
        for part in parts:
            part = part.strip()
            if _is_korean(part):
                sentences.append(part)
    return sentences


def _is_english(sentence: str) -> bool:
    alpha = len(_EN_ALPHA_RE.findall(sentence))
    hangul = len(_KR_CHAR_RE.findall(sentence))
    return alpha >= _MIN_EN_ALPHA and hangul == 0


def _tokenize_english(text: str) -> list[str]:
    """Split cleaned text into English sentences."""
    sentences = []
    for line in text.splitlines():
        line = line.strip()
        if not line or not _is_english(line):
            continue
        parts = re.split(r"(?<=[.!?])\s+", line)
        for part in parts:
            part = part.strip()
            if _is_english(part):
                sentences.append(part)
    return sentences


_TOKENIZERS = {"kr": _tokenize_korean, "en": _tokenize_english}


def _resolve_paths(vault_path: Path, paths: list[str]) -> list[Path]:
    """Expand input paths to a sorted unique list of .md files inside the vault.

    Each entry may be:
      - vault-relative path (e.g. "01 Command Center/foo.md" or "01 Command Center")
      - absolute path inside the vault
    Files: included as-is. Directories: rglob *.md.
    Anything outside the vault raises ValueError.
    """
    vault_resolved = vault_path.resolve()
    seen: set[Path] = set()
    out: list[Path] = []
    for raw in paths:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = vault_path / raw
        resolved = candidate.resolve()
        try:
            resolved.relative_to(vault_resolved)
        except ValueError as exc:
            raise ValueError(f"path outside vault: {raw}") from exc
        if not resolved.exists():
            raise ValueError(f"path does not exist: {raw}")
        if resolved.is_file():
            if resolved.suffix == ".md" and resolved not in seen:
                seen.add(resolved)
                out.append(resolved)
        else:
            for md_file in sorted(resolved.rglob("*.md")):
                if md_file not in seen:
                    seen.add(md_file)
                    out.append(md_file)
    return out


def crawl_vault(
    vault_path: Path,
    db: Database,
    paths: list[str],
    lang: str = "kr",
    force: bool = False,
    resume: bool = True,
) -> Generator[dict, None, None]:
    """
    Yield dicts: {kr_text, source_file, folder_key, lang} for new/changed sentences.

    `lang` controls which tokenizer is used:
        kr → only lines with >=5 hangul chars
        en → only lines with >=20 ASCII alphabetic chars and no hangul

    Scope is mandatory: caller must provide `paths` (files and/or directories
    inside the vault). Whole-vault crawling is intentionally not supported —
    the goal is to process hand-picked content, not bulk-process AI text.
    """
    if not paths:
        raise ValueError("crawl_vault requires explicit paths (file or folder)")
    if lang not in _TOKENIZERS:
        raise ValueError(f"unsupported lang: {lang!r}. expected one of {list(_TOKENIZERS)}")
    tokenize = _TOKENIZERS[lang]

    md_files = _resolve_paths(vault_path, paths)
    if not md_files:
        return

    checkpoint = db.load_checkpoint() if resume else None
    last_file = checkpoint["last_file"] if checkpoint else None
    passed_checkpoint = last_file is None

    for md_file in md_files:
        rel_path = str(md_file.relative_to(vault_path))

        if not passed_checkpoint:
            if rel_path == last_file:
                passed_checkpoint = True
            else:
                continue

        current_hash = _file_hash(md_file)
        if not force and db.get_file_hash(rel_path) == current_hash:
            continue

        folder_key = resolve_folder_key(rel_path)
        try:
            raw = md_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        cleaned = _clean_markdown(raw)
        sentences = tokenize(cleaned)

        for sentence in sentences:
            yield {
                "kr_text": sentence,
                "source_file": rel_path,
                "folder_key": folder_key,
                "lang": lang,
            }

        db.upsert_file_hash(rel_path, current_hash)
        db.save_checkpoint(rel_path, len(sentences))
