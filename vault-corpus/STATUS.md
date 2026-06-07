# STATUS: HELD (2026-06-05)

vault-corpus is **on hold**. It was an early draft of an LLM-Wiki / RAG corpus
builder (KR Obsidian vault → EN chunked, vector-indexed corpus + topic-cluster MOCs).

## Decision
- Do **not** add new dependencies on this package.
- `identity-corpus` no longer depends on it (the 3 borrowed functions —
  `scanner.file_fingerprint`, `translator.translate_chunk`, `store.embed` — were
  inlined or replaced with native implementations).
- To be **rebuilt from scratch** by benchmarking a canonical, properly-engineered
  RAG-corpus implementation rather than extending this draft.

## Until rebuilt
Treat the existing code as reference-only. No active development.
