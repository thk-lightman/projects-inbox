"""OpenAI API call accounting.

Single in-memory tracker that the build / delta / smoke-test CLI commands
use to report (a) the number of translate / embed calls a run made and
(b) an estimated USD cost. Prices are documented per-model so the user can
audit which cost figure drove a given run.

The tracker is intentionally additive: callers ``record_translate`` and
``record_embed`` after each successful API call. No background sampling,
no monkey-patching of the OpenAI SDK, no implicit globals — every
incrementing call site lives in the build pipeline (or its delta variant)
so the accounting is easy to trace by ``grep``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


# Prices in USD per 1M tokens, sourced from OpenAI pricing as of 2026-05.
# Update here when pricing changes — single source of truth for cost
# estimation across full and delta builds.
_DEFAULT_PRICING_USD_PER_M_TOKENS: Mapping[str, Mapping[str, float]] = {
    # Embedding model — single price (no input/output split).
    "text-embedding-3-large": {"input": 0.13},
    # Chat models used for translation. We charge input + output separately;
    # callers pass actual token counts when known, else fall back to a
    # conservative per-call estimate.
    "gpt-5-mini": {"input": 0.25, "output": 2.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
}

# When a caller does not pass token counts (the common case — the response
# object's usage field is normally enough, but we want a sane default for
# unit tests and mock clients), we estimate per-call cost via these
# conservative per-call token assumptions. One Korean chunk ≈ 600 tokens
# in + 700 tokens out for translation; one chunk ≈ 400 tokens for embedding.
_DEFAULT_TRANSLATE_TOKENS_IN = 600
_DEFAULT_TRANSLATE_TOKENS_OUT = 700
_DEFAULT_EMBED_TOKENS = 400


@dataclass
class ApiCostTracker:
    """Accumulates translate + embed call counts and estimated USD cost.

    Fields are public on purpose — the CLI prints them verbatim at end of
    run, and integration tests assert on counts directly without going
    through accessor methods.

    Attributes:
        translate_calls: Number of completed chat-completion calls.
        embed_calls: Number of completed embedding calls.
        translate_tokens_in: Sum of prompt-token counts across translate
            calls. Zero when callers do not pass usage data (estimator
            falls back to ``_DEFAULT_TRANSLATE_TOKENS_IN``).
        translate_tokens_out: Sum of completion-token counts across
            translate calls.
        embed_tokens: Sum of input-token counts across embed calls.
        translate_model: Chat model id used for translation (drives price
            lookup). Set once at construction.
        embed_model: Embedding model id used for vectorization.
    """

    translate_model: str = "gpt-5-mini"
    embed_model: str = "text-embedding-3-large"
    translate_calls: int = 0
    embed_calls: int = 0
    translate_tokens_in: int = 0
    translate_tokens_out: int = 0
    embed_tokens: int = 0

    def record_translate(
        self,
        *,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
    ) -> None:
        """Record one successful chat-completion call.

        When ``tokens_in`` / ``tokens_out`` are ``None`` the estimator
        defaults are used at cost time — counts remain accurate, only the
        dollar figure becomes an estimate rather than a measurement.
        """
        self.translate_calls += 1
        if tokens_in is not None:
            self.translate_tokens_in += tokens_in
        if tokens_out is not None:
            self.translate_tokens_out += tokens_out

    def record_embed(self, *, tokens: int | None = None) -> None:
        """Record one successful embedding call."""
        self.embed_calls += 1
        if tokens is not None:
            self.embed_tokens += tokens

    def estimated_usd(
        self,
        pricing: Mapping[str, Mapping[str, float]] | None = None,
    ) -> float:
        """Return estimated USD cost for accumulated calls.

        Uses per-million-token pricing from ``pricing`` (default:
        :data:`_DEFAULT_PRICING_USD_PER_M_TOKENS`). Falls back to default
        per-call token assumptions when the caller never recorded usage,
        so an early-development run with mock clients still produces a
        sensible non-zero figure.
        """
        prices = pricing or _DEFAULT_PRICING_USD_PER_M_TOKENS

        translate_prices = prices.get(self.translate_model, {})
        embed_prices = prices.get(self.embed_model, {})

        tokens_in = self.translate_tokens_in or (
            self.translate_calls * _DEFAULT_TRANSLATE_TOKENS_IN
        )
        tokens_out = self.translate_tokens_out or (
            self.translate_calls * _DEFAULT_TRANSLATE_TOKENS_OUT
        )
        embed_tokens = self.embed_tokens or (
            self.embed_calls * _DEFAULT_EMBED_TOKENS
        )

        translate_cost = (
            tokens_in * translate_prices.get("input", 0.0)
            + tokens_out * translate_prices.get("output", 0.0)
        ) / 1_000_000
        embed_cost = (
            embed_tokens * embed_prices.get("input", 0.0)
        ) / 1_000_000

        return translate_cost + embed_cost

    def total_calls(self) -> int:
        return self.translate_calls + self.embed_calls

    def summary_lines(self) -> list[str]:
        """Render a stable, copy-pastable multi-line cost summary."""
        usd = self.estimated_usd()
        return [
            f"api calls: translate={self.translate_calls} embed={self.embed_calls} "
            f"total={self.total_calls()}",
            f"tokens: translate_in={self.translate_tokens_in} "
            f"translate_out={self.translate_tokens_out} "
            f"embed_in={self.embed_tokens}",
            f"estimated cost: ${usd:.4f} "
            f"(translate_model={self.translate_model}, embed_model={self.embed_model})",
        ]


__all__ = ["ApiCostTracker"]
