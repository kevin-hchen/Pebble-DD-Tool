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

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Direct runs do not load conftest.py, so the no-network guard is installed
# here too. See tests/netguard.py.
from tests import netguard  # noqa: E402

netguard.install()
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures.ctgov import LANDSCAPE_PAGE  # noqa: E402

from medrag.biomarker import (  # noqa: E402
    ELIGIBLE,
    ELIGIBLE_BY_EXCLUSION,
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


def test_indirect_mss_by_excluding_msi_h_is_eligible_by_exclusion():
    """MSS expressed only by excluding MSI-H — STELLAR-303 and HARMONi-GI3's
    real pattern. This is ELIGIBLE BY EXCLUSION, not UNCLEAR: excluding the
    opposite is a confident, if indirect, statement of eligibility, and folding
    it into UNCLEAR is exactly how those two Phase 3 trials used to vanish from
    an MSS patient's search. Never dropped either way."""
    m = match_biomarker(
        "Inclusion Criteria:\n* Colorectal cancer\n\nExclusion Criteria:\n* Known MSI-H or dMMR",
        "MSS")
    assert m.status == ELIGIBLE_BY_EXCLUSION
    assert "MSI-H" in m.evidence
    assert m.is_candidate


def test_negation_at_a_distance_is_recognised():
    """STELLAR-303's real inclusion line: the negation ('NOT') and the marker
    ('MSI-high') are two words apart ('NOT to have'), not adjacent. A matcher
    anchored immediately before the marker misses this and inverts the trial —
    reading a trial that excludes MSI-H as one that requires it."""
    m = match_biomarker(
        "Inclusion Criteria:\n* Documented NOT to have microsatellite "
        "instability-high (MSI-high) or mismatch repair deficient (dMMR) CRC "
        "by tissue-based analysis.",
        "MSS")
    assert m.status == ELIGIBLE_BY_EXCLUSION, (
        f"got {m.status} — a distant negation must still be recognised, not read "
        "as the trial requiring MSI-H"
    )


def test_testing_requirement_carries_no_signal():
    """'must have been assessed for X status' mandates a TEST, not a RESULT —
    C-800-25's real inclusion line. It must not be read as requiring the
    marker (nor excluding it): the real exclusion sentence two lines later is
    what should decide the trial."""
    text = (
        "Inclusion Criteria:\n"
        "* The tumor must have been assessed for microsatellite instability "
        "high (MSI-H) or deficient mismatch repair (dMMR) status per a "
        "standard local testing method.\n\n"
        "Exclusion Criteria:\n"
        "* Tumor is MSI-H/dMMR per a standard local testing method.\n"
    )
    m = match_biomarker(text, "MSS")
    assert m.status == ELIGIBLE_BY_EXCLUSION, (
        f"got {m.status} — the 'must be assessed for' sentence must not itself "
        "resolve the marker, and must not out-rank the real exclusion sentence"
    )
    assert "assessed" not in m.evidence, "the test-requirement sentence must not be the evidence shown"


def test_genuine_contradiction_is_unclear():
    """A single sentence naming both the marker and its opposite cannot be read
    cleanly — this is the real remaining UNCLEAR case, distinct from the
    excludes-the-opposite pattern above."""
    m = match_biomarker("Inclusion Criteria:\n* MSI-H or MSS tumors accepted", "MSS")
    assert m.status == UNCLEAR
    assert m.is_candidate


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
    assert ls.n_eligible == 2                # A (recruiting) + E (closed)
    assert ls.n_eligible_by_exclusion == 1   # B (excludes MSI-H, indirect)
    assert ls.n_unclear == 0
    assert ls.n_excluded == 1                # C (requires MSI-H)
    assert ls.n_not_mentioned == 2           # D (no mention) + F (no eligibility text)
    assert ls.n_no_eligibility_text == 1     # F


def test_eligible_by_exclusion_trial_is_shown_not_dropped():
    ls = build_landscape(_store(), condition="colorectal cancer", biomarker="MSS")
    shown = {t.record.nct_id for t in ls.trials}
    assert "NCT10000002" in shown, "the indirect-MSS trial must be shown, flagged"
    assert "NCT10000003" not in shown, "the MSI-H-required trial is not a candidate"
    indirect = next(t for t in ls.trials if t.record.nct_id == "NCT10000002")
    assert indirect.match.status == ELIGIBLE_BY_EXCLUSION and indirect.match.evidence


def test_every_shown_trial_carries_its_eligibility_evidence():
    ls = build_landscape(_store(), condition="colorectal cancer", biomarker="MSS")
    assert ls.trials and all(t.match.evidence for t in ls.trials)


def test_biomarker_state_is_not_a_sort_key():
    """The bug this replaces: eligible-before-by-exclusion-before-unclear was the
    PRIMARY sort key, so every trial naming the marker directly outranked every
    trial that states it by excluding the opposite — on the live colorectal
    store that put two Phase 3 trials central to this disease below five hundred
    lesser ones. Ranking decides the order; state is a column.

    Here NCT10000002 (by exclusion, open) must outrank NCT10000005 (named
    directly, closed to enrolment), which the old ordering could never do.
    """
    ls = build_landscape(_store(), condition="colorectal cancer", biomarker="MSS")
    order = [t.record.nct_id for t in ls.trials]
    by_exclusion, explicit_but_closed = order.index("NCT10000002"), order.index("NCT10000005")
    assert by_exclusion < explicit_but_closed, (
        f"got {order} — a by-exclusion trial that outscores an explicitly-eligible "
        "one must be allowed to outrank it"
    )
    assert order == sorted(
        order, key=lambda n: (-next(t for t in ls.trials if t.record.nct_id == n).ranking.score, n)
    ), "rows must be in descending score order, ties broken on NCT ID"


def test_every_printed_row_states_why_it_ranks_where_it_does():
    """A capped table whose rows cannot be accounted for is the arbitrary-sample
    failure ranking.py exists to prevent — the same rule the diligence memo's
    sample already follows."""
    ls = build_landscape(_store(), condition="colorectal cancer", biomarker="MSS")
    for t in ls.trials:
        assert t.ranking is not None
        assert "score" in t.ranking.explain()


def test_proximity_only_scores_when_the_patient_asked_for_a_location():
    """Distance is a real signal for a patient and meaningless with the location
    box empty. It must not print a zero-point line on every row of a search that
    never mentioned a place."""
    anywhere = build_landscape(_store(), condition="colorectal cancer", biomarker="MSS")
    assert not any("site in the patient's" in t.ranking.explain() for t in anywhere.trials)

    houston = build_landscape(_store(), condition="colorectal cancer", biomarker="MSS",
                              location="Houston")
    near = next(t for t in houston.trials if t.record.nct_id == "NCT10000001")
    assert "site in the patient's city" in near.ranking.explain()
    assert near.ranking.score > next(
        t for t in anywhere.trials if t.record.nct_id == "NCT10000001").ranking.score


# ------------------------------------------------------------- the display cap


def test_the_printed_list_is_capped_and_the_counts_are_not():
    """A cap narrows what is PRINTED. Everything the reader is asked to trust —
    the screen counts, the candidate total — is still over the whole
    population."""
    ls = build_landscape(_store(), condition="colorectal cancer", biomarker="MSS",
                         show_limit=1)
    assert len(ls.trials) == 1
    assert ls.n_candidates == 3, "the cap must not change what was counted"
    assert ls.n_eligible == 2 and ls.n_eligible_by_exclusion == 1
    assert ls.n_ranked_out == 2


def test_a_capped_landscape_says_what_the_cap_left_out_and_in_which_state():
    """'2 not listed' invites the assumption that the held-back rows are the
    weaker by-exclusion and unclear ones. The state breakdown is what makes that
    checkable rather than assumed."""
    ls = build_landscape(_store(), condition="colorectal cancer", biomarker="MSS",
                         show_limit=1)
    text = " ".join(ls.sample_lines())
    assert "highest-ranked of 3" in text
    assert "2 are not listed" in text
    assert "eligible" in text, "the excluded rows must be broken down by state"
    assert ls.ranked_out_by_state[ELIGIBLE_BY_EXCLUSION] == 1


def test_an_uncapped_landscape_says_it_is_showing_everything():
    """Not-capped and capped-to-exactly-the-population must read differently —
    the same not-assessed-vs-nothing-found rule applied to a sample."""
    ls = build_landscape(_store(), condition="colorectal cancer", biomarker="MSS",
                         show_limit=None)
    assert ls.n_ranked_out == 0
    assert "Showing all 3" in " ".join(ls.sample_lines())


def test_the_page_and_the_memo_cannot_show_different_rows():
    """Two surfaces, one behaviour. The cap is applied in build_landscape, so
    the Streamlit page renders the same `ls.trials` the export does — this is
    true by construction, and this test is what keeps it that way if someone
    adds a second cap in a renderer."""
    ls = build_landscape(_store(), condition="colorectal cancer", biomarker="MSS",
                         show_limit=2)
    md = render_markdown(ls)
    printed = [t.record.nct_id for t in ls.trials]
    assert len(printed) == 2
    for nct in printed:
        assert nct in md
    assert "NCT10000005" not in md or "NCT10000005" in printed
    # And the sample statement the page prints is the same text the memo does.
    for line in ls.sample_lines():
        assert line in md


def test_the_default_cap_matches_the_diligence_memos_sample_cap():
    """30 is not a number chosen for patients; it is the one the aggregate
    diligence sections already use, adopted so the two capped trial tables this
    tool prints agree. If one moves, this fails and the other should be
    considered."""
    from medrag.landscape import DEFAULT_SHOW_LIMIT

    qs = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config" / "landscape.yaml").read_text())
    caps = {q["k"] for q in qs["questions"] if q.get("aggregate")}
    assert caps == {DEFAULT_SHOW_LIMIT}, (
        f"landscape page caps at {DEFAULT_SHOW_LIMIT}, diligence census sections at "
        f"{caps} — two surfaces must agree on the cap or state why they differ"
    )


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
    assert ("| NCT ID | Title | Phase | Status | Intervention | Sponsor | Locations | "
            "PI | Contact | Eligibility match | Why ranked here |") in md
    assert "ELIGIBLE BY EXCLUSION" in md and "MSI-H" in md
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
