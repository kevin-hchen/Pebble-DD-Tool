"""The census/live equality gate, kept as a test.

`build_landscape` prefilters in SQL on the INGEST-TIME biomarker census, then
live-screens only what survives. That is only sound if the census admits exactly
what the live matcher admits. If the two ever diverge, the prefilter silently
drops trials a patient should have seen — and it drops them invisibly, because
the page's counts would still add up.

This is the ingest-time-versus-query-time divergence CLAUDE.md warns about,
arriving disguised as an optimisation. It was NOT hypothetical: the first run of
this comparison, over all 74 families and 7 markers (2,150,918 record
comparisons), found 124 divergences in 62 families — all in KRAS_G12C and
KRAS_G12D, and all caused by `markers.resolve_marker` substring-matching a query
onto the wrong marker, plus one trial where an assay-panel listing was read as a
status. Both are fixed; this test exists so neither can come back.

It is deliberately a PARITY test rather than a test of either side's answers:
it does not assert that the census is right, only that the two paths cannot
disagree. Whichever is wrong, a disagreement is a bug, and the prefilter must
not be the thing that hides it.

Run against fixtures here (fast, in CI). The full 74-family sweep against the
live store is `scripts/check_census_parity.py`, which is what the numbers above
came from.

No network: tests/netguard.py blocks sockets.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import netguard  # noqa: E402

netguard.install()

from medrag.biomarker import (  # noqa: E402
    ELIGIBLE,
    ELIGIBLE_BY_EXCLUSION,
    UNCLEAR,
    match_biomarker,
)
from medrag.biomarker_gating import MARKER_KEYS, gating_token  # noqa: E402
from medrag.landscape import build_landscape  # noqa: E402
from medrag.trials.client import TrialRecord  # noqa: E402
from medrag.trials.store import TrialStore  # noqa: E402

ADMITTING_LIVE = {ELIGIBLE, ELIGIBLE_BY_EXCLUSION, UNCLEAR}

#: Eligibility texts chosen to exercise every shape that has ever gone wrong
#: here, not merely a representative sample.
CASES = [
    # plain statements
    "Inclusion Criteria:\n* Microsatellite stable (MSS) colorectal cancer",
    "Inclusion Criteria:\n* MSI-H or dMMR tumour required",
    # by-exclusion phrasing: names the marker only by excluding its opposite
    "Inclusion Criteria:\n* Metastatic CRC\nExclusion Criteria:\n* MSI-H or dMMR",
    # negation at a distance, and as a suffix
    "Inclusion Criteria:\n* Documented NOT to have microsatellite instability-high (MSI-high)",
    "Inclusion Criteria:\n* RAS wild-type as confirmed centrally",
    "Inclusion Criteria:\n* KRAS WT tumour",
    # a genuine self-contradiction -> live says UNCLEAR, census says REQUIRED;
    # BOTH admit, which is the property that makes the prefilter safe
    "Inclusion Criteria:\n* MSS tumour\nExclusion Criteria:\n* Microsatellite stable disease",
    # test requirement: names a marker, states no direction
    "Inclusion Criteria:\n* Tumour must have been assessed for MSI-H or dMMR status",
    # assay PANEL listing — the real NCT05619172 shape
    "Inclusion Criteria:\n* RAS wild type as confirmed by: locally performed ctDNA "
    "assessment including at least mutations in exon 2 (G12D, G12V, G12C, G12S, "
    "G12A, G12R, G13D) and exon 3",
    # single-variant requirements that must SURVIVE the panel rule
    "Inclusion Criteria:\n* Documented KRASG12D mutation in tissue or liquid biopsy.",
    "Inclusion Criteria:\n* Subject has KRasG12C mutation in tumor tissue.",
    # prior-therapy exclusion naming a drug class
    "Exclusion Criteria:\n* Prior exposure to any direct small molecule KRAS inhibitor.",
    # HER2 needing a qualifier
    "Inclusion Criteria:\n* HER2-positive by IHC 3+",
    "Exclusion Criteria:\n* HER2-negative disease",
    # nothing at all, and no text at all
    "Inclusion Criteria:\n* Age 18 or older",
    "",
]


def _store() -> TrialStore:
    store = TrialStore(Path(tempfile.mkdtemp()) / "trials.db")
    store.upsert(
        [TrialRecord(nct_id=f"NCT{i:06d}", brief_title=f"Trial {i}",
                     overall_status="RECRUITING", conditions=["Colorectal Cancer"],
                     eligibility_criteria=text)
         for i, text in enumerate(CASES, start=1)],
        set_key="colorectal")
    return store


def _census_admits(store: TrialStore, marker: str) -> set[str]:
    req = f"%{gating_token(marker, 'REQUIRED')}%"
    exc = f"%{gating_token(marker, 'ELIGIBLE_BY_EXCLUSION')}%"
    return {r[0] for r in store.conn.execute(
        "SELECT nct_id FROM trials WHERE biomarker_gating LIKE ? OR biomarker_gating LIKE ?",
        (req, exc))}


def _live_admits(store: TrialStore, marker: str) -> set[str]:
    admits = set()
    for record in store.query(query_set="colorectal", limit=10_000):
        match = match_biomarker(
            record.eligibility_criteria, marker,
            detailed_description=record.detailed_description,
            brief_summary=record.brief_summary, keywords=record.keywords)
        if match.status in ADMITTING_LIVE:
            admits.add(record.nct_id)
    return admits


def test_the_census_admits_exactly_what_the_live_matcher_admits():
    """THE GATE. Every curated marker, every fixture. A single divergence means
    the prefilter can drop a trial a patient should have seen."""
    store = _store()
    try:
        for marker in MARKER_KEYS:
            census, live = _census_admits(store, marker), _live_admits(store, marker)
            assert census == live, (
                f"marker {marker}: the ingest-time census and the live matcher "
                f"disagree.\n  census only: {sorted(census - live)}\n"
                f"  live only:   {sorted(live - census)}\n"
                "  One of them is wrong. The prefilter must not ship while they "
                "differ, and must not be the thing that hides the difference.")
    finally:
        store.close()


def test_a_self_contradicting_trial_is_admitted_by_both_paths():
    """The specific case that makes the prefilter safe at all.

    The two modules resolve a conflict differently ON PURPOSE — the census picks
    REQUIRED, the live matcher picks UNCLEAR (CLAUDE.md explains why neither is
    'more correct'). Both are ADMITTING, so a contradictory trial survives the
    prefilter and reaches the live screen that flags it. If the census ever
    resolved a conflict to EXCLUDED instead, the prefilter would start dropping
    exactly the trials most worth a human's attention.
    """
    store = _store()
    try:
        contradiction = "NCT000007"    # the MSS-required/MSS-excluded fixture
        assert contradiction in _census_admits(store, "MSS")
        assert contradiction in _live_admits(store, "MSS")
    finally:
        store.close()


def test_the_prefiltered_landscape_matches_a_full_screen_exactly():
    """End to end: the same trials AND the same counts, prefilter on or off.

    Counts matter as much as the rows. With the prefilter on, `n_excluded` and
    `n_not_mentioned` come from SQL rather than from screening records to throw
    them away, so a mistake there would show as a page reporting "0 excluded"
    for a population where hundreds are.
    """
    store = _store()
    try:
        landscape = build_landscape(store, condition="colorectal cancer",
                                    biomarker="MSS", show_limit=None)
        shown = {t.record.nct_id for t in landscape.trials}
        assert shown == _live_admits(store, "MSS")

        excluded = not_mentioned = 0
        for record in store.query(query_set="colorectal", limit=10_000):
            status = match_biomarker(
                record.eligibility_criteria, "MSS",
                detailed_description=record.detailed_description,
                brief_summary=record.brief_summary, keywords=record.keywords).status
            if status == "EXCLUDED":
                excluded += 1
            elif status == "NOT MENTIONED":
                not_mentioned += 1
        assert landscape.n_excluded == excluded
        assert landscape.n_not_mentioned == not_mentioned
        # And the denominator stays the whole population, not the prefiltered subset.
        assert landscape.n_condition == store.count(query_set="colorectal")
    finally:
        store.close()


def test_an_uncurated_marker_does_not_use_the_prefilter():
    """There is no census for a marker `config/markers.yaml` does not know, so
    that path must still screen the whole population. Prefiltering on an absent
    census would return nothing and read as 'no trials match'."""
    store = _store()
    try:
        landscape = build_landscape(store, condition="colorectal cancer",
                                    biomarker="FGFR2 fusion", show_limit=None)
        assert landscape.biomarker_curated is False
        assert landscape.n_condition == store.count(query_set="colorectal")
        assert any("generic text search" in w for w in landscape.warnings)
    finally:
        store.close()


def test_the_count_of_trials_with_no_eligibility_text_survives_the_prefilter():
    """A record with nothing to screen is never loaded under the prefilter, so
    this count has to come from SQL. Reporting 0 would claim every trial had
    text to screen."""
    store = _store()
    try:
        landscape = build_landscape(store, condition="colorectal cancer",
                                    biomarker="MSS", show_limit=None)
        assert landscape.n_no_eligibility_text == store.count_without_eligibility(
            query_set="colorectal")
        assert landscape.n_no_eligibility_text >= 1, \
            "the fixtures include a trial with no eligibility text"
    finally:
        store.close()


# ------------------------------------------------- other pairs that must agree
#
# The audit that followed the census/live divergence: everywhere two code paths
# answer the same question, assert they cannot disagree. This one was found by
# accident, from a performance gate; these exist so the next one is not.


def test_the_two_ways_of_counting_the_biomarker_census_agree():
    """`store.landscape()` computes `by_biomarker` inline while
    `store.biomarker_counts()` answers the same question standalone for the
    coverage statement. Same census, two queries — so they can drift."""
    store = _store()
    try:
        result = store.landscape(query_set="colorectal", sample_limit=5)
        for marker in MARKER_KEYS:
            standalone = store.biomarker_counts(marker, query_set="colorectal")
            inline = (result.get("by_biomarker") or {}).get(marker)
            if inline is None:
                continue
            for status, n in standalone.items():
                assert inline.get(status, 0) == n, (
                    f"{marker}/{status}: landscape() says {inline.get(status, 0)}, "
                    f"biomarker_counts() says {n}")
    finally:
        store.close()


def test_the_two_ways_of_counting_a_population_agree():
    """`count(query_set=)` reads the join table; a landscape reads the trials
    table through it. A membership row with no trial, or the reverse, would make
    these disagree — and the count is printed as a denominator."""
    store = _store()
    try:
        counted = store.count(query_set="colorectal")
        queried = len(store.query(query_set="colorectal", limit=10_000))
        orphans = store.conn.execute(
            "SELECT COUNT(*) FROM trial_query_sets q WHERE NOT EXISTS "
            "(SELECT 1 FROM trials t WHERE t.nct_id = q.nct_id)").fetchone()[0]
        assert orphans == 0, f"{orphans} membership rows point at no trial"
        assert counted == queried
    finally:
        store.close()


def test_the_weak_and_strong_completeness_checks_cannot_disagree_in_the_unsafe_direction():
    """`CoverageReport.complete` (errors only) and `verify_ingest` (errors AND
    every query reaching its reported total) both answer "did this ingest
    finish". They are allowed to differ ONE way: verify_ingest may be stricter.
    The reverse — `complete` True while verify_ingest says PARTIAL is fine, but
    verify_ingest COMPLETE while `complete` is False would mean the strong check
    is weaker than the weak one."""
    from medrag.trials.queries import CONDITION, CoverageReport, QueryYield, TrialQuery
    from medrag.trials.store import INGEST_COMPLETE, verify_ingest

    cases = [
        # (fetched, reported, error) per query
        [(10, 10, "")],                       # clean
        [(4, 10, "")],                        # short: weak says complete, strong must not
        [(0, None, "RuntimeError: boom")],    # errored: both must refuse
        [(10, 10, ""), (5, 9, "")],           # one short
    ]
    for spec in cases:
        yields = [QueryYield(query=TrialQuery(CONDITION, f"q{i}"), fetched=f,
                             reported_total=r, new=f, error=e)
                  for i, (f, r, e) in enumerate(spec)]
        report = CoverageReport(set_key="x", set_label="x", yields=yields,
                                total_unique=sum(f for f, _r, _e in spec))
        strong, _why = verify_ingest(
            held=report.total_unique, total_unique=report.total_unique,
            yields=[{"query": y.query.label, "fetched": y.fetched,
                     "reported_total": y.reported_total, "error": y.error}
                    for y in yields],
            errors=list(report.errors))
        if strong == INGEST_COMPLETE:
            assert report.complete, (
                "verify_ingest graded COMPLETE where CoverageReport.complete did not — "
                "the strong check has become weaker than the weak one")


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