"""Topic-strategy OpenAlex query-building tests (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fetch_papers import fetch_openalex_topic_papers  # noqa: E402


def _capture():
    seen = {}

    def fake_get(url, params=None, **kw):
        seen["url"] = url
        seen["params"] = params or {}
        return {"results": []}

    return seen, fake_get


def test_keyword_strategy_filters_date_and_citations():
    seen, fake = _capture()
    fetch_openalex_topic_papers("bayes", "keyword", 12, 20, http_get=fake)
    p = seen["params"]
    assert p["search"] == "bayes"
    assert "sort" not in p
    assert "from_publication_date:" in p["filter"]
    assert "cited_by_count:>19" in p["filter"]


def test_classic_strategy_sorts_by_citations_no_date():
    seen, fake = _capture()
    fetch_openalex_topic_papers("bayes", "classic", 12, 100, http_get=fake)
    p = seen["params"]
    assert p["sort"] == "cited_by_count:desc"
    assert "cited_by_count:>99" in p["filter"]
    assert "from_publication_date" not in p["filter"]


def test_recent_strategy_sorts_by_date_no_citation_floor():
    seen, fake = _capture()
    fetch_openalex_topic_papers("bayes", "recent", 3, 50, http_get=fake)
    p = seen["params"]
    assert p["sort"] == "publication_date:desc"
    assert "from_publication_date:" in p["filter"]
    assert "cited_by_count" not in p["filter"]
