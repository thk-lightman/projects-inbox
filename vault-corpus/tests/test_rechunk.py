"""Tests for ``rechunk_changed_file`` (Sub-AC 6.4.2).

Verifies the delta-build helper that re-parses a single changed markdown
file. The fixture markdown is chosen to exercise the three chunk shapes
``rechunk_changed_file`` must handle correctly:

* a ``##``-only top-level heading
* a ``###`` nested heading under that ``##``
* a leading preamble (content before the first heading)

For every produced chunk the test asserts BOTH the exact ``chunk_id`` and
the exact ``body``. The ids are re-computed inline with
:func:`compute_chunk_id` rather than hard-coded hex strings so that any
intentional change to the id formula (or to the chunker output) surfaces
in this test as a clear miscompare rather than a silent "expected hash
changed, just paste the new one" patch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vault_corpus.chunker import Chunk, compute_chunk_id
from vault_corpus.pipeline import rechunk_changed_file


# ---------------------------------------------------------------------------
# Fixture markdown
# ---------------------------------------------------------------------------


_FIXTURE_MD = (
    "---\n"
    "title: 변경된 노트\n"
    "tags: [delta-test]\n"
    "---\n"
    "서문 단락. 첫 번째 헤딩 이전 내용.\n"
    "\n"
    "## 목표\n"
    "이번 달에 달성할 목표를 적는다.\n"
    "\n"
    "### 세부 목표\n"
    "세부 항목 1.\n"
    "세부 항목 2.\n"
)


# Expected chunk shapes, in the order ``split_by_headings`` emits them.
# Each tuple is ``(heading_chain, body)``. ``body`` is the byte-exact
# substring the chunker should hand back — including the heading line and
# its trailing newline(s).
_EXPECTED_CHUNKS: list[tuple[list[str], str]] = [
    ([], "서문 단락. 첫 번째 헤딩 이전 내용.\n\n"),
    ([" 목표"], "## 목표\n이번 달에 달성할 목표를 적는다.\n\n"),
    (
        [" 목표", " 세부 목표"],
        "### 세부 목표\n세부 항목 1.\n세부 항목 2.\n",
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_fixture(tmp_path: Path) -> Path:
    """Write the fixture markdown to ``tmp_path`` and return the file path."""
    path = tmp_path / "delta-note.md"
    path.write_text(_FIXTURE_MD, encoding="utf-8")
    return path


def _normalize_chain(chain: list[str]) -> list[str]:
    """Trim leading/trailing whitespace from every chain element.

    ``split_by_headings`` strips the heading title before stashing it in
    ``heading_chain``, so our expected fixture must match that exact
    representation. The constants above intentionally include the leading
    space (``" 목표"``) only to make the fixture self-documenting; this
    helper applies the same ``strip()`` the chunker applies internally so
    the test reads naturally without coupling to whitespace minutiae.
    """
    return [c.strip() for c in chain]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_rechunk_changed_file_returns_chunks_with_expected_ids_and_bodies(
    tmp_path: Path,
) -> None:
    """All three fixture chunks come back with matching ids AND bodies."""
    path = _write_fixture(tmp_path)

    chunks = rechunk_changed_file(path)

    assert isinstance(chunks, list)
    assert len(chunks) == len(_EXPECTED_CHUNKS)

    for actual, (expected_chain_raw, expected_body) in zip(
        chunks, _EXPECTED_CHUNKS, strict=True
    ):
        assert isinstance(actual, Chunk)
        expected_chain = _normalize_chain(expected_chain_raw)

        # 1. Body matches byte-for-byte.
        assert actual.body == expected_body, (
            f"body mismatch for chain={expected_chain}:\n"
            f"  expected={expected_body!r}\n  actual={actual.body!r}"
        )

        # 2. Heading chain matches exactly.
        assert actual.heading_chain == expected_chain

        # 3. chunk_id matches the canonical formula. Recomputing here (vs.
        #    pasting a hex constant) means a deliberate id-formula change
        #    will fail with a meaningful diff instead of "hex string X
        #    changed to Y".
        expected_id = compute_chunk_id(path, expected_chain, expected_body)
        assert actual.chunk_id == expected_id

        # 4. Source path and language are carried through unchanged.
        assert actual.source_path == path
        assert actual.lang == "ko"

        # 5. Front-matter is attached to every chunk (delta-build callers
        #    rely on this when routing by tags/status).
        assert actual.frontmatter == {
            "title": "변경된 노트",
            "tags": ["delta-test"],
        }


def test_rechunk_changed_file_is_idempotent(tmp_path: Path) -> None:
    """Calling twice on the same unchanged file yields identical chunk_ids.

    Pins the property the delta-build loop depends on: re-running on an
    unchanged file produces the same ids, so the existing-id set lookup
    cleanly reports "skip". A non-deterministic chunker would silently
    re-translate the whole vault on every build.
    """
    path = _write_fixture(tmp_path)

    first = rechunk_changed_file(path)
    second = rechunk_changed_file(path)

    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]
    assert [c.body for c in first] == [c.body for c in second]


def test_rechunk_changed_file_honors_injected_read_and_chunker(
    tmp_path: Path,
) -> None:
    """``read`` and ``chunker`` overrides are forwarded without going to disk.

    Guards the test-injection seam used by higher-level orchestration
    tests so they can drive ``rechunk_changed_file`` without writing
    temporary files. Also confirms the function does not silently shadow
    its arguments with the module-level defaults.
    """
    sentinel_path = Path("/non/existent/path/that/must/not/be/read.md")
    captured: dict[str, object] = {}

    def fake_read(p: Path) -> str:
        captured["read_path"] = p
        return "## injected\nfake body\n"

    def fake_chunker(p: Path, text: str) -> list[Chunk]:
        captured["chunker_path"] = p
        captured["chunker_text"] = text
        return [
            Chunk(
                source_path=p,
                heading_chain=["injected"],
                body=text,
                chunk_id="deadbeef",
                lang="ko",
                frontmatter={},
            )
        ]

    chunks = rechunk_changed_file(
        sentinel_path, read=fake_read, chunker=fake_chunker
    )

    assert captured["read_path"] == sentinel_path
    assert captured["chunker_path"] == sentinel_path
    assert captured["chunker_text"] == "## injected\nfake body\n"
    assert len(chunks) == 1
    assert chunks[0].chunk_id == "deadbeef"


def test_rechunk_changed_file_raises_oserror_for_missing_path(
    tmp_path: Path,
) -> None:
    """Missing files surface as ``OSError`` so the caller can branch on it.

    Delta-build orchestrator distinguishes ``M`` (modified) from ``D``
    (deleted) via the git-diff status letter — it should never call
    ``rechunk_changed_file`` for a deleted path. If it does, we want a
    loud error, not a silent empty list that would look like "file is
    intentionally chunk-less".
    """
    missing = tmp_path / "does-not-exist.md"

    with pytest.raises(OSError):
        rechunk_changed_file(missing)


def test_rechunk_changed_file_returns_empty_for_front_matter_only_note(
    tmp_path: Path,
) -> None:
    """Front-matter-only notes contribute zero chunks (matches ``chunk_note``)."""
    path = tmp_path / "fm-only.md"
    path.write_text("---\ntitle: 빈 노트\n---\n", encoding="utf-8")

    assert rechunk_changed_file(path) == []
