"""Watchlist parser unit tests."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fetch_papers import parse_journals_watchlist, parse_labs_watchlist  # noqa: E402


LABS_FIXTURE = """---
tags: [topic/research/curation]
---

# docs-watchlist-labs

ignore preamble.

## 표

| PI 이름 | OpenAlex authorId | S2 authorId | 위치 필터 | citation_min | 분야 | 메모 |
|---|---|---|---|---|---|---|
| _예: Andrew Gelman_ | _A5009543412_ | _1742064_ | _last_ | _30_ | _Bayesian/통계_ | _stan_ |
| Andrew Gelman | A5009543412 | 1742064 | last | 30 | Bayesian | stan core |
| Aleksander Madry | A5012345678 |  | any |  | ML | robustness |
"""


JOURNALS_FIXTURE = """| 저널·컨퍼런스 | OpenAlex source id | S2 venue 이름 | citation_min | lookback_months | 분야 | 메모 |
|---|---|---|---|---|---|---|
| _예: JASA_ | _S148538149_ | _JASA_ | _15_ | _24_ | _stat_ | _theory_ |
| NeurIPS | S203510302 | NeurIPS | 50 | 12 | ML | proceedings |
| JASA | S148538149 | Journal of the American Statistical Association | 15 | 24 | stat |  |
"""


def test_labs_parser_skips_example_row():
    rows = parse_labs_watchlist(LABS_FIXTURE)
    assert len(rows) == 2, [r.pi_name for r in rows]
    assert rows[0].pi_name == "Andrew Gelman"
    assert rows[0].openalex_author_id == "A5009543412"
    assert rows[0].s2_author_id == "1742064"
    assert rows[0].position_filter == "last"
    assert rows[0].citation_min == 30
    assert rows[1].pi_name == "Aleksander Madry"
    assert rows[1].s2_author_id == ""
    assert rows[1].position_filter == "any"
    assert rows[1].citation_min == 0


def test_labs_parser_effective_citation_min_uses_default():
    rows = parse_labs_watchlist(LABS_FIXTURE)
    assert rows[0].effective_citation_min() == 30  # override
    assert rows[1].effective_citation_min() == 20  # default


def test_journals_parser_skips_example_row():
    rows = parse_journals_watchlist(JOURNALS_FIXTURE)
    assert len(rows) == 2
    assert rows[0].venue_name == "NeurIPS"
    assert rows[0].openalex_source_id == "S203510302"
    assert rows[0].citation_min == 50
    assert rows[0].lookback_months == 12
    assert rows[1].venue_name == "JASA"
    assert rows[1].lookback_months == 24


def test_journals_parser_default_lookback_months_when_blank():
    fixture = """| 저널·컨퍼런스 | OpenAlex source id | S2 venue 이름 | citation_min | lookback_months | 분야 | 메모 |
|---|---|---|---|---|---|---|
| ICLR | S4306419644 | ICLR |  |  | ML |  |
"""
    rows = parse_journals_watchlist(fixture)
    assert len(rows) == 1
    assert rows[0].effective_lookback_months() == 12
    assert rows[0].effective_citation_min() == 20
