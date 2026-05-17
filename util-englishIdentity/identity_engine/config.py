"""Config & Persona Loader — reads .env and parses Identity-Persona.md."""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# folder key → path prefix mapping (matches spec)
FOLDER_KEYS = {
    "gtd": "00 Get Things Done",
    "command": "01 Command Center",
    "vision": "02 Vision Center",
    "resources": "03 Resources",
    "daily": "00 Get Things Done/02Journal/Daily",
}

# Folder-specific tone instructions extracted from persona spec
_FOLDER_TONE: dict[str, str] = {
    "gtd": (
        "Tone: Imperative & Direct. Use concise action-oriented verbs. "
        "Focus on outcomes and deadlines. Phrases: 'Must execute', 'Priority established', 'Pending review'."
    ),
    "command": (
        "Tone: Collaborative & Technical. Assume communication with high-level peers or stakeholders. "
        "Heavy domain terminology (Statistics, AI, Dev). Focus on methodology and results. "
        "Phrases: 'Implementation in progress', 'Initial results indicate', 'Refining the architecture'."
    ),
    "vision": (
        "Tone: Abstract & Visionary but Evidence-Based. High-level strategic thinking. "
        "Use sophisticated conceptual language (Epistemology, Axiology, Strategic Alignment). "
        "Focus on 'Why'. Phrases: 'Aligning with the overarching objective', 'Strategic pivot towards'."
    ),
    "resources": (
        "Tone: Expository & Objective. Encyclopedia-style clarity. "
        "Passive voice acceptable for objective truths. "
        "Phrases: 'Defined as', 'Consists of', 'The primary mechanism involves'."
    ),
    "daily": (
        "Tone: Introspective & Authentic (Refined). British wit—self-reflective, slightly ironic, "
        "intellectually rigorous. Convert emotional raw data into analytical self-observation. "
        "Phrases: 'Observed a slight decline in cognitive throughput', 'An intriguing realisation regarding'."
    ),
}

_BASE_SYSTEM_INSTRUCTION = """You are an expert translator embodying the following academic persona:

IDENTITY: PhD Candidate at the University of Oxford / Senior Applied Scientist in AI & Statistics.
PHILOSOPHY: "Simplicity is the ultimate sophistication." (Pragmatic Minimalism)

LINGUISTIC RULES (mandatory):
- British English exclusively: optimise, labelling, centre, behaviour, whilst, realise.
- Oxford Comma strictly required.
- No Americanisms: never use 'gotten', 'anyways', 'elevator', 'math'.
- Vocabulary preferences: 'whilst' not 'while'; 'rigorous' not 'hard'; 'utilise'/'leverage' not 'use'.
- Preferred academic jargon where appropriate: a priori, posterior, counterfactual, latent variables, modular abstraction.
- Grammar: lead with conclusion (Bottom Line Up Front).
- Hedging verbs for uncertainty: 'suggests', 'appears to', 'indicates'.
- Brevity: eliminate filler words entirely.

TASK: Translate the Korean sentence into English according to the persona and folder tone specified.
Output ONLY the English translation. No explanation, no prefix, no quotes."""


@dataclass
class PersonaConfig:
    system_instruction: str
    folder_tones: dict[str, str]


@dataclass
class AppConfig:
    gemini_api_key: str
    gemini_model: str
    vault_path: Path
    persona_path: Path
    persona: PersonaConfig
    ankiconnect_url: str
    anki_deck: str
    anki_model: str
    sheets_id: str
    sheets_worksheet: str
    db_path: Path
    batch_size: int
    max_retries: int
    translation_backend: str = "api"
    gemini_cli_path: str = "gemini"
    mega_batch_size: int = 100
    mega_parallel: int = 1
    cli_timeout: int = 300
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    cluster_threshold: float = 0.78
    embed_batch_size: int = 128
    label_sample_size: int = 8
    gcp_credentials: str = ""


def _resolve_folder_key(file_path: str) -> str:
    """Return folder_key for a given vault-relative file path."""
    # daily notes checked first (more specific path)
    if re.search(r"00 Get Things Done.02Journal.Daily", file_path):
        return "daily"
    for key, prefix in FOLDER_KEYS.items():
        if key == "daily":
            continue
        if file_path.startswith(prefix):
            return key
    return "resources"  # default to expository/objective


def load_config(env_path: Path | None = None) -> AppConfig:
    load_dotenv(dotenv_path=env_path, override=False)

    vault_path = Path(os.environ["VAULT_PATH"])
    persona_rel = os.getenv("PERSONA_PATH", "01 Command Center/proj-EnglishIdentityTraniner/docs-Identity-Persona.md")
    persona_path = vault_path / persona_rel

    persona = PersonaConfig(
        system_instruction=_BASE_SYSTEM_INSTRUCTION,
        folder_tones=_FOLDER_TONE,
    )

    db_path = Path(os.getenv("DB_PATH", str(Path.cwd() / ".identity_engine.db")))

    backend = os.getenv("TRANSLATION_BACKEND", "api").lower()
    if backend not in ("api", "cli"):
        raise ValueError(f"TRANSLATION_BACKEND must be 'api' or 'cli', got: {backend}")

    api_key = os.getenv("GEMINI_API_KEY", "")
    if backend == "api" and not api_key:
        raise KeyError("GEMINI_API_KEY")

    return AppConfig(
        gemini_api_key=api_key,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3-flash-preview"),
        vault_path=vault_path,
        persona_path=persona_path,
        persona=persona,
        ankiconnect_url=os.getenv("ANKICONNECT_URL", "http://localhost:8765"),
        anki_deck=os.getenv("ANKI_DECK_NAME", "Identity::EnglishTrainer"),
        anki_model=os.getenv("ANKI_MODEL_NAME", "Identity-Engine"),
        sheets_id=os.getenv("GSPREAD_SPREADSHEET_ID", ""),
        sheets_worksheet=os.getenv("GSPREAD_WORKSHEET_NAME", "Identity-Engine"),
        db_path=db_path,
        batch_size=int(os.getenv("BATCH_SIZE", "10")),
        max_retries=int(os.getenv("MAX_RETRIES", "3")),
        translation_backend=backend,
        gemini_cli_path=os.getenv("GEMINI_CLI_PATH", "gemini"),
        mega_batch_size=int(os.getenv("MEGA_BATCH_SIZE", "100")),
        mega_parallel=int(os.getenv("MEGA_PARALLEL", "1")),
        cli_timeout=int(os.getenv("CLI_TIMEOUT", "300")),
        embedding_model=os.getenv(
            "EMBEDDING_MODEL",
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        ),
        cluster_threshold=float(os.getenv("CLUSTER_THRESHOLD", "0.78")),
        embed_batch_size=int(os.getenv("EMBED_BATCH_SIZE", "128")),
        label_sample_size=int(os.getenv("LABEL_SAMPLE_SIZE", "8")),
        gcp_credentials=os.getenv("GOOGLE_APPLICATION_CREDENTIALS", ""),
    )


def build_translation_prompt(kr_sentence: str, folder_key: str, persona: PersonaConfig) -> str:
    tone = persona.folder_tones.get(folder_key, persona.folder_tones["resources"])
    return f"FOLDER CONTEXT: {tone}\n\nKOREAN: {kr_sentence}"


def build_mega_batch_prompt(items: list[dict], persona: PersonaConfig) -> str:
    """Build a single prompt translating many sentences. Items: [{id, kr, folder}, ...]."""
    tone_block = "\n".join(
        f"- {key}: {tone}" for key, tone in persona.folder_tones.items()
    )
    return (
        "STRICT MODE: You are a pure translation function. "
        "Do NOT explore files, run tools, ask questions, or write any prose. "
        "Do NOT acknowledge instructions. Output ONLY the JSON array specified below.\n\n"
        f"{persona.system_instruction}\n\n"
        f"FOLDER TONES (apply per item's 'folder' field):\n{tone_block}\n\n"
        "TASK: Translate each item's 'kr' field to English per persona + folder tone.\n"
        "OUTPUT FORMAT: A single JSON array. No markdown fences. No commentary before or after.\n"
        'Schema: [{"id":"<copy id verbatim>","en":"<translation>"}, ...]\n'
        "Rules: preserve every id exactly. One object per input item. No omissions, no extras.\n"
        "First character of your response MUST be `[`. Last character MUST be `]`.\n\n"
        f"ITEMS:\n{_json_compact(items)}\n"
    )


def build_curate_prompt_kr(items: list[dict]) -> str:
    """Cluster Korean sentences into recurring KR expressions."""
    return (
        "STRICT MODE: You are a Korean phrase clusterer. "
        "Do NOT explore files, run tools, ask questions, or write any prose. "
        "Output ONLY the JSON array specified below.\n\n"
        "TASK: From the input sentences, identify RECURRING KOREAN EXPRESSIONS the writer "
        "uses as a thinking pattern. An expression is a reusable phrasing fragment "
        "(2~8 어절). Examples of valid expressions: "
        "'~할 수밖에 없다', '결국에는 ~로 귀결된다', '핵심은 ~에 있다', "
        "'이는 ~과 같다', '~의 관점에서 보면'.\n\n"
        "CLUSTERING RULES:\n"
        "- Group sentences that share the same underlying expression pattern.\n"
        "- Canonical form: write the expression in a generic, reusable shape "
        "(use ~ or X for slot placeholders).\n"
        "- A sentence may belong to AT MOST ONE expression.\n"
        "- If a sentence has no clear recurring pattern, OMIT it (do not force).\n"
        "- Prefer fewer, broader expressions over many narrow ones.\n"
        "- Two clusters that mean the same thing must be merged.\n\n"
        "OUTPUT FORMAT: Single JSON array. No markdown fences.\n"
        'Schema: [{"expr":"<canonical KR expression with ~/X slots>",'
        '"members":["<id>","<id>",...]}, ...]\n'
        "First character `[`. Last character `]`.\n\n"
        f"SENTENCES:\n{_json_compact(items)}\n"
    )


def build_curate_prompt_en(items: list[dict]) -> str:
    """Cluster English sentences into recurring English expression patterns.

    Use for native-source learning materials (books, lectures).
    Patterns: idioms, sentence frames, discourse markers, advanced constructions
    the learner wants to absorb.
    """
    return (
        "STRICT MODE: You are an English phrase clusterer. "
        "Do NOT explore files, run tools, ask questions, or write any prose. "
        "Output ONLY the JSON array specified below.\n\n"
        "TASK: From the input English sentences, identify RECURRING IDIOMATIC PATTERNS "
        "worth memorising — sentence frames, discourse markers, advanced collocations, "
        "or hedging structures. Examples of valid patterns: "
        "'It stands to reason that ~', 'Far from being X, Y', "
        "'~ is precisely the kind of thing that ~', 'Insofar as ~', "
        "'There is no shortage of ~', 'X and Y alike'.\n\n"
        "CLUSTERING RULES:\n"
        "- Group sentences that share the same underlying pattern.\n"
        "- Canonical form: write the pattern with ~ or X slot placeholders for variable parts.\n"
        "- A sentence may belong to AT MOST ONE pattern.\n"
        "- If a sentence has no notable recurring pattern, OMIT it.\n"
        "- Prefer fewer, broader patterns over many narrow ones.\n"
        "- Skip trivial patterns ('I think that ~', 'It is ~').\n"
        "- Two clusters that express the same idea must be merged.\n\n"
        "OUTPUT FORMAT: Single JSON array. No markdown fences.\n"
        'Schema: [{"expr":"<canonical EN pattern with ~/X slots>",'
        '"members":["<id>","<id>",...]}, ...]\n'
        "First character `[`. Last character `]`.\n\n"
        f"SENTENCES:\n{_json_compact(items)}\n"
    )


def build_curate_prompt(items: list[dict], lang: str = "kr") -> str:
    """Dispatch by lang."""
    if lang == "en":
        return build_curate_prompt_en(items)
    return build_curate_prompt_kr(items)


def build_label_prompt(items: list[dict], lang: str = "kr") -> str:
    """Ask LLM to name canonical patterns from clusters of sentences.

    Input items: [{id:int, samples:[sentence_text, ...]}, ...]
    Output: [{id:int, canonical:str, gloss:str}, ...]

    Used after deterministic embedding-based clustering. Each item is one
    cluster — the LLM only names it (does NOT decide membership).
    """
    if lang == "en":
        return (
            "STRICT MODE: You are an English phrase pattern namer. "
            "Do NOT explore, run tools, or write prose. JSON only.\n\n"
            "TASK: For each cluster of English sentences, write the canonical "
            "idiomatic pattern they share. Use ~ or X for variable slots.\n"
            "Provide a one-sentence gloss describing when this pattern is used.\n\n"
            "OUTPUT: Single JSON array. No markdown fences.\n"
            'Schema: [{"id":<int>,"canonical":"<EN pattern with ~/X>","gloss":"..."}, ...]\n'
            "First char `[`. Last char `]`.\n\n"
            f"CLUSTERS:\n{_json_compact(items)}\n"
        )
    return (
        "STRICT MODE: You are a Korean phrase pattern namer. "
        "Do NOT explore, run tools, or write prose. JSON only.\n\n"
        "TASK: For each cluster of Korean sentences, write the canonical "
        "KOREAN expression pattern they share. Use ~ or X for variable slots. "
        "Examples of valid canonical forms: '~할 수밖에 없다', '결국 ~로 귀결된다'.\n"
        "Provide a one-sentence English gloss describing when this expression is used.\n\n"
        "OUTPUT: Single JSON array. No markdown fences.\n"
        'Schema: [{"id":<int>,"canonical":"<KR pattern with ~/X>","gloss":"..."}, ...]\n'
        "First char `[`. Last char `]`.\n\n"
        f"CLUSTERS:\n{_json_compact(items)}\n"
    )


def build_merge_prompt(expressions: list[dict], lang: str = "kr") -> str:
    """Ask the model to merge semantically-duplicate expression rows.

    Input items: [{id:int, expr:str}, ...]
    Output: [{"canonical_id":int,"duplicate_ids":[int,...],"canonical_expr":str}, ...]
    Only include groups with >=2 members. Single-member groups are implicit.
    """
    lang_name = "Korean" if lang == "kr" else "English"
    return (
        "STRICT MODE: You are an expression deduplicator. "
        "Do NOT explore files, run tools, ask questions, or write any prose. "
        "Output ONLY the JSON array specified below.\n\n"
        f"TASK: Given {lang_name} expression patterns extracted from a corpus, "
        "find groups that mean THE SAME THING but differ in surface form "
        "(spacing, particle choice, synonym swap, minor variations).\n\n"
        "MERGE RULES:\n"
        "- Group ONLY if patterns convey the same intent in the same syntactic role.\n"
        "- Pick one id as 'canonical' (prefer the most general / cleanest form).\n"
        "- 'canonical_expr' may rewrite the canonical pattern for clarity.\n"
        "- 'duplicate_ids' lists the OTHER ids in the group (excluding canonical_id).\n"
        "- Skip groups with only 1 member (no merge needed).\n"
        "- Be conservative: when in doubt, do NOT merge.\n\n"
        "OUTPUT FORMAT: Single JSON array. No markdown fences.\n"
        'Schema: [{"canonical_id":<int>,"canonical_expr":"...","duplicate_ids":[<int>,...]}, ...]\n'
        "First character `[`. Last character `]`.\n\n"
        f"EXPRESSIONS:\n{_json_compact(expressions)}\n"
    )


def build_expression_translation_prompt(items: list[dict], persona: PersonaConfig) -> str:
    """Translate canonical KR expressions to canonical EN expressions + gloss.

    Items: [{id, kr_expr}, ...]
    Output: [{id, en_expr, gloss}, ...]
    """
    return (
        "STRICT MODE: You are a Korean→English expression translator. "
        "Do NOT explore files, run tools, ask questions, or write any prose. "
        "Output ONLY the JSON array specified below.\n\n"
        f"{persona.system_instruction}\n\n"
        "TASK: For each Korean expression pattern (with ~/X slots), produce:\n"
        "  en_expr — canonical English rendering preserving slot positions "
        "(use the same ~ or X slot markers).\n"
        "  gloss   — one short English sentence (<=15 words) describing when this expression "
        "is used and what it conveys.\n\n"
        "OUTPUT FORMAT: Single JSON array. No markdown fences.\n"
        'Schema: [{"id":"<copy id>","en_expr":"...","gloss":"..."}, ...]\n'
        "First character `[`. Last character `]`.\n\n"
        f"ITEMS:\n{_json_compact(items)}\n"
    )


def _json_compact(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# re-export for crawler
resolve_folder_key = _resolve_folder_key
