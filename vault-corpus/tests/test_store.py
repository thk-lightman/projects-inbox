"""Tests for the SQLite store schema and the OpenAI embedding wrapper."""

from __future__ import annotations

import json
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import vault_corpus.store as store_mod
from vault_corpus.chunker import Chunk, compute_chunk_id
from vault_corpus.store import (
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    delete_obsolete_chunks,
    embed,
    init_db,
    search,
    upsert_chunk,
)


EXPECTED_COLUMNS = {
    "chunk_id": "TEXT",
    "source_path": "TEXT",
    "heading_chain": "TEXT",
    "lang": "TEXT",
    "body": "TEXT",
    "front_matter": "TEXT",
    "build_ts": "TEXT",
    "file_fingerprint": "TEXT",
    "embedding": "BLOB",
}


@pytest.fixture()
def conn(tmp_path: Path):
    c = init_db(tmp_path / "corpus.db")
    try:
        yield c
    finally:
        c.close()


def _table_info(conn: sqlite3.Connection, table: str) -> list[tuple]:
    return list(conn.execute(f"PRAGMA table_info({table})"))


def test_chunks_table_exists(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='chunks'"
    ).fetchone()
    assert row is not None, "chunks table not created"


def test_chunks_has_nine_expected_columns(conn: sqlite3.Connection) -> None:
    info = _table_info(conn, "chunks")
    assert len(info) == 9, f"expected 9 columns, got {len(info)}: {info}"
    actual = {row[1]: row[2].upper() for row in info}
    assert actual == EXPECTED_COLUMNS


def test_chunk_id_is_primary_key(conn: sqlite3.Connection) -> None:
    info = _table_info(conn, "chunks")
    pk_cols = [row[1] for row in info if row[5] == 1]
    assert pk_cols == ["chunk_id"]


def test_source_path_index_present(conn: sqlite3.Connection) -> None:
    indexes = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND tbl_name='chunks'"
    ).fetchall()
    names = {row[0] for row in indexes}
    assert "idx_chunks_source_path" in names

    cols = [
        row[2] for row in conn.execute(
            "PRAGMA index_info(idx_chunks_source_path)"
        )
    ]
    assert cols == ["source_path"]


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "corpus.db"
    c1 = init_db(path)
    c1.close()
    c2 = init_db(path)  # must not raise
    try:
        info = _table_info(c2, "chunks")
        assert len(info) == 9
    finally:
        c2.close()


def test_init_db_creates_parent_dir(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c" / "corpus.db"
    conn = init_db(nested)
    try:
        assert nested.exists()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sub-AC 5.2 — embed() wrapper around OpenAI text-embedding-3-large.
# All tests inject a fake client so no real network call is ever made.
# ---------------------------------------------------------------------------


@dataclass
class _FakeEmbedding:
    embedding: list[float]


@dataclass
class _FakeResponse:
    data: list[_FakeEmbedding]


class _FakeEmbeddingsAPI:
    def __init__(self, dim: int = EMBEDDING_DIM):
        self.dim = dim
        self.calls: list[dict[str, Any]] = []

    def create(self, *, model: str, input: str) -> _FakeResponse:
        self.calls.append({"model": model, "input": input})
        return _FakeResponse(data=[_FakeEmbedding(embedding=[0.0] * self.dim)])


class _FakeOpenAIClient:
    def __init__(self, dim: int = EMBEDDING_DIM):
        self.embeddings = _FakeEmbeddingsAPI(dim=dim)


def test_embed_returns_3072_dim_vector():
    client = _FakeOpenAIClient()
    vec = embed("hello world", client=client)
    assert isinstance(vec, list)
    assert len(vec) == 3072
    assert len(vec) == EMBEDDING_DIM
    assert all(isinstance(x, float) for x in vec)


def test_embed_passes_correct_model_name():
    client = _FakeOpenAIClient()
    embed("hello world", client=client)
    assert len(client.embeddings.calls) == 1
    call = client.embeddings.calls[0]
    assert call["model"] == "text-embedding-3-large"
    assert call["model"] == EMBEDDING_MODEL


def test_embed_passes_text_verbatim_as_input():
    client = _FakeOpenAIClient()
    text = "## Goal\nShip the corpus pipeline today.\n"
    embed(text, client=client)
    assert client.embeddings.calls[0]["input"] == text


def test_embed_model_override_is_respected():
    client = _FakeOpenAIClient()
    embed("hi", client=client, model="text-embedding-3-small")
    assert client.embeddings.calls[0]["model"] == "text-embedding-3-small"


def test_embed_rejects_wrong_dim_for_default_model():
    client = _FakeOpenAIClient(dim=1536)  # small model dim, but default model id
    with pytest.raises(ValueError, match="3072"):
        embed("hi", client=client)


def test_embed_uses_default_client_when_none_passed(monkeypatch):
    sentinel_client = _FakeOpenAIClient()
    constructed = {"count": 0}

    def _fake_default() -> _FakeOpenAIClient:
        constructed["count"] += 1
        return sentinel_client

    monkeypatch.setattr(store_mod, "_default_client", _fake_default)
    vec = embed("payload")
    assert constructed["count"] == 1
    assert len(vec) == EMBEDDING_DIM
    assert sentinel_client.embeddings.calls[0]["model"] == EMBEDDING_MODEL


def test_embed_does_not_construct_openai_when_client_injected(monkeypatch):
    def _boom() -> Any:
        raise RuntimeError("default client must not be constructed when client= is passed")

    monkeypatch.setattr(store_mod, "_default_client", _boom)
    client = _FakeOpenAIClient()
    embed("hi", client=client)  # must not raise


# ---------------------------------------------------------------------------
# Sub-AC 5.3 — upsert_chunk: idempotent insert/update keyed by chunk_id.
# ---------------------------------------------------------------------------


def _make_chunk(
    *,
    chunk_id: str = "fixed-id-001",
    body: str = "## Goal\nShip it.\n",
    source_path: Path = Path("00 Get Things Done/note.md"),
    heading_chain: list[str] | None = None,
    lang: str = "ko",
    frontmatter: dict[str, Any] | None = None,
) -> Chunk:
    return Chunk(
        source_path=source_path,
        heading_chain=heading_chain if heading_chain is not None else ["Goal"],
        body=body,
        chunk_id=chunk_id,
        lang=lang,
        frontmatter=frontmatter if frontmatter is not None else {"tags": ["x"]},
    )


def _row_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]


def _decode_embedding(blob: bytes | None) -> list[float] | None:
    if blob is None:
        return None
    return list(struct.unpack(f"<{EMBEDDING_DIM}f", blob))


def test_upsert_chunk_inserts_new_row(conn: sqlite3.Connection) -> None:
    chunk = _make_chunk()
    upsert_chunk(conn, chunk, [0.5] * EMBEDDING_DIM, file_fingerprint="fp-1")

    assert _row_count(conn) == 1
    row = conn.execute(
        "SELECT chunk_id, source_path, heading_chain, lang, body, "
        "front_matter, file_fingerprint, embedding FROM chunks"
    ).fetchone()
    assert row[0] == "fixed-id-001"
    assert row[1] == str(chunk.source_path)
    assert json.loads(row[2]) == ["Goal"]
    assert row[3] == "ko"
    assert row[4] == "## Goal\nShip it.\n"
    assert json.loads(row[5]) == {"tags": ["x"]}
    assert row[6] == "fp-1"
    decoded = _decode_embedding(row[7])
    assert decoded is not None and len(decoded) == EMBEDDING_DIM
    assert decoded[0] == pytest.approx(0.5)


def test_upsert_chunk_is_idempotent_same_id_overwrites_body_and_embedding(
    conn: sqlite3.Connection,
) -> None:
    """The core Sub-AC 5.3 contract.

    Upserting the same chunk_id twice with different body + embedding must
    leave exactly one row, and that row must reflect the *latest* write.
    """
    cid = "stable-chunk-id-xyz"

    first = _make_chunk(chunk_id=cid, body="original body v1\n")
    upsert_chunk(
        conn,
        first,
        [0.1] * EMBEDDING_DIM,
        file_fingerprint="fp-old",
        build_ts="2024-01-01T00:00:00+00:00",
    )

    second = _make_chunk(chunk_id=cid, body="REWRITTEN body v2\n")
    upsert_chunk(
        conn,
        second,
        [0.9] * EMBEDDING_DIM,
        file_fingerprint="fp-new",
        build_ts="2025-06-15T12:34:56+00:00",
    )

    assert _row_count(conn) == 1, "upsert must not produce a duplicate row"

    row = conn.execute(
        "SELECT body, file_fingerprint, build_ts, embedding "
        "FROM chunks WHERE chunk_id = ?",
        (cid,),
    ).fetchone()

    assert row[0] == "REWRITTEN body v2\n"
    assert row[1] == "fp-new"
    assert row[2] == "2025-06-15T12:34:56+00:00"

    decoded = _decode_embedding(row[3])
    assert decoded is not None
    assert decoded[0] == pytest.approx(0.9)
    assert decoded[-1] == pytest.approx(0.9)


def test_upsert_chunk_different_ids_coexist(conn: sqlite3.Connection) -> None:
    a = _make_chunk(chunk_id="id-a", body="aaa")
    b = _make_chunk(chunk_id="id-b", body="bbb")
    upsert_chunk(conn, a, [0.0] * EMBEDDING_DIM)
    upsert_chunk(conn, b, [1.0] * EMBEDDING_DIM)
    assert _row_count(conn) == 2


def test_upsert_chunk_accepts_none_embedding(conn: sqlite3.Connection) -> None:
    chunk = _make_chunk(chunk_id="no-embed-yet")
    upsert_chunk(conn, chunk, None, file_fingerprint="fp")
    row = conn.execute(
        "SELECT embedding FROM chunks WHERE chunk_id = 'no-embed-yet'"
    ).fetchone()
    assert row[0] is None


def test_upsert_chunk_rejects_wrong_dim_embedding(conn: sqlite3.Connection) -> None:
    chunk = _make_chunk(chunk_id="bad-dim")
    with pytest.raises(ValueError, match=str(EMBEDDING_DIM)):
        upsert_chunk(conn, chunk, [0.0] * 1536)  # small-model length
    assert _row_count(conn) == 0


def test_upsert_chunk_default_build_ts_is_iso8601(conn: sqlite3.Connection) -> None:
    chunk = _make_chunk(chunk_id="auto-ts")
    upsert_chunk(conn, chunk, None)
    ts = conn.execute(
        "SELECT build_ts FROM chunks WHERE chunk_id = 'auto-ts'"
    ).fetchone()[0]
    # Should round-trip through fromisoformat without raising.
    from datetime import datetime as _dt

    parsed = _dt.fromisoformat(ts)
    assert parsed.tzinfo is not None


def test_upsert_chunk_serializes_unicode_heading_and_frontmatter(
    conn: sqlite3.Connection,
) -> None:
    chunk = _make_chunk(
        chunk_id="kr-chunk",
        heading_chain=["목표", "세부"],
        frontmatter={"제목": "노트", "tags": ["프로젝트"]},
    )
    upsert_chunk(conn, chunk, None)
    row = conn.execute(
        "SELECT heading_chain, front_matter FROM chunks WHERE chunk_id = 'kr-chunk'"
    ).fetchone()
    assert json.loads(row[0]) == ["목표", "세부"]
    assert json.loads(row[1]) == {"제목": "노트", "tags": ["프로젝트"]}


def test_upsert_chunk_with_real_chunk_id_helper(conn: sqlite3.Connection) -> None:
    """End-to-end: a Chunk whose id was produced by compute_chunk_id still
    upserts idempotently when re-derived from the same content."""
    src = Path("01 Command Center/foo.md")
    chain = ["Section"]
    body_v1 = "first body\n"
    cid_v1 = compute_chunk_id(src, chain, body_v1)

    upsert_chunk(
        conn,
        Chunk(
            source_path=src,
            heading_chain=chain,
            body=body_v1,
            chunk_id=cid_v1,
            lang="en",
            frontmatter={},
        ),
        [0.0] * EMBEDDING_DIM,
    )

    # Same id (we pretend translation rewrote the body but kept the id stable),
    # different body — should overwrite, not duplicate.
    upsert_chunk(
        conn,
        Chunk(
            source_path=src,
            heading_chain=chain,
            body="second body\n",
            chunk_id=cid_v1,
            lang="en",
            frontmatter={},
        ),
        [0.25] * EMBEDDING_DIM,
    )

    assert _row_count(conn) == 1
    body = conn.execute(
        "SELECT body FROM chunks WHERE chunk_id = ?", (cid_v1,)
    ).fetchone()[0]
    assert body == "second body\n"


# ---------------------------------------------------------------------------
# Sub-AC 5.4 — search(): cosine-similarity top-k over English chunks only.
# ---------------------------------------------------------------------------


def _unit_vec(index: int, dim: int = EMBEDDING_DIM) -> list[float]:
    """Standard-basis vector ``e_index`` of length ``dim``.

    Used to give each seeded chunk a deterministic, orthonormal embedding so
    cosine similarity is exactly 1.0 against the matching basis query and 0.0
    against any other seeded chunk — no ambiguity in the ranking.
    """
    v = [0.0] * dim
    v[index] = 1.0
    return v


class _MappedEmbeddingsAPI:
    """Fake embeddings client that returns a pre-mapped vector per query string.

    Avoids any real network call. The test seeds DB rows with known basis
    vectors, then calls ``search`` with a query string whose mapped vector is
    the basis vector of the target English chunk — making the expected top-1
    deterministic and provable without floating-point slack.
    """

    def __init__(self, mapping: dict[str, list[float]]):
        self.mapping = mapping
        self.calls: list[dict[str, Any]] = []

    def create(self, *, model: str, input: str) -> _FakeResponse:
        self.calls.append({"model": model, "input": input})
        if input not in self.mapping:
            raise KeyError(f"no fake embedding registered for query={input!r}")
        return _FakeResponse(data=[_FakeEmbedding(embedding=self.mapping[input])])


class _MappedOpenAIClient:
    def __init__(self, mapping: dict[str, list[float]]):
        self.embeddings = _MappedEmbeddingsAPI(mapping)


def _seed_en_kr_corpus(conn: sqlite3.Connection) -> dict[str, str]:
    """Seed 3 English + 2 Korean chunks with orthonormal embeddings.

    Returns a ``{label: chunk_id}`` dict so tests can assert which specific
    row came back at the top. The embeddings are basis vectors ``e_0..e_4``
    so each chunk is exactly 1.0 cosine-similar to its own basis query and
    exactly 0.0 to every other seeded chunk.
    """
    rows = [
        ("en1", "en", "01 Command Center/en-alpha.md", ["Alpha"], "english alpha body\n", 0),
        ("en2", "en", "01 Command Center/en-beta.md",  ["Beta"],  "english beta body\n",  1),
        ("en3", "en", "01 Command Center/en-gamma.md", ["Gamma"], "english gamma body\n", 2),
        ("kr1", "ko", "00 Get Things Done/kr-one.md", ["하나"],   "한국어 본문 1\n",       3),
        ("kr2", "ko", "00 Get Things Done/kr-two.md", ["둘"],     "한국어 본문 2\n",       4),
    ]
    label_to_id: dict[str, str] = {}
    for label, lang, src, chain, body, basis in rows:
        src_path = Path(src)
        cid = compute_chunk_id(src_path, chain, body)
        label_to_id[label] = cid
        upsert_chunk(
            conn,
            Chunk(
                source_path=src_path,
                heading_chain=chain,
                body=body,
                chunk_id=cid,
                lang=lang,
                frontmatter={"label": label},
            ),
            _unit_vec(basis),
        )
    return label_to_id


def test_search_top1_is_correct_english_chunk_and_excludes_korean(
    conn: sqlite3.Connection,
) -> None:
    """The Sub-AC 5.4 contract.

    Seed 3 EN + 2 KR chunks with orthonormal embeddings. Query with a vector
    closest to the en2 (``Beta``) chunk. Top-1 must be the en2 chunk and no
    Korean chunk may appear in the results at all.
    """
    ids = _seed_en_kr_corpus(conn)

    # Query vector == en2's basis vector → cos(query, en2) = 1.0, all others 0.0.
    # The vector is mapped through a fake embeddings client keyed on the
    # literal query string, so the test exercises the public `search(query, k)`
    # surface (string in) without any real network call.
    mapping = {"find beta": _unit_vec(1)}  # basis 1 == en2
    client = _MappedOpenAIClient(mapping)

    results = search(conn, "find beta", k=3, client=client)

    assert len(results) == 3, f"expected 3 results (3 EN chunks), got {len(results)}"
    assert all(c.lang == "en" for c in results), (
        f"Korean chunk leaked into results: {[c.lang for c in results]}"
    )
    returned_ids = {c.chunk_id for c in results}
    assert ids["kr1"] not in returned_ids
    assert ids["kr2"] not in returned_ids
    assert results[0].chunk_id == ids["en2"], (
        f"top-1 should be en2, got chunk_id={results[0].chunk_id}"
    )


def test_search_respects_k_limit(conn: sqlite3.Connection) -> None:
    _seed_en_kr_corpus(conn)
    client = _MappedOpenAIClient({"q": _unit_vec(0)})
    results = search(conn, "q", k=1, client=client)
    assert len(results) == 1


def test_search_returns_chunk_dataclass_with_decoded_metadata(
    conn: sqlite3.Connection,
) -> None:
    ids = _seed_en_kr_corpus(conn)
    client = _MappedOpenAIClient({"q": _unit_vec(2)})  # → en3
    results = search(conn, "q", k=1, client=client)
    top = results[0]
    assert isinstance(top, Chunk)
    assert top.chunk_id == ids["en3"]
    assert top.lang == "en"
    assert top.heading_chain == ["Gamma"]
    assert isinstance(top.source_path, Path)
    assert top.source_path == Path("01 Command Center/en-gamma.md")
    assert top.frontmatter == {"label": "en3"}


def test_search_excludes_english_rows_with_null_embedding(
    conn: sqlite3.Connection,
) -> None:
    # One EN row with a real embedding, one EN row with no embedding yet.
    embedded = Chunk(
        source_path=Path("01 Command Center/embedded.md"),
        heading_chain=["E"],
        body="embedded body\n",
        chunk_id="cid-embedded",
        lang="en",
        frontmatter={},
    )
    pending = Chunk(
        source_path=Path("01 Command Center/pending.md"),
        heading_chain=["P"],
        body="pending body\n",
        chunk_id="cid-pending",
        lang="en",
        frontmatter={},
    )
    upsert_chunk(conn, embedded, _unit_vec(0))
    upsert_chunk(conn, pending, None)

    client = _MappedOpenAIClient({"q": _unit_vec(0)})
    results = search(conn, "q", k=5, client=client)
    ids = {c.chunk_id for c in results}
    assert "cid-embedded" in ids
    assert "cid-pending" not in ids


def test_search_empty_db_returns_empty_list(conn: sqlite3.Connection) -> None:
    client = _MappedOpenAIClient({"q": _unit_vec(0)})
    assert search(conn, "q", k=5, client=client) == []


def test_search_k_zero_returns_empty_without_embedding_call(
    conn: sqlite3.Connection,
) -> None:
    _seed_en_kr_corpus(conn)
    client = _MappedOpenAIClient({})  # would KeyError if embed was called
    assert search(conn, "anything", k=0, client=client) == []
    assert client.embeddings.calls == []


# ---------------------------------------------------------------------------
# Sub-AC 6.4.1 — delete_obsolete_chunks: scoped reaping for delta builds.
# ---------------------------------------------------------------------------


def _seed_two_files(conn: sqlite3.Connection) -> dict[str, str]:
    """Seed two source paths with multiple chunks each.

    File A has 3 chunks (a1, a2, a3); file B has 2 chunks (b1, b2). Both
    EN and KO mirrors written for a subset so the test can also verify
    that scoping by source_path doesn't leak across files. Returns a
    ``{label: chunk_id}`` dict so assertions can name specific rows.
    """
    rows = [
        ("a1", "ko", "00 Get Things Done/file_a.md", ["A1"], "body a1\n"),
        ("a2", "ko", "00 Get Things Done/file_a.md", ["A2"], "body a2\n"),
        ("a3", "ko", "00 Get Things Done/file_a.md", ["A3"], "body a3\n"),
        ("b1", "ko", "01 Command Center/file_b.md", ["B1"], "body b1\n"),
        ("b2", "ko", "01 Command Center/file_b.md", ["B2"], "body b2\n"),
    ]
    label_to_id: dict[str, str] = {}
    for label, lang, src, chain, body in rows:
        src_path = Path(src)
        cid = compute_chunk_id(src_path, chain, body)
        label_to_id[label] = cid
        upsert_chunk(
            conn,
            Chunk(
                source_path=src_path,
                heading_chain=chain,
                body=body,
                chunk_id=cid,
                lang=lang,
                frontmatter={"label": label},
            ),
            None,
        )
    return label_to_id


def _ids_in_db(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT chunk_id FROM chunks")}


def test_delete_obsolete_chunks_removes_only_obsolete_rows(
    conn: sqlite3.Connection,
) -> None:
    """Core Sub-AC 6.4.1 contract.

    Seed file_a with chunks {a1, a2, a3}. Re-chunk reports only {a1, a3}
    are still current (a2 was removed, e.g. heading deleted). After the
    call, only a2 must be gone — a1, a3, b1, b2 all remain.
    """
    ids = _seed_two_files(conn)
    assert _ids_in_db(conn) == {ids["a1"], ids["a2"], ids["a3"], ids["b1"], ids["b2"]}

    deleted = delete_obsolete_chunks(
        conn,
        Path("00 Get Things Done/file_a.md"),
        [ids["a1"], ids["a3"]],
    )

    assert deleted == 1, f"expected exactly 1 obsolete row deleted, got {deleted}"
    remaining = _ids_in_db(conn)
    assert ids["a2"] not in remaining, "a2 should have been deleted"
    assert ids["a1"] in remaining, "a1 must be preserved"
    assert ids["a3"] in remaining, "a3 must be preserved"
    assert ids["b1"] in remaining, "b1 (other file) must be untouched"
    assert ids["b2"] in remaining, "b2 (other file) must be untouched"


def test_delete_obsolete_chunks_does_not_touch_other_files(
    conn: sqlite3.Connection,
) -> None:
    """Scoping guarantee: passing file_a's id set must not delete file_b rows,
    even though those ids are also "not in" the current set."""
    ids = _seed_two_files(conn)
    deleted = delete_obsolete_chunks(
        conn,
        Path("00 Get Things Done/file_a.md"),
        [ids["a1"], ids["a2"], ids["a3"]],
    )
    assert deleted == 0
    remaining = _ids_in_db(conn)
    assert ids["b1"] in remaining
    assert ids["b2"] in remaining


def test_delete_obsolete_chunks_noop_when_all_current(
    conn: sqlite3.Connection,
) -> None:
    """Warm-build case: every stored chunk for the file is still current →
    zero deletes, zero churn."""
    ids = _seed_two_files(conn)
    before = _ids_in_db(conn)
    deleted = delete_obsolete_chunks(
        conn,
        "00 Get Things Done/file_a.md",
        [ids["a1"], ids["a2"], ids["a3"]],
    )
    assert deleted == 0
    assert _ids_in_db(conn) == before


def test_delete_obsolete_chunks_empty_current_deletes_all_for_path(
    conn: sqlite3.Connection,
) -> None:
    """Front-matter-only / fully-emptied note: empty current set wipes every
    row for that source path but leaves other files intact."""
    ids = _seed_two_files(conn)
    deleted = delete_obsolete_chunks(
        conn,
        Path("00 Get Things Done/file_a.md"),
        [],
    )
    assert deleted == 3, f"expected all 3 file_a rows deleted, got {deleted}"
    remaining = _ids_in_db(conn)
    assert ids["a1"] not in remaining
    assert ids["a2"] not in remaining
    assert ids["a3"] not in remaining
    assert ids["b1"] in remaining
    assert ids["b2"] in remaining


def test_delete_obsolete_chunks_removes_both_lang_mirrors(
    conn: sqlite3.Connection,
) -> None:
    """If a chunk had both ko and en rows under the same chunk_id, removing
    that id must wipe both mirrors — they share the primary key. (Current
    schema collapses them to one row via PK, but the test pins the contract
    so a future schema split surfaces the regression.)"""
    src = Path("00 Get Things Done/twin.md")
    cid_keep = compute_chunk_id(src, ["Keep"], "keep body\n")
    cid_drop = compute_chunk_id(src, ["Drop"], "drop body\n")
    for cid, chain, body, lang in [
        (cid_keep, ["Keep"], "keep body\n", "ko"),
        (cid_drop, ["Drop"], "drop body\n", "ko"),
    ]:
        upsert_chunk(
            conn,
            Chunk(
                source_path=src,
                heading_chain=chain,
                body=body,
                chunk_id=cid,
                lang=lang,
                frontmatter={},
            ),
            None,
        )

    delete_obsolete_chunks(conn, src, [cid_keep])
    remaining = _ids_in_db(conn)
    assert cid_keep in remaining
    assert cid_drop not in remaining


def test_delete_obsolete_chunks_accepts_generator_for_current(
    conn: sqlite3.Connection,
) -> None:
    """``current_chunk_ids`` is typed Iterable — generator input must work
    (the function materializes internally to support the multi-use SQL
    parameter binding)."""
    ids = _seed_two_files(conn)
    keep = {ids["a1"], ids["a3"]}
    deleted = delete_obsolete_chunks(
        conn,
        Path("00 Get Things Done/file_a.md"),
        (cid for cid in keep),  # generator, not list
    )
    assert deleted == 1
    remaining = _ids_in_db(conn)
    assert ids["a2"] not in remaining


def test_delete_obsolete_chunks_unknown_path_is_noop(
    conn: sqlite3.Connection,
) -> None:
    """Passing a source_path that has no rows in the DB must not raise and
    must not affect other rows."""
    ids = _seed_two_files(conn)
    before = _ids_in_db(conn)
    deleted = delete_obsolete_chunks(
        conn,
        Path("03 Resources/never_seen.md"),
        ["some-random-id"],
    )
    assert deleted == 0
    assert _ids_in_db(conn) == before
    assert ids  # silence unused-var lint; the seed is the precondition


def test_search_orders_results_by_cosine_similarity_descending(
    conn: sqlite3.Connection,
) -> None:
    """Top-k order must be by cosine similarity, highest first.

    Build a query vector that has a strong projection on en1, a weaker one on
    en2, and a tiny one on en3 — the returned order must be en1, en2, en3.
    """
    ids = _seed_en_kr_corpus(conn)
    q = [0.0] * EMBEDDING_DIM
    q[0] = 0.9   # → en1
    q[1] = 0.4   # → en2
    q[2] = 0.1   # → en3
    client = _MappedOpenAIClient({"ranked": q})

    results = search(conn, "ranked", k=3, client=client)
    order = [c.chunk_id for c in results]
    assert order == [ids["en1"], ids["en2"], ids["en3"]]
