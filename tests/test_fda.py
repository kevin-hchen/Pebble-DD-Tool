"""Tests for the openFDA device store — a third structured store beside trials.

No network: the client is driven through a mocked requests transport against
fixtures captured from the live api.fda.gov (see fixtures/openfda.py). Parsing,
skip/limit pagination, the SQLite store, the schema refusal, product-code
matching, the negative-evidence wiring, routing, context assembly and the memo
rendering are all covered without a live call.
"""

from __future__ import annotations

import sqlite3
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

from fixtures.openfda import CLEARANCE_PAGE, EVENT_PAGE, NOT_FOUND, RECALL_PAGE  # noqa: E402

from medrag.config import Config  # noqa: E402
from medrag.context import (  # noqa: E402
    FDA_LABEL,
    TRIAL_LABEL,
    build_evidence,
    render_context,
)
from medrag.fda import client as fda_client  # noqa: E402
from medrag.fda.client import (  # noqa: E402
    count_510k,
    parse_510k,
    parse_event,
    parse_recall,
    search_510k,
    search_events,
    search_recalls,
)
from medrag.fda.store import STORE_VERSION, FDAStore, FDAStoreSchemaError  # noqa: E402
from medrag.negative_evidence import (  # noqa: E402
    find_adverse_events,
    find_device_recalls,
    run_negative_pass,
)
from medrag.router import classify_by_rules, extract_filters  # noqa: E402

# ------------------------------------------------------------- mocked transport


def _resp(page, status=200):
    r = MagicMock(status_code=status)
    r.json.return_value = page
    r.raise_for_status.return_value = None
    return r


def _mock_get(*responses):
    return MagicMock(side_effect=list(responses))


def _clearances():
    return [c for c in (parse_510k(r) for r in CLEARANCE_PAGE["results"]) if c]


def _recalls():
    return [r for r in (parse_recall(x) for x in RECALL_PAGE["results"]) if r]


def _events():
    return [e for e in (parse_event(x) for x in EVENT_PAGE["results"]) if e]


def _store() -> FDAStore:
    store = FDAStore(Path(tempfile.mkdtemp()) / "fda.db")
    store.upsert_clearances(_clearances())
    store.upsert_recalls(_recalls())
    store.upsert_events(_events())
    return store


# ------------------------------------------------------------- parsing


def test_parse_510k_reads_top_level_and_openfda():
    c = parse_510k(CLEARANCE_PAGE["results"][0])
    assert c.k_number == "K781171"
    assert c.product_code == "FRN"
    assert c.device_class == "2"            # from the openfda block
    assert c.regulation_number == "880.5725"
    assert c.decision_description == "Substantially Equivalent"


def test_parse_recall_carries_join_keys():
    r = parse_recall(RECALL_PAGE["results"][0])
    assert r.recall_number == "Z-0001-2011"
    assert r.product_code == "FRN"
    assert r.recalling_firm == "Baxter Healthcare Corp."
    assert r.k_numbers, "recalls link back to clearances by k_number"


def test_parse_event_uses_nested_product_code():
    """MAUDE has no top-level product_code; it is under device[]. The parser must
    reach into device[0].device_report_product_code or every event is orphaned."""
    e = parse_event(next(x for x in EVENT_PAGE["results"] if x["event_type"] == "Death"))
    assert e.event_type == "Death"
    assert e.product_code == "FRN"
    assert e.brand_name and e.narrative, "the event description narrative is extracted"


def test_event_date_format_is_preserved_verbatim():
    e = parse_event(EVENT_PAGE["results"][0])
    # Live event.date_received is YYYYMMDD, unlike the ISO dates on 510k/recall.
    assert e.date_received.isdigit() and len(e.date_received) == 8


# ------------------------------------------------------------- fetch / pagination


def test_search_510k_parses_a_page():
    with patch.object(fda_client.requests, "get", _mock_get(_resp(CLEARANCE_PAGE))):
        out = search_510k(product_code="FRN", max_records=50)
    assert len(out) == 2 and out[0].k_number == "K781171"


def test_multi_term_search_uses_space_separated_AND():
    """A literal '+AND+' is URL-encoded to %2B and breaks the query; the client
    must join terms with ' AND ' so requests encodes it correctly."""
    getter = _mock_get(_resp(CLEARANCE_PAGE))
    with patch.object(fda_client.requests, "get", getter):
        search_510k(product_code="FRN", device_name="infusion pump", max_records=50)
    search = getter.call_args[1]["params"]["search"]
    assert " AND " in search and "+AND+" not in search


def test_404_means_no_matches_not_error():
    with patch.object(fda_client.requests, "get", _mock_get(_resp(NOT_FOUND, status=404))):
        assert search_recalls(product_code="ZZZ") == []


def test_events_search_uses_the_nested_field():
    getter = _mock_get(_resp(EVENT_PAGE))
    with patch.object(fda_client.requests, "get", getter):
        search_events(product_code="FRN", max_records=10)
    assert "device.device_report_product_code:FRN" in getter.call_args[1]["params"]["search"]


# ------------------------------------------------------------- store


def test_clearances_match_on_product_code_and_device_name():
    store = _store()
    assert {c.k_number for c in store.clearances(product_code="FRN")} == {"K781171", "K931318"}
    assert store.clearances(device_name="pediatric")[0].k_number == "K781171"
    store.close()


def test_product_code_is_the_device_join_key():
    store = _store()
    assert store.product_codes_for_device("infusion pump") == ["FRN"]
    store.close()


def test_recalls_query_by_product_code():
    store = _store()
    recalls = store.recalls(product_code="FRN")
    assert len(recalls) == 3
    assert {r.recall_status for r in recalls} >= {"Terminated", "Open, Classified"}
    store.close()


def test_events_sort_worst_severity_first():
    store = _store()
    events = store.events(product_code="FRN", limit=2)
    assert [e.event_type for e in events] == ["Death", "Injury"]
    store.close()


def test_event_counts_report_the_full_denominator():
    store = _store()
    counts = store.event_counts("FRN")
    assert counts.get("Death") == 1 and counts.get("Malfunction") == 1
    store.close()


def test_stale_fda_db_is_refused_with_a_rebuild_step():
    path = Path(tempfile.mkdtemp()) / "fda.db"
    FDAStore(path).close()
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()
    try:
        FDAStore(path)
    except FDAStoreSchemaError as exc:
        assert "rm " in str(exc) and "re-ingest" in str(exc).lower()
    else:
        raise AssertionError("a stale fda.db must be refused, not silently read")


def test_current_db_reports_its_version():
    store = _store()
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == STORE_VERSION
    store.close()


# ------------------------------------------------------------- sample vs total


def test_count_510k_reads_the_reported_total():
    getter = _mock_get(_resp(CLEARANCE_PAGE))
    with patch.object(fda_client.requests, "get", getter):
        total = count_510k(product_code="FRN")
    assert total == 848            # the fixture's meta.results.total, not len(results)
    assert getter.call_args[1]["params"]["limit"] == 1  # cheap: one row, read the meta


def test_store_total_is_the_local_count_not_the_category():
    store = _store()
    # The store holds 2 FRN clearances; the category (848) is recorded separately.
    assert store.clearances_total(product_code="FRN") == 2
    assert store.category_total("FRN") is None      # not set until ingest records it
    store.set_category_total("FRN", 848)
    assert store.category_total("FRN") == 848
    store.close()


def test_category_total_survives_a_reopen():
    path = Path(tempfile.mkdtemp()) / "fda.db"
    s = FDAStore(path)
    s.upsert_clearances(_clearances())
    s.set_category_total("FRN", 848)
    s.close()
    reopened = FDAStore(path)
    assert reopened.category_total("FRN") == 848
    reopened.close()


# ------------------------------------------------------------- negative evidence


def test_recalls_are_a_deterministic_negative_half():
    store = _store()
    recalls = find_device_recalls(store, product_code="FRN")
    assert len(recalls) == 3
    store.close()


def test_device_name_resolves_to_product_code_for_recalls():
    store = _store()
    # No product code given: resolve it from the clearances by device name.
    recalls = find_device_recalls(store, device_name="infusion pump")
    assert recalls, "a device name must resolve to its product code and find recalls"
    store.close()


def test_adverse_events_capped_and_severity_sorted_with_totals():
    store = _store()
    events, totals = find_adverse_events(store, product_code="FRN", limit=2)
    assert [e.event_type for e in events] == ["Death", "Injury"]
    assert sum(totals.values()) == 3, "totals report the full count, not the shown subset"
    store.close()


def test_run_negative_pass_wires_fda_beside_trials():
    store = _store()
    neg = run_negative_pass("Infusion pump X is safe.", Config(openai_api_key=None),
                            evidence=[], fda_store=store, product_code="FRN")
    assert len(neg.recalls) == 3 and neg.adverse_events
    assert neg.fda_searched is True
    # Separate lines: recalls and events are their own fields, not merged into
    # stopped_trials.
    assert neg.stopped_trials == []
    assert "FDA recall" in neg.summary() and "adverse-event" in neg.summary()
    store.close()


def test_no_fda_store_is_unsearched_not_empty():
    """'Not checked' must stay distinct from 'no recalls found'."""
    neg = run_negative_pass("claim", Config(openai_api_key=None), evidence=[], fda_store=None)
    assert neg.fda_searched is False and neg.recalls == []


# ------------------------------------------------------------- routing


def test_regulatory_questions_flag_the_fda_store():
    for q in ("Is device X FDA-cleared and have there been recalls?",
              "What is the device class and product code?",
              "Any adverse events reported in MAUDE?"):
        assert classify_by_rules(q).needs_regulatory, q


def test_non_regulatory_question_does_not_flag_fda():
    assert not classify_by_rules("What is the mechanism of action?").needs_regulatory


def test_product_code_filter_extracted_only_when_named():
    assert extract_filters("recalls for product code FRN")["product_code"] == "FRN"
    assert "product_code" not in extract_filters("what recalls exist for this pump")


# ------------------------------------------------------------- context assembly


def test_fda_records_share_the_single_evidence_numbering():
    """Trials, then FDA, then literature — one continuous numbering so a citation
    [n] resolves the same everywhere."""
    from fixtures.ctgov import PAGE_ONE

    from medrag.trials.client import parse_study

    trial = parse_study(PAGE_ONE["studies"][1])
    ev = build_evidence(trials=[trial], fda=_clearances())
    assert [e.index for e in ev] == [1, 2, 3]
    assert ev[0].kind == TRIAL_LABEL
    assert ev[1].kind == FDA_LABEL and ev[1].identifier == "K781171"
    rendered = render_context(ev)
    assert "FDA RECORD — K781171" in rendered
    assert rendered.index("[1]") < rendered.index("[2]") < rendered.index("[3]")


def test_fda_block_states_class_and_product_code():
    ev = build_evidence(fda=_clearances())
    text = ev[0].text
    assert "Product code: FRN" in text and "Device class: 2" in text


# ------------------------------------------------------------- memo rendering


def test_memo_renders_recalls_and_events_on_their_own_lines():
    """A recall and a halted trial are different failure modes and must not be
    merged into one list — the memo gives each its own heading."""
    from fixtures.ctgov import PAGE_ONE

    from medrag.diligence import DiligenceQuestion, MemoResult, SectionResult
    from medrag.generator import Answer
    from medrag.memo import render_markdown
    from medrag.negative_evidence import NegativeEvidence, StoppedTrial
    from medrag.router import Route
    from medrag.trials.client import parse_study
    from medrag.validation import ValidationReport

    neg = NegativeEvidence(
        claim="Infusion pump X is safe.",
        stopped_trials=[StoppedTrial(record=parse_study(PAGE_ONE["studies"][1]))],
        recalls=_recalls(),
        adverse_events=find_adverse_events(_store(), product_code="FRN", limit=2)[0],
        event_totals={"Death": 1, "Injury": 1, "Malfunction": 1},
    )
    section = SectionResult(
        question=DiligenceQuestion(id="s", section="Safety", question="Safe?", negative=True),
        rendered_question="Is infusion pump X safe?",
        answer=Answer(text="No answer.", model="none"),
        evidence=[], validation=ValidationReport(assessed=False),
        route=Route.STRUCTURED, route_method="rules", negative=neg,
    )
    md = render_markdown(MemoResult(asset="Infusion Pump X", indication="",
                                    question_set="qs", sections=[section]))
    assert "**Trials stopped early**" in md
    assert "**FDA recalls**" in md
    assert "FDA adverse events" in md
    # Recalls come after the trial stops, not merged into them.
    assert md.index("Trials stopped early") < md.index("FDA recalls")
    assert "Z-0001-2011" in md and "Death" in md


def test_memo_states_the_510k_sample_against_the_total_with_caveat():
    """A section that shows clearances must say how many of how many, and warn
    that applicant names over-count companies."""
    from medrag.diligence import DiligenceQuestion, MemoResult, SectionResult
    from medrag.generator import Answer
    from medrag.memo import render_markdown
    from medrag.router import Route
    from medrag.validation import ValidationReport

    section = SectionResult(
        question=DiligenceQuestion(id="c", section="Clearances", question="Who else?"),
        rendered_question="Who else is cleared in this category?",
        answer=Answer(text="Several firms.", model="none"),
        evidence=[], validation=ValidationReport(assessed=False),
        route=Route.STRUCTURED, route_method="rules",
        provenance={"n_fda": 25, "n_fda_store_total": 150,
                    "n_fda_category_total": 848, "fda_product_code": "FRN"},
    )
    md = render_markdown(MemoResult(asset="Infusion Pump X", indication="",
                                    question_set="qs", sections=[section]))
    assert "showing 25 of 150 held locally for product code FRN" in md
    assert "openFDA reports 848 cleared in this category" in md
    assert "over-count distinct companies" in md and "Imed" in md


def test_no_fda_caveat_when_a_section_has_no_clearances():
    from medrag.diligence import DiligenceQuestion, MemoResult, SectionResult
    from medrag.generator import Answer
    from medrag.memo import render_markdown
    from medrag.router import Route
    from medrag.validation import ValidationReport

    section = SectionResult(
        question=DiligenceQuestion(id="m", section="Mechanism", question="How?"),
        rendered_question="What is the mechanism?",
        answer=Answer(text="A mechanism.", model="none"),
        evidence=[], validation=ValidationReport(assessed=False),
        route=Route.SEMANTIC, route_method="rules", provenance={"n_fda": 0},
    )
    md = render_markdown(MemoResult(asset="X", indication="", question_set="qs", sections=[section]))
    assert "over-count" not in md and "510(k) clearances: showing" not in md


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
    print("\nall FDA tests passed" if not failures else f"\n{failures} test(s) failed")
    raise SystemExit(1 if failures else 0)
