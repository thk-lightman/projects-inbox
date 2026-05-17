"""Incremental clusterer — deterministic, cross-batch aware.

For each sentence (with embedding):
  1. Score against ALL existing expression centroids (same lang).
  2. If max similarity >= threshold → attach to that expression, update centroid.
  3. Else → create new expression seeded with this sentence.

Cross-batch consistency is automatic: existing centroids are always in the pool.
No LLM calls here — all deterministic vector math.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

import numpy as np

from .config import AppConfig
from .database import Database, STATUS_CURATED
from .embedder import (
    cosine_sim_matrix,
    from_blob,
    stack_blobs,
    to_blob,
    update_centroid,
)


class Clusterer:
    def __init__(self, config: AppConfig, db: Database, lang: str = "kr"):
        self._cfg = config
        self._db = db
        self._lang = lang
        self._threshold = float(config.cluster_threshold)

    def cluster_sentences(
        self,
        sentence_rows: list[sqlite3.Row],
        on_progress=None,
    ) -> dict:
        """Assign each sentence to an expression (existing or new).

        Returns counts: {attached, created, skipped}.
        Sentences must have non-null embedding.
        """
        attached = created = skipped = 0
        if not sentence_rows:
            return {"attached": 0, "created": 0, "skipped": 0}

        # Load existing centroids once (snapshot). New ones added during this run
        # are appended to the in-memory pool so subsequent sentences can attach.
        existing = self._db.get_expressions_with_centroid(self._lang)
        expr_ids: list[int] = [e["id"] for e in existing]
        member_counts: list[int] = [int(e["member_count"]) for e in existing]
        centroid_matrix = stack_blobs([e["centroid"] for e in existing])

        for sent in sentence_rows:
            if sent["embedding"] is None:
                skipped += 1
                if on_progress: on_progress(sent["id"], skipped=True)
                continue
            vec = from_blob(sent["embedding"])

            target_idx = -1
            if centroid_matrix.size:
                sims = cosine_sim_matrix(vec, centroid_matrix)
                best = int(np.argmax(sims))
                if float(sims[best]) >= self._threshold:
                    target_idx = best

            if target_idx >= 0:
                expr_id = expr_ids[target_idx]
                old_centroid = centroid_matrix[target_idx]
                old_count = member_counts[target_idx]
                new_centroid = update_centroid(old_centroid, old_count, vec)
                # Link instance first (may fail if already linked → don't bump count)
                linked = self._db.link_instance(expr_id, sent["id"])
                if linked:
                    new_count = old_count + 1
                    self._db.update_expression_centroid(
                        expr_id, to_blob(new_centroid), new_count,
                    )
                    # Update in-memory snapshot
                    centroid_matrix[target_idx] = new_centroid
                    member_counts[target_idx] = new_count
                    attached += 1
                else:
                    skipped += 1
            else:
                # Seed a new expression with this sentence
                seed_text = sent["kr_text"]
                expr_id = self._db.create_expression_with_centroid(
                    seed_text=seed_text,
                    lang=self._lang,
                    centroid_blob=to_blob(vec),
                )
                self._db.link_instance(expr_id, sent["id"])
                self._db.update_expression_centroid(expr_id, to_blob(vec), 1)
                # Append to in-memory pool
                expr_ids.append(expr_id)
                member_counts.append(1)
                if centroid_matrix.size == 0:
                    centroid_matrix = vec.reshape(1, -1)
                else:
                    centroid_matrix = np.vstack([centroid_matrix, vec])
                created += 1

            self._db.set_sentence_status(sent["kr_hash"], STATUS_CURATED)
            if on_progress: on_progress(sent["id"])

        return {"attached": attached, "created": created, "skipped": skipped}
