"""openFDA DRUG store — approvals, labels and recalls, beside the device store.

No network: the client is driven through a mocked requests transport against
fixtures captured from the live api.fda.gov (see fixtures/openfda_drugs.py).

THE PROPERTY THIS FILE EXISTS FOR

"Not found in drugsFDA" must never render as "not approved". Absence is
consistent with four different facts — never submitted, submitted and not
approved, approved under a name we did not match, or approved outside the US —
and this database can distinguish none of them. A memo that says "not FDA
approved" about an approved competitor is a false statement of fact; about the
asset under diligence it is worse. The absence tests below drive every renderer
that touches a drug record and assert that no code path turns an empty result
into an approval claim.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import netguard  # noqa: E402

netguard.install()
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures.openfda_drugs import (  # noqa: E402
    DRUG_ENFORCEMENT_PAGE,
    DRUG_LABEL_PAGE,
    DRUGSFDA_PAGE,
    NOT_FOUND,
)

from medrag.context import (  # noqa: E402
    FDA_DRUG_LABEL,
    build_evidence,
    provenance_summary,
    render_context,
)
from medrag.fda import client as fda_client  # noqa: E402
from medrag.fda.drug_store import (  # noqa: E402
    STORE_VERSION,
    ApprovalAnswer,
    DrugStore,
    DrugStoreSchemaError,
)
from medrag.fda.drugs import (  # noqa: E402
    ABSENCE_MEANINGS,
    APPROVED,
    MAX_SECTION_CHARS,
    TENTATIVE_APPROVAL,
    count_applications,
    parse_application,
    parse_drug_recall,
    parse_label,
    search_applications,
    search_drug_recalls,
    search_labels,
)
from medrag.router import classify_by_rules  # noqa: E402

# ------------------------------------------------------------- mocked transport


def _resp(page, status=200):
    r = MagicMock(status_code=status)
    r.json.return_value = page
    r.raise_for_status.return_value = None
    return r


def _apps():
    return [a for a in (parse_application(r) for r in DRUGSFDA_PAGE["results"]) if a]


def _by_number(number):
    return next(a for a in _apps() if a.application_number == number)


def _store(record_search=True) -> DrugStore:
    store = DrugStore(Path(tempfile.mkdtemp()) / "drugs.db")
    store.upsert_applications(_apps())
    store.upsert_labels([parse_label(r) for r in DRUG_LABEL_PAGE["results"]])
    store.upsert_recalls([parse_drug_recall(r) for r in DRUG_ENFORCEMENT_PAGE["results"]])
    if record_search:
        store.record_search("pembrolizumab", reported_total=2, n_applications=1)
    return store


# ------------------------------------------------------------- parsing


def test_approval_is_read_from_the_orig_submission_not_a_supplement():
    """Approval is a submission fact. A SUPPL row is an efficacy supplement and
    can never make an unapproved application approved."""
    a = _by_number("BLA125514")
    assert a.approval_status == APPROVED
    assert a.approval_date == "20140904"        # Keytruda's real approval date
    assert a.application_type == "BLA"
    assert a.review_priority == "PRIORITY"
    assert a.n_supplements > 0, "supplements are counted separately, not as approvals"


def test_tentative_approval_is_not_approval():
    """Measured vocabulary: AP 25,490, TA 1,140. A TA means the FDA found the
    application met requirements but could NOT approve it."""
    a = _by_number("ANDA213576")
    assert a.approval_status == TENTATIVE_APPROVAL
    assert a.is_approved is False, "a tentative approval must never read as approved"


def test_an_application_with_no_openfda_block_is_still_parsed_and_matchable():
    """The measurement that decided the matching field: products.active_ingredients.name
    is present on 99% of applications, openfda.generic_name on 43%. NDA017488
    carries no openfda block at all."""
    a = _by_number("NDA017488")
    assert a.has_openfda is False
    assert a.generic_names == []
    assert a.all_ingredients == ["ETHINYL ESTRADIOL", "NORETHINDRONE"]
    assert a.match_names, "an application with no openfda block must still be findable"


def test_a_combination_product_keeps_both_active_ingredients():
    a = _by_number("ANDA076290")
    assert a.all_ingredients == ["LIDOCAINE", "PRILOCAINE"]


def test_discontinued_is_tracked_separately_from_never_approved():
    """Approved-then-withdrawn and never-approved are different facts."""
    a = _by_number("NDA017488")
    assert a.approval_status == APPROVED and a.all_discontinued is True


def test_label_sections_are_truncated_and_the_truncation_is_recorded():
    """Real sections reach 213 KB. Silent truncation would be a memo quoting a
    label it only partly read."""
    label = parse_label(DRUG_LABEL_PAGE["results"][0])
    assert label.set_id and label.indications
    for name, text in label.sections.items():
        assert len(text) <= MAX_SECTION_CHARS + 2, name
    assert isinstance(label.truncated_sections, list)


def test_a_label_that_cannot_be_joined_to_an_application_says_so():
    """Only 74,827 of 261,379 labels (29%) carry openfda.application_number."""
    label = parse_label(DRUG_LABEL_PAGE["results"][0])
    assert label.linked == bool(label.application_numbers)
    orphan = parse_label({"set_id": "abc", "indications_and_usage": ["x"]})
    assert orphan.linked is False


def test_a_drug_recall_with_an_empty_openfda_block_still_parses():
    """82% of drug recalls carry no openfda block and cannot be joined to an
    application; they must still be readable."""
    r = parse_drug_recall(DRUG_ENFORCEMENT_PAGE["results"][0])
    assert r.recall_number and r.application_numbers == []
    assert r.classification.startswith("Class")
    assert r.severity_rank <= 3


# ------------------------------------------------------------- fetch (mocked)


def test_search_uses_the_ingredient_field_not_only_openfda():
    """The query must name products.active_ingredients.name, or it silently
    reaches 43% of the database."""
    with patch.object(fda_client.requests, "get", MagicMock(
            side_effect=[_resp(DRUGSFDA_PAGE), _resp({"results": []})])) as g:
        apps = search_applications("pembrolizumab")
    assert len(apps) == len(DRUGSFDA_PAGE["results"])
    sent = g.call_args_list[0].kwargs["params"]["search"]
    assert "products.active_ingredients.name" in sent
    assert "openfda.generic_name" in sent


def test_search_expands_aliases_through_the_shared_agents_table():
    """A brand or development code must reach the same application the generic
    does — via config/agents.yaml, the SAME table the trial store uses."""
    with patch.object(fda_client.requests, "get", MagicMock(
            side_effect=[_resp(DRUGSFDA_PAGE), _resp({"results": []})])) as g:
        search_applications("Keytruda")
    sent = g.call_args_list[0].kwargs["params"]["search"]
    assert "pembrolizumab" in sent.lower(), "the brand name must expand to the generic"


def test_a_404_is_no_matches_not_an_error():
    with patch.object(fda_client.requests, "get", MagicMock(
            return_value=_resp(NOT_FOUND, status=404))):
        assert search_applications("nonexistentdrug") == []
        assert search_labels("nonexistentdrug") == []
        assert search_drug_recalls("nonexistentdrug") == []
        assert count_applications("nonexistentdrug") == 0


def test_an_unparseable_asset_never_becomes_an_unfiltered_search():
    """An empty query must not fetch the whole database."""
    with patch.object(fda_client.requests, "get", MagicMock()) as g:
        assert search_applications("") == []
        assert count_applications("") is None
    g.assert_not_called()


# ------------------------------------------------------------- the store


def test_the_store_matches_on_ingredient_brand_and_development_code():
    store = _store()
    for asset in ("pembrolizumab", "Keytruda", "MK-3475"):
        got = {a.application_number for a in store.applications(asset)}
        assert got == {"BLA125514"}, f"{asset} -> {got}"
    store.close()


def test_an_application_with_no_openfda_block_is_found_by_its_ingredient():
    store = _store()
    assert {a.application_number for a in store.applications("norethindrone")} == {"NDA017488"}
    store.close()


def test_a_combination_asset_ands_its_ingredients():
    """Same policy as the trial store: the asset IS the combination."""
    store = _store()
    assert {a.application_number for a in store.applications("lidocaine and prilocaine")} \
        == {"ANDA076290"}
    assert store.applications("lidocaine and pembrolizumab") == []
    store.close()


def test_a_salt_form_and_its_base_find_each_other():
    """drugsFDA writes the salt into the ingredient name. Measured: 568 of the
    1,000 most-used ingredient names are multi-word, HYDROCHLORIDE alone
    accounting for 181."""
    from medrag.agents import name_tokens

    tokens = name_tokens("IRINOTECAN HYDROCHLORIDE")
    assert "irinotecan" in tokens and "irinotecanhydrochloride" in tokens

    from medrag.agents import expand_aliases

    forms, curated = expand_aliases("leucovorin calcium")
    assert "leucovorin" in forms and curated is True


def test_a_salt_word_is_never_stripped_to_a_different_substance():
    """"POTASSIUM CHLORIDE" is the drug, not a salt of potassium. Both readings
    are emitted; nothing is replaced."""
    from medrag.agents import name_tokens

    tokens = name_tokens("POTASSIUM CHLORIDE")
    assert "potassiumchloride" in tokens, "the full name must remain matchable"


def test_stale_drug_db_is_refused_with_a_rebuild_instruction():
    import sqlite3

    path = Path(tempfile.mkdtemp()) / "drugs.db"
    DrugStore(path).close()
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()
    try:
        DrugStore(path)
    except DrugStoreSchemaError as exc:
        assert "rm " in str(exc) and str(STORE_VERSION) in str(exc)
    else:
        raise AssertionError("a stale drugs.db must be refused, not silently read")


def test_the_store_records_what_was_searched_and_when():
    """Coverage and freshness, the same declaration the other two stores make."""
    store = _store()
    fresh = store.freshness()
    assert "pembrolizumab" in fresh
    assert fresh["pembrolizumab"]["reported_total"] == 2
    assert fresh["pembrolizumab"]["searched_at"]
    store.close()


# ------------------------------------------------- ABSENCE IS NOT DISAPPROVAL


def test_an_unsearched_asset_is_distinct_from_one_searched_and_not_found():
    """The not-assessed-vs-nothing-found rule, on the question where getting it
    wrong is most expensive."""
    store = _store()
    never = store.approval_answer("zanzalintinib")
    assert never.searched is False and never.found is False
    assert "NOT checked" in never.statement()
    assert "not a finding about the asset" in never.statement()
    store.close()


def test_a_searched_and_not_found_asset_states_all_four_meanings():
    store = _store()
    store.record_search("botensilimab", reported_total=0, n_applications=0)
    answer = store.approval_answer("botensilimab")
    assert answer.searched is True and answer.found is False
    statement = answer.statement()
    for meaning in ABSENCE_MEANINGS:
        assert meaning in statement, f"absence meaning missing: {meaning}"
    assert "says nothing either way about approval status" in statement
    assert "US applications only" in statement, "the outside-the-US gap must be stated"
    store.close()


def test_is_approved_is_false_for_every_shape_of_absence_and_never_asserts_the_negative():
    """The load-bearing assertion. `is_approved` requires positive evidence — a
    matched application whose ORIG submission was approved — so no arrangement
    of missing data can produce True, and no statement asserts non-approval."""
    store = _store()
    store.record_search("botensilimab", reported_total=0, n_applications=0)
    for asset in ("botensilimab", "zanzalintinib", "", "   "):
        answer = store.approval_answer(asset)
        assert answer.is_approved is False
        assert answer.approved_applications == []
        text = answer.statement().lower()
        for forbidden in ("is not approved", "is unapproved", "has not been approved",
                          "no fda approval exists", "never approved"):
            assert forbidden not in text, f"{asset!r} produced a disapproval claim: {text}"
    store.close()


def test_no_renderer_turns_an_empty_result_into_an_approval_claim():
    """Drives every consumer of a drug record with an EMPTY result and asserts
    none of them state approval or non-approval. A guard the library enforces
    but production bypasses is decoration — so this exercises the assembled
    context and the provenance summary, not just the store."""
    store = _store()
    store.record_search("botensilimab", reported_total=0, n_applications=0)
    answer = store.approval_answer("botensilimab")
    assert not answer.applications

    evidence = build_evidence(drugs=answer.applications)
    assert evidence == [], "an empty result must contribute no evidence at all"
    assert render_context(evidence) == ""
    summary = provenance_summary(evidence)
    assert summary["n_fda_drug"] == 0 and summary["n_fda_drug_approved"] == 0

    blob = " ".join([answer.statement(), render_context(evidence)]).lower()
    for forbidden in ("is not approved", "is unapproved", "not fda approved",
                      "has no approval", "never approved"):
        assert forbidden not in blob
    store.close()


def test_an_answer_built_with_no_store_at_all_still_refuses_to_claim_anything():
    answer = ApprovalAnswer(asset="anything")
    assert answer.searched is False and answer.is_approved is False
    assert "NOT checked" in answer.statement()


def test_a_tentative_approval_never_reads_as_approved_in_any_surface():
    """Store, statement and assembled context must agree that TA is not AP."""
    store = _store()
    store.record_search("eluxadoline", reported_total=1, n_applications=1)
    answer = store.approval_answer("eluxadoline")
    assert answer.found is True and answer.is_approved is False
    assert "not an approval" in answer.statement().lower()

    context = render_context(build_evidence(drugs=answer.applications))
    assert "TENTATIVE APPROVAL" in context
    assert "NOT an approval" in context
    store.close()


def test_a_discontinued_drug_is_never_described_as_unapproved():
    store = _store()
    answer = store.approval_answer("norethindrone")
    assert answer.is_approved is True
    text = (answer.statement() + render_context(build_evidence(drugs=answer.applications)))
    assert "Discontinued" in text
    assert "NOT the same as never approved" in text or "not the same as never approved" in text
    store.close()


# ------------------------------------------------------------- context + routing


def test_a_drug_citation_resolves_to_an_application_number():
    """Not a generic FDA RECORD: a device clearance and a drug approval are
    different regulatory objects and a reader must be able to tell which one a
    citation points at."""
    store = _store()
    evidence = build_evidence(drugs=store.applications("pembrolizumab"))
    assert len(evidence) == 1
    e = evidence[0]
    assert e.kind == FDA_DRUG_LABEL
    assert e.identifier == "BLA 125514", "the FDA prints a space; a citation must too"
    assert "accessdata.fda.gov" in e.url
    assert "`BLA 125514`" in e.bib_line()
    assert e.meta["approval_status"] == APPROVED
    store.close()


def test_drug_evidence_shares_the_single_citation_numbering():
    """One numbering across every store — a parallel scheme has been a bug twice."""
    from medrag.trials.client import TrialRecord

    store = _store()
    trial = TrialRecord(nct_id="NCT00000001", brief_title="T")
    evidence = build_evidence(trials=[trial], drugs=store.applications("pembrolizumab"))
    assert [e.index for e in evidence] == [1, 2]
    assert evidence[1].identifier == "BLA 125514"
    store.close()


def test_approval_questions_route_to_the_drug_store():
    for q in ("Is this drug FDA approved?",
              "When was it approved and for which indications?",
              "What does the label say about boxed warnings?",
              "Are there ANDA filings for this molecule?",
              "Has the NDA been withdrawn?"):
        d = classify_by_rules(q)
        assert d.needs_drug_regulatory, f"did not reach the drug store: {q}"


def test_a_device_question_does_not_pull_in_the_drug_store():
    d = classify_by_rules("What is the predicate device for this 510(k) product code FRN?")
    assert d.needs_regulatory and not d.needs_drug_regulatory


def test_a_question_can_need_both_regulatory_stores():
    d = classify_by_rules("What is the regulatory status: any 510(k) clearance or NDA approval?")
    assert d.needs_regulatory and d.needs_drug_regulatory


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
    print("\nall drug-store tests passed" if not failures else f"\n{failures} test(s) failed")
    raise SystemExit(1 if failures else 0)
