"""Path-confinement harness for vault_corpus.scanner.

Verifies that no read or write syscall originating from
``list_scoped_files`` or ``file_fingerprint`` ever targets a path
outside ``vault_root``.

Strategy:
- Build a synthetic vault under ``tmp_path/vault``.
- Plant adversarial bait files OUTSIDE the vault but INSIDE ``tmp_path``
  (so they live in the watched sandbox).
- Monkeypatch ``builtins.open`` plus the os-level syscalls that pathlib
  uses for directory enumeration and stat (``os.stat``, ``os.lstat``,
  ``os.scandir``, ``os.listdir``). Each wrapper records the incoming
  path argument.
- Assert that every recorded syscall whose target lies inside the
  sandbox stays inside ``vault_root``.
- Additionally assert the scanner is fully read-only by blocking
  write-mode opens and every mutating ``os.*`` call and confirming the
  scanner triggers none of them.

System paths (Python stdlib, /etc, etc.) are deliberately not policed:
the SUT is the scanner's behavior against the vault sandbox.
"""

from __future__ import annotations

import builtins
import os
import os.path as osp
from pathlib import Path

import pytest

from vault_corpus.scanner import file_fingerprint, list_scoped_files


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _norm_abs(raw_arg) -> str | None:
    """Coerce a syscall path argument into a normalized absolute string.

    Returns None for file descriptors or non-path arguments. Performs no
    filesystem access (no realpath / no stat), so it is safe to call
    from inside a syscall wrapper without re-entering patched code.
    """
    if isinstance(raw_arg, int):  # file descriptor
        return None
    try:
        s = os.fspath(raw_arg)
    except TypeError:
        return None
    if isinstance(s, bytes):
        try:
            s = s.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(s, str):
        return None
    if not osp.isabs(s):
        s = osp.abspath(s)
    return osp.normpath(s)


class FSWatch:
    """Records syscalls and flags any that escape ``vault_root``.

    Both the raw and the symlink-resolved forms of sandbox and vault
    roots are tracked so macOS's ``/var`` -> ``/private/var`` symlink
    does not cause false positives.
    """

    def __init__(self, sandbox: str, vault: str) -> None:
        self.sandbox_variants = self._root_variants(sandbox)
        self.vault_variants = self._root_variants(vault)
        # Ancestors of vault are unavoidably lstat'd by Path.resolve()
        # while canonicalizing vault_root itself. Allow them.
        self.vault_ancestors: tuple[str, ...] = self._ancestors(self.vault_variants)
        self.calls: list[tuple[str, str]] = []
        self.violations: list[tuple[str, str]] = []
        self._guard = False

    @staticmethod
    def _ancestors(roots: tuple[str, ...]) -> tuple[str, ...]:
        out: set[str] = set()
        for r in roots:
            cur = r
            while True:
                parent = osp.dirname(cur)
                if parent == cur:
                    break
                out.add(parent)
                cur = parent
        return tuple(out)

    @staticmethod
    def _root_variants(p: str) -> tuple[str, ...]:
        variants = {p, osp.realpath(p)}
        # On macOS tmp_path may be /var/folders/... while realpath is
        # /private/var/folders/... — capture both directions.
        if p.startswith("/private/"):
            variants.add(p[len("/private") :])
        else:
            variants.add("/private" + p)
        return tuple(variants)

    @staticmethod
    def _matches(path: str, roots: tuple[str, ...]) -> bool:
        for r in roots:
            if path == r or path.startswith(r + os.sep):
                return True
        return False

    def check(self, syscall: str, raw_arg) -> None:
        if self._guard:
            return
        self._guard = True
        try:
            p = _norm_abs(raw_arg)
            if p is None:
                return
            if not self._matches(p, self.sandbox_variants):
                # System / unrelated path — not part of the SUT sandbox.
                return
            self.calls.append((syscall, p))
            if self._matches(p, self.vault_variants):
                return
            # Path.resolve() walks each ancestor of vault_root and lstats it
            # to canonicalize the path. This is unavoidable and does not
            # leak outside the vault — allow it.
            if p in self.vault_ancestors:
                return
            self.violations.append((syscall, p))
        finally:
            self._guard = False


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------


def _make_md(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture
def patched_fs(monkeypatch, tmp_path):
    """Build the sandbox, plant bait, and install fs syscall recorders."""

    vault = tmp_path / "vault"
    for rel, body in [
        ("00 Get Things Done/a.md", "alpha"),
        ("01 Command Center/sub/b.md", "beta"),
        ("02 Vision Center/c.md", "gamma"),
        ("03 Resources/deep/d.md", "delta\n## H\nbody\n"),
        ("05Publish/e.md", "epsilon"),
    ]:
        _make_md(vault / rel, body)

    # Bait files OUTSIDE vault but INSIDE sandbox — scanner must never touch.
    outside = tmp_path / "outside"
    outside.mkdir()
    _make_md(outside / "leak.md", "must-not-be-read")

    # Adversarial: parallel directory named like a scoped top-level dir.
    bait_top = tmp_path / "00 Get Things Done"
    bait_top.mkdir()
    _make_md(bait_top / "bait.md", "nope")

    # Adversarial: hidden directory under sandbox.
    bait_hidden = tmp_path / ".obsidian"
    bait_hidden.mkdir()
    _make_md(bait_hidden / "secret.md", "nope")

    # Pre-compute realpath forms BEFORE installing patches, so that the
    # realpath walk itself (which lstats every ancestor of the bait dirs)
    # doesn't pollute watch.calls with bait-path lstats.
    forbidden_prefixes = []
    for d in (outside, bait_top, bait_hidden):
        s = str(d)
        forbidden_prefixes.append(s)
        rp = osp.realpath(s)
        if rp != s:
            forbidden_prefixes.append(rp)

    watch = FSWatch(str(tmp_path), str(vault))

    real_open = builtins.open
    real_stat = os.stat
    real_lstat = os.lstat
    real_scandir = os.scandir
    real_listdir = os.listdir

    def w_open(file, *args, **kwargs):
        watch.check("open", file)
        return real_open(file, *args, **kwargs)

    def w_stat(path, *args, **kwargs):
        watch.check("stat", path)
        return real_stat(path, *args, **kwargs)

    def w_lstat(path, *args, **kwargs):
        watch.check("lstat", path)
        return real_lstat(path, *args, **kwargs)

    def w_scandir(path="."):
        watch.check("scandir", path)
        return real_scandir(path)

    def w_listdir(path="."):
        watch.check("listdir", path)
        return real_listdir(path)

    monkeypatch.setattr(builtins, "open", w_open)
    monkeypatch.setattr(os, "stat", w_stat)
    monkeypatch.setattr(os, "lstat", w_lstat)
    monkeypatch.setattr(os, "scandir", w_scandir)
    monkeypatch.setattr(os, "listdir", w_listdir)

    return {
        "vault": vault,
        "outside": outside,
        "bait_top": bait_top,
        "bait_hidden": bait_hidden,
        "watch": watch,
        "forbidden_prefixes": tuple(forbidden_prefixes),
    }


# ---------------------------------------------------------------------------
# read-syscall confinement
# ---------------------------------------------------------------------------


def test_list_scoped_files_no_paths_outside_vault(patched_fs):
    vault = patched_fs["vault"]
    watch: FSWatch = patched_fs["watch"]

    files = list_scoped_files(vault)

    assert files, "synthetic scoped files were not discovered"
    assert watch.calls, "instrumentation captured no fs calls — patches broken"
    assert not watch.violations, (
        f"list_scoped_files touched {len(watch.violations)} path(s) "
        f"outside vault_root; first 5: {watch.violations[:5]}"
    )


def test_file_fingerprint_no_paths_outside_vault(patched_fs):
    vault = patched_fs["vault"]
    watch: FSWatch = patched_fs["watch"]

    files = list_scoped_files(vault)

    # Isolate the fingerprint pass.
    watch.calls.clear()
    watch.violations.clear()

    for p in files:
        fp = file_fingerprint(p)
        assert len(fp) == 64
        int(fp, 16)  # validates hex

    assert watch.calls, "file_fingerprint made no fs calls"
    assert not watch.violations, (
        f"file_fingerprint touched {len(watch.violations)} path(s) "
        f"outside vault_root; first 5: {watch.violations[:5]}"
    )


def test_bait_paths_never_touched(patched_fs):
    """Adversarial paths planted outside vault receive zero syscalls."""
    vault = patched_fs["vault"]
    watch: FSWatch = patched_fs["watch"]
    forbidden = patched_fs["forbidden_prefixes"]

    files = list_scoped_files(vault)
    for p in files:
        file_fingerprint(p)

    bad = []
    for syscall, path in watch.calls:
        for pref in forbidden:
            if path == pref or path.startswith(pref + os.sep):
                bad.append((syscall, path))
                break

    assert not bad, f"scanner touched bait paths outside vault: {bad[:5]}"


# ---------------------------------------------------------------------------
# write-syscall confinement (vault immutability)
# ---------------------------------------------------------------------------


def test_no_write_syscalls_anywhere(patched_fs, monkeypatch):
    """Scanner must never invoke any mutating filesystem syscall.

    Layers a second set of patches on top of ``patched_fs`` that
    intercepts every write-style ``os.*`` call and any write-mode
    ``open()``. Each such call is recorded; the test asserts the
    scanner's normal path triggered zero of them.
    """
    vault = patched_fs["vault"]

    write_calls: list[tuple[str, str]] = []

    def _make_blocker(name):
        def w(path, *a, **kw):
            p = _norm_abs(path)
            write_calls.append((name, p if p is not None else repr(path)))
            raise PermissionError(
                f"{name} blocked in confinement test: {path}"
            )

        return w

    for fname in (
        "remove",
        "unlink",
        "rmdir",
        "rename",
        "replace",
        "mkdir",
        "makedirs",
        "chmod",
        "chown",
        "symlink",
        "truncate",
        "link",
    ):
        if hasattr(os, fname):
            monkeypatch.setattr(os, fname, _make_blocker(fname))

    # Layer over the already-patched builtins.open to block write-mode opens
    # while still delegating reads to the read-recording wrapper above.
    read_recording_open = builtins.open

    def w_open(file, mode="r", *args, **kwargs):
        m = mode if isinstance(mode, str) else "r"
        if any(ch in m for ch in ("w", "a", "x")) or "+" in m:
            write_calls.append(("open:" + m, str(file)))
            raise PermissionError(f"write-mode open blocked: {file} ({m})")
        return read_recording_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", w_open)

    files = list_scoped_files(vault)
    assert files, "discovery returned no files under write-blocked patches"
    for p in files:
        fp = file_fingerprint(p)
        assert len(fp) == 64

    assert not write_calls, (
        f"scanner attempted {len(write_calls)} write syscall(s); "
        f"first 5: {write_calls[:5]}"
    )


# ---------------------------------------------------------------------------
# meta-test: the harness itself actually detects an escape
# ---------------------------------------------------------------------------


def test_harness_detects_synthetic_escape(patched_fs):
    """Sanity check: if something opens a file outside vault, the watch flags it."""
    watch: FSWatch = patched_fs["watch"]
    outside = patched_fs["outside"]

    # Hand-roll an out-of-vault read; this MUST register as a violation.
    with builtins.open(outside / "leak.md", "rb") as f:
        f.read()

    assert watch.violations, (
        "harness failed to detect a hand-rolled escape; "
        "the confinement tests above would give false negatives"
    )
