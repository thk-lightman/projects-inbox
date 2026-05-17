"""vault-corpus: Korean Obsidian vault → English chunk corpus + vector index."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("vault-corpus")
except PackageNotFoundError:  # not installed, e.g. running from source tree
    __version__ = "0.0.0+local"

__all__ = ["__version__"]
