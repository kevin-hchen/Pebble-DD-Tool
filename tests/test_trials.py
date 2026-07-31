"""Tests for clinicaltrials.gov ingestion, the SQLite trial store, routing,
and provenance-labelled context assembly.

All network calls are mocked, so parsing, pagination, filtering and routing are
verified without hitting the API.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Direct runs do not load conftest.py, so the no-network guard is installed
# here too. See tests/netguard.py.
from tests import netguard  # noqa: E402

netguard.install()
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures.ctgov import EMPTY_PAGE, PAGE_ONE, PAGE_TWO  # noqa: E402

from medrag.config import Config  # noqa: E402
from medrag.context import (  # noqa: E402
    LIT_LABEL,
    TRIAL_LABEL,
    build_evidence,
    provenance_summary,
    render_context,
)
from medrag.documents import Chunk, Retrieved  # noqa: E402
from medrag.router import (  # noqa: E402
    Route,
    Router,
    classify_by_rules,
    extract_filters,
)
from medrag.trials import client as ctgov  # noqa: E402
from medrag.trials.client import TrialRecord, parse_study, search_trials  # noqa: E402
from medrag.trials.store import TrialStore  # noqa: E402


def _mock_get(pages):
    """Return a requests.get stand-in that serves pages in order."""
    responses = []
    for page in pages:
        r = MagicMock(status_code=200)
        r.json.return_value = page
        r.raise_for_status.return_value = None
        responses.append(r)
    return MagicMock(side_effect=responses)


def _all_records() -> list[TrialRecord]:
    with patch.object(ctgov.requests, "get", _mock_get([PAGE_ONE, PAGE_TWO])):
        return search_trials(condition="heart failure", max_records=100)


def _store(records=None) -> TrialStore:
    store = TrialStore(Path(tempfile.mkdtemp()) / "trials.db")
    store.upsert(records if records is not None else _all_records())
    return store


# ------------------------------------------------------------------ parsing


def test_parses_core_fields():
    rec = parse_study(PAGE_ONE["studies"][0])
    assert rec.nct_id == "NCT03057977"
    assert rec.phase == "Phase 3"
    assert rec.overall_status == "COMPLETED"
    assert rec.enrollment_count == 5988 and rec.enrollment_type == "ACTUAL"
    assert rec.lead_sponsor == "Boehringer Ingelheim" and rec.sponsor_class == "INDUSTRY"
    assert rec.conditions == ["Heart Failure"]
    assert "Empagliflozin" in rec.interventions
    assert rec.collaborators == ["Eli Lilly and Company"]
    assert rec.start_date == "2017-03-27"
    assert rec.url == "https://clinicaltrials.gov/study/NCT03057977"
    assert rec.stopped_early is False


def test_parses_why_stopped():
    rec = parse_study(PAGE_ONE["studies"][1])
    assert rec.overall_status == "TERMINATED" and rec.stopped_early
    assert "efficacy boundary" in rec.why_stopped
    assert "STOPPED:" in rec.summary()


def test_multi_phase_trial():
    rec = parse_study(PAGE_TWO["studies"][1])
    assert rec.phase == "Phase 2/Phase 3"
    assert rec.enrollment_type == "ESTIMATED"


def test_sparse_record_does_not_crash():
    rec = parse_study(PAGE_TWO["studies"][2])
    assert rec.nct_id == "NCT09999999"
    assert rec.phase == "" and rec.enrollment_count is None and rec.conditions == []


def test_record_without_nct_id_is_skipped():
    assert parse_study(PAGE_TWO["studies"][3]) is None


def test_stopped_without_reason_is_still_flagged():
    rec = parse_study(PAGE_TWO["studies"][0])
    assert rec.stopped_early and rec.why_stopped == ""


# ------------------------------------------------------------------ fetching


def test_pagination_follows_page_token():
    mock = _mock_get([PAGE_ONE, PAGE_TWO])
    with patch.object(ctgov.requests, "get", mock):
        records = search_trials(condition="heart failure", max_records=100)

    assert mock.call_count == 2, "must follow nextPageToken to page two"
    assert mock.call_args_list[1][1]["params"]["pageToken"] == "TOKEN_PAGE_2"
    assert len(records) == 5, "5 valid records; the ID-less one is dropped"
    assert "NCT09999999" in {r.nct_id for r in records}


def test_max_records_stops_early():
    mock = _mock_get([PAGE_ONE, PAGE_TWO])
    with patch.object(ctgov.requests, "get", mock):
        records = search_trials(condition="heart failure", max_records=2)
    assert len(records) == 2
    assert mock.call_count == 1, "must not request a second page once satisfied"


def test_query_params_use_v2_names():
    mock = _mock_get([EMPTY_PAGE])
    with patch.object(ctgov.requests, "get", mock):
        search_trials(condition="heart failure", intervention="empagliflozin",
                      sponsor="Boehringer", status=["terminated", "withdrawn"])
    params = mock.call_args[1]["params"]
    assert params["query.cond"] == "heart failure"
    assert params["query.intr"] == "empagliflozin"
    assert params["query.spons"] == "Boehringer"
    assert params["filter.overallStatus"] == "TERMINATED|WITHDRAWN"


def test_empty_result_returns_empty_list():
    with patch.object(ctgov.requests, "get", _mock_get([EMPTY_PAGE])):
        assert search_trials(condition="nothing matches this") == []


def test_offline_blocks_registry_calls():
    try:
        search_trials(condition="anything", offline=True)
    except RuntimeError as exc:
        assert "offline" in str(exc)
    else:
        raise AssertionError("offline mode must block clinicaltrials.gov")


# ------------------------------------------------------------------ store


def test_store_roundtrip():
    store = _store()
    assert len(store) == 5
    rec = store.get("NCT01234567")
    assert rec.conditions == ["Solid Tumor", "Neoplasms"]
    assert rec.interventions == ["Compound X"]


def test_upsert_updates_status_in_place():
    """A trial that was RECRUITING can become TERMINATED; that transition is
    the point of re-ingesting, and must not create a duplicate row."""
    store = _store()
    before = len(store)

    changed = store.get("NCT05555555")
    changed.overall_status = "TERMINATED"
    changed.why_stopped = "Strategic reprioritization"
    store.upsert([changed])

    assert len(store) == before
    after = store.get("NCT05555555")
    assert after.overall_status == "TERMINATED" and after.stopped_early


def test_stopped_trials_query_is_deterministic():
    """The deterministic half of the negative-evidence pass: no model involved."""
    stopped = _store().stopped_trials(intervention="Compound X")
    ids = {r.nct_id for r in stopped}
    assert ids == {"NCT01234567", "NCT07654321"}
    assert all(r.stopped_early for r in stopped)


def test_structured_filters():
    store = _store()
    assert {r.nct_id for r in store.query(phase="Phase 3")} == {"NCT03057977", "NCT05555555"}
    assert {r.nct_id for r in store.query(sponsor="Example Therapeutics")} == {
        "NCT01234567",
        "NCT07654321",
    }
    assert {r.nct_id for r in store.query(condition="heart failure")} == {
        "NCT03057977",
        "NCT05555555",
    }


def test_stopped_trials_sort_first():
    results = _store().query(intervention="Compound X", limit=10)
    assert results[0].stopped_early, "negative signal must lead"


def test_full_text_search():
    hits = _store().search("empagliflozin")
    assert hits and hits[0].nct_id == "NCT03057977"


def test_stats_report_why_stopped_fill_rate():
    stats = _store().stats()
    assert stats["total"] == 5
    assert stats["stopped"] == 2 and stats["stopped_with_reason"] == 1
    assert stats["why_stopped_fill_rate"] == 0.5
    assert stats["by_status"]["TERMINATED"] == 1


def test_stats_on_empty_store_does_not_divide_by_zero():
    assert _store([]).stats()["why_stopped_fill_rate"] is None


# ------------------------------------------------------------------ routing


def test_rules_route_registry_questions_to_structured():
    for q in [
        "Which Phase 3 trials on this target were terminated?",
        "Who else has run trials on this mechanism?",
        "Has any trial in this indication been withdrawn?",
    ]:
        assert classify_by_rules(q).route == Route.STRUCTURED, q


def test_rules_route_science_questions_to_semantic():
    for q in [
        "What is the mechanism of action and the strongest evidence for it?",
        "What does the literature say about the safety profile?",
        "What hazard ratio was reported for the primary endpoint?",
    ]:
        assert classify_by_rules(q).route == Route.SEMANTIC, q


def test_rules_route_mixed_questions_to_both():
    q = "Have any trials on this mechanism been terminated, and what does the literature say about efficacy?"
    assert classify_by_rules(q).route == Route.BOTH


def test_unmatched_question_defaults_to_literature():
    decision = classify_by_rules("Tell me about this asset.")
    assert decision.route == Route.SEMANTIC and "default" in decision.reason


def test_filter_extraction():
    f = extract_filters("Were any Phase III trials terminated? See NCT01234567")
    assert f["phase"] == "Phase 3"
    assert f["stopped_only"] is True
    assert f["nct_ids"] == ["NCT01234567"]
    assert extract_filters("What is the mechanism?") == {}


def test_router_uses_rules_without_api_key():
    decision = Router(Config(openai_api_key=None)).route("Which trials were terminated?")
    assert decision.method == "rules" and decision.route == Route.STRUCTURED


def test_router_parses_model_response():
    client = MagicMock()
    completion = MagicMock()
    completion.choices = [
        MagicMock(message=MagicMock(content='{"route": "both", "reason": "needs registry and papers"}'))
    ]
    client.chat.completions.create.return_value = completion

    with patch("openai.OpenAI", return_value=client):
        decision = Router(Config(openai_api_key="sk-test")).route("anything")

    assert decision.route == Route.BOTH and decision.method == "llm"
    assert decision.needs_trials and decision.needs_literature


def test_router_falls_back_on_bad_model_output():
    """A router that silently degrades to always-BOTH would look like it works
    while costing double and diluting every answer."""
    client = MagicMock()
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(content="not json at all"))]
    client.chat.completions.create.return_value = completion

    with patch("openai.OpenAI", return_value=client):
        decision = Router(Config(openai_api_key="sk-test")).route(
            "Which Phase 3 trials were terminated?"
        )

    assert decision.method == "llm-fallback"
    assert decision.route == Route.STRUCTURED, "must fall back to rules, not to BOTH"


def test_router_offline_never_calls_model():
    client = MagicMock()
    with patch("openai.OpenAI", return_value=client):
        decision = Router(Config(offline=True, openai_api_key="sk-test")).route("q")
    assert decision.method == "rules"
    client.chat.completions.create.assert_not_called()


# ------------------------------------------------------------------ context


def _passage() -> Retrieved:
    return Retrieved(
        chunk=Chunk(
            chunk_id="34449189::0",
            doc_id="34449189",
            text="Empagliflozin reduced hospitalization (hazard ratio 0.79).",
            title="Empagliflozin in HFpEF",
            citation="Anker et al., N Engl J Med, 2021",
            url="https://pubmed.ncbi.nlm.nih.gov/34449189/",
        ),
        score=0.81,
    )


def test_evidence_is_labelled_by_provenance():
    trials = [r for r in _all_records() if r.nct_id == "NCT01234567"]
    evidence = build_evidence(trials=trials, passages=[_passage()])

    assert [e.kind for e in evidence] == [TRIAL_LABEL, LIT_LABEL]
    assert evidence[0].identifier == "NCT01234567"
    assert evidence[1].identifier == "PMID 34449189"

    rendered = render_context(evidence)
    assert "[1] (TRIAL RECORD — NCT01234567)" in rendered
    # Literature carries its evidence tier in the header too, so the model knows
    # it is reading a case report before it weighs the claim.
    assert "[2] (LITERATURE — PMID 34449189 — UNGRADED)" in rendered
    assert "WHY STOPPED: Interim analysis" in rendered


def test_stopped_without_reason_says_so_explicitly():
    """Silence reads as 'no reason to worry'; it actually means nothing was filed."""
    trials = [r for r in _all_records() if r.nct_id == "NCT07654321"]
    assert "WHY STOPPED: not stated by sponsor" in render_context(build_evidence(trials=trials))


def test_trials_are_numbered_before_literature():
    evidence = build_evidence(trials=_all_records()[:2], passages=[_passage()])
    assert evidence[0].index == 1 and evidence[-1].kind == LIT_LABEL
    assert [e.index for e in evidence] == list(range(1, len(evidence) + 1))


def test_provenance_summary_counts_stopped():
    summary = provenance_summary(build_evidence(trials=_all_records(), passages=[_passage()]))
    assert summary["n_trials"] == 5
    assert summary["n_literature"] == 1
    assert summary["n_stopped_trials"] == 2
    # Evidence-tier breakdown was added alongside grading.
    assert "evidence_tiers" in summary and "n_weak_evidence" in summary


def test_context_respects_char_budget():
    evidence = build_evidence(trials=_all_records(), passages=[_passage()], max_chars=200)
    assert len(evidence) >= 1
    assert len(render_context(evidence)) < 1000


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
    print("\nall trial/router tests passed" if not failures else f"\n{failures} test(s) failed")
    raise SystemExit(1 if failures else 0)
