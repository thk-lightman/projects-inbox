"""Language-aware markdown cleanup and sentence tokenization."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


def _strip_markdown_noise(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"~~~.*?~~~", "", text, flags=re.DOTALL)
    text = re.sub(r"\[\[([^|\]]*\|)?([^\]]+)\]\]", r"\2", text)
    cleaned: list[str] = []
    for line in text.splitlines():
        line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)
        line = re.sub(r"^\s*[-*+]\s+", "", line)
        line = re.sub(r"^\s*\d+[.)]\s+", "", line)
        if re.match(r"^\s*\|", line):
            continue
        cleaned.append(line.strip())
    return "\n".join(cleaned)


def _normalize_sentence(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def tokenize_sentences(text: str, lang: str) -> list[str]:
    """Split cleaned markdown into thresholded Korean or English sentences."""

    cleaned = _strip_markdown_noise(text)
    if lang == "kr":
        pieces = re.split(r"(?<=[.?!])\s+|(?<=[다요임음]\.)\s*|\n(?=[가-힣])", cleaned)
        sentences = [_normalize_sentence(piece) for piece in pieces]
        return [s for s in sentences if len(re.findall(r"[가-힣]", s)) >= 5]
    if lang == "en":
        pieces = re.split(r"(?<=[.?!])\s+(?=[A-Z\"'])|(?<=[?!])\s*", cleaned)
        sentences = [_normalize_sentence(piece) for piece in pieces]
        return [s for s in sentences if len(re.findall(r"[A-Za-z]", s)) >= 20]
    raise ValueError("lang must be 'kr' or 'en'")


def sentence_id(text: str, source_path: Path) -> str:
    """Return a stable SHA-256 id for normalized text and source path."""

    normalized = _normalize_sentence(text).casefold()
    payload = f"{normalized}\0{source_path.as_posix()}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
