from __future__ import annotations

from pathlib import Path

import pytest

from identity_corpus.scanner import assert_scoped_identity_path, list_identity_files, scan_notes


def test_list_identity_files_is_confined_to_two_folders(tmp_path: Path) -> None:
    identity = tmp_path / "04 PracticeMakesPerfect" / "IDENTITY"
    allowed_kr = identity / "kr-self" / "me.md"
    allowed_en = identity / "en-ref" / "ref.md"
    outside = tmp_path / "00 Get Things Done" / "task.md"
    hidden = identity / "kr-self" / ".hidden" / "x.md"
    for path in (allowed_kr, allowed_en, outside, hidden):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Title\nbody", encoding="utf-8")

    files = list_identity_files(tmp_path)

    assert files == {"kr-self": [allowed_kr], "en-ref": [allowed_en]}
    with pytest.raises(ValueError):
        assert_scoped_identity_path(outside, tmp_path)


def test_scan_notes_extracts_metadata(tmp_path: Path) -> None:
    path = tmp_path / "04 PracticeMakesPerfect" / "IDENTITY" / "kr-self" / "2026-01-02 note.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ntags: [identity, voice]\n---\n# 내 제목\n본문 #inline", encoding="utf-8")

    [note] = scan_notes(tmp_path)

    assert note.title == "내 제목"
    assert note.lang == "kr"
    assert note.date == "2026-01-02"
    assert set(note.tags) == {"identity", "voice", "inline"}
    assert note.fingerprint
