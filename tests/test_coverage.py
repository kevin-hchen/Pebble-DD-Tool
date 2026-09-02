"""Tests for the coverage statement (medrag/coverage.py) — the block every
landscape output must show on the page: what was searched, what was not, what
matched, all traced to stored counts and the registry's own reported total.

Three properties matter most:

  1. Numbers come from stored counts, never a retrieved sample — proven by
     seeding a store with more trials than any sample would show and checking
     the coverage statement still has the right totals.
  2. "Not searched" and "searched, found nothing" read differently.
  3. A diligence section that narrows further than the query set (status,
     phase) gets a biomarker breakdown scoped to ITS population, not the
     query set's — this was a real bug caught during development: reusing
     `by_biomarker[marker]` for the coverage line silently zeroed the
     NOT_MENTIONED/EXCLUDED counts, because the section's own WHERE clause
     already requires the marker to be REQUIRED-or-ELIGIBLE_BY_EXCLUSION.

No network: tests/netguard.py blocks sockets.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import netguard  # noqa: E402

netguard.install()

from medrag.coverage import (  # noqa: E402
    biomarker_coverage_from_counts,
    build_coverage_statement,
    load_registries_config,
    render_lines,
)
from medrag.trials.client import TrialRecord  # noqa: E402
from medrag.trials.queries import (  # noqa: E402
    CONDITION,
    TERM,
    CoverageReport,
    QueryYield,
    TrialQuery,
)
from medrag.trials.store import TrialStore  # noqa: E402


def _trial(nct, elig, status="RECRUITING"):
    return TrialRecord(nct_id=nct, brief_title=nct, overall_status=status,
                       conditions=["Colorectal Cancer"], eligibility_criteria=elig)


def _store_with(records, yields, set_key="colorectal"):
    store = TrialStore(Path(tempfile.mkdtemp()) / "t.db")
    store.upsert(records, provenance={r.nct_id: ["cond:colorectal cancer"] for r in records},
                set_key=set_key)
    report = CoverageReport(set_key=set_key, set_label=set_key, curated=True,
                            yields=yields, total_unique=len(records))
    store.record_coverage(report)
    return store


# ------------------------------------------------------------- registries config


def test_shipped_registries_config_names_ctgov_and_the_gap():
    cfg = load_registries_config()
    assert cfg.searched_name == "ClinicalTrials.gov"
    assert "WHO ICTRP" in cfg.not_searched
    assert cfg.caveat


def test_missing_registries_config_still_names_ctgov():
    cfg = load_registries_config(Path(tempfile.mkdtemp()) / "missing.yaml")
    assert cfg.searched_name == "ClinicalTrials.gov"
    assert cfg.not_searched == ()


# --------------------------------------------------------- not-searched vs empty


def test_never_ingested_reads_differently_from_searched_and_empty():
    store = TrialStore(Path(tempfile.mkdtemp()) / "t.db")
    cs = build_coverage_statement(store, "nonexistent-set")
    lines = render_lines(cs)
    assert not cs.ever_ingested
    assert "has not looked" in " ".join(lines)
    assert "0 of 0" not in " ".join(lines), (
        "never-ingested must not read as a completed search that found nothing"
    )


def test_searched_and_found_nothing_reads_as_a_completed_search():
    store = _store_with(
        [], [QueryYield(query=TrialQuery(CONDITION, "x"), reported_total=0, fetched=0, new=0)],
    )
    cs = build_coverage_statement(store, "colorectal")
    assert cs.ever_ingested and cs.complete
    store.close()


# ------------------------------------------------------------- incomplete fetch


def test_a_query_that_did_not_complete_is_named_and_marks_the_count_a_lower_bound():
    store = _store_with(
        [_trial("NCT1", "Inclusion Criteria:\n* MSS")],
        [QueryYield(query=TrialQuery(CONDITION, "colorectal cancer"), reported_total=1,
                   fetched=1, new=1),
         QueryYield(query=TrialQuery(CONDITION, "bowel cancer"), error="RuntimeError: timeout")],
    )
    cs = build_coverage_statement(store, "colorectal")
    lines = render_lines(cs)
    text = " ".join(lines)
    assert not cs.complete
    assert "cond:bowel cancer" in text
    assert "LOWER BOUND" in text
    store.close()


# --------------------------------------------------------------- stored counts,
# --------------------------------------------------------------- not the sample


def test_biomarker_breakdown_reflects_the_full_population_not_a_sample():
    """A store holding far more trials than any sample would print — the
    coverage numbers must still be exact, because they come from SQL COUNT,
    not from counting rendered rows."""
    records = (
        [_trial(f"NCT_EXP{i}", "Inclusion Criteria:\n* MSS tumors") for i in range(20)]
        + [_trial(f"NCT_SYN{i}", "Inclusion Criteria:\n* Proficient mismatch repair (pMMR)")
           for i in range(8)]
        + [_trial(f"NCT_EXCL{i}", "Exclusion Criteria:\n* Known MSI-H or dMMR") for i in range(16)]
        + [_trial(f"NCT_MSI{i}", "Inclusion Criteria:\n* Tumors must be MSI-H") for i in range(62)]
        + [_trial(f"NCT_NM{i}", "Inclusion Criteria:\n* Age 18+") for i in range(1138)]
    )
    store = _store_with(
        records,
        [QueryYield(query=TrialQuery(CONDITION, "colorectal cancer"), reported_total=len(records),
                   fetched=len(records), new=len(records))],
    )
    cs = build_coverage_statement(store, "colorectal", marker="MSS")
    bm = cs.biomarker
    assert bm.explicit == 20
    assert bm.by_synonym == 8
    assert bm.by_exclusion == 16
    assert bm.opposite_count == 62
    assert bm.not_mentioned == 1138
    assert bm.eligible_total == 44
    assert bm.eligible_total + bm.opposite_count + bm.not_mentioned == len(records) == 1244
    store.close()


def test_render_lines_matches_the_documented_example_shape():
    records = (
        [_trial("NCT_EXP", "Inclusion Criteria:\n* MSS tumors")]
        + [_trial("NCT_EXCL", "Exclusion Criteria:\n* Known MSI-H or dMMR")]
        + [_trial(f"NCT_MSI{i}", "Inclusion Criteria:\n* Tumors must be MSI-H") for i in range(2)]
        + [_trial(f"NCT_NM{i}", "Inclusion Criteria:\n* Age 18+") for i in range(5)]
    )
    store = _store_with(
        records,
        [QueryYield(query=TrialQuery(CONDITION, "colorectal cancer"), reported_total=len(records),
                   fetched=len(records), new=len(records))],
    )
    cs = build_coverage_statement(store, "colorectal", marker="MSS")
    lines = render_lines(cs)
    assert lines[0].startswith("Searched: ClinicalTrials.gov")
    assert lines[1].startswith("Not searched:")
    assert any(line.startswith("Of ") and "explicit" in line and "by exclusion" in line
              and "by synonym" in line for line in lines)
    store.close()


# ------------------------------------------------------- arithmetic invariant


def test_eligible_total_never_double_counts_the_three_buckets():
    bm = biomarker_coverage_from_counts(
        "MSS",
        gating_counts={"ELIGIBLE_BY_EXCLUSION": 8, "EXCLUDED": 62, "NOT_MENTIONED": 1138},
        basis_counts={"EXPLICIT": 16, "SYNONYM": 23},
        population_total=1247,
    )
    assert bm.eligible_total == 16 + 8 + 23 == 47
    assert bm.eligible_total + bm.opposite_count + bm.not_mentioned == 1247


# ------------------------------------------------- diligence-section narrowing
# ------------------------------------------------- (the bug caught in development)


def test_section_narrowed_by_status_gets_a_breakdown_scoped_to_its_own_population():
    """The regression this module exists to prevent: `store.landscape()`'s
    biomarker-filtered `total`/`by_biomarker` are already scoped to
    REQUIRED-or-ELIGIBLE_BY_EXCLUSION for the filtered marker, so reusing them
    for the coverage line would always show 0 NOT_MENTIONED and 0 excluded —
    the arithmetic would silently fail to sum to the real population."""
    records = [
        _trial("NCT_EXP", "Inclusion Criteria:\n* MSS tumors", status="RECRUITING"),
        _trial("NCT_EXCL_OPP", "Exclusion Criteria:\n* Known MSI-H or dMMR", status="RECRUITING"),
        _trial("NCT_MSI", "Inclusion Criteria:\n* Tumors must be MSI-H", status="RECRUITING"),
        _trial("NCT_NM", "Inclusion Criteria:\n* Age 18+", status="RECRUITING"),
        # Closed — must be excluded from the section's RECRUITING-only scope.
        _trial("NCT_CLOSED_EXP", "Inclusion Criteria:\n* MSS tumors", status="COMPLETED"),
    ]
    store = _store_with(
        records,
        [QueryYield(query=TrialQuery(CONDITION, "colorectal cancer"), reported_total=5,
                   fetched=5, new=5)],
    )
    agg = store.landscape(
        query_set="colorectal", statuses=["RECRUITING", "NOT_YET_RECRUITING"],
        biomarker_filters=[("MSS", ["REQUIRED", "ELIGIBLE_BY_EXCLUSION"])],
    )
    assert agg["total"] == 2, "sanity: the section's own (post-filter) total"

    cs = agg["coverage_statement"]
    bm = cs.biomarker
    assert bm.population_total == 4, "the RECRUITING population BEFORE the biomarker filter"
    assert bm.explicit == 1 and bm.by_exclusion == 1
    assert bm.opposite_count == 1 and bm.not_mentioned == 1
    assert bm.eligible_total + bm.opposite_count + bm.not_mentioned == bm.population_total
    assert "RECRUITING" in bm.scope_note
    store.close()


def test_no_scope_note_when_the_section_adds_no_extra_narrowing():
    records = [_trial("NCT1", "Inclusion Criteria:\n* MSS tumors")]
    store = _store_with(
        records,
        [QueryYield(query=TrialQuery(CONDITION, "colorectal cancer"), reported_total=1,
                   fetched=1, new=1)],
    )
    agg = store.landscape(query_set="colorectal", biomarker_filters=[("MSS", "REQUIRED")])
    bm = agg["coverage_statement"].biomarker
    assert bm.scope_note == ""
    assert bm.population_total == 1
    store.close()


def test_no_biomarker_line_when_the_section_filters_no_marker_or_more_than_one():
    records = [_trial("NCT1", "Inclusion Criteria:\n* MSS tumors\n* BRAF V600E mutation")]
    store = _store_with(
        records,
        [QueryYield(query=TrialQuery(CONDITION, "colorectal cancer"), reported_total=1,
                   fetched=1, new=1)],
    )
    no_filter = store.landscape(query_set="colorectal")
    assert no_filter["coverage_statement"].biomarker is None

    two_markers = store.landscape(
        query_set="colorectal",
        biomarker_filters=[("MSS", "REQUIRED"), ("BRAF_V600E", "REQUIRED")],
    )
    assert two_markers["coverage_statement"].biomarker is None
    store.close()


# ------------------------------------------------------------- same numbers,
# ------------------------------------------------------------- every surface


def test_render_lines_is_the_only_thing_that_renders_and_is_deterministic():
    """Calling render_lines twice on the same statement must produce identical
    text — this is what makes 'same numbers in Streamlit, Markdown and PDF'
    true by construction rather than by three renderers staying in sync by
    coincidence."""
    records = [_trial("NCT1", "Inclusion Criteria:\n* MSS")]
    store = _store_with(
        records,
        [QueryYield(query=TrialQuery(CONDITION, "colorectal cancer"), reported_total=1,
                   fetched=1, new=1),
         QueryYield(query=TrialQuery(TERM, "MSS colorectal"), reported_total=1, fetched=1, new=0)],
    )
    cs = build_coverage_statement(store, "colorectal", marker="MSS")
    assert render_lines(cs) == render_lines(cs)
    store.close()


if __name__ == "__main__":
    import traceback

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except Exception:
                failures += 1
                print(f"FAIL  {name}")
                traceback.print_exc()
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
