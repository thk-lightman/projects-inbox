# Identity-Corpus — Continuation Briefing

Paste this entire file as the first message of a new Claude Code session opened at
`/Users/mori/GIT/forMori/project-mori/identity-corpus`.

---

## What this project is

A Python CLI tool that converts the user's curated identity materials — Korean
long-form notes the user wrote themselves (`IDENTITY/kr-self/`) and English
reference sentences the user hand-picked (`IDENTITY/en-ref/`) — into:

1. **sentence_bank.db** — deduplicated sentence-level SQLite store keyed by
   pragmatic tags
2. **voice_profile.md** — a regenerable markdown profile that distills the
   user's voice into reusable patterns + exemplar sentences

The end goal is **English-language interview self-transfer**: enabling a Korean
speaker (the user) to switch into an English-speaker identity by leaning on a
sentence bank organized around the 5-axis pragmatic taxonomy in `taxonomy.yaml`,
especially axis IV (Interview Moves) and axis V (Epistemic & Register).

## Lineage — read before changing anything

This project sits at the apex of a 3-repo evolution. Understand the lineage so
you don't re-introduce the failures it was designed to avoid:

| Repo | Role |
|---|---|
| `../util-englishIdentity/` | **Failed precursor** (archived). Same goal, attempted by indiscriminately scanning 1000+ vault files with per-sentence LLM translation. Three failures encoded as constraints in this project's seed.yaml: (1) scope blowup, (2) translation cost blowup, (3) LLM-only labeling with no cross-batch consistency. |
| `../vault-corpus/` | **Infrastructure prequel.** Built and tested (303 tests passing) but full vault build not yet run (OpenAI quota). Provides reusable modules: `vault_corpus.cost.ApiCostTracker`, `vault_corpus.store.embed`, `vault_corpus.translator.translate_chunk`, `vault_corpus.pipeline.git_diff_changed_files`. Identity-corpus depends on it via a path entry, not vendored. |
| `./` (this repo) | **Target.** Seed + taxonomy authored; **implementation code = 0 lines.** |

## Current state

```
identity-corpus/
├── seed.yaml          ← 9 acceptance criteria, fully specified (READ FIRST)
├── taxonomy.yaml      ← 5 axes × 58 leaves, hierarchical (READ FIRST)
├── .gitignore
├── .env.example       ← OPENAI_API_KEY only
└── NEXT-SESSION-PROMPT.md   ← this file
```

Vault-side preparation (already created, empty):
```
~/Obsidian/Obsidian_Master_v2/04 PracticeMakesPerfect/IDENTITY/
├── kr-self/   (empty — user will populate by hand)
└── en-ref/    (empty — user will populate by hand)
```

## Decisions already locked (do NOT re-litigate)

- **Scope is hard-locked** to `IDENTITY/kr-self/` and `IDENTITY/en-ref/` only.
  No flag widens scope. This is the direct fix for the util-englishIdentity
  1000-file mistake.
- **5-axis taxonomy** = user's pre-existing I/II/III (구조적 틀, 서술 도구,
  기능 수행) + IV Interview Moves + V Epistemic & Register. Total 58 leaves.
  Each cluster gets at most one leaf per axis.
- **Cluster threshold default = 0.78** (cosine). Carried over from
  util-englishIdentity's manually-tuned value.
- **Sentence tokenizer thresholds**: KR sentences ≥ 5 Hangul chars; EN
  sentences ≥ 20 alphabetic chars.
- **Review state machine**: exactly 3 states (`draft`, `locked`, `archived`).
  Transitions ONLY via TSV import.
- **voice_profile.md includes ONLY `locked` sentences.** This is the gate that
  prevents auto-extracted noise from polluting the profile.
- **Translation runs at most ONCE per cluster** (on the representative
  sentence), never per sentence. Cost guarantee.
- **Delta build uses git-diff** (`git diff --name-only HEAD~1`) against the
  vault repo, filtered to IDENTITY/ paths. Manual trigger only, not on every
  commit. Same pattern as vault-corpus's `git_diff_changed_files`.
- **vault-corpus is a path dependency**, not vendored:
  `vault-corpus = { path = "../vault-corpus", develop = true }`
- **Repo is part of the `project-mori/` monorepo** (no separate `.git` inside
  `identity-corpus/`). Commits go to project-mori's git history.
- **Out of v1 scope (explicit non-goals)**: Anki sync, Google Sheets sync,
  web UI, MCP server, chat interface. Do not propose these.

## Immediate next action — pick one

**Option A — Launch ouroboros on the seed (recommended).**
The seed is well-specified with 9 ACs. Ouroboros can implement it without
operator intervention. Cost: zero OpenAI tokens (it's code-writing only — no
runtime calls). Uses ouroboros's own runtime budget.

Invoke via the MCP tool `ouroboros_start_execute_seed`:
- `seed_path`: `/Users/mori/GIT/forMori/project-mori/identity-corpus/seed.yaml`
- `cwd`: `/Users/mori/GIT/forMori/project-mori/identity-corpus`
- `max_iterations`: 15
- `model_tier`: high

Then monitor via `ouroboros_ac_tree_hud(session_id)`. Expect the same kind of
sub-AC tree progress that vault-corpus produced. If specific ACs fail (the
vault-corpus run failed AC6 sub-tasks), inspect the actual files on disk
rather than trusting the HUD's green marks — the prior session caught a
discrepancy where HUD said done but the file was missing.

**Option B — Hand-implement, AC by AC.**
Slower but full visibility. Start with AC1 (repo bootstrap: pyproject.toml,
src/identity_corpus/ skeleton, README), then AC2 (dual-folder scanner),
then proceed in order. Read `../vault-corpus/src/vault_corpus/scanner.py`,
`store.py`, `pipeline.py` for shape conventions to mirror.

**Option C — Wait for IDENTITY/ folder population first.**
The pipeline cannot smoke-test without input. If the user has not yet moved
notes into `IDENTITY/kr-self/` and `IDENTITY/en-ref/`, you can build the
pipeline cold but cannot verify it end-to-end. Ask the user before choosing.

## Pre-flight checks before any runtime invocation

- `OPENAI_API_KEY` is set in `identity-corpus/.env` (vault-corpus's `.env`
  has it; copy or symlink).
- Verify the user's OpenAI quota — vault-corpus Phase 2 (single translation
  call) was blocked by `insufficient_quota` 429 in the previous session. If
  quota is still depleted, identity-corpus runtime will fail the same way.
  Code-writing via ouroboros is unaffected.
- Verify the vault is a git repo (`git -C ~/Obsidian/Obsidian_Master_v2 rev-parse`).
  Required for `--delta` mode.

## Pre-flight checks before any code change to vault-corpus

vault-corpus is the parent infrastructure. Do not modify it from inside
identity-corpus work unless you discover a genuine bug or missing API.
If you need a new helper, prefer adding it inside identity-corpus first
and promoting to vault-corpus later.

## Files to read in this order

1. `seed.yaml` — 9 ACs, ontology, evaluation principles, exit conditions
2. `taxonomy.yaml` — the 5-axis hybrid taxonomy with full leaf inventory
3. `../vault-corpus/pyproject.toml` — dependency style to mirror
4. `../vault-corpus/src/vault_corpus/scanner.py` — confinement pattern to copy
5. `../vault-corpus/src/vault_corpus/store.py` — SQLite schema pattern (pgvector-portable)
6. `../vault-corpus/src/vault_corpus/pipeline.py` — orchestrator + git_diff helper
7. `../vault-corpus/src/vault_corpus/cost.py` — `ApiCostTracker` to reuse
8. The user's vault note saved by the prior session:
   `~/Obsidian/Obsidian_Master_v2/00 Get Things Done/03Inbox-bits/wip-vault-corpus.md`
   — explains how identity-corpus relates to vault-corpus, the LLM Wiki track,
   and the RAG track. Useful conceptual orientation.

## What to skip

- Do not re-run vault-corpus tests or rebuild vault-corpus — it was passing
  (303/303) and committed at `fc80b72` in the prior session.
- Do not redesign the taxonomy — the user explicitly approved the 5-axis
  hybrid structure with my proposed leaves intact.
- Do not propose alternative scopes (e.g. "let's also include 03 Resources").
  The single-folder hard-lock is the project's defining constraint.

## Reporting cadence the user prefers

- Terse. Caveman-style ok in chat but not in code/commits/PRs.
- Report at AC boundaries, not per-file.
- When something fails, surface the actual file/line cause; don't summarize
  it away.
- Don't claim "complete" if any sub-AC is missing on disk.
