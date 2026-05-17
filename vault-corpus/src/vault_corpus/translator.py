"""KR→EN translation pipeline.

This module owns every step of the Korean → English chunk translation
pathway. Pure prompt construction lives in :func:`build_translation_prompt`
and pure response parsing in :func:`parse_translation_response`, so both
sides can be inspected and tested without any network calls; the actual
OpenAI client invocation (:func:`translate_chunk`) layers on top.

Running this module directly as ``python -m vault_corpus.translator``
exposes an inspection CLI (see :func:`_main`) that prints the exact
request payload, the system prompt template, and the translated body for a
fixture chunk — with either a stubbed client (``--demo``, no API key
needed) or the real OpenAI client. This is the documented entry point for
inspecting a single translation in isolation.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from .chunker import Chunk, compute_chunk_id


class TranslationResponseError(ValueError):
    """Raised when an OpenAI translation response cannot be parsed.

    Carries a human-readable message describing which field was missing
    or malformed so the caller can surface a precise diagnostic without
    re-inspecting the raw response.
    """

# Default chat model. The seed allows either gpt-5-mini or gpt-4o-mini;
# we ship gpt-4o-mini as the default because it is GA, low cost, and well
# benchmarked for KR→EN. Overridable per call.
DEFAULT_TRANSLATION_MODEL = "gpt-4o-mini"

# Deterministic seed for OpenAI's chat ``seed`` parameter. Combined with
# temperature=0 and a fixed system prompt this makes the translation request
# reproducible to the extent the OpenAI API guarantees (best-effort, but
# stable across retries within a model version).
DEFAULT_TRANSLATION_SEED = 7

# System prompt template. Kept as a module-level constant so unit tests can
# assert on it exactly and so prompt changes show up as a single-line diff.
TRANSLATION_SYSTEM_PROMPT = (
    "You are a precise Korean-to-English translator for Markdown notes.\n"
    "Translate the user's Korean Markdown content into natural English while:\n"
    "  - Preserving Markdown structure exactly: heading levels (##, ###),"
    " list markers, bold/italic, blockquotes, tables.\n"
    "  - Preserving code blocks verbatim — do NOT translate code, identifiers,"
    " or fenced block contents.\n"
    "  - Preserving inline code spans, URLs, and Obsidian [[wikilinks]] verbatim.\n"
    "  - Preserving the heading chain: if the input begins with a ## or ###"
    " heading, the output must begin with the same heading level.\n"
    "  - Translating only natural-language Korean. Leave English passages,"
    " numbers, and proper nouns unchanged unless a standard English form exists.\n"
    "Output English Markdown only. No commentary, no preface, no surrounding"
    " quotes or code fences."
)


def build_translation_prompt(
    chunk: Chunk,
    *,
    model: str = DEFAULT_TRANSLATION_MODEL,
    seed: int = DEFAULT_TRANSLATION_SEED,
) -> dict[str, Any]:
    """Build the deterministic OpenAI chat request payload for a chunk.

    Pure function: takes a Korean :class:`Chunk`, returns the exact ``dict``
    that should be splatted into ``client.chat.completions.create(**payload)``.
    No I/O, no API calls, no client construction.

    The payload is fully deterministic given the same ``chunk`` + ``model`` +
    ``seed`` inputs:

    * ``temperature=0`` — zero sampling randomness.
    * ``seed`` — passed to OpenAI's deterministic-sampling hint.
    * ``messages`` — fixed system prompt + chunk body as the user turn.

    The user message is the chunk ``body`` verbatim (heading line included)
    so the model sees the same markdown structure that will appear in the
    English mirror. This keeps the KR→EN mapping 1:1 at the chunk level —
    the translated body is what gets stored under the same ``chunk_id``.

    Args:
        chunk: Korean source chunk. Only ``body`` is sent over the wire;
            ``source_path``, ``frontmatter``, etc. stay local.
        model: OpenAI chat model id. Defaults to
            :data:`DEFAULT_TRANSLATION_MODEL`.
        seed: Deterministic-sampling seed. Defaults to
            :data:`DEFAULT_TRANSLATION_SEED`.

    Returns:
        Dict with keys ``model``, ``temperature``, ``seed``, ``messages``
        — ready to pass to ``client.chat.completions.create``.
    """
    return {
        "model": model,
        "temperature": 0,
        "seed": seed,
        "messages": [
            {"role": "system", "content": TRANSLATION_SYSTEM_PROMPT},
            {"role": "user", "content": chunk.body},
        ],
    }


# Matches a leading ``##``/``###`` ATX heading line in a translated chunk
# body. Kept locally (rather than reusing ``chunker._HEADING_RE``) so the
# parser is self-contained and stable even if the chunker's regex shifts.
_RESPONSE_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*#*\s*$")


def _resolve(container: Any, key: str) -> Any:
    """Lookup ``key`` on ``container``, supporting dict and SDK-object shapes.

    The OpenAI Python SDK returns ``ChatCompletion`` objects that expose
    ``choices``, ``message``, ``content`` as attributes; the raw HTTP JSON
    exposes the same fields as dict keys. Tests typically hand-craft dicts
    while production code passes SDK objects. Supporting both keeps the
    parser usable from either side without a translation step.
    """
    if isinstance(container, dict):
        if key not in container:
            raise KeyError(key)
        return container[key]
    try:
        return getattr(container, key)
    except AttributeError as exc:
        raise KeyError(key) from exc


def _extract_message_content(raw_response: Any) -> str:
    """Pull ``choices[0].message.content`` out of an OpenAI chat response.

    Raises :class:`TranslationResponseError` (not :class:`KeyError` /
    :class:`AttributeError`) so callers can use a single exception type
    in their retry / dead-letter logic.
    """
    if raw_response is None:
        raise TranslationResponseError("response is None")

    try:
        choices = _resolve(raw_response, "choices")
    except KeyError as exc:
        raise TranslationResponseError("response is missing 'choices'") from exc

    if choices is None:
        raise TranslationResponseError("response 'choices' is None")
    try:
        first_choice = choices[0]
    except (IndexError, TypeError) as exc:
        raise TranslationResponseError("response 'choices' is empty or not indexable") from exc

    try:
        message = _resolve(first_choice, "message")
    except KeyError as exc:
        raise TranslationResponseError("first choice is missing 'message'") from exc

    try:
        content = _resolve(message, "content")
    except KeyError as exc:
        raise TranslationResponseError("message is missing 'content'") from exc

    if content is None:
        raise TranslationResponseError("message 'content' is null")
    if not isinstance(content, str):
        raise TranslationResponseError(
            f"message 'content' is not a string (got {type(content).__name__})"
        )
    return content


def _extract_heading_chain(body: str) -> list[str]:
    """Walk leading ``##``/``###`` heading lines and return their titles.

    Stops at the first non-heading content line. Blank lines that appear
    between consecutive heading lines are tolerated (so ``## A\\n\\n### B``
    yields ``["A", "B"]``) but a leading blank-only prefix with no heading
    after it yields ``[]``.

    A chunk body with no leading heading (preamble or note with no
    ``##``/``###`` headings at all) yields an empty list — matching the
    chunker's ``heading_chain=[]`` convention.
    """
    chain: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            if chain:
                # Blank line between headings — keep walking.
                continue
            # Leading blank line before any heading — keep scanning so a
            # response that starts with a blank line then `## H` still
            # gets parsed cleanly.
            continue
        m = _RESPONSE_HEADING_RE.match(line)
        if not m:
            break
        chain.append(m.group(2).strip())
    return chain


def parse_translation_response(raw_response: Any) -> dict[str, Any]:
    """Extract translated body + heading_chain from an OpenAI chat response.

    Pure function: no I/O, no client construction. Accepts either a dict
    response (test fixtures, raw HTTP JSON) or an OpenAI SDK
    ``ChatCompletion`` object (attribute access). The return value is a
    plain ``dict`` so the caller can serialize, log, or merge it without
    needing the SDK as a dependency.

    The ``heading_chain`` is derived from the leading ``##``/``###`` lines
    of the translated body via :func:`_extract_heading_chain`. Because the
    translation prompt sends the source chunk body verbatim (heading line
    included), a well-formed response begins with the translated form of
    that same heading — making the chain recoverable without an extra
    round-trip.

    Args:
        raw_response: OpenAI chat completion response. Dict or SDK object.

    Returns:
        Dict with two keys:

        * ``body`` (``str``) — the translated English Markdown content,
          verbatim from ``choices[0].message.content``.
        * ``heading_chain`` (``list[str]``) — translated heading titles
          parsed from the leading ``##``/``###`` lines of ``body``.

    Raises:
        TranslationResponseError: When the response is ``None``, missing
            ``choices``, has an empty ``choices`` list, is missing the
            nested ``message`` / ``content`` fields, or carries a
            ``content`` value that is not a string.
    """
    content = _extract_message_content(raw_response)
    heading_chain = _extract_heading_chain(content)
    return {"body": content, "heading_chain": heading_chain}


def translate_chunk(
    chunk: Chunk,
    client: Any,
    *,
    model: str = DEFAULT_TRANSLATION_MODEL,
    seed: int = DEFAULT_TRANSLATION_SEED,
) -> Chunk:
    """Translate a Korean :class:`Chunk` to English via the OpenAI chat API.

    Composition pipeline:

    1. :func:`build_translation_prompt` constructs the deterministic chat
       payload from the source chunk.
    2. ``client.chat.completions.create(**payload)`` sends the request to
       OpenAI. The ``client`` argument is duck-typed so unit tests can pass a
       ``unittest.mock.MagicMock`` and assert on the call arguments.
    3. :func:`parse_translation_response` extracts the translated body and
       its heading chain from the response.

    The returned chunk preserves the source ``chunk_id``, ``source_path``,
    and ``frontmatter`` verbatim — only ``body``, ``heading_chain``, and
    ``lang`` change. Sharing the ``chunk_id`` is intentional: the seed
    contract requires that the English mirror can be looked up by the same
    content-derived id as its Korean source, so a pgvector migration never
    has to re-embed or re-key chunks.

    Args:
        chunk: Korean source chunk. Must have ``lang == "ko"``.
        client: OpenAI client object exposing
            ``client.chat.completions.create(**payload)``. Real production
            callers pass an ``openai.OpenAI()`` instance; tests pass a mock.
        model: OpenAI chat model id. Forwarded to
            :func:`build_translation_prompt`. Defaults to
            :data:`DEFAULT_TRANSLATION_MODEL`.
        seed: Deterministic-sampling seed. Forwarded to
            :func:`build_translation_prompt`. Defaults to
            :data:`DEFAULT_TRANSLATION_SEED`.

    Returns:
        A new frozen :class:`Chunk` with ``lang="en"``, the translated
        ``body`` and ``heading_chain``, and the **same** ``chunk_id``,
        ``source_path``, and ``frontmatter`` as the input.

    Raises:
        TranslationResponseError: Propagated from
            :func:`parse_translation_response` when the OpenAI response is
            malformed (missing ``choices`` / ``message`` / ``content``,
            null content, non-string content, etc.).
    """
    payload = build_translation_prompt(chunk, model=model, seed=seed)
    raw_response = client.chat.completions.create(**payload)
    parsed = parse_translation_response(raw_response)
    return replace(
        chunk,
        body=parsed["body"],
        heading_chain=list(parsed["heading_chain"]),
        lang="en",
    )


# ---------------------------------------------------------------------------
# Inspection CLI — ``python -m vault_corpus.translator``
# ---------------------------------------------------------------------------
#
# Sub-AC 4.4: a copy-pasteable command for inspecting a single translation
# in isolation. The CLI deliberately keeps zero hidden state and zero
# side-effects beyond stdout — no DB writes, no file writes, no network
# calls in ``--demo`` mode. A smoke test exercises this exact entry point
# with a stubbed client to assert non-empty English output (see
# ``tests/test_translator_cli.py``).


# Fixture chunk used by the inspection CLI's ``--demo`` mode. Picked to be
# short, contain a `##` heading (so the heading_chain round-trip is visible
# in the printed output), and use natural Korean so a real OpenAI call would
# also produce a meaningful English result.
_DEMO_KOREAN_BODY = "## 목표\n매일 코드를 짠다.\n"

# Canned English the stub client returns for the fixture. Kept here (not in
# the test) so the CLI's ``--demo`` is fully self-contained and the README
# snippet's expected output is reproducible without importing test helpers.
_DEMO_ENGLISH_BODY = "## Goal\nWrite code daily.\n"


class _StubChatCompletions:
    """Stand-in for ``client.chat.completions``.

    ``create(**payload)`` returns a dict shaped like the OpenAI chat
    completion JSON response, with ``choices[0].message.content`` set to
    the canned English body. The shape matches what
    :func:`parse_translation_response` accepts, so the rest of the
    translator pipeline runs untouched in demo mode.
    """

    def __init__(self, english_body: str) -> None:
        self._english_body = english_body
        # Capture the most recent call so the CLI can echo back the exact
        # payload that was "sent" — useful when inspecting without a real
        # API key.
        self.last_payload: dict[str, Any] | None = None

    def create(self, **payload: Any) -> dict[str, Any]:
        self.last_payload = payload
        return {
            "id": "chatcmpl-stub",
            "object": "chat.completion",
            "model": payload.get("model", DEFAULT_TRANSLATION_MODEL),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": self._english_body},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }


class _StubClient:
    """Minimal OpenAI-client stand-in exposing ``client.chat.completions.create``.

    Used by ``python -m vault_corpus.translator --demo`` so the inspection
    command runs with zero network calls and zero ``OPENAI_API_KEY``
    requirement. Production callers pass a real ``openai.OpenAI()`` instance
    instead.
    """

    def __init__(self, english_body: str = _DEMO_ENGLISH_BODY) -> None:
        self.chat = _StubChat(english_body)


class _StubChat:
    def __init__(self, english_body: str) -> None:
        self.completions = _StubChatCompletions(english_body)


def _build_demo_chunk(korean_body: str = _DEMO_KOREAN_BODY) -> Chunk:
    """Construct the fixture Korean chunk used by the inspection CLI."""
    source_path = Path("notes/demo-fixture.md")
    # Heading chain is empty for body-only inputs; otherwise mirror the
    # leading ``##`` so chunk_id matches what the chunker would produce.
    heading_chain: list[str] = []
    first_line = korean_body.lstrip().splitlines()[0] if korean_body.strip() else ""
    if first_line.startswith("## "):
        heading_chain = [first_line[3:].rstrip(" #").strip()]
    elif first_line.startswith("### "):
        heading_chain = [first_line[4:].rstrip(" #").strip()]
    return Chunk(
        source_path=source_path,
        heading_chain=heading_chain,
        body=korean_body,
        chunk_id=compute_chunk_id(source_path, heading_chain, korean_body),
        lang="ko",
        frontmatter={},
    )


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m vault_corpus.translator",
        description=(
            "Inspect a single KR→EN translation in isolation. "
            "Prints the request payload, the prompt template, and the "
            "translated English body for a fixture chunk."
        ),
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help=(
            "Use a built-in stub OpenAI client instead of hitting the real "
            "API. No OPENAI_API_KEY required. Default when neither --demo "
            "nor --live is given."
        ),
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Use the real openai.OpenAI() client. Requires OPENAI_API_KEY "
            "in the environment. Mutually exclusive with --demo."
        ),
    )
    parser.add_argument(
        "--text",
        default=None,
        metavar="KOREAN",
        help=(
            "Korean markdown body to translate instead of the built-in "
            "fixture. Useful for ad-hoc inspection of a specific chunk."
        ),
    )
    parser.add_argument(
        "--show-payload",
        action="store_true",
        help="Print the JSON request payload to stdout before translating.",
    )
    parser.add_argument(
        "--show-prompt",
        action="store_true",
        help="Print the system prompt template to stdout before translating.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_TRANSLATION_MODEL,
        help=f"OpenAI chat model id (default: {DEFAULT_TRANSLATION_MODEL}).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_TRANSLATION_SEED,
        help=f"Deterministic seed (default: {DEFAULT_TRANSLATION_SEED}).",
    )
    return parser


def _main(argv: list[str] | None = None) -> int:
    """Inspection CLI entry point. Returns a Unix exit code.

    Behaviour:

    * ``--demo`` (default if neither flag given): use :class:`_StubClient`,
      print translated body to stdout, exit 0.
    * ``--live``: import ``openai.OpenAI``, hit the real API, print
      translated body to stdout. Requires ``OPENAI_API_KEY``.
    * ``--show-payload``: print the JSON request payload before
      translating.
    * ``--show-prompt``: print the system prompt template before
      translating.
    * ``--text "<korean>"``: translate an arbitrary Korean string instead
      of the built-in fixture.

    The function is exposed (rather than inlined into ``if __name__ ==
    "__main__"``) so tests can call it in-process and the smoke test can
    also drive it via ``subprocess`` without any path hacks.
    """
    args = _build_argparser().parse_args(argv)

    if args.demo and args.live:
        print("error: --demo and --live are mutually exclusive", file=sys.stderr)
        return 2

    # Default to demo when neither flag is given so the documented
    # copy-paste command works without an API key.
    use_stub = args.demo or not args.live

    korean_body = args.text if args.text is not None else _DEMO_KOREAN_BODY
    chunk = _build_demo_chunk(korean_body)
    payload = build_translation_prompt(chunk, model=args.model, seed=args.seed)

    if args.show_prompt:
        print("=== system prompt template ===")
        print(TRANSLATION_SYSTEM_PROMPT)
        print()

    if args.show_payload:
        print("=== request payload ===")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print()

    if use_stub:
        # Pick canned English: if the user supplied --text, echo a generic
        # stub line; otherwise return the fixture's expected English so
        # README output and smoke-test assertions stay stable.
        english_body = (
            _DEMO_ENGLISH_BODY
            if args.text is None
            else "## (stub translation)\n" + args.text.strip() + "\n"
        )
        client: Any = _StubClient(english_body)
    else:
        try:
            from openai import OpenAI  # local import: only required for --live
        except ImportError as exc:  # pragma: no cover — exercised manually
            print(f"error: openai SDK not installed: {exc}", file=sys.stderr)
            return 3
        client = OpenAI()

    translated = translate_chunk(chunk, client, model=args.model, seed=args.seed)
    print("=== translated body ===")
    print(translated.body, end="" if translated.body.endswith("\n") else "\n")
    print(f"=== chunk_id (unchanged): {translated.chunk_id} ===")
    print(f"=== heading_chain: {translated.heading_chain} ===")
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess test
    raise SystemExit(_main())
