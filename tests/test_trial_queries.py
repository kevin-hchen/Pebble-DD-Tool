"""Tests for exhaustive registry fetching, query-set union, fetch provenance,
and the rule that the local layer never re-narrows on a condition string.

The property under test throughout is REACHABILITY: a trial the ingest went and
got must still be there when the landscape asks for it. Five of six known MSS
colorectal trials were missing from a 500-record store of a 10,193-study query —
three stages conspired, and each gets a test here.

All network calls are mocked; tests/netguard.py blocks real sockets.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import netguard  # noqa: E402

netguard.install()

from medrag.landscape import build_landscape  # noqa: E402
from medrag.trials import client as ctgov  # noqa: E402
from medrag.trials.client import IncompleteFetch, run_query  # noqa: E402
from medrag.trials.queries import (  # noqa: E402
    CONDITION,
    TERM,
    QuerySet,
    TrialQuery,
    fetch_query_set,
    load_query_sets,
    resolve_query_set,
)
from medrag.trials.store import TrialStore  # noqa: E402


def _study(nct: str, conditions: list[str], elig: str = "Inclusion Criteria:\n- adult") -> dict:
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct, "briefTitle": f"Study {nct}"},
            "statusModule": {"overallStatus": "RECRUITING"},
            "conditionsModule": {"conditions": conditions},
            "eligibilityModule": {"eligibilityCriteria": elig},
        }
    }


def _page(studies: list[dict], total: int, token: str | None = None) -> dict:
    d = {"studies": studies, "totalCount": total}
    if token:
        d["nextPageToken"] = token
    return d


def _mock_get(pages):
    responses = []
    for page in pages:
        r = MagicMock(status_code=200)
        r.json.return_value = page
        r.raise_for_status.return_value = None
        responses.append(r)
    return MagicMock(side_effect=responses)


def _dispatching_mock(by_query: dict[str, list[dict]]):
    """Serve different page lists per query string, so a union across several
    queries can be driven without assuming call order."""
    state: dict[str, int] = {}

    def _get(url, params=None, timeout=None):
        key = params.get("query.cond") or params.get("query.term") or ""
        pages = by_query.get(key, [_page([], 0)])
        i = state.get(key, 0)
        state[key] = i + 1
        r = MagicMock(status_code=200)
        r.json.return_value = pages[min(i, len(pages) - 1)]
        r.raise_for_status.return_value = None
        return r

    return MagicMock(side_effect=_get)


def _store(records=None, provenance=None, set_key="colorectal") -> TrialStore:
    store = TrialStore(Path(tempfile.mkdtemp()) / "trials.db")
    if records:
        store.upsert(records, provenance=provenance, set_key=set_key)
    return store


# ------------------------------------------------------- exhaustive fetching


def test_fetch_runs_to_exhaustion_when_no_cap_is_given():
    pages = [
        _page([_study("NCT1", ["A"])], 3, token="T2"),
        _page([_study("NCT2", ["A"])], 3, token="T3"),
        _page([_study("NCT3", ["A"])], 3),
    ]
    with patch.object(ctgov.requests, "get", _mock_get(pages)):
        result = run_query(condition="anything")
    assert len(result.records) == 3, "must follow every nextPageToken, not stop at page one"
    assert result.pages == 3
    assert result.complete


def test_registry_reported_total_is_captured_with_the_records():
    with patch.object(ctgov.requests, "get", _mock_get([_page([_study("NCT1", ["A"])], 1)])):
        result = run_query(condition="anything")
    assert result.reported_total == 1, "countTotal is the denominator every count needs"


def test_max_records_is_an_explicit_override_and_marks_truncation():
    pages = [_page([_study("NCT1", ["A"]), _study("NCT2", ["A"])], 9, token="T2")]
    with patch.object(ctgov.requests, "get", _mock_get(pages)):
        result = run_query(condition="anything", max_records=1)
    assert len(result.records) == 1
    assert result.truncated, "a cap must be recorded, not silently applied"
    assert not result.complete


def test_short_fetch_fails_loudly_with_both_numbers():
    """The registry says 500, pagination yields 2. Keeping the 2 and saying
    nothing is how a truncated store passes for a complete one."""
    with patch.object(ctgov.requests, "get",
                      _mock_get([_page([_study("NCT1", ["A"]), _study("NCT2", ["A"])], 500)])):
        try:
            run_query(condition="colorectal cancer")
        except IncompleteFetch as exc:
            assert "500" in str(exc) and "2" in str(exc), "both numbers must be in the message"
            assert exc.reported_total == 500 and exc.fetched == 2
        else:
            raise AssertionError("a short fetch must raise, not return a subset")


def test_studies_without_an_nct_id_do_not_trip_the_completeness_check():
    """The API counts them; we cannot store them. That is not a lost record."""
    page = _page([_study("NCT1", ["A"]), {"protocolSection": {}}], 2)
    with patch.object(ctgov.requests, "get", _mock_get([page])):
        result = run_query(condition="anything")
    assert len(result.records) == 1 and result.skipped_no_id == 1
    assert result.complete


# ------------------------------------------------------------- query sets


def test_shipped_config_has_a_reviewed_colorectal_set():
    sets = load_query_sets()
    assert "colorectal" in sets, "the shipped config must carry the worked example"
    qs = sets["colorectal"]
    values = {q.value.lower() for q in qs.queries}
    for expected in ("colorectal cancer", "colon cancer", "rectal cancer", "bowel cancer"):
        assert expected in values, f"{expected} missing from the reviewed set"
    assert any(q.kind == TERM for q in qs.queries), \
        "a term query is the only axis that reaches basket trials"


def test_a_condition_with_no_reviewed_set_is_flagged_as_uncurated():
    qs = resolve_query_set("a disease nobody has curated", sets={})
    assert not qs.curated, "a single ad-hoc string must not pass for a reviewed synonym set"
    assert len(qs.queries) == 1


def test_a_phrasing_variant_resolves_to_the_reviewed_set():
    """Typing 'metastatic colorectal cancer' must not silently drop to one query
    when a reviewed colorectal set exists."""
    qs = resolve_query_set("metastatic colorectal cancer")
    assert qs.key == "colorectal" and qs.curated


def test_query_set_unions_by_nct_id():
    by_query = {
        "colorectal cancer": [_page([_study("NCT1", ["Colorectal Cancer"]),
                                     _study("NCT2", ["Colorectal Cancer"])], 2)],
        "colon cancer": [_page([_study("NCT2", ["Colorectal Cancer"]),
                                _study("NCT3", ["Colon Cancer"])], 2)],
    }
    qset = QuerySet("crc", "CRC", (TrialQuery(CONDITION, "colorectal cancer"),
                                   TrialQuery(CONDITION, "colon cancer")))
    with patch.object(ctgov.requests, "get", _dispatching_mock(by_query)):
        records, _prov, cov = fetch_query_set(qset)
    assert {r.nct_id for r in records} == {"NCT1", "NCT2", "NCT3"}
    assert cov.total_unique == 3


def test_provenance_records_every_query_that_found_a_trial():
    by_query = {
        "colorectal cancer": [_page([_study("NCT2", ["Colorectal Cancer"])], 1)],
        "colon cancer": [_page([_study("NCT2", ["Colorectal Cancer"])], 1)],
    }
    qset = QuerySet("crc", "CRC", (TrialQuery(CONDITION, "colorectal cancer"),
                                   TrialQuery(CONDITION, "colon cancer")))
    with patch.object(ctgov.requests, "get", _dispatching_mock(by_query)):
        _records, prov, _cov = fetch_query_set(qset)
    assert prov["NCT2"] == ["cond:colorectal cancer", "cond:colon cancer"], \
        "a trial found twice keeps both labels — that is the audit trail"


def test_marginal_yield_counts_only_trials_no_earlier_query_found():
    by_query = {
        "colorectal cancer": [_page([_study("NCT1", ["A"]), _study("NCT2", ["A"])], 2)],
        "colon cancer": [_page([_study("NCT2", ["A"]), _study("NCT3", ["A"])], 2)],
        "bowel cancer": [_page([_study("NCT1", ["A"])], 1)],
    }
    qset = QuerySet("crc", "CRC", tuple(
        TrialQuery(CONDITION, v) for v in ("colorectal cancer", "colon cancer", "bowel cancer")))
    with patch.object(ctgov.requests, "get", _dispatching_mock(by_query)):
        _records, _prov, cov = fetch_query_set(qset)
    yields = {y.query.value: (y.fetched, y.new) for y in cov.yields}
    assert yields["colorectal cancer"] == (2, 2)
    assert yields["colon cancer"] == (2, 1)
    assert yields["bowel cancer"] == (1, 0), "a query that adds nothing must report zero"
    assert "near-complete" in cov.summary(), \
        "zero marginal yield is a measurable completeness claim and must be said"


def test_one_failing_query_does_not_discard_the_others_but_marks_coverage_incomplete():
    def _get(url, params=None, timeout=None):
        if params.get("query.cond") == "colon cancer":
            raise RuntimeError("registry timeout")
        r = MagicMock(status_code=200)
        r.json.return_value = _page([_study("NCT1", ["A"])], 1)
        r.raise_for_status.return_value = None
        return r

    qset = QuerySet("crc", "CRC", (TrialQuery(CONDITION, "colorectal cancer"),
                                   TrialQuery(CONDITION, "colon cancer")))
    with patch.object(ctgov.requests, "get", MagicMock(side_effect=_get)):
        records, _prov, cov = fetch_query_set(qset)
    assert len(records) == 1, "the query that worked must still contribute"
    assert not cov.complete and cov.errors, "the shortfall must be recorded, not absorbed"


# ------------------------------------------------- provenance in the store


def test_reingest_merges_provenance_rather_than_replacing_it():
    """Otherwise 'did we ever search for colon cancer?' loses its answer the next
    time a different query set touches the same trial."""
    rec = ctgov.parse_study(_study("NCT1", ["Colorectal Cancer"]))
    store = _store([rec], {"NCT1": ["cond:colorectal cancer"]}, set_key="colorectal")
    store.upsert([rec], provenance={"NCT1": ["term:MSS colorectal"]}, set_key="basket")
    assert store.found_by("NCT1") == ["cond:colorectal cancer", "term:MSS colorectal"]
    assert store.count(query_set="colorectal") == 1
    assert store.count(query_set="basket") == 1


def test_query_set_token_filter_does_not_collide_on_prefixes():
    a = ctgov.parse_study(_study("NCT1", ["A"]))
    b = ctgov.parse_study(_study("NCT2", ["B"]))
    store = _store([a], {}, set_key="colorectal")
    store.upsert([b], provenance={}, set_key="colo")
    assert store.count(query_set="colo") == 1, "'colo' must not match 'colorectal'"
    assert store.count(query_set="colorectal") == 1


def test_coverage_never_recorded_is_distinct_from_coverage_recorded_empty():
    store = _store()
    assert store.coverage("colorectal") is None, \
        "not-ingested must not read the same as ingested-and-found-nothing"


# ---------------------------------------- the local re-narrowing regression


def test_landscape_keeps_a_trial_whose_condition_string_lacks_the_query_words():
    """MOUNTAINEER-03 registers 'Colorectal Neoplasms'. The old local filter ran
    LOWER(conditions) LIKE '%colorectal cancer%' and threw away a trial the fetch
    had deliberately retrieved. The fetch defines the population."""
    rec = ctgov.parse_study(_study(
        "NCT05253651", ["Colorectal Neoplasms"],
        elig="Inclusion Criteria:\n- MSS colorectal cancer"))
    store = _store([rec], {"NCT05253651": ["cond:colorectal neoplasms"]},
                   set_key="colorectal")
    ls = build_landscape(store, condition="colorectal cancer", biomarker="MSS",
                         query_set="colorectal")
    assert ls.n_condition == 1, "a fetched trial must not be dropped by a local string match"
    assert "NCT05253651" in {t.record.nct_id for t in ls.trials}


def test_landscape_population_total_is_the_ingested_set():
    recs = [ctgov.parse_study(_study(f"NCT{i}", ["Colorectal Neoplasms"])) for i in range(5)]
    store = _store(recs, {}, set_key="colorectal")
    ls = build_landscape(store, condition="colorectal cancer", biomarker="MSS",
                         query_set="colorectal")
    assert ls.population_total == 5 and ls.n_condition == 5


def test_landscape_reports_truncation_rather_than_silently_capping():
    recs = [ctgov.parse_study(_study(f"NCT{i}", ["Colorectal Cancer"])) for i in range(10)]
    store = _store(recs, {}, set_key="colorectal")
    ls = build_landscape(store, condition="colorectal cancer", biomarker="MSS",
                         query_set="colorectal", limit=3)
    assert ls.population_total == 10 and ls.n_condition == 3
    assert any("only 3 of 10" in w for w in ls.warnings), \
        "a capped screen must say so; a silent one reads as complete"


def test_basket_trial_gap_is_a_named_warning_not_a_silent_absence():
    rec = ctgov.parse_study(_study("NCT1", ["Colorectal Cancer"]))
    qset = QuerySet("colorectal", "Colorectal cancer", (TrialQuery(CONDITION, "colorectal cancer"),))
    with patch.object(ctgov.requests, "get", _mock_get([_page([_study("NCT1", ["A"])], 1)])):
        _r, prov, cov = fetch_query_set(qset)
    store = _store([rec], prov, set_key="colorectal")
    store.record_coverage(cov)
    ls = build_landscape(store, condition="colorectal cancer", biomarker="MSS",
                         query_set="colorectal")
    assert any("asket" in w for w in ls.warnings), \
        "an unreachable class of trial must be named in the output, not inferred from absence"


def test_uncurated_single_string_ingest_warns_the_reader():
    rec = ctgov.parse_study(_study("NCT1", ["Rare Disease"]))
    qset = resolve_query_set("some rare disease", sets={})
    with patch.object(ctgov.requests, "get", _mock_get([_page([_study("NCT1", ["A"])], 1)])):
        _r, prov, cov = fetch_query_set(qset)
    store = _store([rec], prov, set_key=qset.key)
    store.record_coverage(cov)
    ls = build_landscape(store, condition="some rare disease", biomarker="MSS",
                         query_set=qset.key)
    assert any("reviewed synonym set" in w for w in ls.warnings)


if __name__ == "__main__":
    import traceback

    failures = 0
    for name, fn in sorted(list(globals().items())):
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
