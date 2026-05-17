# vault-corpus

Build-time pipeline that mirrors a Korean Obsidian vault into an English,
heading-chunked, vector-indexed corpus and generates topic-cluster MOC
(Map-of-Content) samples. The original Obsidian vault is **read-only** and
never mutated by this tool.

## Status

v0.1 scaffold. Sub-AC implementation in progress.

## Install

```bash
pyenv local 3.12
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Copy `.env.example` to `.env` and set `OPENAI_API_KEY=sk-...`. The `.env`
file is gitignored.

## Usage

The `vault-corpus` CLI is the only entry point. Five subcommands:

```bash
# Full rebuild: scan vault, chunk, translate KR→EN, embed, upsert.
# Idempotent — re-running with no vault changes does zero API calls.
vault-corpus build --vault ~/Obsidian/Obsidian_Master_v2

# Incremental: reprocess only files surfaced by `git diff` inside the vault repo.
vault-corpus build --delta --vault ~/Obsidian/Obsidian_Master_v2

# DB stats: chunk counts per lang, last build timestamp, file count.
vault-corpus status

# Cluster English embeddings, generate 10 MOC markdown samples under data/moc_samples/.
vault-corpus moc generate --n 10

# Run the 5 smoke queries; exit non-zero if any returns < 3 results above floor.
vault-corpus smoke-test
```

All artifacts (SQLite DB, English mirror, MOC samples) live under the project
repo's `data/` directory. Nothing is ever written into the Obsidian vault.

## Scope

Input is exactly these top-level directories under the vault:

- `00 Get Things Done`
- `01 Command Center`
- `02 Vision Center`
- `03 Resources`
- `05Publish`

Hidden directories (`.obsidian/`, `.trash/`, `.mindos/`) and out-of-scope
top-level directories (`04 PracticeMakesPerfect`, `90System`, `91 Archives`,
`999 LOCAL`) are skipped at the scanner level. See
`src/vault_corpus/scanner.py`.

## API Integration — Step-by-Step

Each OpenAI touchpoint is implemented as an independently testable function.
This section walks through them in isolation so you can inspect each request
and response shape by hand.

### Step 1: Auth (env var loading)

API keys come from environment variables only. Never hardcoded, never
committed. Loaded lazily so unit tests can mock `OPENAI_API_KEY` without a
real key.

```python
import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()  # reads .env from CWD
api_key = os.environ["OPENAI_API_KEY"]
client = OpenAI(api_key=api_key)
print(client.models.list().data[0].id)  # smoke check
```

### Step 2: Single-text embedding call

`embed(text)` wraps the `embeddings.create` endpoint and returns a 3072-dim
`list[float]`. Inspecting one call in isolation:

```python
from openai import OpenAI

client = OpenAI()
resp = client.embeddings.create(
    model="text-embedding-3-large",
    input="hello world",
)
vec = resp.data[0].embedding
print(len(vec), vec[:3])  # 3072 [0.01, -0.02, ...]
print(resp.usage)         # CompletionUsage(prompt_tokens=2, total_tokens=2)
```

Request payload shape: `{"model": str, "input": str | list[str]}`. Response:
`{"data": [{"embedding": [..3072 floats..]}], "usage": {...}}`.

### Step 3: Single translation call

`translate_chunk(chunk)` wraps `chat.completions.create` with a deterministic
KR→EN system prompt. The returned English chunk shares the source `chunk_id`
so the KR↔EN link is preserved through the content hash, not a separate join
table.

```python
from pathlib import Path
from openai import OpenAI
from vault_corpus.chunker import Chunk, compute_chunk_id
from vault_corpus.translator import translate_chunk

src, chain = Path("note.md"), ["목표"]
body = "## 목표\n매일 코드를 짠다.\n"
ko = Chunk(source_path=src, heading_chain=chain, body=body,
           chunk_id=compute_chunk_id(src, chain, body))
en = translate_chunk(ko, OpenAI())            # one chat.completions.create call
print(en.lang, en.heading_chain)               # 'en' ['Goal']
print(en.body)                                 # "## Goal\nWrite code daily.\n"
assert en.chunk_id == ko.chunk_id              # preserved across KR↔EN
```

Response shape: `{"choices": [{"message": {"content": "<english markdown>"}}], "usage": {...}}`.
`translate_chunk` extracts `choices[0].message.content`, recovers the
`heading_chain` from the leading `##`/`###` line via
`parse_translation_response`, and returns a new frozen `Chunk` with
`lang="en"`.

#### Request payload shape

`vault_corpus.translator.build_translation_prompt(chunk)` is a pure function
that returns the exact `dict` splatted into `client.chat.completions.create`:

```json
{
  "model": "gpt-4o-mini",
  "temperature": 0,
  "seed": 7,
  "messages": [
    {"role": "system", "content": "<TRANSLATION_SYSTEM_PROMPT — see below>"},
    {"role": "user",   "content": "## 목표\n매일 코드를 짠다.\n"}
  ]
}
```

Key invariants:

- `temperature=0` and a fixed `seed` make the request reproducible across
  retries (best-effort, per OpenAI's deterministic-sampling guarantees).
- `messages[1].content` is the chunk `body` **verbatim** — heading line
  included. No path, no frontmatter, no `chunk_id` is ever sent over the
  wire (verified by `test_only_chunk_body_is_sent_not_path_or_frontmatter`).
- The default `model` is `gpt-4o-mini`. Override per call via the `model=`
  kwarg (or use `gpt-5-mini` for higher-quality runs).

#### Prompt template

The system prompt is a single module-level constant
`vault_corpus.translator.TRANSLATION_SYSTEM_PROMPT`. Substantive directives
the model is told to obey:

1. Translate Korean Markdown to natural English.
2. **Preserve Markdown structure exactly** — heading levels (`##`, `###`),
   list markers, bold/italic, blockquotes, tables.
3. **Preserve code blocks verbatim** — do NOT translate code, identifiers,
   or fenced block contents.
4. **Preserve inline code spans, URLs, and Obsidian `[[wikilinks]]`** verbatim.
5. **Preserve the heading chain** — if the input begins with `##`/`###`,
   the output must begin with the same heading level. This is what makes
   `parse_translation_response` able to recover `heading_chain` from the
   English body without an extra round-trip.
6. Output English Markdown only — no commentary, preface, or surrounding
   fences.

Inspect the live template:

```bash
python -m vault_corpus.translator --demo --show-prompt
```

#### Inspect a single translation in isolation

The translator module ships a self-contained inspection CLI. Run it with
zero setup — no OpenAI key needed — to see the payload, the prompt, and the
translated body for a fixture chunk end-to-end:

```bash
# Stub mode: uses a built-in fake client. No network, no API key.
python -m vault_corpus.translator --demo --show-payload --show-prompt
```

Expected output (abridged):

```
=== system prompt template ===
You are a precise Korean-to-English translator for Markdown notes.
...

=== request payload ===
{
  "model": "gpt-4o-mini",
  "temperature": 0,
  "seed": 7,
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user",   "content": "## 목표\n매일 코드를 짠다.\n"}
  ]
}

=== translated body ===
## Goal
Write code daily.
=== chunk_id (unchanged): <sha256> ===
=== heading_chain: ['Goal'] ===
```

Translate an arbitrary Korean string through the same pipeline:

```bash
python -m vault_corpus.translator --demo --text "## 학습\n오늘은 transformers 논문을 읽었다."
```

Hit the real OpenAI API (requires `OPENAI_API_KEY`):

```bash
python -m vault_corpus.translator --live --text "## 목표\n매일 코드를 짠다."
```

Or, from a Python REPL, call `translate_chunk` directly with a mocked client
to inspect any chunk in your DB without burning tokens — see
`tests/test_translator.py` for the exact mock shape.

### Step 4: End-to-end search call

Search is one OpenAI embedding call plus a cosine top-k pass over the
stored English chunk vectors. `search` accepts the raw query string and
embeds it internally so the query vector lives in exactly the same space
as the corpus (same `text-embedding-3-large` model).

```python
from pathlib import Path
from vault_corpus.store import init_db, search_with_scores

conn = init_db(Path("data/vault.db"))
hits = search_with_scores(conn, "how to write daily standup notes", k=5)
for chunk, score in hits:
    print(f"{score:.3f}  {chunk.source_path}  {chunk.heading_chain}")
    print(chunk.body[:120], "...\n")
# Inspect the underlying embedding request directly:
from vault_corpus.store import embed
qvec = embed("how to write daily standup notes")
print(len(qvec))  # 3072
```

Use `search(conn, query, k)` for chunks-only output, or
`search_with_scores` when you need the cosine similarity (the smoke-test
gate uses scores to enforce its "≥ 3 results above floor" rule).

### API call accounting

Every command logs total API call count and an estimated USD cost (from
prompt-token + embedding-token usage in each response object). This is how
you verify reproducibility: a no-op `build` should report **0 API calls**.

## Architecture

See [`docs/architecture.md`](docs/architecture.md) for the storage layer /
access layer split, extension points (MCP server, CLI search UI, LLM Wiki
publisher), and the pgvector migration path.

## Test

```bash
pytest
```
