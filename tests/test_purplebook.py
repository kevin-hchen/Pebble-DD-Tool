"""Purple Book: biosimilars, interchangeability, and three variants of absence.

THE PROPERTIES THIS FILE GUARDS

1. A small-molecule NDA asset is absent from the Purple Book because it is the
   WRONG BOOK. The section says which book was consulted and why — the mirror of
   what phase 3 does for a BLA asset.
2. An investigational biologic is absent by construction, same as botensilimab
   in the Orange Book.
3. THE ONE SPECIFIC TO THIS SOURCE: a licensed biologic with no biosimilars
   listed means no biosimilar has been LICENSED. Biosimilar programmes are
   invisible until licensure, so rendering that as "no biosimilar competition"
   would be a false statement about the competitive position of exactly the
   assets a healthcare investor cares most about.

And: biosimilar entry is not generic entry, and interchangeability is a separate
regulatory finding from biosimilarity.

No network — real rows from the published monthly CSV.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import netguard  # noqa: E402

netguard.install()
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures.purplebook import (  # noqa: E402
    BOT_DETECTION_BODY,
    PURPLE_BOOK_CSV_BYTES,
)

from medrag.fda.bulk import (  # noqa: E402
    BlockedByBotDetection,
    check_not_blocked,
    load_delimited,
)
from medrag.fda.drug_store import DrugStore  # noqa: E402
from medrag.fda.drugs import DrugApplication, DrugProduct  # noqa: E402
from medrag.fda.purplebook import (  # noqa: E402
    FULL_DATABASE_SECTION,
    HEADER_MARKER,
    LIMITS,
    BiologicProtectionAnswer,
    PurpleBookLayoutError,
    check_layout,
    parse_row,
)

#: Phrases that would turn "not licensed" or "not applicable" into a claim that
#: the sponsor has nothing, or that nothing is coming.
FORBIDDEN = (
    "no protection", "unprotected", "no exclusivity", "no intellectual property",
    "no competition", "no biosimilar competition", "no competitors",
    "faces no competition", "uncontested",
)
#: Claims the data cannot support.
FORBIDDEN_ENTRY = ("biosimilars enter", "biosimilar entry on", "generics enter",
                   "loses exclusivity on", "competition arrives")


def _load():
    return load_delimited("purplebook", "u", fetch=lambda _u: PURPLE_BOOK_CSV_BYTES,
                          export_label="June 2026", header_marker=HEADER_MARKER,
                          section=FULL_DATABASE_SECTION)


def _products():
    return [p for p in (parse_row(r) for r in _load().rows) if p]


def _store(with_purple_book=True) -> DrugStore:
    store = DrugStore(Path(tempfile.mkdtemp()) / "drugs.db")
    if with_purple_book:
        store.upsert_purple_book(_products())
    return store


def _approved(number, app_type, ingredient):
    return DrugApplication(
        application_number=f"{app_type}{number}", sponsor_name="X",
        approval_status="APPROVED", approval_date="20160101",
        products=[DrugProduct(product_number="001", brand_name=ingredient.upper(),
                              active_ingredients=[ingredient.upper()],
                              marketing_status="Prescription")])


# ------------------------------------------------- the two-section file


def test_the_full_database_section_is_selected_not_the_changes_report():
    """Each monthly file opens with a CHANGES report under an identical header.
    Taking the first section would reduce the Purple Book to one month."""
    load = _load()
    assert load.section_note == "section 2 of 2 in the published file"
    # The decoy row in section 1 must not appear.
    assert not any(p.applicant == "x" for p in _products())
    assert len(_products()) >= 6


def test_the_delimited_loader_states_that_completeness_is_not_asserted():
    """A catalogued JSON export declares total_records and can be checked. This
    CSV declares nothing, and the coverage line must not imply parity."""
    lines = " ".join(_load().freshness.render_lines())
    assert "declares no record count" in lines
    assert "has NOT been checked against a total" in lines


def test_a_bot_detection_page_is_not_read_as_a_missing_file():
    """purplebooksearch.fda.gov served HTTP 404 with this body three times in a
    row — Akamai bot detection, not a missing resource. Reading the status alone
    would have recorded the Purple Book as unavailable."""
    try:
        check_not_blocked(BOT_DETECTION_BODY, "https://purplebooksearch.fda.gov/x")
    except BlockedByBotDetection as exc:
        assert "NOT evidence that the source is unavailable" in str(exc)
        assert "browser User-Agent" in str(exc)
    else:
        raise AssertionError("a bot-detection page must raise, not parse as empty")


def test_a_renamed_column_fails_loudly_rather_than_emptying_a_field():
    """A CSV has no schema version. A renamed License Type column would turn
    every biosimilar into an originator, silently."""
    try:
        check_layout(["Applicant", "BLA Number"])
    except PurpleBookLayoutError as exc:
        assert "License Type" in str(exc)
        assert "silently read empty" in str(exc)
    else:
        raise AssertionError("a changed layout must be refused")


def test_a_legacy_five_digit_bla_is_parsed():
    """5-digit BLAs exist (legacy NDAs transitioned to BLAs) and broke the first
    version of the recon aligner, which assumed 6 digits."""
    assert any(len(p.bla_number) == 5 for p in _products())


# ------------------------------------------------- ABSENCE VARIANT 1: wrong book


def test_a_small_molecule_is_routed_to_the_orange_book_and_told_so():
    store = _store()
    store.upsert_applications([_approved("021880", "NDA", "lenalidomide")])
    answer = store.biologic_protection_answer("lenalidomide")
    assert answer.applicable is False
    text = " ".join(answer.render_lines())
    assert "does not apply" in text
    assert "small molecules are approved rather than licensed" in text
    assert "Orange Book was consulted instead" in text
    assert answer.consulted_book == "Orange Book"
    for phrase in FORBIDDEN:
        assert phrase not in text.lower()
    store.close()


# ------------------------------------------------- ABSENCE VARIANT 2: no licence


def test_an_investigational_biologic_is_absent_by_construction():
    store = _store()
    answer = store.biologic_protection_answer("botensilimab")
    assert answer.applicable is False
    text = " ".join(answer.render_lines())
    assert "absent from the Purple Book by construction" in text
    assert "says nothing about what the sponsor owns or what it has in development" in text
    for phrase in FORBIDDEN:
        assert phrase not in text.lower()
    store.close()


def test_no_purple_book_data_says_not_checked():
    store = _store(with_purple_book=False)
    answer = store.biologic_protection_answer("adalimumab")
    assert answer.searched is False
    text = " ".join(answer.render_lines())
    assert "NOT checked" in text and "not a finding about the asset" in text
    store.close()


# --------------------------------- ABSENCE VARIANT 3: licensed, no biosimilars


def test_a_licensed_biologic_with_no_biosimilars_says_none_is_LICENSED():
    """The variant specific to this source, and the one most likely to mislead:
    biosimilar programmes are invisible until licensure."""
    store = _store()
    store.upsert_applications([_approved("125514", "BLA", "pembrolizumab")])
    answer = store.biologic_protection_answer("pembrolizumab")
    assert answer.found is True
    assert answer.has_licensed_biosimilar is False
    text = " ".join(answer.render_lines())
    assert "No biosimilar to this reference product has been LICENSED" in text
    assert "not publicly registered and are invisible here until the day they are licensed" in text
    assert "not a statement that none is in development" in text
    assert "not a measure of how contested the molecule is" in text
    for phrase in FORBIDDEN:
        assert phrase not in text.lower(), f"rendered absence as “{phrase}”"
    store.close()


def test_no_biosimilars_never_renders_as_no_competition():
    store = _store()
    store.upsert_applications([_approved("125514", "BLA", "pembrolizumab")])
    text = " ".join(store.biologic_protection_answer("pembrolizumab").render_lines()).lower()
    for phrase in ("no competition", "no competitors", "uncontested", "no rivals",
                   "sole product", "monopoly"):
        assert phrase not in text
    store.close()


# ------------------------------- biosimilar is not generic; interchangeability


def test_biosimilar_entry_is_not_rendered_in_the_same_shape_as_generic_entry():
    store = _store()
    store.upsert_applications([_approved("125057", "BLA", "adalimumab")])
    text = " ".join(store.biologic_protection_answer("adalimumab").render_lines())
    assert "A biosimilar is not a generic" in text
    assert "own clinical programme" in text
    assert "NOT automatically substitutable" in text
    assert "wider here than for a small molecule" in text
    for phrase in FORBIDDEN_ENTRY:
        assert phrase not in text.lower(), f"claimed “{phrase}”"
    store.close()


def test_interchangeability_is_recorded_separately_from_biosimilarity():
    """Two different FDA findings. A biosimilar is not interchangeable unless
    separately designated."""
    products = _products()
    inter = next(p for p in products if p.is_interchangeable)
    plain = next(p for p in products if p.is_biosimilar and not p.is_interchangeable)
    assert inter.is_biosimilar and inter.is_interchangeable
    assert plain.is_biosimilar and not plain.is_interchangeable

    store = _store()
    store.upsert_applications([_approved("125057", "BLA", "adalimumab")])
    answer = store.biologic_protection_answer("adalimumab")
    assert answer.biosimilars and answer.interchangeables
    assert len(answer.interchangeables) < len(answer.biosimilars)
    text = " ".join(answer.render_lines())
    assert "separate finding from biosimilarity" in text
    assert "substituted at the pharmacy without prescriber intervention" in text
    store.close()


def test_a_biosimilar_with_no_interchangeability_designation_says_so():
    answer = BiologicProtectionAnswer(
        asset="x", searched=True,
        products=[p for p in _products() if p.is_originator][:1],
        biosimilars=[p for p in _products()
                     if p.is_biosimilar and not p.is_interchangeable][:1])
    text = " ".join(answer.render_lines())
    assert "none of the licensed biosimilars carries an FDA interchangeability" in text
    assert "a prescriber has to specify them" in text


def test_the_limits_are_in_the_section():
    store = _store()
    store.upsert_applications([_approved("125057", "BLA", "adalimumab")])
    text = " ".join(store.biologic_protection_answer("adalimumab").render_lines())
    for limit in LIMITS:
        assert limit in text
    store.close()


# ------------------------------------------------- exclusivity sparsity


def test_an_empty_exclusivity_field_is_reported_as_coverage_not_a_finding():
    """Measured: the headline exclusivity column is empty on every row of the
    published file, and reference-product exclusivity on 98.4% of rows."""
    answer = BiologicProtectionAnswer(
        asset="x", searched=True,
        products=[p for p in _products()
                  if not p.orphan_exclusivity_date and not p.ref_product_exclusivity_date][:1],
        exclusivity_fill_note="Exclusivity coverage in the Purple Book is sparse.")
    text = " ".join(answer.render_lines())
    assert "the FDA has not published a date in these fields" in text
    assert "sparse" in text
    for phrase in FORBIDDEN:
        assert phrase not in text.lower()


def test_a_listed_exclusivity_date_is_never_a_competition_date():
    store = _store()
    store.upsert_applications([_approved("125514", "BLA", "pembrolizumab")])
    text = " ".join(store.biologic_protection_answer("pembrolizumab").render_lines())
    if "Listed exclusivity:" in text:
        assert "NOT a date on which a biosimilar arrives" in text
        assert "nothing here says whether one is being developed" in text
    store.close()


def test_products_round_trip_and_interchangeability_survives_the_store():
    store = _store()
    got = store.purple_book_products("adalimumab")
    assert got
    assert any(p.is_interchangeable for p in got)
    store.close()


def test_the_reverse_reference_lookup_finds_biosimilars_of_an_asset():
    """`ref_tokens` exists so "what references this molecule" is a query."""
    store = _store()
    refs = store.purple_book_products("adalimumab", column="ref_tokens")
    assert refs and all(p.is_biosimilar for p in refs)
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
    print("\nall Purple Book tests passed" if not failures else f"\n{failures} test(s) failed")
    raise SystemExit(1 if failures else 0)
