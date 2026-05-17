"""Unit tests for vault_corpus.cost.ApiCostTracker."""

from __future__ import annotations

import pytest

from vault_corpus.cost import ApiCostTracker


def test_initial_state_is_zero():
    t = ApiCostTracker()
    assert t.translate_calls == 0
    assert t.embed_calls == 0
    assert t.total_calls() == 0
    assert t.estimated_usd() == 0.0


def test_record_translate_increments_call_count():
    t = ApiCostTracker()
    t.record_translate()
    t.record_translate()
    assert t.translate_calls == 2
    assert t.translate_tokens_in == 0  # no usage data → default estimator at cost time


def test_record_translate_with_tokens_accumulates():
    t = ApiCostTracker()
    t.record_translate(tokens_in=120, tokens_out=240)
    t.record_translate(tokens_in=80, tokens_out=160)
    assert t.translate_tokens_in == 200
    assert t.translate_tokens_out == 400


def test_record_embed_with_tokens():
    t = ApiCostTracker()
    t.record_embed(tokens=512)
    t.record_embed(tokens=1024)
    assert t.embed_calls == 2
    assert t.embed_tokens == 1536


def test_estimated_usd_uses_measured_tokens_when_present():
    t = ApiCostTracker(translate_model="gpt-4o-mini", embed_model="text-embedding-3-large")
    # 1M input tokens at $0.15 + 1M output tokens at $0.60 + 1M embed at $0.13
    t.record_translate(tokens_in=1_000_000, tokens_out=1_000_000)
    t.record_embed(tokens=1_000_000)
    usd = t.estimated_usd()
    assert usd == pytest.approx(0.15 + 0.60 + 0.13, rel=1e-6)


def test_estimated_usd_falls_back_to_default_when_no_usage():
    """No usage data → estimator uses per-call defaults so figure is non-zero."""
    t = ApiCostTracker()
    t.record_translate()
    t.record_embed()
    assert t.estimated_usd() > 0.0


def test_summary_lines_format_is_stable():
    t = ApiCostTracker()
    t.record_translate(tokens_in=100, tokens_out=200)
    t.record_embed(tokens=400)
    lines = t.summary_lines()
    assert len(lines) == 3
    assert "translate=1" in lines[0]
    assert "embed=1" in lines[0]
    assert "translate_in=100" in lines[1]
    assert "estimated cost: $" in lines[2]


def test_total_calls_sums_both():
    t = ApiCostTracker()
    for _ in range(3):
        t.record_translate()
    for _ in range(7):
        t.record_embed()
    assert t.total_calls() == 10


def test_custom_pricing_overrides_default():
    t = ApiCostTracker(translate_model="custom-model", embed_model="custom-embed")
    t.record_translate(tokens_in=1_000_000, tokens_out=1_000_000)
    t.record_embed(tokens=1_000_000)
    pricing = {
        "custom-model": {"input": 1.00, "output": 2.00},
        "custom-embed": {"input": 0.50},
    }
    assert t.estimated_usd(pricing) == pytest.approx(1.00 + 2.00 + 0.50, rel=1e-6)
