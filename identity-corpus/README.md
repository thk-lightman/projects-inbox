# identity-corpus

Derive a deduplicated sentence bank + regenerable `voice_profile.md` from
curated KR-self / EN-ref identity notes.

Scope-locked to two opt-in folders under the Obsidian vault:
`IDENTITY/kr-self/` (Korean long-form notes the operator wrote) and
`IDENTITY/en-ref/` (English reference notes the operator hand-picked).
No flag widens scope — direct antidote to the util-englishIdentity
1000-file scanning failure.

## Usage

Install (editable) alongside the sibling `vault-corpus` path dependency:

```bash
cd ~/GIT/forMori/project-mori/identity-corpus
poetry install
export OPENAI_API_KEY=sk-...
```

Core commands:

```bash
identity-corpus build                     # full pipeline (scan, tokenize, embed, cluster, tag, translate reps)
identity-corpus build --delta             # reprocess only changed file_fingerprints
identity-corpus status                    # counts of files / sentences / clusters / tags
identity-corpus tags list                 # taxonomy leaves + pending suggestions
identity-corpus review export --out review.tsv   # dump draft clusters for human review
identity-corpus review import review.tsv         # apply locked/archived state transitions
identity-corpus profile generate          # regenerate voice_profile.md from locked clusters
identity-corpus validate data/corpus.json # validate canonical Corpus JSON
identity-corpus search "decision" --status locked
```

Artifacts live under `data/` inside this repo (`data/sentence_bank.db`,
`data/voice_profile.md`, `data/review_*.tsv`). The Obsidian vault is
read-only at every code path.

## Lineage

This repo is the sequel to `vault-corpus` and the replacement for the
abandoned `util-englishIdentity` attempt.

- **vault-corpus** — sibling repo, declared as a path dependency
  (`vault-corpus = { path = "../vault-corpus", develop = true }`).
  identity-corpus reuses `vault_corpus.cost.ApiCostTracker`,
  `vault_corpus.store.embed`, `vault_corpus.translator.translate_chunk`,
  and the SQLite schema pattern. No fork, no vendor copy.
- **util-englishIdentity** — abandoned predecessor. Three failures
  identity-corpus explicitly absorbs:
  1. Indiscriminate 1000+ file scan → fixed by hard scope-lock to
     `IDENTITY/kr-self/` and `IDENTITY/en-ref/`. No widening flag.
  2. Per-sentence translation cost blowup → fixed by KR-direct
     embedding, incremental cosine clustering, and one translation
     call per cluster representative.
  3. LLM-only labeling with no cross-batch consistency → fixed by
     deterministic embedding clustering (centroid pool persists in
     SQLite) plus an explicit `taxonomy.yaml` the LLM may extend via
     `suggested_new_tags` for operator promote/reject.

Non-goals for v1 (also lessons from util-englishIdentity scope creep):
no Anki sync, no Google Sheets sync, no chat UI, no MCP server.

## API Integration

Three OpenAI touchpoints, each injectable for tests. Defaults read
`OPENAI_API_KEY` from the environment.

### 1. Embedding (KR-direct, 3072-dim)

Uses `vault_corpus.store.embed` with `text-embedding-3-large`. Korean
sentences embed directly — translation happens later on the cluster
representative only.

```python
from openai import OpenAI
from identity_corpus.clusterer import embed_sentence

client = OpenAI()  # reads OPENAI_API_KEY
vector = embed_sentence("나는 정체성을 문장으로 만든다.", client)
assert len(vector) == 3072
```

### 2. Translation (one call per KR cluster representative)

Uses `vault_corpus.translator.translate_chunk` with `gpt-4o-mini` by
default. Called once per KR-origin cluster, never per sentence.

```python
from openai import OpenAI
from identity_corpus.store import init_db
from identity_corpus.translator import translate_representative

client = OpenAI()
db = init_db("data/sentence_bank.db")
en = translate_representative(db, cluster_id=12, client=client)
print(en)
```

### 3. Pragmatic tagging (one call per cluster)

Calls an OpenAI chat model with the cluster representative + the
known taxonomy. Returns `(axis, group, leaf)` triples and any
`suggested_new_tags` for operator review.

```python
from pathlib import Path
from openai import OpenAI
from identity_corpus.store import init_db
from identity_corpus.tagger import tag_cluster
from identity_corpus.tagger import load_taxonomy

client = OpenAI()
db = init_db("data/sentence_bank.db")
taxonomy = load_taxonomy(Path("taxonomy.yaml"))
result = tag_cluster(db, cluster_id=12, client=client, taxonomy=taxonomy)
print(result["suggested_new_tags"])
```

Secrets are loaded from environment variables only. Never hardcoded,
never committed.
