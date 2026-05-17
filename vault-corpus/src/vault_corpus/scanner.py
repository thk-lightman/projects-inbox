"""Vault file discovery.

Read-only scanner: enumerate Korean markdown notes under the 5 scoped
top-level directories of the Obsidian vault. Never writes to the vault.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

SCOPED_DIRS: tuple[str, ...] = (
    "00 Get Things Done",
    "01 Command Center",
    "02 Vision Center",
    "03 Resources",
    "05Publish",
)


def _has_hidden_segment(rel_parts: tuple[str, ...]) -> bool:
    """True if any path segment starts with '.' (hidden dir/file)."""
    return any(part.startswith(".") for part in rel_parts if part not in ("", "/"))


def list_scoped_files(vault_root: Path) -> list[Path]:
    """Return sorted absolute paths of markdown files in scope.

    Scope rules:
    - File must live under one of SCOPED_DIRS at the top level of vault_root.
    - File name ends with ``.md`` (case-sensitive, matches Obsidian convention).
    - No path segment may start with ``.`` (skips .obsidian, .trash, .mindos, ...).
    - Resolved path must remain inside ``vault_root`` (symlink-escape guard).

    The vault is treated as immutable; this function performs only reads.
    """
    if not isinstance(vault_root, Path):
        vault_root = Path(vault_root)
    vault_root = vault_root.expanduser()
    if not vault_root.is_dir():
        raise NotADirectoryError(f"vault_root not a directory: {vault_root}")

    root_resolved = vault_root.resolve()
    results: list[Path] = []

    for top in SCOPED_DIRS:
        base = vault_root / top
        if not base.is_dir():
            continue
        for path in base.rglob("*.md"):
            if not path.is_file():
                continue
            try:
                rel = path.relative_to(vault_root)
            except ValueError:
                continue
            if _has_hidden_segment(rel.parts):
                continue
            try:
                resolved = path.resolve()
                resolved.relative_to(root_resolved)
            except ValueError:
                continue
            results.append(path)

    results.sort()
    return results


def file_fingerprint(path: Path) -> str:
    """Deterministic fingerprint of file content + mtime for delta detection.

    Returns a hex SHA-256 digest computed from:
    - the file's raw bytes
    - the file's mtime in nanoseconds (st_mtime_ns), encoded as 8-byte big-endian

    Properties:
    - Stable across repeated calls on an unchanged file (same content, same mtime).
    - Changes when file content changes.
    - Changes when mtime changes, even if content is byte-identical
      (mtime-only touches still trigger reprocessing in delta builds).

    Read-only: never writes to or mutates the file.
    """
    if not isinstance(path, Path):
        path = Path(path)
    path = path.expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"file_fingerprint: not a file: {path}")

    st = path.stat()
    mtime_ns = st.st_mtime_ns

    h = hashlib.sha256()
    # Domain-separate the two inputs so e.g. content trailing bytes can't
    # collide with a different (content, mtime) pair.
    h.update(b"content:")
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    h.update(b"|mtime_ns:")
    # 16 bytes covers mtime_ns well past year 2262; big-endian for stability.
    h.update(mtime_ns.to_bytes(16, byteorder="big", signed=False))
    return h.hexdigest()
