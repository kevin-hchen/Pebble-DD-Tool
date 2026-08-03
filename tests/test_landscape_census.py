"""Indication-first landscape: the trial-side biomarker gating census, the SQL
aggregates over the full match set, per-question status, and the indication-only
(no --asset) run.

No network. The load-bearing property here is that NOT_MENTIONED is never folded
into an eligible/required set — the same "we did not find it is not it is not
there" rule as ValidationReport.assessed and NegativeEvidence.searched.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Direct runs do not load conftest.py, so the no-network guard is installed
# here too. See tests/netguard.py.
from tests import netguard  # noqa: E402

netguard.install()
sys.path.insert(0, str(Path(__file__).resolve().parent))

from medrag.biomarker_gating import (  # noqa: E402
    EXCLUDED,
    NOT_MENTIONED,
    REQUIRED,
    gate_markers,
)
from medrag.config import Config  # noqa: E402
from medrag.diligence import DiligenceQuestion, DiligenceRunner, load_question_set  # noqa: E402
from medrag.memo import render_markdown  # noqa: E402
from medrag.trials.client import TrialRecord  # noqa: E402
from medrag.trials.store import TrialStore  # noqa: E402


def _trial(nct, elig, *, status="RECRUITING", phase="Phase 2", sponsor="Uni",
           sponsor_class="OTHER", completion="2027-06-01", conditions=("Colorectal Cancer",)):
    return TrialRecord(
        nct_id=nct, brief_title=f"Trial {nct}", phase=phase, overall_status=status,
        lead_sponsor=sponsor, sponsor_class=sponsor_class,
        primary_completion_date=completion, conditions=list(conditions),
        eligibility_criteria=elig,
    )


def _store(records):
    st = TrialStore(Path(tempfile.mkdtemp()) / "t.db")
    # Stamped with the query set a real ingest records: the census selects the
    # population the fetch defined, not a condition substring.
    st.upsert(records, provenance={r.nct_id: ["cond:colorectal cancer"] for r in records},
              set_key="colorectal")
    return st


# ------------------------------------------------------------- the gating parser


def test_mss_and_msi_h_required():
    assert gate_markers("Inclusion Criteria:\n* Microsatellite stable (MSS)")["MSS"].status == REQUIRED
    assert gate_markers("Inclusion Criteria:\n* Tumors must be MSI-H or dMMR")["MSI_H"].status == REQUIRED


def test_non_msi_h_in_inclusion_is_excluded_not_required():
    """'non-MSI-H' names MSI-H but negates it — the trial does NOT want MSI-H."""
    flags = gate_markers("Inclusion Criteria:\n* Documented non-MSI-H colorectal cancer")
    assert flags["MSI_H"].status == EXCLUDED
    assert "non-MSI-H" in flags["MSI_H"].span


def test_marker_in_exclusion_section_is_excluded():
    flags = gate_markers("Inclusion Criteria:\n* mCRC\n\nExclusion Criteria:\n* BRAF V600E mutation")
    assert flags["BRAF_V600E"].status == EXCLUDED


def test_kras_g12c_and_ras_both_flag():
    flags = gate_markers("Inclusion Criteria:\n* KRAS G12C mutation confirmed")
    assert flags["KRAS_G12C"].status == REQUIRED and flags["RAS"].status == REQUIRED


def test_absent_markers_are_not_mentioned_with_a_span_of_nothing():
    flags = gate_markers("Inclusion Criteria:\n* Age 18+\n* Measurable disease")
    assert all(f.status == NOT_MENTIONED for f in flags.values())
    assert all(f.span == "" for f in flags.values())


def test_every_marker_is_always_present_in_the_result():
    flags = gate_markers("")
    from medrag.biomarker_gating import MARKER_KEYS
    assert set(flags) == set(MARKER_KEYS), "a marker must never be omitted, only NOT_MENTIONED"


# ------------------------------------------------------------- the crucial rule


def test_not_mentioned_is_never_folded_into_a_required_set():
    """The regression the task demands: a landscape must not quietly count a trial
    that never named the marker as requiring it."""
    store = _store([
        _trial("NCT01", "Inclusion Criteria:\n* MSS tumors"),          # MSS REQUIRED
        _trial("NCT02", "Inclusion Criteria:\n* Age 18+, measurable"),  # MSS NOT_MENTIONED
        _trial("NCT03", "Exclusion Criteria:\n* MSS tumors"),           # MSS EXCLUDED
    ])
    census = store.landscape(condition="colorectal cancer")
    assert census["by_biomarker"]["MSS"] == {"REQUIRED": 1, "EXCLUDED": 1, "NOT_MENTIONED": 1}

    required = store.landscape(condition="colorectal cancer", biomarker_filters=[("MSS", "REQUIRED")])
    ids = {r.nct_id for r in required["sample"]}
    assert ids == {"NCT01"}, "NOT_MENTIONED and EXCLUDED trials must not appear in the REQUIRED set"
    assert required["total"] == 1
    store.close()


# ------------------------------------------------------------- SQL aggregates


def _mixed_store(n_extra=0):
    recs = [
        _trial("NCT_A", "Inclusion Criteria:\n* MSS/pMMR", status="RECRUITING",
               phase="Phase 2", sponsor_class="INDUSTRY", completion="2026-03-01"),
        _trial("NCT_B", "Exclusion Criteria:\n* MSI-H or dMMR", status="RECRUITING",
               phase="Phase 3", sponsor_class="OTHER", completion="2027-09-01"),
        _trial("NCT_C", "Inclusion Criteria:\n* MSI-H required", status="TERMINATED",
               phase="Phase 1", sponsor_class="INDUSTRY", completion="2024-01-01"),
        _trial("NCT_D", "Inclusion Criteria:\n* Age 18+", status="COMPLETED",
               phase="Phase 2", sponsor_class="NIH", completion="2023-06-01"),
    ]
    for i in range(n_extra):
        recs.append(_trial(f"NCT_X{i}", "Inclusion Criteria:\n* MSS", status="RECRUITING"))
    return _store(recs)


def test_counts_are_over_the_full_set_and_sample_is_capped():
    store = _mixed_store(n_extra=20)   # 24 trials total
    census = store.landscape(condition="colorectal cancer", sample_limit=5)
    assert census["total"] == 24
    assert census["shown"] == 5
    assert census["dropped"] == 19, "the memo must be able to say 19 were not listed"
    store.close()


def test_breakdowns_group_the_whole_set():
    store = _mixed_store()
    census = store.landscape(condition="colorectal cancer")
    assert census["by_status"]["RECRUITING"] == 2
    assert census["by_status"]["TERMINATED"] == 1
    assert census["by_sponsor_class"]["INDUSTRY"] == 2
    assert census["by_completion_year"]["2027"] == 1 and census["by_completion_year"]["2023"] == 1
    store.close()


def test_status_filter_narrows_the_denominator():
    store = _mixed_store()
    recruiting = store.landscape(condition="colorectal cancer", statuses=["RECRUITING"])
    assert recruiting["total"] == 2
    stopped = store.landscape(condition="colorectal cancer",
                              statuses=["TERMINATED", "WITHDRAWN", "SUSPENDED"])
    assert stopped["total"] == 1
    store.close()


def test_eligibility_readable_counts_non_empty_text():
    store = _store([
        _trial("NCT1", "Inclusion Criteria:\n* MSS"),
        _trial("NCT2", ""),          # no eligibility text
    ])
    census = store.landscape(condition="colorectal cancer")
    assert census["total"] == 2 and census["eligibility_readable"] == 1
    store.close()


# ------------------------------------------------------------- diligence wiring


def _runner(store):
    cfg = Config(openai_api_key=None, data_dir=Path(tempfile.mkdtemp()))
    cfg.ensure_dirs()
    return DiligenceRunner(cfg, rag=None, trial_store=store, fda_store=None)


def test_status_is_threaded_per_question():
    store = _mixed_store()
    runner = _runner(store)
    q = DiligenceQuestion(id="r", section="Recruiting", question="Recruiting in {indication}?",
                          route="structured", status=["RECRUITING"], k=10)
    result = runner.run_question(q, asset="", indication="colorectal cancer")
    statuses = {e.meta.get("status") for e in result.evidence}
    assert statuses == {"RECRUITING"}, "a per-question status filter must reach the store query"
    runner.close()


def test_indication_only_run_needs_no_asset():
    store = _mixed_store()
    runner = _runner(store)
    qs = load_question_set("config/landscape.yaml")
    memo = runner.run(asset="", indication="colorectal cancer", question_set=qs, progress=False)
    runner.close()
    md = render_markdown(memo)
    assert md.startswith("# Landscape — colorectal cancer")
    assert "{asset}" not in md, "an unfilled {asset} placeholder must degrade, not leak"


def test_census_counts_a_trial_whose_condition_string_lacks_the_indication_words():
    """The diligence consumer's copy of the retrieval bug. store.landscape() used
    to take condition=indication and re-run LOWER(conditions) LIKE, which on the
    real store counted 5,201 of a 12,092-trial fetched population — it discarded
    every trial registered as "Colorectal Neoplasms". Two callers with different
    population logic is the shape this repo has already been bitten by twice."""
    store = _store([
        _trial("NCT_LIT", "Inclusion Criteria:\n* MSS", conditions=("Colorectal Cancer",)),
        _trial("NCT_NEO", "Inclusion Criteria:\n* MSS", conditions=("Colorectal Neoplasms",)),
    ])
    runner = _runner(store)
    q = DiligenceQuestion(id="l", section="Landscape", question="What runs in {indication}?",
                          aggregate=True, k=10)
    result = runner.run_question(q, asset="", indication="colorectal cancer")
    assert result.aggregate["total"] == 2, (
        "the census must count the fetched population; a trial registered as "
        "'Colorectal Neoplasms' was dropped by a substring re-match"
    )
    assert "NCT_NEO" in {r.nct_id for r in result.aggregate["sample"]}
    runner.close()


def test_section_retrieval_selects_the_fetched_population_not_a_condition_substring():
    """`_trials_for`'s copy of the same rule. It ANDs intervention with the
    population, so a trial registered as "Colorectal Neoplasms" was dropped from
    an asset's evidence and the section fell through to free-text search.

    The store deliberately holds one trial the substring DOES match, so the
    structured result is non-empty and the free-text fallback never fires. That
    is the partial-drop case: with the fallback masked, the dropped trial is
    invisible. A fixture with only the unmatched trial passes either way, because
    FTS rescues it — which is exactly why the fallback hides this defect."""
    store = _store([
        _trial("NCT_LIT", "Inclusion Criteria:\n* MSS", conditions=("Colorectal Cancer",)),
        _trial("NCT_NEO", "Inclusion Criteria:\n* MSS", conditions=("Colorectal Neoplasms",)),
    ])
    runner = _runner(store)
    try:
        records = runner._trials_for(
            "what runs here?", asset="", indication="colorectal cancer", filters={}, limit=6)
    finally:
        runner.close()
    assert {"NCT_LIT", "NCT_NEO"} <= {r.nct_id for r in records}, (
        "a fetched trial must reach the section; the substring re-match dropped it "
        "and the fallback could not fire because the result was not empty"
    )


def test_census_names_the_query_set_it_counted_not_the_typed_words():
    store = _mixed_store()
    runner = _runner(store)
    q = DiligenceQuestion(id="l", section="L", question="What runs in {indication}?",
                          aggregate=True, k=5)
    result = runner.run_question(q, asset="", indication="colorectal cancer")
    md = render_markdown(
        __import__("medrag.diligence", fromlist=["MemoResult"]).MemoResult(
            asset="", indication="colorectal cancer", question_set="landscape",
            sections=[result]))
    assert "colorectal" in md and "query set" in md, (
        "the memo must say which population it counted, since it is no longer the "
        "reader's own phrasing"
    )
    runner.close()


def test_never_ingested_census_does_not_read_as_no_such_trials_exist():
    """A zero because nothing was fetched and a zero because nothing matched are
    different findings — the same rule as ValidationReport.assessed."""
    store = _store([_trial("NCT_A", "Inclusion Criteria:\n* MSS")])
    runner = _runner(store)
    q = DiligenceQuestion(id="l", section="L", question="What runs in {indication}?",
                          aggregate=True, k=5)
    result = runner.run_question(q, asset="", indication="pancreatic cancer")
    assert result.aggregate["total"] == 0
    md = render_markdown(
        __import__("medrag.diligence", fromlist=["MemoResult"]).MemoResult(
            asset="", indication="pancreatic cancer", question_set="landscape",
            sections=[result]))
    assert "NOT a finding that no such trials exist" in md, (
        "an uningested indication must not report as an empty field"
    )
    runner.close()


def test_aggregate_section_states_denominator_and_labels_the_sample():
    store = _mixed_store(n_extra=20)
    runner = _runner(store)
    q = DiligenceQuestion(id="l", section="Landscape", question="What runs in {indication}?",
                          aggregate=True, k=5)
    result = runner.run_question(q, asset="", indication="colorectal cancer")
    assert result.aggregate is not None and result.aggregate["total"] == 24
    md = render_markdown(
        __import__("medrag.diligence", fromlist=["MemoResult"]).MemoResult(
            asset="", indication="colorectal cancer", question_set="landscape", sections=[result]))
    assert "24 trials" in md
    assert "showing 5 of 24" in md
    assert "19 matching trial(s) are NOT listed" in md
    runner.close()


def test_aggregate_biomarker_table_shows_not_mentioned_as_a_gap():
    store = _mixed_store()
    runner = _runner(store)
    q = DiligenceQuestion(id="b", section="Biomarkers", question="Gating in {indication}?",
                          aggregate=True, k=10)
    result = runner.run_question(q, asset="", indication="colorectal cancer")
    md = render_markdown(
        __import__("medrag.diligence", fromlist=["MemoResult"]).MemoResult(
            asset="", indication="colorectal cancer", question_set="landscape", sections=[result]))
    # RAS is named by none of the four fixtures -> 4 not mentioned, shown as a gap.
    assert "Not mentioned" in md and "NOT_MENTIONED is a gap" in md
    runner.close()


def test_single_match_renders_singular_grammar():
    store = _store([_trial("NCT01", "Inclusion Criteria:\n* MSS tumors")])
    runner = _runner(store)
    q = DiligenceQuestion(id="s", section="One", question="{indication}?", aggregate=True, k=5)
    result = runner.run_question(q, asset="", indication="colorectal cancer")
    md = render_markdown(
        __import__("medrag.diligence", fromlist=["MemoResult"]).MemoResult(
            asset="", indication="colorectal cancer", question_set="landscape", sections=[result]))
    assert "**1 trial** matches" in md and "1 trials" not in md
    runner.close()


# ------------------------------------------------------------- real captured CTgov v2

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load_study(nct):
    import json

    from medrag.trials.client import parse_study
    return parse_study(json.loads((FIXTURES / f"ctgov_study_{nct}.json").read_text()))


def test_real_ctgov_response_carries_full_eligibility_text():
    """A genuine API response, not synthetic prose: the eligibility module must
    survive parsing intact, or the whole gating census rests on nothing."""
    r = _load_study("NCT06513221")
    assert r.nct_id == "NCT06513221"
    assert len(r.eligibility_criteria) > 1000, "full criteria text, not a truncated stub"
    assert "Inclusion Criteria" in r.eligibility_criteria


def test_gating_resolves_mss_on_real_trial_language():
    """'MSS-type mCRC' in a real inclusion list must gate MSS:REQUIRED — the parser
    is proven against registry language, not only hand-written fixtures."""
    r = _load_study("NCT06513221")
    assert gate_markers(r.eligibility_criteria)["MSS"].status == REQUIRED


def test_real_recruiting_trial_carries_a_site_investigator_email():
    """The contactsLocationsModule parse, against live data: a recruiting site's
    per-site contact email is preferred over an absent central contact."""
    import json
    r = _load_study("NCT06509126")
    assert r.overall_status == "RECRUITING"
    assert "@" in json.dumps(r.locations), "a usable investigator email must reach the record"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as exc:
                failures += 1
                print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print("\nall landscape-census tests passed" if not failures else f"\n{failures} failed")
    raise SystemExit(1 if failures else 0)
