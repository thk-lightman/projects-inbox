"""Sub-AC 4.3: .gitignore must exclude data/, .venv/, .env, *.db, OpenAI key patterns."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GITIGNORE = REPO_ROOT / ".gitignore"

REQUIRED_ENTRIES = ["data/", ".venv/", ".env", "*.db", "sk-*"]


def _load_entries() -> set[str]:
    assert GITIGNORE.exists(), f".gitignore missing at {GITIGNORE}"
    lines = GITIGNORE.read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip() and not line.strip().startswith("#")}


def test_gitignore_exists():
    assert GITIGNORE.is_file(), ".gitignore must exist at repo root"


def test_gitignore_excludes_data_dir():
    assert "data/" in _load_entries()


def test_gitignore_excludes_venv():
    assert ".venv/" in _load_entries()


def test_gitignore_excludes_env_file():
    assert ".env" in _load_entries()


def test_gitignore_excludes_db_files():
    assert "*.db" in _load_entries()


def test_gitignore_excludes_openai_key_pattern():
    """OpenAI API keys begin with `sk-`; .gitignore must block accidental commits."""
    assert "sk-*" in _load_entries()


def test_gitignore_all_required_entries_present():
    entries = _load_entries()
    missing = [e for e in REQUIRED_ENTRIES if e not in entries]
    assert not missing, f"missing required .gitignore entries: {missing}"
