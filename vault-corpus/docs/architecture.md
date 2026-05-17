# Architecture

This document describes the internal split between the **storage layer** and
the **access layer**, the **extension points** for future adapters, and the
**migration path** to pgvector. The goal is a stable storage shape that
future consumers (MCP server, CLI search UI, LLM Wiki publisher) can sit on
without reshaping the database.

## Overview

```
                      Obsidian vault (read-only, never written)
                                   │
                                   ▼
       ┌────────────── scanner.py ──────────────┐
       │ list_scoped_files, file_fingerprint    │
       └────────────────┬───────────────────────┘
                        ▼
       ┌────────────── chunker.py ──────────────┐
       │ chunk_note → Chunk(chunk_id, body, ..) │
       └────────────────┬───────────────────────┘
                        ▼
       ┌─────────── translator.py ──────────────┐  ← OpenAI chat API
       │ translate_chunk(ko_chunk) → en_chunk   │
       └────────────────┬───────────────────────┘
                        ▼
       ┌─────────────  store.py  ───────────────┐  ← OpenAI embeddings API
       │ embed, upsert_chunk, search            │
       │ SQLite-VSS virtual table `chunks`      │
       └────────────────┬───────────────────────┘
                        ▼
       ┌─────────────  cluster.py  ─────────────┐
       │ HDBSCAN over English embeddings        │
       │ MOC file generation (data/moc_samples) │
       └────────────────────────────────────────┘
```

## Storage Layer

Owns persistence. One module: `src/vault_corpus/store.py`. One database
file: `data/vault.db`. Schema is intentionally narrow so it can be lifted
into pgvector with no code rewrite and no re-embedding.

### Schema

```sql
CREATE VIRTUAL TABLE chunks USING vss0(embedding(3072));
CREATE TABLE chunk_meta (
    chunk_id          TEXT PRIMARY KEY,   -- SHA-256(source_path \n heading_chain \n body)
    source_path       TEXT NOT NULL,      -- vault-relative path of the original Korean note
    heading_chain     TEXT NOT NULL,      -- JSON list[str]
    lang              TEXT NOT NULL,      -- "ko" | "en"
    body              TEXT NOT NULL,
    front_matter      TEXT,               -- JSON dict
    build_ts          TEXT NOT NULL,      -- ISO8601
    file_fingerprint  TEXT NOT NULL       -- content+mtime hash, for delta detection
);
CREATE INDEX chunk_meta_source_path ON chunk_meta(source_path);
```

### Why `chunk_id` is content-derived

`chunk_id = sha256(f"{source_path}\n{heading_chain}\n{body}")`.

This is the single most important design choice. Because the ID is derived
from content rather than auto-generated, **every chunk has the same ID
regardless of database engine, row order, or insertion timestamp**. KR and
EN variants of the same chunk share the same ID, eliminating any need for a
join table to link translations.

### Migration to pgvector

Migration requires only recreating the `chunks` and `chunk_meta` tables with
the same columns under PostgreSQL + pgvector, then copying rows. No
re-embedding is needed because `chunk_id` is content-derived: an existing
embedding remains valid for its chunk on any engine.

Concretely:

```sql
-- pgvector target
CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY, embedding vector(3072));
CREATE TABLE chunk_meta (...same columns as SQLite...);
CREATE INDEX ON chunks USING ivfflat (embedding vector_cosine_ops);
```

Copy rows with `COPY` or a streaming script. Done. Embedding cost: zero.

## Access Layer

Owns reads and writes against the storage layer. Each function is small,
pure where possible, and independently testable with a mocked OpenAI
client. The CLI (`cli.py`) is a thin orchestration layer over these.

| Function | Module | Side effect |
|----------|--------|-------------|
| `list_scoped_files(vault_root)` | `scanner.py` | read-only fs |
| `file_fingerprint(path)`        | `scanner.py` | read-only fs |
| `chunk_note(path, text)`        | `chunker.py` | pure |
| `translate_chunk(chunk)`        | `translator.py` | 1 OpenAI chat call |
| `embed(text)`                   | `store.py`   | 1 OpenAI embeddings call |
| `upsert_chunk(chunk, vec)`      | `store.py`   | 1 sqlite write |
| `search(qvec, k)`               | `store.py`   | 1 sqlite read |
| `cluster_embeddings(...)`       | `cluster.py` | pure |
| `generate_moc(cluster_id)`      | `cluster.py` | 1 fs write under data/moc_samples/ |

The CLI never reaches into SQL or OpenAI directly. It composes these
functions. This keeps the dependency on OpenAI and sqlite-vss localized,
which is what makes the extension points below possible.

## Extension Points

These are explicit non-goals for v1, but the boundaries below are designed
so each can be added later without reshaping storage.

### MCP server

A future `vault_corpus/adapters/mcp.py` exposes `search(query, k)` as an MCP
tool. It calls `store.search()` directly. No DB changes needed — only a
thin wrapper translating MCP requests into store calls.

### CLI search UI

A future `vault-corpus search "query"` subcommand wraps `store.search()` and
pretty-prints the top-k chunks. Same access layer, new presentation only.

### LLM Wiki publisher

A future `vault_corpus/adapters/wiki.py` reads MOC files plus their cluster
chunks, formats them as a published wiki page, and pushes to a remote.
Reads from `store` + `data/moc_samples/`, writes nothing back to the vault.

### Clustering tuning

`cluster.py` defaults: HDBSCAN with `min_cluster_size=10`,
`min_samples=5`, cosine distance over L2-normalized embeddings. Falls back
to k-means(k=10) if HDBSCAN produces fewer than 10 non-noise clusters.
Operator overrides via `--min-cluster-size` and `--algo` CLI flags.

## Reproducibility & Vault Immutability

- `build` is idempotent. `chunk_id` is content-derived, so re-running over
  an unchanged vault matches existing rows and performs zero API calls.
- `build --delta` reads `git diff --name-only HEAD~1 HEAD` inside the vault
  repo to find changed markdown files within scope, and reprocesses only
  those.
- No code path writes into the vault directory. The scanner only reads;
  the chunker, translator, embedder, and store write only under the
  project repo's `data/` directory.

## Testing Boundaries

- `scanner.py`: filesystem fixtures, no network.
- `chunker.py`: pure functions, table-driven tests.
- `translator.py`: mocked OpenAI client, asserts request payload shape.
- `store.py`: in-memory sqlite + mocked embed, asserts upsert idempotence
  and cosine-rank correctness.
- `cluster.py`: synthetic embeddings, asserts cluster count and centroid
  selection.
- `cli.py`: typer's `CliRunner`, asserts exit codes and command wiring.
