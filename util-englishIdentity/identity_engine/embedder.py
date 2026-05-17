"""Embedder — lazy-load sentence-transformers model. Batch encode sentences
into float32 numpy vectors stored as bytes in SQLite BLOB columns.

Vector arithmetic helpers (cosine, mean) operate on numpy arrays.
"""
from __future__ import annotations

from typing import Iterable, Optional

import numpy as np

from .config import AppConfig


_VEC_DTYPE = np.float32


class Embedder:
    """Singleton-ish wrapper. First call loads the model (~120MB→~500MB depending)."""

    def __init__(self, config: AppConfig):
        self._cfg = config
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            # Lazy import — sentence_transformers pulls torch (heavy).
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self._cfg.embedding_model)
        return self._model

    @property
    def dim(self) -> int:
        return int(self._ensure_model().get_sentence_embedding_dimension())

    def encode_batch(
        self,
        texts: list[str],
        batch_size: int = 64,
        normalize: bool = True,
    ) -> np.ndarray:
        """Returns (N, D) float32 array. Normalized to unit length by default."""
        model = self._ensure_model()
        vecs = model.encode(
            texts,
            batch_size=batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=normalize,
        )
        return vecs.astype(_VEC_DTYPE, copy=False)


# ── serialization ────────────────────────────────────────────────────────────

def to_blob(vec: np.ndarray) -> bytes:
    return np.ascontiguousarray(vec, dtype=_VEC_DTYPE).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=_VEC_DTYPE)


# ── vector ops ───────────────────────────────────────────────────────────────

def cosine_sim_matrix(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """query: (D,) normalized. matrix: (N, D) normalized. Returns (N,) similarities."""
    if matrix.size == 0:
        return np.empty(0, dtype=_VEC_DTYPE)
    return matrix @ query


def update_centroid(
    old_centroid: np.ndarray,
    old_count: int,
    new_vec: np.ndarray,
) -> np.ndarray:
    """Running mean update. Re-normalize after."""
    if old_count == 0:
        out = new_vec.copy()
    else:
        out = (old_centroid * old_count + new_vec) / (old_count + 1)
    norm = np.linalg.norm(out)
    if norm > 0:
        out = out / norm
    return out.astype(_VEC_DTYPE, copy=False)


def stack_blobs(blobs: Iterable[Optional[bytes]]) -> np.ndarray:
    rows = [from_blob(b) for b in blobs if b]
    return np.vstack(rows) if rows else np.empty((0, 0), dtype=_VEC_DTYPE)
