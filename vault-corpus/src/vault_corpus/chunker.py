"""Chunking utilities for vault notes.

Parses YAML front-matter and splits a note body into ``##``-bounded chunks
(with ``###`` tracked as nested headings inside the parent ``##`` section).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_FRONTMATTER_DELIM = "---"

# Fenced code block opener/closer: 3+ backticks or tildes, optional info string.
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")

# ``##`` or ``###`` heading (not ``#`` h1, not ``####``+). Optional trailing ``#``s.
_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*#*\s*$")


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Extract YAML front-matter from a markdown note.

    Returns a ``(metadata, body)`` tuple. ``metadata`` is an empty dict when the
    note has no front-matter or the front-matter block fails to parse as a YAML
    mapping. ``body`` is the markdown content with the front-matter block (and
    its closing delimiter line) stripped — including the trailing newline that
    separates the delimiter from the body, when present.

    A valid front-matter block requires the very first line to be ``---``
    (optionally with trailing whitespace) and a matching ``---`` terminator on
    a later line. Anything before the opening delimiter disqualifies the note
    as having front-matter, matching Obsidian's behavior.
    """
    if not text:
        return {}, text

    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n").strip() != _FRONTMATTER_DELIM:
        return {}, text

    closing_index: int | None = None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n").strip() == _FRONTMATTER_DELIM:
            closing_index = i
            break

    if closing_index is None:
        return {}, text

    yaml_block = "".join(lines[1:closing_index])
    try:
        parsed = yaml.safe_load(yaml_block) if yaml_block.strip() else {}
    except yaml.YAMLError:
        parsed = {}

    metadata = parsed if isinstance(parsed, dict) else {}
    body = "".join(lines[closing_index + 1 :])
    return metadata, body


def split_by_headings(body: str) -> list[tuple[list[str], str]]:
    """Split a note body into ``##``-bounded chunks.

    Returns an ordered list of ``(heading_chain, chunk_body)`` tuples.

    Boundary rules:

    * Only ``##`` and ``###`` ATX headings act as boundaries. ``#`` (h1) and
      ``####``+ are treated as plain body content.
    * A ``##`` heading opens a new top-level chunk; ``heading_chain`` becomes
      ``[h2_title]``.
    * A ``###`` heading opens a nested chunk; ``heading_chain`` becomes
      ``[current_h2_title, h3_title]``. A leading ``###`` with no preceding
      ``##`` yields ``[h3_title]``.
    * Lines inside fenced code blocks (``` ``` ``` or ``~~~``) are never
      treated as headings.
    * Content appearing before the first heading becomes a leading chunk with
      an empty ``heading_chain`` (skipped when that preamble is whitespace
      only).
    * A body with zero ``##``/``###`` headings returns a single
      ``([], body)`` chunk so callers can always treat the note as at least
      one chunk.
    * Each chunk body includes its own heading line plus everything up to
      the next boundary, preserving original line terminators.
    """
    if not body:
        return [([], "")]

    lines = body.splitlines(keepends=True)
    in_fence = False
    boundaries: list[tuple[int, int, str]] = []

    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            boundaries.append((i, level, title))

    if not boundaries:
        return [([], body)]

    chunks: list[tuple[list[str], str]] = []

    first_idx = boundaries[0][0]
    if first_idx > 0:
        preamble = "".join(lines[:first_idx])
        if preamble.strip():
            chunks.append(([], preamble))

    current_h2: str | None = None
    for j, (idx, level, title) in enumerate(boundaries):
        end = boundaries[j + 1][0] if j + 1 < len(boundaries) else len(lines)
        chunk_body = "".join(lines[idx:end])
        if level == 2:
            current_h2 = title
            chain = [title]
        else:
            chain = [current_h2, title] if current_h2 else [title]
        chunks.append((chain, chunk_body))

    return chunks


def compute_chunk_id(
    source_path: Path, heading_chain: list[str], body: str
) -> str:
    """Compute a stable chunk identifier.

    Returns the SHA-256 hex digest of ``f"{source_path}\\n{heading_chain}\\n{body}"``.

    The hash is content-derived so the same chunk produces the same id across
    runs and DB engines — required for pgvector migration to skip re-embedding.
    Any change to ``source_path``, ``heading_chain``, or ``body`` yields a
    different id.
    """
    payload = f"{source_path}\n{heading_chain}\n{body}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Chunk:
    """A single heading-bounded segment of a vault note.

    Attributes:
        source_path: Path to the source vault note (Korean markdown file).
        heading_chain: Ordered ``##``/``###`` titles leading to this chunk.
            Empty list for notes without ``##``/``###`` headings, or for the
            preamble that precedes the first heading.
        body: Markdown chunk body. Includes the heading line(s) for heading
            chunks. Excludes the YAML front-matter block.
        lang: Language tag; ``"ko"`` for source chunks, ``"en"`` for
            translated chunks. Default ``"ko"``.
        chunk_id: SHA-256 of ``f"{source_path}\\n{heading_chain}\\n{body}"``.
            Stable across runs and DB engines so KR↔EN can share the same id
            and pgvector migration needs no re-embedding.
        frontmatter: Parsed YAML front-matter mapping from the source note.
            The same mapping is attached to every chunk produced from a given
            note so downstream code can route by tags / status / etc.
    """

    source_path: Path
    heading_chain: list[str]
    body: str
    chunk_id: str
    lang: str = "ko"
    frontmatter: dict[str, Any] = field(default_factory=dict)


def chunk_note(path: Path, text: str) -> list[Chunk]:
    """Parse a vault note and return its ``Chunk`` list.

    Composes :func:`parse_frontmatter`, :func:`split_by_headings`, and
    :func:`compute_chunk_id`:

    1. Front-matter (between leading ``---`` lines) is extracted and attached
       to every resulting chunk as ``frontmatter`` metadata; it is **never**
       part of any chunk ``body``.
    2. The remaining body is split into ``##``/``###`` chunks. Notes with no
       such headings yield exactly one chunk covering the entire post-front-
       matter body; the ``heading_chain`` is an empty list in that case.
    3. Each chunk gets a content-derived ``chunk_id`` (see
       :func:`compute_chunk_id`).

    Edge case: a note that contains *only* front-matter (no body) yields zero
    chunks — there is nothing chunkable, and emitting an empty-body chunk
    would waste a translation+embedding round-trip.
    """
    metadata, body = parse_frontmatter(text)
    segments = split_by_headings(body)

    if not segments:
        return []

    # When the note is entirely front-matter (or empty after stripping it),
    # split_by_headings returns ``[([], "")]``. Skip that empty placeholder
    # so we never store a content-less chunk.
    only_segment = segments[0]
    if len(segments) == 1 and not only_segment[0] and not only_segment[1]:
        return []

    chunks: list[Chunk] = []
    for chain, chunk_body in segments:
        cid = compute_chunk_id(path, chain, chunk_body)
        chunks.append(
            Chunk(
                source_path=path,
                heading_chain=list(chain),
                body=chunk_body,
                chunk_id=cid,
                lang="ko",
                frontmatter=dict(metadata),
            )
        )
    return chunks
