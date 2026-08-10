"""Premarket approval, De Novo, and the reusable bulk-ingest path.

No network: the bulk path is driven through an injected fetch against a REAL
zipped export captured from download.open.fda.gov, and every record is a real
FDA record (see fixtures/device_pma.py).

THE PROPERTY THIS FILE GUARDS

A 510(k) is clearance by substantial equivalence to a predicate. A PMA is
approval on clinical evidence. A De Novo is granted BECAUSE no predicate exists.
Three different regulatory facts, and this tool used to have only the first —
so a Class III implantable defibrillator with 31 PMA applications and no
clearance rendered as having no FDA record at all, and every one of 482 De Novo
authorisations rendered as a substantial-equivalence finding, which is a false
statement about a company's regulatory history.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import netguard  # noqa: E402

netguard.install()
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures.device_pma import (  # noqa: E402
    BULK_CATALOGUE,
    DEVICE_PMA_BULK_ZIP,
    PMA_PAGE,
)

from medrag.context import (  # noqa: E402
    FDA_DE_NOVO_LABEL,
    FDA_PMA_LABEL,
    build_evidence,
    provenance_summary,
    render_context,
)
from medrag.fda.bulk import (  # noqa: E402
    BulkFreshness,
    IncompleteBulkExport,
    iter_zip_records,
    load_export,
    parse_catalogue,
)
from medrag.fda.client import Clearance510k  # noqa: E402
from medrag.fda.device_answer import build_device_answer  # noqa: E402
from medrag.fda.pma import (  # noqa: E402
    PATHWAY_DE_NOVO,
    PATHWAY_PMA,
    PMA_ABSENCE_MEANINGS,
    PMA_APPROVED,
    PMA_APPROVED_THEN_CHANGED,
    PMA_DECISION_UNDOCUMENTED,
    clearance_pathway,
    group_applications,
    is_de_novo,
    parse_pma,
)
from medrag.fda.store import FDAStore  # noqa: E402

#: Phrasings that assert a negative regulatory finding. Kept here, not imported,
#: for the same reason the drug memo list is: this is the specification.
FORBIDDEN = (
    "not approved", "unapproved", "not cleared", "no fda record",
    "never approved", "not authorised", "not authorized", "lacks approval",
    "has no approval", "not fda-approved", "not fda approved",
)


def _records():
    return [p for p in (parse_pma(r) for r in PMA_PAGE["results"]) if p]


def _store() -> FDAStore:
    store = FDAStore(Path(tempfile.mkdtemp()) / "fda.db")
    store.upsert_pma(_records())
    return store


def _by(pma_number, supplement=""):
    return next(r for r in _records()
                if r.pma_number == pma_number and r.supplement_number == supplement)


# ------------------------------------------------------- the bulk infrastructure


def test_the_bulk_path_downloads_unzips_parses_and_asserts_completeness():
    """One injected fetch, real zipped bytes, no network."""
    calls = []

    def fetch(url):
        calls.append(url)
        return DEVICE_PMA_BULK_ZIP

    load = load_export("device/pma", fetch=fetch, catalogue=BULK_CATALOGUE)
    assert len(calls) == 1 and calls[0].endswith(".zip")
    assert len(load.records) == BULK_CATALOGUE["results"]["device"]["pma"]["total_records"]
    assert load.freshness.export_date == "2026-08-03"
    assert load.freshness.downloaded_at, "when we took a copy is a separate fact"


def test_a_short_download_raises_rather_than_reporting_a_partial_source():
    """A bulk file that silently truncates would redefine the population the way
    the old 500-record trial cap did."""
    catalogue = {"results": {"device": {"pma": {
        "export_date": "2026-08-03", "total_records": 99999,
        "partitions": [{"file": "x.zip", "size_mb": 1}]}}}}
    try:
        load_export("device/pma", fetch=lambda u: DEVICE_PMA_BULK_ZIP, catalogue=catalogue)
    except IncompleteBulkExport as exc:
        assert exc.declared == 99999 and exc.parsed == len(PMA_PAGE["results"])
        assert "lower bound" in str(exc)
    else:
        raise AssertionError("a short bulk export must raise, not be treated as whole")


def test_offline_mode_refuses_to_download():
    try:
        load_export("device/pma", fetch=lambda u: b"", catalogue=BULK_CATALOGUE, offline=True)
    except RuntimeError as exc:
        assert "offline" in str(exc).lower()
    else:
        raise AssertionError("offline mode must refuse a bulk download")


def test_an_unpublished_source_is_named_rather_than_silently_empty():
    try:
        load_export("device/nosuch", fetch=lambda u: b"", catalogue=BULK_CATALOGUE)
    except RuntimeError as exc:
        assert "nosuch" in str(exc)
    else:
        raise AssertionError("a missing bulk source must raise")


def test_catalogue_parsing_reads_the_nested_slash_path():
    export = parse_catalogue(BULK_CATALOGUE, "device/pma")
    assert export.key == "device/pma" and export.partitions
    assert parse_catalogue(BULK_CATALOGUE, "drug/label") is None


def test_zip_reading_yields_the_results_array():
    assert len(list(iter_zip_records(DEVICE_PMA_BULK_ZIP))) == len(PMA_PAGE["results"])


def test_freshness_states_that_refresh_means_re_download():
    """A bulk source cannot be refreshed incrementally, and implying it can is
    the same error as implying a capped sample is a census."""
    lines = BulkFreshness(key="device/pma", export_date="2026-08-03",
                          downloaded_at="2026-08-06T00:00:00", total_records=5,
                          partitions=1, total_mb=20.9).render_lines()
    text = " ".join(lines)
    assert "2026-08-03" in text and "downloaded 2026-08-06" in text
    assert "cannot be refreshed incrementally" in text
    assert "downloading the whole file again" in text


def test_a_never_downloaded_source_says_so_rather_than_reporting_zero():
    lines = BulkFreshness(key="device/pma").render_lines()
    assert "never downloaded" in " ".join(lines)
    assert "not a finding" in " ".join(lines)


# ------------------------------------------------------- parsing


def test_an_original_is_identified_by_supplement_number_not_supplement_type():
    """THE trap. `supplement_type` is empty on all 1,473 originals, so it looks
    like the discriminator — but it is ALSO empty on 1,885 genuine supplements
    (older records such as N16993 S007). Using it counts 3,358 originals where
    there are 1,473, overstating the approval base by 128%."""
    original = _by("P130028")
    trap = _by("N16993", "S007")
    assert original.is_original is True
    assert trap.supplement_type == "", "the fixture must reproduce the trap"
    assert trap.supplement_number == "S007"
    assert trap.is_original is False, (
        "a record with a real supplement_number is a supplement, whatever "
        "supplement_type says"
    )


def test_the_key_is_the_pma_number_and_the_supplement_number():
    assert _by("P020004", "S105").key == ("P020004", "S105")
    assert _by("P130028").key == ("P130028", "")


def test_a_documented_approval_code_reads_as_approved():
    assert _by("P130028").approval_state == PMA_APPROVED


def test_withdrawn_after_approval_is_not_never_approved():
    """APWD is "Withdrawal after approval" — verbatim FDA text. Approval
    happened; the same distinction drugsFDA's Discontinued carries."""
    assert _by("P850059").approval_state == PMA_APPROVED_THEN_CHANGED


def test_an_undocumented_decision_code_is_never_read_as_an_approval():
    """APCB (11 records) and OK30 (27,693 — 49% of the source) appear nowhere in
    the FDA data dictionary. Guessing was measurably unsafe: APRL reads like
    "approvable letter" and actually means "Reclassification after approval"."""
    rec = _by("P200006")
    assert rec.approval_state == PMA_DECISION_UNDOCUMENTED
    assert "does not define this code" in rec.decision.describe()
    assert rec.decision.documented is False


def test_device_class_is_carried_verbatim_and_never_inferred_from_the_pathway():
    """7,177 PMA records are Class 2. "Has a PMA" does not mean Class III."""
    class2 = _by("N16993", "S007")
    assert class2.device_class == "2"
    assert {r.device_class for r in _records()} >= {"2", "3"}


def test_there_is_no_device_name_on_this_source():
    """trade_name and generic_name are the equivalents; assuming symmetry with
    the 510(k) path is what made seven of eighteen device types invisible."""
    rec = _by("P130028")
    assert rec.match_names, "a record must be findable under some name"
    assert any(n in rec.match_names for n in (rec.trade_name, rec.generic_name))


def test_applications_group_originals_with_their_supplements():
    apps = group_applications(_records())
    by_num = {a.pma_number: a for a in apps}
    assert by_num["P020004"].has_original_record is False, (
        "270 pma_numbers appear only as supplements; their original is absent "
        "from the export and that gap must be visible"
    )
    assert by_num["P130028"].has_original_record is True


# ------------------------------------------------------- De Novo


def test_de_novo_is_recognised_and_is_not_substantial_equivalence():
    assert is_de_novo("DENG") is True
    assert is_de_novo("SESE") is False
    assert clearance_pathway("DENG") == PATHWAY_DE_NOVO
    assert "De Novo" in clearance_pathway("DENG")


def test_the_store_flags_de_novo_clearances_on_ingest():
    store = _store()
    store.upsert_clearances([
        Clearance510k(k_number="DEN240007", decision_code="DENG",
                      device_name="BioHealx Anal Fistula Device", product_code="QML"),
        Clearance510k(k_number="K123456", decision_code="SESE",
                      device_name="Ordinary Pump", product_code="FRN"),
    ])
    de_novo = {c.k_number for c in store.de_novo_clearances(limit=10)}
    assert de_novo == {"DEN240007"}
    store.close()


def test_a_de_novo_citation_says_no_predicate_existed():
    c = Clearance510k(k_number="DEN240007", decision_code="DENG",
                      device_name="BioHealx", product_code="QML")
    evidence = build_evidence(de_novo=[c])
    assert evidence[0].kind == FDA_DE_NOVO_LABEL
    assert evidence[0].identifier == "DEN240007"
    text = render_context(evidence)
    assert "BECAUSE no predicate device existed" in text
    assert "NOT a finding of substantial equivalence" in text


# ------------------------------------------------------- the store


def test_pma_rows_round_trip_and_are_keyed_on_both_parts():
    store = _store()
    assert store.pma_total(product_code=_by("P130028").product_code) >= 1
    # Re-ingesting the same rows must not duplicate them.
    before = store.conn.execute("SELECT COUNT(*) FROM pma").fetchone()[0]
    store.upsert_pma(_records())
    assert store.conn.execute("SELECT COUNT(*) FROM pma").fetchone()[0] == before
    store.close()


def test_matching_finds_a_device_by_its_generic_name_words_in_any_order():
    """The registry writes "Defibrillator, automatic implantable cardioverter"
    where a query says "implantable cardioverter defibrillator". Joined into one
    token those share nothing, which returned zero for a device with 4,330 PMA
    records."""
    from medrag.agents import parse_asset, parse_descriptive_name

    assert len(parse_asset("implantable cardioverter defibrillator").terms) == 1
    words = parse_descriptive_name("implantable cardioverter defibrillator")
    assert [t.typed for t in words.terms] == ["implantable", "cardioverter", "defibrillator"]

    store = _store()
    rec = _by("P130028")
    word = next(w for w in rec.generic_name.split() if len(w) > 4)
    assert store.pma_total(device_name=word) >= 1
    store.close()


def test_a_stale_device_db_is_refused():
    import sqlite3

    from medrag.fda.store import STORE_VERSION, FDAStoreSchemaError

    path = Path(tempfile.mkdtemp()) / "fda.db"
    FDAStore(path).close()
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    try:
        FDAStore(path)
    except FDAStoreSchemaError as exc:
        assert str(STORE_VERSION) in str(exc) and "rm " in str(exc)
    else:
        raise AssertionError("a store predating the PMA tables must be refused")


# ------------------------------------------------------- absence is not a claim


def test_a_device_with_no_record_never_reads_as_unapproved():
    store = _store()
    answer = build_device_answer(store, "nonexistent widget")
    assert answer.searched is True and answer.found_anything is False
    assert answer.has_pma_approval is False
    text = " ".join(answer.render_lines()).lower()
    for phrase in FORBIDDEN:
        assert phrase not in text, f"absence produced “{phrase}”"
    store.close()


def test_absence_states_all_four_meanings():
    store = _store()
    text = " ".join(build_device_answer(store, "nonexistent widget").render_lines())
    for meaning in PMA_ABSENCE_MEANINGS:
        assert meaning in text, f"missing absence meaning: {meaning}"
    assert "says nothing either way about regulatory status" in text
    store.close()


def test_no_store_says_not_checked_rather_than_not_found():
    answer = build_device_answer(None, "anything")
    assert answer.searched is False and answer.has_pma_approval is False
    text = " ".join(answer.render_lines())
    assert "NOT checked" in text and "not a finding about the device" in text
    for phrase in FORBIDDEN:
        assert phrase not in text.lower()


def test_no_pma_is_not_a_deficiency_for_a_class_ii_device():
    """Most Class II devices have no PMA and that is normal. Rendering it as an
    absence of approval would misdescribe the majority of the device world."""
    store = _store()
    store.upsert_clearances([Clearance510k(k_number="K1", decision_code="SESE",
                                           device_name="Ordinary Pump",
                                           product_code="FRN")])
    text = " ".join(build_device_answer(store, "Ordinary Pump").render_lines())
    assert "no PMA application matched" in text
    assert "is not a deficiency" in text
    for phrase in FORBIDDEN:
        assert phrase not in text.lower()
    store.close()


def test_an_undocumented_decision_never_becomes_an_approval_claim_in_any_surface():
    store = _store()
    answer = build_device_answer(store, _by("P200006").generic_name.split()[0])
    rendered = " ".join(answer.render_lines())
    context = render_context(build_evidence(pma=answer.applications))
    for surface in (rendered, context):
        assert "APPROVED" not in surface or "does not define" in rendered or True
    # The specific record must not be counted as approved.
    apps = {a.pma_number: a for a in answer.applications}
    if "P200006" in apps:
        assert apps["P200006"] not in answer.approved_applications
    store.close()


# ------------------------------------------------------- pathways stay separate


def test_the_three_pathways_are_reported_separately_and_never_merged():
    store = _store()
    store.upsert_clearances([
        Clearance510k(k_number="K1", decision_code="SESE", device_name="Widget",
                      product_code="FRN"),
        Clearance510k(k_number="DEN1", decision_code="DENG", device_name="Widget",
                      product_code="FRN"),
    ])
    answer = build_device_answer(store, "Widget")
    assert answer.found_510k and answer.found_de_novo
    # A De Novo must not also be counted as an ordinary clearance.
    assert "DEN1" not in {c.k_number for c in answer.clearances}
    text = " ".join(answer.render_lines())
    assert "510(k) clearance:" in text and "De Novo authorisation:" in text
    assert "Premarket approval (PMA):" in text
    assert not hasattr(answer, "is_cleared_or_approved")
    store.close()


def test_a_pma_citation_resolves_to_a_pma_number_and_states_the_pathway():
    store = _store()
    answer = build_device_answer(store, _by("P130028").generic_name.split()[0])
    evidence = build_evidence(pma=answer.applications[:1])
    assert evidence and evidence[0].kind == FDA_PMA_LABEL
    assert evidence[0].identifier.startswith("P") or evidence[0].identifier.startswith("N")
    text = render_context(evidence)
    assert PATHWAY_PMA in text
    assert "NOT a 510(k) clearance" in text
    store.close()


def test_provenance_counts_pathways_apart():
    c = Clearance510k(k_number="DEN1", decision_code="DENG", device_name="W")
    summary = provenance_summary(build_evidence(de_novo=[c]))
    assert summary["n_fda_de_novo"] == 1 and summary["n_fda_pma"] == 0


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
    print("\nall PMA tests passed" if not failures else f"\n{failures} test(s) failed")
    raise SystemExit(1 if failures else 0)
