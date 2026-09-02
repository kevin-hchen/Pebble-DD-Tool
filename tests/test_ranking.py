"""Tests for the deterministic trial-relevance ranking (medrag/ranking.py) and
its wiring into store.landscape()'s sample selection.

Two properties matter most, matching the two hard rules in
config/ranking.yaml: every score is explainable from a one-line breakdown with
no model call anywhere, and the weights come from YAML, not from a value
baked into this module. Most tests here use a small, HAND-BUILT config
(`_CFG`) rather than the shipped `config/ranking.yaml`, so they keep testing
the ENGINE (tier matching, bin matching, recency, provenance, explain
formatting, tie-breaking) even if an analyst later reweights the real file.
A few tests at the bottom check the SHIPPED config directly, for invariants
that must survive any reweighting — sponsor_class must never become a scored
signal, in particular.

No network: this module makes no I/O beyond reading a local YAML file.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import netguard  # noqa: E402

netguard.install()

from medrag.ranking import Ranking, ScoredSignal, load_ranking_config, score_record  # noqa: E402
from medrag.trials.client import parse_study  # noqa: E402
from medrag.trials.store import TrialStore  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# A small, isolated config for testing the engine's MECHANISM, independent of
# the shipped weights.
_CFG = {
    "signals": {
        "phase": {
            "tiers": [
                {"match": ["PHASE3"], "points": 30, "label": "Phase 3"},
                {"match": ["PHASE1"], "points": 10, "label": "Phase 1"},
            ],
            "default": {"points": 0, "label": "phase not stated"},
        },
        "status": {
            "tiers": [
                {"match": ["RECRUITING"], "points": 20, "label": "open to enrolment"},
            ],
            "default": {"points": 3, "label": "status not stated"},
        },
        "enrollment": {
            "bins": [
                {"min": 300, "points": 15, "label": "{count} enrolled"},
                {"min": 100, "points": 10, "label": "{count} enrolled"},
            ],
            "default": {"points": 0, "label": "enrolment not stated"},
        },
        "allocation": {
            "tiers": [{"match": ["RANDOMIZED"], "points": 10, "label": "randomised"}],
            "default": {"points": 0, "label": "allocation not stated"},
        },
        "sites": {
            "bins": [{"min": 5, "points": 5, "label": "{count} sites"}],
            "default": {"points": 0, "label": "no sites on file"},
        },
        "provenance": {
            "term_query_bonus": 6,
            "term_query_label": "topic-specific search match",
        },
        "recency": {
            "bins": [{"within_days": 730, "points": 8, "label": "started within 2 years"}],
            "default": {"points": 0, "label": "start date not stated or old"},
        },
    }
}


def _record(**overrides):
    base = dict(phase="", overall_status="", enrollment_count=None, allocation="",
               start_date="", locations=[])
    base.update(overrides)
    return SimpleNamespace(**base)


# --------------------------------------------------------------- engine mechanics


def test_phase_tier_matches_and_scores():
    r = score_record(_record(phase="PHASE3"), [], _CFG, today=date(2026, 1, 1))
    assert any(s.label == "Phase 3" and s.points == 30 for s in r.signals)


def test_unmatched_value_falls_to_default_not_zero_silently():
    r = score_record(_record(phase="PHASE2"), [], _CFG, today=date(2026, 1, 1))
    phase_signal = next(s for s in r.signals if "phase" in s.label.lower() or s.label == "phase not stated")
    assert phase_signal.label == "phase not stated" and phase_signal.points == 0


def test_enrollment_bin_picks_the_highest_bin_the_count_clears():
    below_both = score_record(_record(enrollment_count=50), [], _CFG, today=date(2026, 1, 1))
    mid = score_record(_record(enrollment_count=150), [], _CFG, today=date(2026, 1, 1))
    high = score_record(_record(enrollment_count=500), [], _CFG, today=date(2026, 1, 1))
    assert below_both.score == 3  # only the status default fires
    assert mid.score == 3 + 10
    assert high.score == 3 + 15


def test_recency_uses_start_date_not_completion_date():
    recent = score_record(_record(start_date="2025-06-01"), [], _CFG, today=date(2026, 1, 1))
    old = score_record(_record(start_date="2015-06-01"), [], _CFG, today=date(2026, 1, 1))
    assert recent.score == 3 + 8
    assert old.score == 3 + 0


def test_a_future_start_date_is_not_penalised_as_old():
    """NOT_YET_RECRUITING trials carry a future start date; that must not
    read as 'ancient' just because the day-delta is negative."""
    r = score_record(_record(start_date="2027-01-01"), [], _CFG, today=date(2026, 1, 1))
    assert any(s.points == 8 for s in r.signals)


def test_sites_counts_locations_length():
    r = score_record(_record(locations=[{"city": "Boston"}, {"city": "Houston"},
                                        {"city": "Chicago"}, {"city": "Denver"}, {"city": "NYC"}]),
                     [], _CFG, today=date(2026, 1, 1))
    assert any(s.points == 5 for s in r.signals)


def test_provenance_bonus_requires_a_term_query_specifically():
    """A trial found only by broad condition queries gets nothing; one also
    found by a query.term search — higher-precision evidence of topical
    relevance than the condition net alone — gets the bonus."""
    cond_only = score_record(_record(), ["cond:colorectal cancer"], _CFG, today=date(2026, 1, 1))
    with_term = score_record(_record(), ["cond:colorectal cancer", "term:MSS colorectal"],
                             _CFG, today=date(2026, 1, 1))
    assert cond_only.score == 3
    assert with_term.score == 3 + 6


def test_allocation_scores_randomised_design():
    r = score_record(_record(allocation="RANDOMIZED"), [], _CFG, today=date(2026, 1, 1))
    assert any(s.label == "randomised" and s.points == 10 for s in r.signals)


def test_missing_config_section_is_simply_not_scored():
    """A config with only some signals defined must not crash on the rest —
    an analyst editing one block of the YAML should not have to touch all of
    them for the file to keep working."""
    partial = {"signals": {"phase": _CFG["signals"]["phase"]}}
    r = score_record(_record(phase="PHASE3", overall_status="RECRUITING"), [], partial,
                     today=date(2026, 1, 1))
    assert r.score == 30 and len(r.signals) == 1


# --------------------------------------------------------------- explain()


def test_explain_lists_only_signals_that_scored():
    r = Ranking(score=38, signals=[ScoredSignal("Phase 3", 30), ScoredSignal("phase not stated", 0),
                                   ScoredSignal("randomised", 8)])
    text = r.explain()
    assert "Phase 3 (+30)" in text and "randomised (+8)" in text
    assert "phase not stated" not in text, "a zero-point signal must not clutter the explanation"
    assert "score 38" in text


def test_explain_says_so_when_nothing_scored():
    r = Ranking(score=0, signals=[ScoredSignal("phase not stated", 0)])
    assert r.explain() == "no ranking signal on file — score 0"


def test_a_partner_can_reconstruct_the_total_from_the_explanation():
    """The whole point of explainability: the printed numbers must sum to the
    printed total, not just gesture at it."""
    r = score_record(_record(phase="PHASE3", overall_status="RECRUITING", enrollment_count=400,
                             allocation="RANDOMIZED"), ["term:MSS colorectal"], _CFG,
                     today=date(2026, 1, 1))
    printed_points = [int(bit.rsplit("+", 1)[1].rstrip(")"))
                      for bit in r.explain().split(" — score")[0].split(" · ")]
    assert sum(printed_points) == r.score


# --------------------------------------------------------------- config loading


def test_missing_config_file_degrades_to_a_flat_tie_not_a_crash():
    cfg = load_ranking_config(Path(tempfile.mkdtemp()) / "does-not-exist.yaml")
    assert cfg == {}
    r = score_record(_record(phase="PHASE3"), [], cfg, today=date(2026, 1, 1))
    assert r.score == 0 and r.signals == []


# --------------------------------------------------------------- real registry data


def test_allocation_is_parsed_from_real_registry_json():
    """STELLAR-303's real, live-captured designModule: designInfo.allocation
    is fetched (whole module requested) and must now be parsed, the same gap
    class as detailed_description/keywords before it."""
    study = json.loads((FIXTURES / "ctgov_study_NCT05425940.json").read_text())
    rec = parse_study(study)
    assert rec.allocation == "RANDOMIZED"


# --------------------------------------------------------------- store wiring


def _trial(nct, **overrides):
    from medrag.trials.client import TrialRecord
    base = dict(nct_id=nct, brief_title=nct, overall_status="RECRUITING", phase="Phase 1",
               conditions=["Colorectal Cancer"], eligibility_criteria="")
    base.update(overrides)
    return TrialRecord(**base)


def test_sample_is_sorted_by_score_descending():
    store = TrialStore(Path(tempfile.mkdtemp()) / "t.db")
    records = [
        _trial("NCT_LOW", phase="Phase 1", overall_status="TERMINATED"),
        _trial("NCT_HIGH", phase="Phase 3", overall_status="RECRUITING", enrollment_count=900),
        _trial("NCT_MID", phase="Phase 2", overall_status="RECRUITING"),
    ]
    store.upsert(records, provenance={r.nct_id: ["cond:colorectal cancer"] for r in records},
                set_key="colorectal")
    census = store.landscape(query_set="colorectal", sample_limit=10)
    ids = [r.nct_id for r in census["sample"]]
    assert ids == ["NCT_HIGH", "NCT_MID", "NCT_LOW"], (
        f"expected descending relevance score order, got {ids}"
    )
    scores = [r.score for r in census["sample_rankings"]]
    assert scores == sorted(scores, reverse=True)
    store.close()


def test_sample_rankings_are_parallel_to_sample_and_explainable():
    store = TrialStore(Path(tempfile.mkdtemp()) / "t.db")
    records = [_trial("NCT_A", phase="Phase 3", overall_status="RECRUITING")]
    store.upsert(records, provenance={"NCT_A": ["cond:colorectal cancer"]}, set_key="colorectal")
    census = store.landscape(query_set="colorectal", sample_limit=10)
    assert len(census["sample_rankings"]) == len(census["sample"]) == 1
    assert "Phase 3" in census["sample_rankings"][0].explain()
    store.close()


def test_ties_break_deterministically_on_nct_id():
    """Two trials identical on every scored signal must still sort the same
    way every run — an unspecified SQL tie order would make the memo's
    sample change between otherwise-identical runs."""
    store = TrialStore(Path(tempfile.mkdtemp()) / "t.db")
    records = [_trial("NCT_B"), _trial("NCT_A")]
    store.upsert(records, provenance={r.nct_id: ["x"] for r in records}, set_key="colorectal")
    first = [r.nct_id for r in store.landscape(query_set="colorectal", sample_limit=10)["sample"]]
    second = [r.nct_id for r in store.landscape(query_set="colorectal", sample_limit=10)["sample"]]
    assert first == second == ["NCT_A", "NCT_B"]
    store.close()


# --------------------------------------------------------------- shipped config invariants


def test_shipped_config_never_scores_sponsor_class():
    """sponsor_class is a deliberate exclusion (see config/ranking.yaml's
    header) — it answers who is paying, not how urgently to read this row.
    Reintroducing it as a scored signal would silently bias the ranking
    toward whichever sponsor class someone decided to weight, and that is
    exactly the kind of change that should require editing the YAML with its
    justification, not slip in as a code change."""
    cfg = load_ranking_config()
    assert "sponsor_class" not in cfg.get("signals", {}), (
        "sponsor_class must stay a labelled dimension (by_sponsor_class), not a ranking weight"
    )


def test_shipped_config_weights_phase_and_status_above_the_secondary_signals():
    """The stated principle (config/ranking.yaml's header): phase and status
    should be the primary drivers, not tied with or beaten by size/provenance/
    recency signals. This pins that ordering so a future reweight has to
    deliberately choose to break it, not drift into it."""
    cfg = load_ranking_config()
    top_phase = max(t["points"] for t in cfg["signals"]["phase"]["tiers"])
    top_status = max(t["points"] for t in cfg["signals"]["status"]["tiers"])
    top_sites = max(b["points"] for b in cfg["signals"]["sites"]["bins"])
    top_recency = max(b["points"] for b in cfg["signals"]["recency"]["bins"])
    provenance_bonus = cfg["signals"]["provenance"]["term_query_bonus"]
    assert top_phase >= top_status > top_sites
    assert top_status > provenance_bonus > 0
    assert top_status > top_recency


def test_shipped_config_keeps_proximity_below_phase_and_status():
    """The arguable weight in the file, so it is pinned with its reasoning
    (config/ranking.yaml's signal 7). Distance is the patient's hardest
    practical constraint and an argument for weighting it above phase exists —
    it is not taken, because the match is an ungeocoded substring test where
    'same state' can mean a six-hour drive. If someone decides otherwise, this
    test should fail and force the argument into the YAML."""
    cfg = load_ranking_config()
    top_proximity = max(t["points"] for t in cfg["signals"]["proximity"]["tiers"])
    top_phase = max(t["points"] for t in cfg["signals"]["phase"]["tiers"])
    top_status = max(t["points"] for t in cfg["signals"]["status"]["tiers"])
    assert top_phase > top_proximity and top_status > top_proximity


def test_proximity_is_not_scored_when_no_patient_location_was_given():
    """A diligence section never asks where a trial's sites are. The signal must
    be absent from its explain() line, not present with zero points — the same
    reason `ValidationReport.assessed` exists: 'not applicable here' and 'scored
    and found nothing' are different statements."""
    rec = _record(phase="Phase 3", overall_status="RECRUITING")
    cfg, today = load_ranking_config(), date(2026, 1, 1)
    without = score_record(rec, [], cfg, today=today)
    with_far = score_record(rec, [], cfg, today=today, proximity_tier=0)
    with_city = score_record(rec, [], cfg, today=today, proximity_tier=3)

    assert all("patient's" not in s.label for s in without.signals)
    assert without.score == with_far.score, "an unmatched location must cost nothing"
    assert with_city.score > without.score


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
