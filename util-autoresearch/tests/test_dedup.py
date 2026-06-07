"""Dedup cascade unit tests."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fetch_papers import DedupStore, Paper  # noqa: E402


def test_doi_takes_priority():
    p = Paper(title="t", authors=["A B"], doi="10.1234/abc.def", arxiv_id="2403.12345")
    assert p.canonical_id() == "10-1234-abc-def"


def test_arxiv_fallback_when_no_doi():
    p = Paper(title="t", authors=["A B"], doi="", arxiv_id="2403.12345")
    assert p.canonical_id() == "arxiv-2403.12345"


def test_hash_fallback_when_no_doi_no_arxiv():
    p = Paper(title="Causal Forests for HTE", authors=["Andrew Gelman", "Other"])
    cid = p.canonical_id()
    assert cid.startswith("hash-")
    assert len(cid) == len("hash-") + 16


def test_hash_stable_across_calls():
    p1 = Paper(title="X", authors=["Y"])
    p2 = Paper(title="X", authors=["Y"])
    assert p1.canonical_id() == p2.canonical_id()


def test_dedup_store_round_trip():
    with tempfile.TemporaryDirectory() as td:
        db = DedupStore(Path(td) / "d.sqlite")
        assert db.has("xx") is False
        db.mark("xx", "openalex", "title")
        assert db.has("xx") is True
        db.mark("xx", "openalex", "title")  # idempotent
        assert db.has("xx") is True
        db.close()


def test_dedup_store_distinct_keys():
    with tempfile.TemporaryDirectory() as td:
        db = DedupStore(Path(td) / "d.sqlite")
        db.mark("a", "s2", "t1")
        db.mark("b", "openalex", "t2")
        assert db.has("a")
        assert db.has("b")
        assert not db.has("c")
        db.close()
