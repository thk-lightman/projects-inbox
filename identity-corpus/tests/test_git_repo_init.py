"""AC5 shell test: verify identity-corpus is its own git repo with >=1 commit."""
from __future__ import annotations

import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )


def test_repo_has_own_dot_git_dir() -> None:
    # AC requires a dedicated repo at this path, not a parent-walked one.
    assert (REPO_ROOT / ".git").is_dir(), ".git dir missing at repo root"


def test_git_rev_parse_head_succeeds() -> None:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"git rev-parse HEAD failed: {result.stderr!r}"
    sha = result.stdout.strip()
    assert len(sha) == 40, f"expected 40-char SHA, got {sha!r}"


def test_git_log_oneline_has_at_least_one_commit() -> None:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "log", "--oneline"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"git log failed: {result.stderr!r}"
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(lines) >= 1, f"expected >=1 commit, got {len(lines)}"


def test_toplevel_is_identity_corpus_root() -> None:
    # Confirms this repo is dedicated (not the parent project-mori repo).
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    toplevel = Path(result.stdout.strip()).resolve()
    assert toplevel == REPO_ROOT, f"toplevel {toplevel} != repo root {REPO_ROOT}"
