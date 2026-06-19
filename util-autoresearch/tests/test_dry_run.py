"""End-to-end dry-run integration with mocked API clients."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fetch_papers import (DedupStore, JournalRow, LabRow, Paper, run_pipeline)  # noqa: E402


def _mock_openalex_author(author_id, lookback_months, citation_min):
    return [
        Paper(title="Causal A", authors=["Student", "Andrew Gelman"],
              doi="10.1/a", venue="JASA", published_date="2025-04-01",
              abstract="abstract A", citation_count=40, raw_source="openalex"),
    ]


def _mock_s2_author(author_id, lookback_months, citation_min):
    return [
        Paper(title="Causal A", authors=["Student", "Andrew Gelman"],
              doi="10.1/a", venue="JASA", published_date="2025-04-01",
              abstract="abstract A from s2", citation_count=42, raw_source="s2"),
    ]


def _mock_openalex_venue(source_id, lookback_months, citation_min):
    return [
        Paper(title="Venue paper B", authors=["X Y"],
              doi="10.2/b", venue="NeurIPS", published_date="2025-05-01",
              abstract="abstract B", citation_count=60, raw_source="openalex"),
    ]


def _mock_s2_venue(venue, lookback_months, citation_min):
    return []


def test_pipeline_dedup_collapses_cross_api_dup():
    labs = [LabRow(pi_name="Andrew Gelman", openalex_author_id="A1",
                    s2_author_id="S1", position_filter="last")]
    journals = [JournalRow(venue_name="NeurIPS", openalex_source_id="V1",
                            s2_venue="NeurIPS")]
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        inbox = td_path / "inbox"
        dedup = DedupStore(td_path / "d.sqlite")
        try:
            counts = run_pipeline(
                labs=labs, journals=journals, inbox_dir=inbox, dedup=dedup,
                dry_run=False,
                openalex_author=_mock_openalex_author,
                s2_author=_mock_s2_author,
                openalex_venue=_mock_openalex_venue,
                s2_venue=_mock_s2_venue,
            )
        finally:
            dedup.close()
        files = sorted(p.name for p in inbox.glob("paper-*.md"))
    # 2 distinct canonical_ids (10.1/a + 10.2/b); s2 dup of 10.1/a deduped.
    # Filename is now paper-W<isoweek>-<title-slug>.md per 2026-06-10 change.
    assert len(files) == 2, files
    assert all(f.startswith("paper-W") and f.endswith(".md") for f in files), files
    assert counts["new_written"] == 2
    assert counts["seen_total"] == 3  # openalex(2) + s2(1)


def test_pipeline_position_filter_rejects_first_author_when_last_required():
    labs = [LabRow(pi_name="Andrew Gelman", openalex_author_id="A1",
                    position_filter="last")]
    journals: list = []

    def _first_author_paper(author_id, lookback_months, citation_min):
        return [Paper(title="x", authors=["Andrew Gelman", "Other"],
                       doi="10.9/z", citation_count=30, raw_source="openalex")]

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        inbox = td_path / "inbox"
        dedup = DedupStore(td_path / "d.sqlite")
        try:
            counts = run_pipeline(
                labs=labs, journals=journals, inbox_dir=inbox, dedup=dedup,
                dry_run=False,
                openalex_author=_first_author_paper,
                s2_author=lambda *a, **k: [],
                openalex_venue=lambda *a, **k: [],
                s2_venue=lambda *a, **k: [],
            )
        finally:
            dedup.close()
    assert counts["new_written"] == 0


def test_pipeline_dry_run_skips_disk_write():
    labs = [LabRow(pi_name="Andrew Gelman", openalex_author_id="A1",
                    position_filter="last")]
    journals: list = []
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        inbox = td_path / "inbox"
        dedup = DedupStore(td_path / "d.sqlite")
        try:
            run_pipeline(
                labs=labs, journals=journals, inbox_dir=inbox, dedup=dedup,
                dry_run=True,
                openalex_author=_mock_openalex_author,
                s2_author=lambda *a, **k: [],
                openalex_venue=lambda *a, **k: [],
                s2_venue=lambda *a, **k: [],
            )
        finally:
            dedup.close()
    assert not inbox.exists() or not list(inbox.glob("paper-*.md"))


def test_pipeline_api_error_does_not_crash_run():
    labs = [LabRow(pi_name="Andrew Gelman", openalex_author_id="A1",
                    s2_author_id="S1", position_filter="any")]
    journals: list = []

    def _broken(*args, **kwargs):
        raise RuntimeError("HTTP 503")

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        inbox = td_path / "inbox"
        dedup = DedupStore(td_path / "d.sqlite")
        try:
            counts = run_pipeline(
                labs=labs, journals=journals, inbox_dir=inbox, dedup=dedup,
                dry_run=False,
                openalex_author=_broken,
                s2_author=_mock_s2_author,
                openalex_venue=lambda *a, **k: [],
                s2_venue=lambda *a, **k: [],
            )
        finally:
            dedup.close()
    # broken openalex => skipped; s2 still works => 1 written
    assert counts["new_written"] == 1
