"""The stopped-trial sweep: two arms, two budgets, two denominators.

WHAT THESE PIN

The sweep is the one subsystem that is supposed to be exhaustive by
construction, and it was the least exhaustive path in the tool. Its indication
arm selected with `LOWER(conditions) LIKE '%<indication>%'` — the fifth
appearance of the substring-over-structured-data defect, and the one place it
had been explicitly exempted on the reasoning that a substring only ever
widens. Measured on the live colorectal store it saw 557 of 1,336 stopped
trials and missed 779 of them (58%), because "Colorectal Neoplasms" does not
contain "colorectal cancer".

Fixing that made the indication arm 2.4x larger, which is exactly the condition
under which one shared budget starts hiding the other arm — so the budgets are
now separate, and these tests pin that they cannot be re-merged silently.

No network: an in-memory store throughout.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import netguard  # noqa: E402

netguard.install()
sys.path.insert(0, str(Path(__file__).resolve().parent))

from medrag.negative_evidence import (  # noqa: E402
    INDICATION_BUDGET,
    INTERVENTION_BUDGET,
    find_stopped_trials,
)
from medrag.trials.client import TrialRecord  # noqa: E402
from medrag.trials.store import TrialStore  # noqa: E402


def _stopped(nct, interventions, conditions, why="Slow accrual", status="TERMINATED"):
    return TrialRecord(nct_id=nct, brief_title=f"Trial {nct}", overall_status=status,
                       phase="Phase 2", why_stopped=why,
                       interventions=list(interventions), conditions=list(conditions))


def _store(records, set_key="colorectal"):
    st = TrialStore(Path(tempfile.mkdtemp()) / "t.db")
    st.upsert(records, provenance={r.nct_id: ["cond:colorectal cancer"] for r in records},
              set_key=set_key)
    return st


# --------------------------------------------------------- the selection fix


def test_the_indication_arm_selects_by_query_set_not_a_condition_substring():
    """The fifth instance of the defect. A trial registered as "Colorectal
    Neoplasms" is in the fetched colorectal population but does not contain the
    substring "colorectal cancer" — the arm must find it anyway."""
    st = _store([
        _stopped("NCT_MESH", ["Drug A"], ["Colorectal Neoplasms"]),
        _stopped("NCT_LITERAL", ["Drug B"], ["Colorectal Cancer"]),
    ])
    by_set = find_stopped_trials(st, condition="colorectal cancer", query_set="colorectal")
    assert {t.record.nct_id for t in by_set.trials} == {"NCT_MESH", "NCT_LITERAL"}
    assert by_set.n_indication_total == 2

    # And the substring path this replaced, for the contrast the fix is about.
    by_substring = find_stopped_trials(st, condition="colorectal cancer")
    assert {t.record.nct_id for t in by_substring.trials} == {"NCT_LITERAL"}, (
        "the substring path must still be measurably worse — if this ever matches "
        "both, the fixture no longer reproduces the bug being fixed"
    )
    st.close()


def test_a_stopped_trial_of_the_compound_elsewhere_survives_the_query_set_arm():
    """The rule the whole sweep exists for, re-pinned against the NEW selection:
    switching the indication arm to a query set must not turn the OR into an
    AND by the back door. The renal trial is not in the colorectal set at all."""
    st = _store([_stopped("NCT_CRC", ["Compound X"], ["Colorectal Neoplasms"])])
    st.upsert([_stopped("NCT_RENAL", ["Compound X"], ["Renal Impairment"])],
              set_key="renal")
    sweep = find_stopped_trials(st, intervention="Compound X", query_set="colorectal")
    assert "NCT_RENAL" in {t.record.nct_id for t in sweep.trials}, (
        "a trial of the same compound stopped in a DIFFERENT indication is the "
        "highest-value output of this sweep and must never be filtered out by the "
        "indication arm's population"
    )
    st.close()


# --------------------------------------------------------- the budget split


def _crowding_store():
    """One compound trial against an indication pool far larger than the whole
    shared budget — the live shape (2-93 vs 1,336)."""
    recs = [_stopped("NCT_COMPOUND", ["Compound X"], ["Colorectal Neoplasms"])]
    # NCT IDs deliberately sort BEFORE the compound trial, so a merged
    # alphabetical sort would push it out.
    recs += [_stopped(f"NCT_A{i:03d}", ["Other Drug"], ["Colorectal Neoplasms"])
             for i in range(60)]
    return _store(recs)


def test_a_large_indication_pool_cannot_crowd_out_the_compounds_own_trial():
    """The risk the split exists to remove. With one shared budget and a global
    sort, 60 indication trials whose NCT IDs sort first take every slot."""
    st = _crowding_store()
    sweep = find_stopped_trials(st, intervention="Compound X", query_set="colorectal")
    ids = {t.record.nct_id for t in sweep.trials}
    assert "NCT_COMPOUND" in ids, (
        "the compound's own stopped trial was crowded out by the indication arm — "
        "this is exactly what separate budgets prevent"
    )
    assert sweep.n_shown_intervention == 1
    st.close()


def test_each_arm_keeps_its_own_budget_rather_than_competing_for_one():
    st = _crowding_store()
    sweep = find_stopped_trials(st, intervention="Compound X", query_set="colorectal")
    assert sweep.n_shown_intervention == 1
    assert sweep.n_shown_indication == INDICATION_BUDGET
    st.close()


def test_the_intervention_arm_is_never_capped_below_the_original_shared_budget():
    """The mistake the measurement caught. Reserving a SHARE of 25 for the
    intervention arm put a ceiling on the high-value arm: for one real asset the
    old shared budget yielded 20 intervention rows and a 15-row reservation
    dropped 5 of them. The intervention arm keeps the whole original budget, so
    it can never lose a row a shared budget would have shown."""
    st = _store([_stopped(f"NCT_C{i:03d}", ["Compound X"], ["Colorectal Neoplasms"])
                 for i in range(40)])
    sweep = find_stopped_trials(st, intervention="Compound X", query_set="colorectal")
    assert INTERVENTION_BUDGET >= 25, (
        "the intervention arm must keep at least the budget the two arms used to "
        "share, or splitting them costs the arm it was meant to protect"
    )
    assert sweep.n_shown_intervention == INTERVENTION_BUDGET
    st.close()


def test_a_small_intervention_arm_does_not_inflate_the_indication_sample():
    """No spillover: 10 of 1,336 and 33 of 1,336 are both unrepresentative, and
    the coverage line states the denominator either way. The bigger memo buys
    nothing."""
    st = _store(
        [_stopped("NCT_C1", ["Compound X"], ["Colorectal Neoplasms"])]
        + [_stopped(f"NCT_I{i:03d}", ["Other"], ["Colorectal Neoplasms"]) for i in range(40)]
    )
    sweep = find_stopped_trials(st, intervention="Compound X", query_set="colorectal")
    assert sweep.n_shown_intervention == 1
    assert sweep.n_shown_indication == INDICATION_BUDGET
    st.close()


# --------------------------------------------------------- the denominators


def test_the_sweep_reports_both_arm_totals_not_just_what_it_shows():
    """Denominator discipline, the same the landscape now has: 25 of 1,336 and
    25 of 25 must not render identically."""
    st = _crowding_store()
    sweep = find_stopped_trials(st, intervention="Compound X", query_set="colorectal")
    assert sweep.n_intervention_total == 1
    assert sweep.n_indication_total == 61      # all 61 are in the colorectal set
    assert sweep.n_total == 61                 # union counts the overlap ONCE
    assert sweep.n_shown < sweep.n_total

    line = sweep.coverage_line()
    assert f"of {sweep.n_total}" in line
    assert "1 of 1 stopped trial(s) of this compound" in line
    assert "stopped trial(s) in this indication" in line
    assert "not listed" in line
    st.close()


def test_the_union_total_never_double_counts_a_trial_found_by_both_arms():
    st = _store([_stopped("NCT_BOTH", ["Compound X"], ["Colorectal Neoplasms"])])
    sweep = find_stopped_trials(st, intervention="Compound X", query_set="colorectal")
    assert sweep.n_intervention_total == 1 and sweep.n_indication_total == 1
    assert sweep.n_total == 1, "one trial found by both arms is still one trial"
    assert sweep.n_shown == 1
    st.close()


def test_an_unsearched_arm_is_distinct_from_an_arm_that_found_nothing():
    """The not-assessed-vs-nothing-found rule, applied per arm."""
    st = _store([_stopped("NCT_A", ["Compound X"], ["Colorectal Neoplasms"])])
    no_asset = find_stopped_trials(st, query_set="colorectal")
    assert no_asset.searched_indication and not no_asset.searched_intervention
    assert "of this compound" not in no_asset.coverage_line()

    no_indication = find_stopped_trials(st, intervention="Compound X")
    assert no_indication.searched_intervention and not no_indication.searched_indication
    assert "in this indication" not in no_indication.coverage_line()
    st.close()


def test_every_shown_trial_states_which_arm_found_it():
    """"This compound failed elsewhere" and "this disease is hard" are different
    findings; a reader must not have to guess which one a row is."""
    st = _store([
        _stopped("NCT_COMPOUND", ["Compound X"], ["Renal Impairment"]),
        _stopped("NCT_INDICATION", ["Other Drug"], ["Colorectal Neoplasms"]),
    ], set_key="colorectal")
    st.upsert([_stopped("NCT_COMPOUND", ["Compound X"], ["Renal Impairment"])],
              set_key="renal")
    sweep = find_stopped_trials(st, intervention="Compound X", query_set="colorectal")
    by_id = {t.record.nct_id: t for t in sweep.trials}
    assert by_id["NCT_COMPOUND"].from_intervention
    assert not by_id["NCT_INDICATION"].from_intervention
    st.close()


def test_a_trial_found_by_both_arms_is_attributed_to_the_intervention_arm():
    st = _store([_stopped("NCT_BOTH", ["Compound X"], ["Colorectal Neoplasms"])])
    sweep = find_stopped_trials(st, intervention="Compound X", query_set="colorectal")
    t = sweep.trials[0]
    assert t.from_intervention and set(t.arms) == {"intervention", "indication"}
    assert sweep.n_shown_intervention == 1 and sweep.n_shown_indication == 0
    st.close()


def test_the_most_recently_started_trial_leads_within_an_arm():
    """The tiebreak decides which 25 of 81 a reader sees, so it cannot be
    arbitrary. NCT ID was the old tiebreak and 89% of the indication arm carries
    a stated reason, so the sort decayed to alphabetical — oldest-registered
    first, the opposite of useful. Measured consequence: 20 of the intervention
    trials a real asset used to show were replaced by a different, older 20."""
    st = _store([
        _stopped("NCT_OLD", ["Compound X"], ["Colorectal Neoplasms"]),
        _stopped("NCT_NEW", ["Compound X"], ["Colorectal Neoplasms"]),
        _stopped("NCT_UNDATED", ["Compound X"], ["Colorectal Neoplasms"]),
    ])
    st.conn.execute("UPDATE trials SET start_date='2016-01-01' WHERE nct_id='NCT_OLD'")
    st.conn.execute("UPDATE trials SET start_date='2025-01-01' WHERE nct_id='NCT_NEW'")
    st.conn.execute("UPDATE trials SET start_date='' WHERE nct_id='NCT_UNDATED'")
    st.conn.commit()
    order = [t.record.nct_id
             for t in find_stopped_trials(st, intervention="Compound X").trials]
    assert order == ["NCT_NEW", "NCT_OLD", "NCT_UNDATED"], (
        f"got {order} — most recent first, and a trial with no start date on file "
        "sorts last rather than leading as if it were the newest"
    )
    st.close()


def test_which_rows_are_shown_does_not_depend_on_the_candidate_window():
    """The window is an implementation detail; if it leaks into WHICH rows a
    reader sees, "showing 25 of 81" means something different every time the
    constant moves."""
    st = _store([_stopped(f"NCT_C{i:03d}", ["Compound X"], ["Colorectal Neoplasms"])
                 for i in range(40)])
    shown = [t.record.nct_id
             for t in find_stopped_trials(st, intervention="Compound X").trials]
    from medrag import negative_evidence as ne

    original = ne._CANDIDATE_WINDOW
    try:
        ne._CANDIDATE_WINDOW = INTERVENTION_BUDGET      # the tightest legal window
        again = [t.record.nct_id
                 for t in find_stopped_trials(st, intervention="Compound X").trials]
    finally:
        ne._CANDIDATE_WINDOW = original
    assert shown == again
    st.close()


def test_stated_reasons_still_come_first_within_the_shown_rows():
    st = _store([
        _stopped("NCT_SILENT", ["Compound X"], ["Colorectal Neoplasms"], why=""),
        _stopped("NCT_STATED", ["Compound X"], ["Colorectal Neoplasms"], why="Futility"),
    ])
    sweep = find_stopped_trials(st, intervention="Compound X", query_set="colorectal")
    assert sweep.trials[0].record.nct_id == "NCT_STATED"
    assert sweep.trials[0].reason_is_stated


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
    print("\nall stopped-sweep tests passed" if not failures else f"\n{failures} test(s) failed")
    raise SystemExit(1 if failures else 0)
