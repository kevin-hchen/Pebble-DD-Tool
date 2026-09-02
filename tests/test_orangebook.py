"""Orange Book: listed protection, and the absence problem that is worst here.

THE PROPERTY THIS FILE GUARDS

An investigational asset CANNOT appear in the Orange Book — listing requires an
approved application. If that renders as "no patents found" it reads as "this
company has no intellectual property", which for a preclinical or Phase 2 asset
is a false statement about the single thing the company is worth.

So the load-bearing test drives a real investigational asset (botensilimab, with
no approved application) and asserts the section renders NOT APPLICABLE with the
reason, and contains no phrase implying an absence of protection.

No network: real records from the bulk export, the bulk path driven through an
injected fetch.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import netguard  # noqa: E402

netguard.install()
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures.orangebook import (  # noqa: E402
    BULK_CATALOGUE,
    ORANGE_BOOK_BULK_ZIP,
    ORANGE_BOOK_PAGE,
)

from medrag.fda.bulk import load_export  # noqa: E402
from medrag.fda.drug_store import DrugStore  # noqa: E402
from medrag.fda.drugs import DrugApplication, DrugProduct  # noqa: E402
from medrag.fda.orangebook import (  # noqa: E402
    LIMITS,
    ProtectionAnswer,
    classify_exclusivity,
    human_date,
    parse_entries,
)

#: Phrases that would turn "not listed" or "not applicable" into a claim that
#: the sponsor holds nothing. This is the specification.
FORBIDDEN = (
    "no patents", "no intellectual property", "no protection", "unprotected",
    "no exclusivity", "patent-free", "off patent", "has no ip",
)
#: The claim the section must never make, however tempting.
FORBIDDEN_ENTRY = ("generics enter", "generic entry on", "goes generic on",
                   "loses exclusivity on", "generic launch")


def _entries():
    return [e for r in ORANGE_BOOK_PAGE["results"] for e in parse_entries(r)]


def _by(app):
    return next(e for e in _entries() if e.application_number == app)


def _store(with_orange_book=True) -> DrugStore:
    store = DrugStore(Path(tempfile.mkdtemp()) / "drugs.db")
    if with_orange_book:
        store.upsert_orange_book(_entries())
    return store


def _approved(asset, ingredient, app_type="NDA", number="021880"):
    return DrugApplication(
        application_number=f"{app_type}{number}", sponsor_name="X",
        approval_status="APPROVED", approval_date="20051227",
        products=[DrugProduct(product_number="001", brand_name=asset.upper(),
                              active_ingredients=[ingredient.upper()],
                              marketing_status="Prescription")])


# ------------------------------------------------- THE ABSENCE PROBLEM


def test_an_investigational_asset_renders_not_applicable_not_no_patents():
    """The load-bearing test. botensilimab has no approved application, so the
    Orange Book was never going to have anything — and saying "no patents found"
    about a preclinical company is a false statement about its only asset."""
    store = _store()
    answer = store.protection_answer("botensilimab")
    assert answer.searched is True
    assert answer.applicable is False, "an unapproved asset is not applicable, not absent"
    text = " ".join(answer.render_lines())
    assert "does not apply" in text
    assert "absent from the Orange Book by construction" in text
    assert "says nothing about what the sponsor owns" in text
    for phrase in FORBIDDEN:
        assert phrase not in text.lower(), f"rendered an unapproved asset as “{phrase}”"
    store.close()


def test_a_biologic_renders_not_applicable_with_its_own_reason():
    """A BLA asset is absent by design, and that absence means something
    different again — the Purple Book, phase 4."""
    store = _store()
    store.upsert_applications([_approved("keytruda", "pembrolizumab",
                                         app_type="BLA", number="125514")])
    answer = store.protection_answer("pembrolizumab")
    assert answer.applicable is False
    text = " ".join(answer.render_lines())
    assert "biologics are licensed rather than approved" in text
    assert "Purple Book" in text
    for phrase in FORBIDDEN:
        assert phrase not in text.lower()
    store.close()


def test_no_orange_book_data_says_not_checked_rather_than_not_applicable():
    """Three states, not two: not checked, not applicable, and checked."""
    store = _store(with_orange_book=False)
    answer = store.protection_answer("lenalidomide")
    assert answer.searched is False
    text = " ".join(answer.render_lines())
    assert "NOT checked" in text and "not a finding about the asset" in text
    for phrase in FORBIDDEN:
        assert phrase not in text.lower()
    store.close()


def test_an_approved_asset_with_no_listed_patents_is_not_called_unprotected():
    """24% of NDAs carry listed patents at any time. For the rest it usually
    means the listed patents expired and were removed, not that none existed."""
    # An innovator entry carrying no listed patents.
    innovator = _by("N021880")
    innovator.patents, innovator.exclusivity = [], []
    answer = ProtectionAnswer(asset="x", searched=True, applicable=True,
                              entries=[innovator])
    text = " ".join(answer.render_lines())
    assert "already expired and been removed" in text
    assert "not that none ever existed" in text
    for phrase in FORBIDDEN:
        assert phrase not in text.lower()


# ------------------------------------------------- the claim it must not make


def test_the_section_says_protection_lapses_never_that_generics_enter():
    store = _store()
    store.upsert_applications([_approved("revlimid", "lenalidomide")])
    text = " ".join(store.protection_answer("lenalidomide").render_lines())
    assert "Earliest listed protection lapses" in text
    assert "NOT a date on which a competing product arrives" in text
    for phrase in FORBIDDEN_ENTRY:
        assert phrase not in text.lower(), f"claimed “{phrase}”, which the data cannot support"
    store.close()


def test_the_limits_are_in_the_section_not_only_in_claude_md():
    store = _store()
    store.upsert_applications([_approved("revlimid", "lenalidomide")])
    text = " ".join(store.protection_answer("lenalidomide").render_lines())
    for limit in LIMITS:
        assert limit in text, f"limit missing from the rendered section: {limit[:50]}"
    assert "not a patent estate" in text
    assert "not freedom-to-operate" in text


# ------------------------------------------------- the derivations asked for


def test_earliest_and_latest_listed_protection_are_derived():
    answer = ProtectionAnswer(asset="lenalidomide", searched=True,
                              entries=[_by("N021880")])
    assert answer.earliest_protection_lapse is not None
    assert answer.latest_protection_lapse >= answer.earliest_protection_lapse
    text = " ".join(answer.render_lines())
    assert human_date(answer.earliest_protection_lapse.strftime("%Y%m%d")) in text


def test_orphan_and_paediatric_exclusivity_are_identified():
    orphan = ProtectionAnswer(asset="a", searched=True, entries=[_by("N021880")])
    assert orphan.has_orphan_exclusivity is True
    paed = ProtectionAnswer(asset="b", searched=True, entries=[_by("N021986")])
    assert paed.has_paediatric_exclusivity is True
    assert classify_exclusivity("ODE-241").kind == "orphan"
    assert classify_exclusivity("PED").kind == "paediatric"
    assert classify_exclusivity("NCE") is None, "no meaning is asserted for unsourced codes"


def test_a_curated_classification_says_it_is_curated():
    """openFDA does not publish a machine-readable exclusivity legend, so orphan
    and paediatric are curated from the code prefix and labelled as such — never
    presented as FDA-documented."""
    text = " ".join(ProtectionAnswer(asset="a", searched=True,
                                     entries=[_by("N021880")]).render_lines())
    assert "CURATED in config/fda_exclusivity_codes.yaml" in text
    assert "not taken from an FDA-published machine-readable legend" in text


def test_an_unsourced_exclusivity_code_asserts_no_meaning():
    from medrag.fda.orangebook import ListedExclusivity

    answer = ProtectionAnswer(
        asset="a", searched=True,
        entries=[_by("A018659")])
    answer.entries[0].exclusivity = [ListedExclusivity("NCE", "20301231")]
    text = " ".join(answer.render_lines())
    assert "meaning not published in a machine-readable FDA source" in text


def test_whether_generics_already_exist_is_reported():
    store = _store()
    store.upsert_applications([_approved("allopurinol", "allopurinol")])
    answer = store.protection_answer("allopurinol")
    assert answer.generics_exist is True
    text = " ".join(answer.render_lines())
    assert "generic (ANDA) application(s) referencing this molecule are already listed" in text
    assert "not that a product is on the market" in text
    store.close()


def test_a_generic_only_match_is_not_reported_as_missing_protection():
    """An old molecule whose originator listing has lapsed while dozens of ANDAs
    remain. A generic lists no patents of its own, so their absence is the
    expected shape rather than a finding."""
    answer = ProtectionAnswer(asset="allopurinol", searched=True,
                              generic_entries=[_by("A018659")])
    assert answer.found is True
    text = " ".join(answer.render_lines())
    assert "a generic application does not list patents of its own" in text
    assert "not a statement about what any sponsor holds" in text
    for phrase in FORBIDDEN:
        assert phrase not in text.lower()


def test_no_generics_is_not_a_claim_that_none_can_exist():
    answer = ProtectionAnswer(asset="x", searched=True, entries=[_by("N021880")])
    text = " ".join(answer.render_lines())
    assert "not a statement that none exists" in text


# ------------------------------------------------- parsing and the bulk path


def test_the_bulk_path_is_reused_unchanged():
    """Phase 1 built this to be reused; this is the reuse."""
    load = load_export("drug/orangebook", fetch=lambda u: ORANGE_BOOK_BULK_ZIP,
                       catalogue=BULK_CATALOGUE)
    assert len(load.records) == len(ORANGE_BOOK_PAGE["results"])
    assert load.freshness.export_date == "2026-08-06"
    assert "downloading the whole file again" in " ".join(load.freshness.render_lines())


def test_patents_and_exclusivity_sit_at_record_level_and_attach_to_each_product():
    entry = _by("N021880")
    assert entry.patents and entry.exclusivity
    assert entry.application_type == "N" and not entry.is_generic


def test_a_delist_requested_patent_is_flagged_not_dropped():
    entry = _by("N021747")
    assert any(p.delist_requested for p in entry.patents)
    answer = ProtectionAnswer(asset="x", searched=True, entries=[entry])
    assert "delist-requested flag" in " ".join(answer.render_lines())


def test_a_record_approved_prior_to_1982_has_no_approval_date():
    entry = _by("A060004")
    assert entry.approved_prior_to_1982 is True and not entry.approval_date


def test_entries_round_trip_through_the_store():
    store = _store()
    got = {e.application_number for e in store.orange_book_entries("lenalidomide")}
    assert "N021880" in got
    store.close()


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
    print("\nall Orange Book tests passed" if not failures else f"\n{failures} test(s) failed")
    raise SystemExit(1 if failures else 0)
