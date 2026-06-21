"""Collection-time Zotero push wiring (graceful + success), no real API."""

from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fetch_papers as fp  # noqa: E402
from fetch_papers import Paper, WrittenPaper, push_written_to_zotero  # noqa: E402


def _wp():
    return WrittenPaper(paper=Paper(title="t", doi="10.1/x"),
                        path=Path("/tmp/x.md"), bucket="topic/x")


def test_skips_when_creds_unset(monkeypatch):
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    monkeypatch.delenv("ZOTERO_USER_ID", raising=False)
    w = _wp()
    assert push_written_to_zotero([w]) == 0
    assert w.zotero_key == ""


def test_skips_when_pyzotero_missing(monkeypatch):
    monkeypatch.setenv("ZOTERO_API_KEY", "k")
    monkeypatch.setenv("ZOTERO_USER_ID", "1")
    fake = types.ModuleType("zotero_save")
    fake.zotero = None  # pyzotero unavailable
    monkeypatch.setitem(sys.modules, "zotero_save", fake)
    w = _wp()
    assert push_written_to_zotero([w]) == 0
    assert w.zotero_key == ""


def test_dry_run_is_noop(monkeypatch):
    monkeypatch.setenv("ZOTERO_API_KEY", "k")
    monkeypatch.setenv("ZOTERO_USER_ID", "1")
    assert push_written_to_zotero([_wp()], dry_run=True) == 0


def test_pushes_and_sets_key_on_success(monkeypatch):
    monkeypatch.setenv("ZOTERO_API_KEY", "k")
    monkeypatch.setenv("ZOTERO_USER_ID", "1")
    monkeypatch.setenv("ZOTERO_COLLECTION", "COLL1234")
    calls = {}

    fake = types.ModuleType("zotero_save")
    fake.zotero = object()  # non-None → pyzotero "available"

    def fake_push(path, *, api_key, user_id, collection=None):
        calls["collection"] = collection
        return {"status": "created", "key": "ZK999"}

    fake.push_to_zotero = fake_push
    monkeypatch.setitem(sys.modules, "zotero_save", fake)

    w = _wp()
    assert push_written_to_zotero([w]) == 1
    assert w.zotero_key == "ZK999"
    assert calls["collection"] == "COLL1234"


def test_one_failure_does_not_abort_rest(monkeypatch):
    monkeypatch.setenv("ZOTERO_API_KEY", "k")
    monkeypatch.setenv("ZOTERO_USER_ID", "1")
    fake = types.ModuleType("zotero_save")
    fake.zotero = object()
    seq = iter([RuntimeError("boom"), {"status": "created", "key": "OK2"}])

    def fake_push(path, **kw):
        r = next(seq)
        if isinstance(r, Exception):
            raise r
        return r

    fake.push_to_zotero = fake_push
    monkeypatch.setitem(sys.modules, "zotero_save", fake)

    a, b = _wp(), _wp()
    assert push_written_to_zotero([a, b]) == 1
    assert b.zotero_key == "OK2"
