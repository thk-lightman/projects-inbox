# Architecture

This document describes the internal architecture of `identity-corpus`.
It is the canonical reference for the five subsystems whose design
decisions drive the rest of the codebase: **Storage**, **Chunking**,
**Cluster Threshold**, **Taxonomy Evolution**, and **pgvector
Migration**. Each section explains the decision, the rationale, and
the boundary that future contributors must not cross without
revisiting the seed.

---

## Storage

`identity-corpus` persists everything in a single SQLite file at
`data/sentence_bank.db`. SQLite was chosen for three reasons:

1. **Zero-ops single-user tool.** The operator runs the CLI locally;
   a server-backed database adds operational surface area that
   provides no value at this scale (low thousands of sentences).
2. **Path-dependency parity with `vault-corpus`.** The sibling repo
   already uses the SQLite store pattern (`vault_corpus.store`), and
   reusing the schema conventions keeps the embedding adapter,
   translator adapter, and cost tracker interchangeable across both
   tools.
3. **Cheap snapshotting and review.** A single file copies, diffs,
   and rolls back without coordination.

### Schema overview

The following tables form the core of the store. Column-level shape is
listed only where it constrains downstream subsystems.

- **`files`** — one row per scanned identity note. Carries the
  absolute path, the `file_fingerprint` (sha256 of normalized
  content), the source folder (`kr-self` or `en-ref`), and the
  language tag. Used by `build --delta` to skip unchanged files.
- **`sentences`** — one row per tokenized sentence. Primary key is
  `sentence_id`, a deterministic content hash so identical sentences
  across files collapse to one row. Columns include the original
  substring, language, source `file_id`, the 3072-dim embedding
  stored as a `BLOB`, the assigned `cluster_id`, and the
  `review_state` (`draft` / `locked` / `archived`).
- **`clusters`** — one row per cluster. Carries the running centroid
  (3072-dim `BLOB`), the chosen representative `sentence_id`, the
  cached translation of the representative (KR clusters only), the
  taxonomy tags, the suggested-new-tag payload, and a count of
  member sentences.
- **`taxonomy_suggestions`** — append-only log of `(axis, value,
  cluster_id)` tuples emitted by the LLM. Operator promotion writes
  back into `taxonomy.yaml`; rejection leaves the row in place for
  audit.
- **`api_costs`** — pass-through table populated by
  `vault_corpus.cost.ApiCostTracker`. Lets `identity-corpus status`
  report cumulative spend per model.

### What the store deliberately does NOT do

- No write path ever touches the Obsidian vault. The vault is read-only.
- No table stores derived voice-profile content; `voice_profile.md`
  is regenerated from `clusters` filtered to `review_state = locked`.
- No row outlives the `archived` state in a way that re-surfaces it
  in the build pipeline.

---

## Chunking

The chunking subsystem turns a markdown file into the unit on which
embedding and clustering operate: a **sentence**.

### Pipeline

1. **Strip non-prose constructs.** Fenced code blocks (``` ``` `),
   inline code spans, Obsidian wikilinks (`[[...]]`), embeds
   (`![[...]]`), and YAML frontmatter are removed before tokenization.
   They are not voice-bearing and would dilute clustering.
2. **Detect language per file.** Files under `IDENTITY/kr-self/` are
   tokenized as Korean; files under `IDENTITY/en-ref/` are tokenized
   as English. The scanner refuses any path outside those two
   directories, so language is unambiguous.
3. **Tokenize into sentences.** A small in-house regex splitter is
   the default; `kss` is an allowed optional substitute for Korean.
4. **Apply minimum-length filters.** Korean sentences require **≥ 5
   Hangul characters**. English sentences require **≥ 20 alphabetic
   characters**. These thresholds were validated against the
   operator's writing style during the abandoned `util-englishIdentity`
   attempt and are inherited intentionally.
5. **Hash to `sentence_id`.** The post-strip, pre-embed substring is
   sha256-hashed so identical sentences in different files share one
   row.

### Why these thresholds

Short fragments (greetings, headings, listy phrases) carry no
pragmatic signal but consume embedding budget and pollute clusters.
The two thresholds are deliberately asymmetric: Hangul characters are
information-dense, so the Korean bar is lower; English alphabetic
characters are sparser per unit of meaning, so the English bar is
higher.

### Boundaries

- No multi-sentence chunks. The cluster is the multi-sentence unit;
  the sentence is the embedding unit.
- No translation at chunk time. Translation is deferred until after
  clustering and runs once per cluster representative.

---

## Cluster Threshold

The clusterer is **incremental cosine-similarity attach-or-create**
against a persistent centroid pool stored in the `clusters` table.

### Algorithm

For each new sentence embedding `e`:

1. Compute cosine similarity against every existing cluster centroid.
2. Take the best match `(cluster_id, sim)`.
3. If `sim >= threshold`, attach the sentence to that cluster and
   recompute the centroid as the running mean of member embeddings.
4. Otherwise, create a new cluster seeded by `e` with `e` as the
   representative.

### Default threshold

The default cosine threshold is **0.78**. It is configurable through
the CLI and the config layer, but the default is deliberate:

- It is the value the abandoned `util-englishIdentity` attempt
  converged on after manual tuning against the operator's own KR
  prose.
- Below ~0.74, semantically distant sentences merge and dilute the
  cluster's representative meaning.
- Above ~0.82, near-duplicates fragment into siblings and the
  pragmatic tagger pays for redundant LLM calls.
- 0.78 sits in the empirically observed sweet spot for OpenAI
  `text-embedding-3-large` on Korean long-form prose.

### Why incremental, not batch

A batch clusterer (k-means, HDBSCAN) would reshuffle assignments on
every rebuild, invalidating cached translations and pragmatic tags.
The incremental attach-or-create algorithm guarantees that an
already-clustered sentence keeps its `cluster_id` across builds,
which is what makes the warm-rebuild path cost zero OpenAI calls.

### Boundaries

- The clusterer never re-evaluates past assignments. Drift is
  accepted as the cost of cache stability.
- Threshold lives in config, not in code constants, so the operator
  can sweep it during evaluation without a code change.

---

## Taxonomy Evolution

Pragmatic tagging uses a **hybrid** approach: a deterministic
embedding-derived cluster identity, plus an LLM-emitted
`(axis, group, leaf)` triple drawn from `taxonomy.yaml`, plus an
escape hatch for the LLM to propose new tags.

### Why hybrid

Pure LLM labeling has no cross-batch consistency — the same sentence
gets different tags on different days. Pure deterministic labeling
cannot discover new pragmatic axes. The hybrid pins consistency to
the embedding cluster (one tag set per cluster, computed once) and
delegates only the *naming* to the LLM, constrained by the taxonomy
file.

### `taxonomy.yaml` is the source of truth

The taxonomy is a 5-axis structure:

- **I — Structural Frames**
- **II — Narrative Tools**
- **III — Functional Acts**
- **IV — Interview Moves**
- **V — Epistemic & Register**

Axes I–III are the operator's pre-existing rhetorical axes from the
abandoned attempt. Axes IV–V were added to support the English-
interview self-transfer use case.

### LLM call contract

For each cluster, the tagger sends:

- the cluster representative sentence
- the cluster's other member sentences (capped)
- the current `taxonomy.yaml` as the allowed label space

and receives back:

- `tags`: an array of `(axis, group, leaf)` triples drawn only from
  the supplied taxonomy
- `suggested_new_tags`: an array of `(axis, proposed_value, reason)`
  triples for labels the LLM judged necessary but absent

`suggested_new_tags` is written into the `suggested_tags` table
and surfaced through the review TSV. It never silently mutates
`taxonomy.yaml`. The operator promotes a suggestion by editing
`taxonomy.yaml` directly and rejects by leaving it.

### Evolution loop

1. Build runs the tagger; suggestions accumulate.
2. Operator inspects suggestions in the review TSV.
3. Operator promotes coherent ones into `taxonomy.yaml` by hand.
4. Next build re-tags affected clusters against the enriched taxonomy.

### Boundaries

- The LLM may not invent axes outside I–V.
- The LLM may not assign a tag absent from the supplied taxonomy
  except via the `suggested_new_tags` channel.
- One tagging LLM call per cluster, never per sentence.

---

## pgvector Migration

`identity-corpus` is designed so a future migration to Postgres +
pgvector requires **no re-embedding**.

### What the SQLite store already does right

- Embeddings are stored as raw 3072-dim `float32` BLOBs whose byte
  layout is identical to what pgvector's `vector` type expects after
  a `bytea`-to-`vector` cast.
- `sentence_id` is a content-derived hash, so rows port across stores
  without identity collisions.
- `cluster_id` references and centroid blobs follow the same shape
  as the sentence embeddings, so the entire vector estate moves as
  one block.
- All store access is funnelled through a thin adapter; no SQL is
  inlined in business logic.

### Migration path (when warranted)

1. Provision Postgres with the `vector` extension; create tables
   mirroring the SQLite schema with `embedding vector(3072)` and a
   `pgvector` IVFFlat or HNSW index over it.
2. Stream rows out of SQLite, casting the embedding `BLOB` to the
   pgvector `vector` literal during insert.
3. Swap the adapter implementation. Business logic (scanner,
   tokenizer, clusterer, tagger, profile generator) is untouched
   because none of it speaks SQL.
4. Replace the in-Python cosine loop in the clusterer with a
   pgvector `ORDER BY embedding <=> $1 LIMIT 1` query for the
   nearest-centroid lookup.

### When to migrate

Not yet. The migration is justified only when one of these thresholds
trips:

- Sentence count exceeds the regime where an in-Python cosine sweep
  over all centroids per insert is comfortable (rule of thumb: tens
  of thousands of clusters).
- Multi-user or remote-access requirements appear (explicit non-goal
  for v1).
- A second tool needs concurrent read/write access to the same store.

Until then, SQLite is the correct store and pgvector is a
known-feasible exit, not a near-term task.

### Boundaries

- No code in v1 may take a dependency on Postgres or pgvector.
- The adapter seam must remain the only place that knows which
  store backs the data.
