"""Sub-AC 2: package skeleton import + __version__ exposure."""

import vault_corpus


def test_import_vault_corpus() -> None:
    assert vault_corpus is not None


def test_version_attribute_exists() -> None:
    assert hasattr(vault_corpus, "__version__")


def test_version_is_non_empty_string() -> None:
    v = vault_corpus.__version__
    assert isinstance(v, str)
    assert len(v) > 0
    assert v.strip() == v
