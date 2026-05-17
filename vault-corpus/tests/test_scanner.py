"""Tests for vault_corpus.scanner.list_scoped_files."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from vault_corpus.scanner import SCOPED_DIRS, file_fingerprint, list_scoped_files

VAULT_ENV_VAR = "VAULT_CORPUS_VAULT_ROOT"
DEFAULT_VAULT = Path.home() / "Obsidian" / "Obsidian_Master_v2"

# Expected count seeded at 1455; allow ±10% filesystem drift.
EXPECTED_COUNT = 1455
DRIFT_TOLERANCE = 0.10

OUT_OF_SCOPE_TOP = (
    "04 PracticeMakesPerfect",
    "90System",
    "91 Archives",
    "999 LOCAL",
)

HIDDEN_TOP = (
    ".obsidian",
    ".trash",
    ".mindos",
    ".git",
)


def _resolve_vault() -> Path:
    root = Path(os.environ.get(VAULT_ENV_VAR, str(DEFAULT_VAULT))).expanduser()
    return root


@pytest.fixture(scope="module")
def vault_root() -> Path:
    root = _resolve_vault()
    if not root.is_dir():
        pytest.skip(f"vault root not present: {root}")
    return root


@pytest.fixture(scope="module")
def scoped_files(vault_root: Path) -> list[Path]:
    return list_scoped_files(vault_root)


def test_returns_list_of_paths(scoped_files: list[Path]) -> None:
    assert isinstance(scoped_files, list)
    assert all(isinstance(p, Path) for p in scoped_files)


def test_all_paths_absolute_and_md(scoped_files: list[Path]) -> None:
    assert scoped_files, "no markdown files discovered — wrong vault root?"
    for p in scoped_files:
        assert p.is_absolute(), f"non-absolute path: {p}"
        assert p.suffix == ".md", f"non-md file leaked: {p}"


def test_count_within_drift_tolerance(scoped_files: list[Path]) -> None:
    count = len(scoped_files)
    low = int(EXPECTED_COUNT * (1 - DRIFT_TOLERANCE))
    high = int(EXPECTED_COUNT * (1 + DRIFT_TOLERANCE))
    assert low <= count <= high, (
        f"file count {count} outside drift window [{low}, {high}] "
        f"around expected {EXPECTED_COUNT}"
    )


def test_no_out_of_scope_top_level(
    scoped_files: list[Path], vault_root: Path
) -> None:
    for p in scoped_files:
        rel = p.relative_to(vault_root)
        top = rel.parts[0]
        assert top not in OUT_OF_SCOPE_TOP, f"out-of-scope file leaked: {p}"
        assert top in SCOPED_DIRS, f"unexpected top-level dir: {top} ({p})"


def test_no_hidden_segment(scoped_files: list[Path], vault_root: Path) -> None:
    for p in scoped_files:
        rel = p.relative_to(vault_root)
        for part in rel.parts:
            assert not part.startswith("."), f"hidden segment in path: {p}"


def test_no_hidden_top_level(scoped_files: list[Path], vault_root: Path) -> None:
    for p in scoped_files:
        rel = p.relative_to(vault_root)
        assert rel.parts[0] not in HIDDEN_TOP, f"hidden top dir leaked: {p}"


def test_results_are_sorted(scoped_files: list[Path]) -> None:
    assert scoped_files == sorted(scoped_files)


def test_results_are_unique(scoped_files: list[Path]) -> None:
    assert len(scoped_files) == len(set(scoped_files))


# ---- synthetic vault tests (run without the real vault) -----------------


def _make_md(p: Path, text: str = "x") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_synthetic_excludes_hidden_and_out_of_scope(tmp_path: Path) -> None:
    # in scope
    _make_md(tmp_path / "00 Get Things Done" / "a.md")
    _make_md(tmp_path / "01 Command Center" / "sub" / "b.md")
    _make_md(tmp_path / "02 Vision Center" / "c.md")
    _make_md(tmp_path / "03 Resources" / "deep" / "nested" / "d.md")
    _make_md(tmp_path / "05Publish" / "e.md")

    # out of scope top-level
    _make_md(tmp_path / "04 PracticeMakesPerfect" / "skip1.md")
    _make_md(tmp_path / "90System" / "skip2.md")
    _make_md(tmp_path / "91 Archives" / "skip3.md")
    _make_md(tmp_path / "999 LOCAL" / "skip4.md")

    # hidden dirs (also under scoped top to test hidden-segment guard)
    _make_md(tmp_path / ".obsidian" / "x.md")
    _make_md(tmp_path / ".trash" / "y.md")
    _make_md(tmp_path / "03 Resources" / ".cache" / "skip.md")

    # non-md files in scope (should be ignored)
    (tmp_path / "03 Resources" / "notes.txt").write_text("nope", encoding="utf-8")

    result = list_scoped_files(tmp_path)
    rel = sorted(str(p.relative_to(tmp_path)) for p in result)
    assert rel == [
        "00 Get Things Done/a.md",
        "01 Command Center/sub/b.md",
        "02 Vision Center/c.md",
        "03 Resources/deep/nested/d.md",
        "05Publish/e.md",
    ]


def test_synthetic_missing_scoped_dir_is_ok(tmp_path: Path) -> None:
    _make_md(tmp_path / "00 Get Things Done" / "only.md")
    result = list_scoped_files(tmp_path)
    assert [p.name for p in result] == ["only.md"]


def test_raises_on_missing_vault_root(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        list_scoped_files(tmp_path / "does-not-exist")


# ---- file_fingerprint tests --------------------------------------------


def _set_mtime_ns(p: Path, mtime_ns: int) -> None:
    """Force mtime to an exact ns value (atime mirrored)."""
    os.utime(p, ns=(mtime_ns, mtime_ns))


def test_fingerprint_stable_on_unchanged_file(tmp_path: Path) -> None:
    p = tmp_path / "note.md"
    p.write_text("hello", encoding="utf-8")
    _set_mtime_ns(p, 1_700_000_000_000_000_000)

    fp1 = file_fingerprint(p)
    fp2 = file_fingerprint(p)
    fp3 = file_fingerprint(p)
    assert fp1 == fp2 == fp3
    # Sanity: hex sha256.
    assert len(fp1) == 64
    int(fp1, 16)


def test_fingerprint_changes_when_content_changes(tmp_path: Path) -> None:
    p = tmp_path / "note.md"
    p.write_text("hello", encoding="utf-8")
    _set_mtime_ns(p, 1_700_000_000_000_000_000)
    fp_before = file_fingerprint(p)

    # New content; pin mtime to same value to prove content alone flips fp.
    p.write_text("hello world", encoding="utf-8")
    _set_mtime_ns(p, 1_700_000_000_000_000_000)
    fp_after = file_fingerprint(p)

    assert fp_before != fp_after


def test_fingerprint_changes_when_mtime_changes(tmp_path: Path) -> None:
    p = tmp_path / "note.md"
    p.write_text("same bytes", encoding="utf-8")
    _set_mtime_ns(p, 1_700_000_000_000_000_000)
    fp_before = file_fingerprint(p)

    # Same content, different mtime (simulates `touch`).
    _set_mtime_ns(p, 1_800_000_000_000_000_000)
    fp_after = file_fingerprint(p)

    assert fp_before != fp_after


def test_fingerprint_differs_between_different_files_with_same_content(
    tmp_path: Path,
) -> None:
    # Same content, same mtime, but different inodes — fingerprint is
    # path-agnostic by design (delta detection keys off (path, fp) pair
    # elsewhere), so these should still match. This guards against any
    # accidental coupling to the file path.
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    a.write_text("identical", encoding="utf-8")
    b.write_text("identical", encoding="utf-8")
    _set_mtime_ns(a, 1_700_000_000_000_000_000)
    _set_mtime_ns(b, 1_700_000_000_000_000_000)
    assert file_fingerprint(a) == file_fingerprint(b)


def test_fingerprint_raises_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        file_fingerprint(tmp_path / "nope.md")


def test_fingerprint_raises_on_directory(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        file_fingerprint(tmp_path)


def test_fingerprint_handles_empty_file(tmp_path: Path) -> None:
    p = tmp_path / "empty.md"
    p.write_bytes(b"")
    _set_mtime_ns(p, 1_700_000_000_000_000_000)
    fp = file_fingerprint(p)
    assert len(fp) == 64


def test_fingerprint_handles_large_file(tmp_path: Path) -> None:
    # Exercise the 64 KiB streaming chunk loop.
    p = tmp_path / "big.md"
    payload = (b"abc123\n" * 20_000)  # ~140 KB, spans multiple chunks
    p.write_bytes(payload)
    _set_mtime_ns(p, 1_700_000_000_000_000_000)
    fp1 = file_fingerprint(p)
    fp2 = file_fingerprint(p)
    assert fp1 == fp2
