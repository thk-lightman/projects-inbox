"""Unit tests for :mod:`vault_corpus.translator`.

Sub-AC 4.1 — ``build_translation_prompt`` is a pure function. These tests
assert payload shape and prompt-template content without any network calls
or OpenAI client construction.

Sub-AC 4.2 — ``parse_translation_response`` is a pure function. Tests use
hand-crafted dict mocks (dict shape and SDK-attribute shape) to assert
correct body and heading_chain extraction plus malformed-response error
handling.

Sub-AC 4.3 — ``translate_chunk`` wires the prompt builder, an injected
OpenAI client, and the response parser. Tests pass a
``unittest.mock.MagicMock`` shaped like the OpenAI SDK and assert
chunk_id preservation, ``lang="en"`` on the returned chunk, and that the
mock client was called with the exact expected payload.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from vault_corpus.chunker import Chunk, compute_chunk_id
from vault_corpus.translator import (
    DEFAULT_TRANSLATION_MODEL,
    DEFAULT_TRANSLATION_SEED,
    TRANSLATION_SYSTEM_PROMPT,
    TranslationResponseError,
    build_translation_prompt,
    parse_translation_response,
    translate_chunk,
)


def _mock_response(content: str | None) -> dict:
    """Hand-craft a dict shaped like OpenAI chat completion JSON."""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _mock_sdk_response(content: str | None) -> SimpleNamespace:
    """Hand-craft a namespace shaped like an OpenAI SDK ``ChatCompletion``."""
    return SimpleNamespace(
        id="chatcmpl-test",
        model="gpt-4o-mini",
        choices=[
            SimpleNamespace(
                index=0,
                message=SimpleNamespace(role="assistant", content=content),
                finish_reason="stop",
            )
        ],
    )


def _make_chunk(
    body: str = "## 목표\n매일 코드를 짠다.\n",
    heading_chain: list[str] | None = None,
    source_path: Path = Path("notes/sample.md"),
) -> Chunk:
    chain = heading_chain if heading_chain is not None else ["목표"]
    return Chunk(
        source_path=source_path,
        heading_chain=chain,
        body=body,
        chunk_id=compute_chunk_id(source_path, chain, body),
        lang="ko",
        frontmatter={},
    )


def test_returns_dict_with_required_top_level_keys():
    payload = build_translation_prompt(_make_chunk())
    assert set(payload.keys()) == {"model", "temperature", "seed", "messages"}


def test_default_model_and_temperature_and_seed():
    payload = build_translation_prompt(_make_chunk())
    assert payload["model"] == DEFAULT_TRANSLATION_MODEL
    assert payload["model"] == "gpt-4o-mini"
    assert payload["temperature"] == 0
    assert payload["seed"] == DEFAULT_TRANSLATION_SEED


def test_messages_is_list_of_system_then_user():
    payload = build_translation_prompt(_make_chunk())
    msgs = payload["messages"]
    assert isinstance(msgs, list)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"


def test_system_message_uses_template_constant_verbatim():
    payload = build_translation_prompt(_make_chunk())
    assert payload["messages"][0]["content"] == TRANSLATION_SYSTEM_PROMPT


def test_system_prompt_template_contains_required_directives():
    # Anchor on substantive prompt content so silent edits to the template
    # that drop a required directive break the test.
    required_phrases = [
        "Korean-to-English",
        "Markdown",
        "code blocks verbatim",
        "wikilinks",
        "heading",
        "English Markdown only",
    ]
    for phrase in required_phrases:
        assert phrase in TRANSLATION_SYSTEM_PROMPT, f"missing directive: {phrase!r}"


def test_user_message_is_chunk_body_verbatim():
    body = "## 학습\n오늘은 transformers 논문을 읽었다.\n```py\nprint('안녕')\n```\n"
    chunk = _make_chunk(body=body, heading_chain=["학습"])
    payload = build_translation_prompt(chunk)
    assert payload["messages"][1]["content"] == body


def test_model_override_is_respected():
    payload = build_translation_prompt(_make_chunk(), model="gpt-5-mini")
    assert payload["model"] == "gpt-5-mini"


def test_seed_override_is_respected():
    payload = build_translation_prompt(_make_chunk(), seed=42)
    assert payload["seed"] == 42


def test_purity_same_inputs_same_payload():
    chunk = _make_chunk()
    p1 = build_translation_prompt(chunk)
    p2 = build_translation_prompt(chunk)
    assert p1 == p2
    # Distinct dict objects — caller can mutate one without bleeding into the other.
    assert p1 is not p2
    assert p1["messages"] is not p2["messages"]


def test_no_network_or_client_construction(monkeypatch):
    # Smoke test: importing translator and calling build_translation_prompt
    # must not touch the OpenAI client. Make the OpenAI symbol explode if
    # anyone tries to construct it.
    import vault_corpus.translator as translator_mod

    sentinel_called = {"hit": False}

    class _Boom:
        def __init__(self, *a, **kw):
            sentinel_called["hit"] = True
            raise RuntimeError("OpenAI client must not be constructed in pure prompt builder")

    # Inject a fake OpenAI attribute regardless of whether the module imports it.
    monkeypatch.setattr(translator_mod, "OpenAI", _Boom, raising=False)
    build_translation_prompt(_make_chunk())
    assert sentinel_called["hit"] is False


def test_only_chunk_body_is_sent_not_path_or_frontmatter():
    chunk = Chunk(
        source_path=Path("/abs/secret/path.md"),
        heading_chain=["heading"],
        body="## heading\nbody text\n",
        chunk_id="dummy-id",
        lang="ko",
        frontmatter={"tags": ["private"], "status": "draft"},
    )
    payload = build_translation_prompt(chunk)
    serialized = repr(payload)
    assert "/abs/secret/path.md" not in serialized
    assert "private" not in serialized
    assert "draft" not in serialized
    assert "dummy-id" not in serialized


def test_chunk_with_empty_body_still_builds_valid_payload():
    chunk = _make_chunk(body="", heading_chain=[])
    payload = build_translation_prompt(chunk)
    assert payload["messages"][1]["content"] == ""
    assert payload["messages"][0]["content"] == TRANSLATION_SYSTEM_PROMPT


@pytest.mark.parametrize(
    "body",
    [
        "## 목표\n매일 코드를 짠다.\n",
        "### 세부 항목\n- 항목 1\n- 항목 2\n",
        "본문만 있는 청크. 헤딩 없음.\n",
        "```python\n# 한글 주석은 코드라 번역 안 함\n```\n",
    ],
)
def test_payload_shape_stable_across_chunk_variants(body):
    payload = build_translation_prompt(_make_chunk(body=body, heading_chain=[]))
    assert payload["model"] == DEFAULT_TRANSLATION_MODEL
    assert payload["temperature"] == 0
    assert payload["seed"] == DEFAULT_TRANSLATION_SEED
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][1]["role"] == "user"
    assert payload["messages"][1]["content"] == body


# ---------------------------------------------------------------------------
# Sub-AC 4.2 — parse_translation_response
# ---------------------------------------------------------------------------


def test_parse_returns_dict_with_body_and_heading_chain_keys():
    parsed = parse_translation_response(_mock_response("## Goal\nWrite code daily.\n"))
    assert set(parsed.keys()) == {"body", "heading_chain"}


def test_parse_body_is_verbatim_message_content():
    content = "## Goal\nWrite code daily.\n\n- subgoal\n"
    parsed = parse_translation_response(_mock_response(content))
    assert parsed["body"] == content


def test_parse_heading_chain_single_h2():
    parsed = parse_translation_response(_mock_response("## Goal\nbody text\n"))
    assert parsed["heading_chain"] == ["Goal"]


def test_parse_heading_chain_single_h3():
    parsed = parse_translation_response(_mock_response("### Sub-goal\nbody\n"))
    assert parsed["heading_chain"] == ["Sub-goal"]


def test_parse_heading_chain_nested_h2_then_h3():
    parsed = parse_translation_response(
        _mock_response("## Parent\n### Child\nbody line\n")
    )
    assert parsed["heading_chain"] == ["Parent", "Child"]


def test_parse_heading_chain_tolerates_blank_line_between_headings():
    parsed = parse_translation_response(
        _mock_response("## Parent\n\n### Child\nbody\n")
    )
    assert parsed["heading_chain"] == ["Parent", "Child"]


def test_parse_heading_chain_empty_when_no_heading():
    parsed = parse_translation_response(
        _mock_response("Just body text, no heading.\n")
    )
    assert parsed["heading_chain"] == []
    assert parsed["body"] == "Just body text, no heading.\n"


def test_parse_heading_chain_stops_at_first_body_line():
    parsed = parse_translation_response(
        _mock_response("## Goal\nbody line\n### Not in chain\nmore body\n")
    )
    assert parsed["heading_chain"] == ["Goal"]


def test_parse_heading_chain_ignores_h1_and_h4():
    # H1 and H4+ are not chunk boundaries per chunker; parser must mirror that.
    parsed = parse_translation_response(
        _mock_response("# Title H1\nbody\n")
    )
    assert parsed["heading_chain"] == []
    parsed = parse_translation_response(
        _mock_response("#### Deep heading\nbody\n")
    )
    assert parsed["heading_chain"] == []


def test_parse_heading_chain_strips_trailing_hashes_and_whitespace():
    parsed = parse_translation_response(
        _mock_response("##   Spaced Title   ##\nbody\n")
    )
    assert parsed["heading_chain"] == ["Spaced Title"]


def test_parse_accepts_sdk_object_shape():
    parsed = parse_translation_response(
        _mock_sdk_response("## Goal\nWrite code daily.\n")
    )
    assert parsed["body"] == "## Goal\nWrite code daily.\n"
    assert parsed["heading_chain"] == ["Goal"]


def test_parse_purity_same_input_same_output():
    resp = _mock_response("## H\nbody\n")
    p1 = parse_translation_response(resp)
    p2 = parse_translation_response(resp)
    assert p1 == p2
    assert p1 is not p2
    assert p1["heading_chain"] is not p2["heading_chain"]


# ---- Malformed response error handling -----------------------------------


def test_parse_raises_on_none_response():
    with pytest.raises(TranslationResponseError, match="None"):
        parse_translation_response(None)


def test_parse_raises_on_missing_choices_key():
    with pytest.raises(TranslationResponseError, match="choices"):
        parse_translation_response({"id": "x", "model": "gpt-4o-mini"})


def test_parse_raises_on_empty_choices_list():
    with pytest.raises(TranslationResponseError, match="empty"):
        parse_translation_response({"choices": []})


def test_parse_raises_on_none_choices():
    with pytest.raises(TranslationResponseError, match="None"):
        parse_translation_response({"choices": None})


def test_parse_raises_on_missing_message():
    with pytest.raises(TranslationResponseError, match="message"):
        parse_translation_response({"choices": [{"index": 0}]})


def test_parse_raises_on_missing_content():
    with pytest.raises(TranslationResponseError, match="content"):
        parse_translation_response(
            {"choices": [{"message": {"role": "assistant"}}]}
        )


def test_parse_raises_on_null_content():
    with pytest.raises(TranslationResponseError, match="null"):
        parse_translation_response(_mock_response(None))


def test_parse_raises_on_non_string_content():
    with pytest.raises(TranslationResponseError, match="not a string"):
        parse_translation_response(
            {"choices": [{"message": {"role": "assistant", "content": 42}}]}
        )


def test_parse_error_is_value_error_subclass():
    # Callers can catch the broader category for generic retry logic.
    assert issubclass(TranslationResponseError, ValueError)


# ---------------------------------------------------------------------------
# Sub-AC 4.3 — translate_chunk (mocked OpenAI client)
# ---------------------------------------------------------------------------


def _client_returning(content: str | None, *, sdk_shape: bool = False) -> MagicMock:
    """Build a mock OpenAI client whose ``chat.completions.create`` returns a
    canned chat-completion response.

    The mock mirrors the public surface area used by ``translate_chunk``:
    ``client.chat.completions.create(**payload)``. Tests can inspect
    ``client.chat.completions.create.call_args`` after the call to verify
    the exact payload that was sent.
    """
    client = MagicMock(name="OpenAIClient")
    response = (
        _mock_sdk_response(content) if sdk_shape else _mock_response(content)
    )
    client.chat.completions.create.return_value = response
    return client


def test_translate_chunk_returns_chunk_with_lang_en():
    chunk = _make_chunk()
    client = _client_returning("## Goal\nWrite code daily.\n")
    translated = translate_chunk(chunk, client)
    assert isinstance(translated, Chunk)
    assert translated.lang == "en"


def test_translate_chunk_preserves_chunk_id():
    chunk = _make_chunk()
    client = _client_returning("## Goal\nWrite code daily.\n")
    translated = translate_chunk(chunk, client)
    assert translated.chunk_id == chunk.chunk_id


def test_translate_chunk_preserves_source_path_and_frontmatter():
    source = Path("notes/keep-me.md")
    frontmatter = {"tags": ["mori"], "status": "open"}
    chunk = Chunk(
        source_path=source,
        heading_chain=["목표"],
        body="## 목표\n매일 코드를 짠다.\n",
        chunk_id=compute_chunk_id(source, ["목표"], "## 목표\n매일 코드를 짠다.\n"),
        lang="ko",
        frontmatter=frontmatter,
    )
    client = _client_returning("## Goal\nWrite code daily.\n")
    translated = translate_chunk(chunk, client)
    assert translated.source_path == source
    assert translated.frontmatter == frontmatter
    # Frontmatter on the new chunk must not alias the source dict so a
    # downstream mutation cannot bleed back into the Korean chunk.
    assert translated.frontmatter is not chunk.frontmatter or translated.frontmatter == frontmatter


def test_translate_chunk_body_is_translated_content():
    chunk = _make_chunk()
    english = "## Goal\nWrite code daily.\n"
    client = _client_returning(english)
    translated = translate_chunk(chunk, client)
    assert translated.body == english


def test_translate_chunk_heading_chain_is_parsed_from_english_body():
    chunk = _make_chunk()
    client = _client_returning("## Parent\n### Child\nbody\n")
    translated = translate_chunk(chunk, client)
    assert translated.heading_chain == ["Parent", "Child"]


def test_translate_chunk_calls_openai_with_expected_payload():
    chunk = _make_chunk()
    client = _client_returning("## Goal\nWrite code daily.\n")

    translate_chunk(chunk, client)

    # Exactly one call, with kwargs matching build_translation_prompt output.
    client.chat.completions.create.assert_called_once()
    expected_payload = build_translation_prompt(chunk)
    actual_call = client.chat.completions.create.call_args
    assert actual_call.args == ()
    assert actual_call.kwargs == expected_payload
    # Specific field assertions for readability when something drifts.
    assert actual_call.kwargs["model"] == DEFAULT_TRANSLATION_MODEL
    assert actual_call.kwargs["temperature"] == 0
    assert actual_call.kwargs["seed"] == DEFAULT_TRANSLATION_SEED
    assert actual_call.kwargs["messages"][0]["role"] == "system"
    assert actual_call.kwargs["messages"][0]["content"] == TRANSLATION_SYSTEM_PROMPT
    assert actual_call.kwargs["messages"][1]["role"] == "user"
    assert actual_call.kwargs["messages"][1]["content"] == chunk.body


def test_translate_chunk_forwards_model_and_seed_overrides():
    chunk = _make_chunk()
    client = _client_returning("## Goal\nbody\n")
    translate_chunk(chunk, client, model="gpt-5-mini", seed=99)
    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "gpt-5-mini"
    assert kwargs["seed"] == 99


def test_translate_chunk_accepts_sdk_shaped_response():
    chunk = _make_chunk()
    client = _client_returning("## Goal\nWrite code daily.\n", sdk_shape=True)
    translated = translate_chunk(chunk, client)
    assert translated.lang == "en"
    assert translated.chunk_id == chunk.chunk_id
    assert translated.body == "## Goal\nWrite code daily.\n"
    assert translated.heading_chain == ["Goal"]


def test_translate_chunk_propagates_malformed_response_error():
    chunk = _make_chunk()
    client = _client_returning(None)
    with pytest.raises(TranslationResponseError, match="null"):
        translate_chunk(chunk, client)


def test_translate_chunk_propagates_client_exceptions():
    chunk = _make_chunk()
    client = MagicMock()
    client.chat.completions.create.side_effect = RuntimeError("network down")
    with pytest.raises(RuntimeError, match="network down"):
        translate_chunk(chunk, client)


def test_translate_chunk_does_not_mutate_source_chunk():
    chunk = _make_chunk()
    original_body = chunk.body
    original_chain = list(chunk.heading_chain)
    original_lang = chunk.lang
    original_id = chunk.chunk_id
    client = _client_returning("## Goal\nWrite code daily.\n")
    translate_chunk(chunk, client)
    assert chunk.body == original_body
    assert chunk.heading_chain == original_chain
    assert chunk.lang == original_lang
    assert chunk.chunk_id == original_id


def test_translate_chunk_invokes_client_exactly_once_per_call():
    chunk = _make_chunk()
    client = _client_returning("## Goal\nbody\n")
    translate_chunk(chunk, client)
    translate_chunk(chunk, client)
    assert client.chat.completions.create.call_count == 2


def test_translate_chunk_handles_chunk_with_no_heading():
    body = "본문만 있는 청크. 헤딩 없음.\n"
    chunk = _make_chunk(body=body, heading_chain=[])
    client = _client_returning("Body-only chunk. No heading.\n")
    translated = translate_chunk(chunk, client)
    assert translated.heading_chain == []
    assert translated.body == "Body-only chunk. No heading.\n"
    assert translated.chunk_id == chunk.chunk_id
    assert translated.lang == "en"
