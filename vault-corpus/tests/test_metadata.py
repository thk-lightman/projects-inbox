"""Sub-AC 1.1: verify pyproject.toml metadata via importlib.metadata."""

from importlib.metadata import metadata, version

import vault_corpus


def test_package_importable() -> None:
    assert hasattr(vault_corpus, "__version__")


def test_project_name() -> None:
    md = metadata("vault-corpus")
    assert md["Name"] == "vault-corpus"


def test_project_version_matches_module() -> None:
    assert version("vault-corpus") == vault_corpus.__version__
    assert vault_corpus.__version__ == "0.1.0"


def test_python_requires_declared() -> None:
    md = metadata("vault-corpus")
    requires_python = md["Requires-Python"]
    assert requires_python is not None
    assert ">=3.12" in requires_python


def test_core_dependencies_declared() -> None:
    md = metadata("vault-corpus")
    requires = md.get_all("Requires-Dist") or []
    joined = " ".join(requires)
    for pkg in ("openai", "sqlite-vss", "scikit-learn", "pyyaml", "typer", "tqdm"):
        assert pkg in joined, f"missing dep declaration: {pkg}"
