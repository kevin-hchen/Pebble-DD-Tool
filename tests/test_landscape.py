"""Tests for the patient trial-landscape: eligibility/contact ingestion, the
trials.db schema bump, biomarker matching (including the indirect MSS/UNCLEAR
path), landscape assembly, and rendering.

No network: everything runs off the LANDSCAPE_PAGE fixture.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Direct runs do not load conftest.py, so the no-network guard is installed
# here too. See tests/netguard.py.
from tests import netguard  # noqa: E402

netguard.install()
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures.ctgov import LANDSCAPE_PAGE  # noqa: E402

from medrag.biomarker import (  # noqa: E402
    ELIGIBLE,
    EXCLUDED,
    NOT_MENTIONED,
    UNCLEAR,
    match_biomarker,
)
from medrag.landscape import build_landscape  # noqa: E402
from medrag.landscape_memo import export, render_markdown, render_pdf  # noqa: E402
from medrag.trials.client import parse_study  # noqa: E402
from medrag.trials.store import STORE_VERSION, TrialStore, TrialStoreSchemaError  # noqa: E402


def _records():
    return [parse_study(s) for s in LANDSCAPE_PAGE["studies"] if parse_study(s)]


def _store() -> TrialStore:
    store = TrialStore(Path(tempfile.mkdtemp()) / "trials.db")
    # Stamped with the query set the way a real ingest does: the landscape
    # selects the population the fetch defined, not a condition substring.
    recs = _records()
    store.upsert(recs, provenance={r.nct_id: ["cond:colorectal cancer"] for r in recs},
                 set_key="colorectal")
    return store


def _by_id(store, nct):
    return store.get(nct)


# ------------------------------------------------------------- biomarker matching


def test_direct_mss_variants_are_eligible():
    for text in (
        "Inclusion Criteria:\n* Microsatellite stable (MSS) tumors",
        "Inclusion Criteria:\n* Proficient mismatch repair (pMMR) by IHC",
        "Inclusion Criteria:\n* Documented non-MSI-H disease",
    ):
        assert match_biomarker(text, "MSS").status == ELIGIBLE


def test_non_msi_h_is_not_misread_as_msi_h():
    """'non-MSI-H' contains 'MSI-H' — it must resolve to ELIGIBLE, not EXCLUDED."""
    m = match_biomarker("Inclusion Criteria:\n* Tumors must be non-MSI-H", "MSS")
    assert m.status == ELIGIBLE


def test_requires_opposite_biomarker_is_excluded():
    m = match_biomarker(
        "Inclusion Criteria:\n* Tumors must be MSI-H (microsatellite instability-high)", "MSS")
    assert m.status == EXCLUDED


def test_indirect_mss_by_excluding_msi_h_is_unclear():
    """The spec case: MSS expressed only by excluding MSI-H. Must be UNCLEAR — not
    asserted eligible, and never silently dropped — with the sentence shown."""
    m = match_biomarker(
        "Inclusion Criteria:\n* Colorectal cancer\n\nExclusion Criteria:\n* Known MSI-H or dMMR",
        "MSS")
    assert m.status == UNCLEAR
    assert "MSI-H" in m.evidence


def test_biomarker_not_mentioned():
    m = match_biomarker("Inclusion Criteria:\n* Age 18+\n* ECOG 0-1", "MSS")
    assert m.status == NOT_MENTIONED and m.evidence == ""


def test_synonyms_resolve_to_the_same_biomarker():
    text = "Inclusion Criteria:\n* Microsatellite stable disease"
    assert match_biomarker(text, "microsatellite stable").status == ELIGIBLE
    assert match_biomarker(text, "pMMR").status == ELIGIBLE


def test_match_always_carries_the_deciding_sentence():
    m = match_biomarker(
        "Inclusion Criteria:\n* Microsatellite stable (MSS) or pMMR tumors", "MSS")
    assert "Microsatellite stable" in m.evidence


# ------------------------------------------------------------- ingestion of new fields


def test_eligibility_and_contacts_are_parsed():
    rec = parse_study(LANDSCAPE_PAGE["studies"][0])
    assert "Microsatellite stable" in rec.eligibility_criteria
    assert rec.minimum_age == "18 Years" and rec.sex == "ALL"
    assert rec.healthy_volunteers is False
    assert rec.principal_investigator["name"] == "Jane A. Smith, MD"
    assert rec.primary_contact["email"] == "crc-trials@dfci.example"
    assert {loc["city"] for loc in rec.locations} == {"Boston", "Houston"}


def test_new_fields_survive_the_store_round_trip():
    store = _store()
    rec = _by_id(store, "NCT10000001")
    assert "Microsatellite stable" in rec.eligibility_criteria
    assert len(rec.locations) == 2 and rec.locations[0]["state"] == "Massachusetts"
    assert rec.overall_officials[0]["role"] == "PRINCIPAL_INVESTIGATOR"
    store.close()


def test_per_site_contacts_are_parsed():
    """The live API nests a contacts[] array inside each location on recruiting
    trials; those site coordinators must survive the parse and round-trip."""
    rec = parse_study(LANDSCAPE_PAGE["studies"][0])
    boston = next(loc for loc in rec.locations if loc["city"] == "Boston")
    site = next(c for c in boston["contacts"] if c["role"] == "CONTACT")
    assert site["phone"] == "617-555-0142" and site["email"] == "boston-crc@dfci.example"

    store = _store()
    stored = _by_id(store, "NCT10000001")
    assert stored.locations[0]["contacts"][0]["name"] == "Site Public Contact"
    store.close()


def test_landscape_prefers_the_nearest_site_contact():
    """A patient should get the coordinator at their nearest site, not the
    overall study contact, when the site lists one."""
    ls = build_landscape(_store(), condition="colorectal cancer", biomarker="MSS",
                         location="Houston")
    trial_a = next(t for t in ls.trials if t.record.nct_id == "NCT10000001")
    # Nearest site is Houston; its coordinator wins over the central contact.
    assert trial_a.contact["phone"] == "713-555-0177"
    assert trial_a.contact is not trial_a.record.primary_contact


def test_contact_falls_back_to_central_when_no_site_contact():
    ls = build_landscape(_store(), condition="colorectal cancer", biomarker="MSS")
    # Trial B lists no per-site contacts, so the central contact is used.
    trial_b = next(t for t in ls.trials if t.record.nct_id == "NCT10000002")
    assert trial_b.contact == trial_b.record.primary_contact


def test_healthy_volunteers_none_stays_distinct_from_false():
    store = _store()
    # Study C files no healthyVolunteers key; A files False. NULL must not read
    # as False.
    assert _by_id(store, "NCT10000003").healthy_volunteers is None
    assert _by_id(store, "NCT10000001").healthy_volunteers is False
    store.close()


# ------------------------------------------------------------- schema refusal


def test_stale_trials_db_is_refused_with_a_rebuild_instruction():
    path = Path(tempfile.mkdtemp()) / "trials.db"
    TrialStore(path).close()  # fresh v2 database
    # Simulate a database written before the eligibility columns existed.
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    try:
        TrialStore(path)
    except TrialStoreSchemaError as exc:
        assert "rebuild" in str(exc).lower() or "re-ingest" in str(exc).lower()
        assert "rm " in str(exc)
    else:
        raise AssertionError("a stale trials.db must be refused, not silently read")


def test_current_database_opens_without_complaint():
    store = _store()
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == STORE_VERSION
    store.close()


# ------------------------------------------------------------- landscape assembly


def test_landscape_buckets_every_outcome():
    ls = build_landscape(_store(), condition="colorectal cancer", biomarker="MSS")
    assert ls.n_condition == 6
    assert ls.n_eligible == 2          # A (recruiting) + E (closed)
    assert ls.n_unclear == 1           # B (indirect)
    assert ls.n_excluded == 1          # C (requires MSI-H)
    assert ls.n_not_mentioned == 2     # D (no mention) + F (no eligibility text)
    assert ls.n_no_eligibility_text == 1  # F


def test_unclear_trial_is_shown_not_dropped():
    ls = build_landscape(_store(), condition="colorectal cancer", biomarker="MSS")
    shown = {t.record.nct_id for t in ls.trials}
    assert "NCT10000002" in shown, "the indirect-MSS trial must be shown, flagged UNCLEAR"
    assert "NCT10000003" not in shown, "the MSI-H-required trial is not a candidate"
    unclear = next(t for t in ls.trials if t.record.nct_id == "NCT10000002")
    assert unclear.match.status == UNCLEAR and unclear.match.evidence


def test_every_shown_trial_carries_its_eligibility_evidence():
    ls = build_landscape(_store(), condition="colorectal cancer", biomarker="MSS")
    assert ls.trials and all(t.match.evidence for t in ls.trials)


def test_eligible_open_trials_sort_first():
    ls = build_landscape(_store(), condition="colorectal cancer", biomarker="MSS")
    order = [t.record.nct_id for t in ls.trials]
    # Eligible-and-recruiting, then eligible-but-closed, then unclear.
    assert order == ["NCT10000001", "NCT10000005", "NCT10000002"]


def test_location_floats_the_matching_site_to_the_top():
    ls = build_landscape(_store(), condition="colorectal cancer", biomarker="MSS",
                         location="Houston")
    trial_a = next(t for t in ls.trials if t.record.nct_id == "NCT10000001")
    assert trial_a.proximity_tier == 3
    assert trial_a.nearest_location["city"] == "Houston"


def test_no_store_returns_empty_landscape_with_warning():
    ls = build_landscape(None, condition="x", biomarker="MSS")
    assert ls.trials == [] and ls.warnings


# ------------------------------------------------------------- rendering


def test_markdown_lists_trials_with_evidence_and_disclaimer():
    ls = build_landscape(_store(), condition="colorectal cancer", biomarker="MSS")
    md = render_markdown(ls)
    assert "# Trial landscape — colorectal cancer" in md
    assert "NCT10000001" in md
    assert "| NCT ID | Title | Phase | Status | Intervention | Sponsor | Locations | PI | Contact | Eligibility match |" in md
    assert "UNCLEAR" in md and "MSI-H" in md
    assert "not medical advice" in md


def test_pdf_export_produces_a_real_pdf():
    ls = build_landscape(_store(), condition="colorectal cancer", biomarker="MSS")
    out = Path(tempfile.mkdtemp()) / "landscape.pdf"
    render_pdf(ls, out)
    assert out.exists() and out.read_bytes().startswith(b"%PDF")
    assert out.stat().st_size > 1500


def test_export_writes_both_formats():
    ls = build_landscape(_store(), condition="colorectal cancer", biomarker="MSS")
    paths = export(ls, Path(tempfile.mkdtemp()))
    assert paths["markdown"].exists() and paths["pdf"].exists()
    assert paths["markdown"].stem == "colorectal-cancer-mss-landscape"


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
    print("\nall landscape tests passed" if not failures else f"\n{failures} test(s) failed")
    raise SystemExit(1 if failures else 0)
