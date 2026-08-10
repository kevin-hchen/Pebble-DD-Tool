"""Tests for the shared marker engine (markers.py) and the two policies built
on it (biomarker.py's patient-side reduction, biomarker_gating.py's trial-side
reduction).

Three things this file exists to prove, each tied to a named defect in the
task that motivated this module:

  1. Negation generalises beyond MSS. "X-negative" and "X wild-type" must never
     resolve to REQUIRING X, for every curated marker — not just MSS/MSI-H.
  2. A sentence that mandates a TEST ("must be assessed for X status") carries
     no signal, so it cannot invert a trial via a later, real exclusion line.
  3. biomarker.py and biomarker_gating.py, run over the same text, never reach
     opposite conclusions — despite using deliberately different vocabularies
     and deliberately different conflict-resolution policies (see markers.py's
     module docstring, "THE TWO POLICIES").

The last section runs the actual six-trial ground truth (real registry JSON,
captured 2026-08-04, in tests/fixtures/ctgov_study_NCT0*.json) end to end and
is the number this task is measured on: recall N of 6, and — the harder
requirement — zero inversions.

No network: fixtures are captured JSON; tests/netguard.py blocks sockets.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import netguard  # noqa: E402

netguard.install()

from medrag import markers as m  # noqa: E402
from medrag.biomarker import (  # noqa: E402
    ELIGIBLE,
    ELIGIBLE_BY_EXCLUSION,
    EXCLUDED,
    NOT_MENTIONED,
    UNCLEAR,
    match_biomarker,
)
from medrag.biomarker_gating import ELIGIBLE_BY_EXCLUSION as G_ELIGIBLE_BY_EXCLUSION
from medrag.biomarker_gating import EXCLUDED as G_EXCLUDED
from medrag.biomarker_gating import NOT_MENTIONED as G_NOT_MENTIONED
from medrag.biomarker_gating import REQUIRED as G_REQUIRED
from medrag.biomarker_gating import gate_markers
from medrag.trials.client import parse_study

FIXTURES = Path(__file__).resolve().parent / "fixtures"

CURATED_MARKERS = list(m.MARKERS)  # MSS, MSI_H, RAS, BRAF_V600E, HER2_AMP, KRAS_G12C, KRAS_G12D


# ------------------------------------------------- defect 1: negation generalises


def _bare_name(key: str) -> str:
    """A plausible bare mention of each marker's name, for building synthetic
    '<name>-negative' / '<name> wild-type' sentences."""
    return {
        "MSS": "MSI-H", "MSI_H": "MSI-H", "RAS": "RAS", "BRAF_V600E": "BRAF",
        "HER2_AMP": "HER2", "KRAS_G12C": "KRAS G12C", "KRAS_G12D": "KRAS G12D",
    }[key]


def test_wild_type_never_requires_the_marker_for_every_curated_marker():
    for key in CURATED_MARKERS:
        name = _bare_name(key)
        # Query for the OWN key when it has no opposite (RAS/BRAF/HER2/KRAS),
        # or for the marker whose bare-name form is being tested otherwise.
        query_key = "MSI_H" if key == "MSS" else key
        text = f"Inclusion Criteria:\n* {name} wild-type required for enrolment"
        flags = gate_markers(text)
        assert flags[query_key].status != G_REQUIRED, (
            f"{query_key}: 'wild-type' text resolved to REQUIRED — must never happen"
        )


def test_negative_suffix_never_requires_the_marker():
    for key, name in (("MSI_H", "MSI-H"), ("HER2_AMP", "HER2")):
        text = f"Inclusion Criteria:\n* {name}-negative disease only"
        flags = gate_markers(text)
        assert flags[key].status != G_REQUIRED, (
            f"{key}: '{name}-negative' resolved to REQUIRED — must never happen"
        )


def test_ras_wild_type_real_mountaineer_phrasing_excludes_ras():
    """MOUNTAINEER-03's actual eligibility sentence."""
    text = ("Inclusion Criteria:\n"
           "* Participant has rat sarcoma viral oncogene homolog wild-type (RAS WT) "
           "disease as determined by local or central testing.")
    assert gate_markers(text)["RAS"].status == G_EXCLUDED


# ------------------------------------------------- defect 2: testing != stating


def test_documented_status_sentence_carries_no_signal():
    """STELLAR-303's actual RAS sentence: documents that a status was recorded,
    states no direction. Must not flip RAS to REQUIRED or EXCLUDED."""
    text = ("Inclusion Criteria:\n"
           "* Documented rat sarcoma (RAS) status (mutant or wild-type [WT]), "
           "by tissue-based analysis.")
    assert gate_markers(text)["RAS"].status == G_NOT_MENTIONED


def test_assessed_for_status_does_not_outrank_a_real_exclusion():
    """C-800-25's actual pair of sentences: a test-requirement line followed by
    a real exclusion line. The real line must decide the trial, not the noise."""
    text = (
        "Inclusion Criteria:\n"
        "* The tumor must have been assessed for microsatellite instability "
        "high (MSI-H) or deficient mismatch repair (dMMR) status per a "
        "standard local testing method.\n\n"
        "Exclusion Criteria:\n"
        "* Tumor is MSI-H/dMMR per a standard local testing method.\n"
    )
    flags = gate_markers(text)
    assert flags["MSI_H"].status == G_EXCLUDED
    assert "assessed" not in flags["MSI_H"].span


def test_a_sentence_with_a_direction_word_is_not_neutralised():
    """The neutraliser must not swallow a sentence that DOES carry a direction —
    only ones that name the marker with no stated result."""
    text = "Exclusion Criteria:\n* Known BRAF V600E mutant status"
    assert gate_markers(text)["BRAF_V600E"].status == G_EXCLUDED


# ------------------------------------------------- negation at a distance


def test_negation_two_words_before_the_marker_is_recognised():
    text = ("Inclusion Criteria:\n* Documented NOT to have microsatellite "
           "instability-high (MSI-high) CRC by tissue-based analysis.")
    flags = gate_markers(text)
    assert flags["MSI_H"].status == G_EXCLUDED, (
        "a negation two words before the marker ('NOT to have') must be recognised, "
        "not read as the trial requiring the marker"
    )


def test_a_marker_named_twice_in_one_sentence_is_not_a_self_contradiction():
    """A spelled-out name immediately restated in parentheses ('microsatellite
    instability-high (MSI-high)') is one clinical statement, not two. Negation
    detected on the first occurrence must not be contradicted by a second,
    closer-to-the-parenthesis occurrence of the same mention."""
    text = ("Inclusion Criteria:\n* Documented NOT to have microsatellite "
           "instability-high (MSI-high) or mismatch repair deficient (dMMR) "
           "CRC by tissue-based analysis.")
    m_patient = match_biomarker(text, "MSS")
    assert m_patient.status == ELIGIBLE_BY_EXCLUSION, (
        f"got {m_patient.status} — a marker restated within one negated sentence "
        "must not be read as self-contradictory"
    )


# ------------------------------------------------- curation status


def test_uncurated_marker_can_only_be_unclear_or_not_mentioned():
    text = "Inclusion Criteria:\n* EGFR wild-type tumors required"
    result_present = match_biomarker(text, "EGFR")
    result_absent = match_biomarker(text, "some totally unregistered marker")
    assert result_present.curated is False
    assert result_present.status in (UNCLEAR, NOT_MENTIONED)
    assert result_absent.curated is False
    assert result_absent.status in (UNCLEAR, NOT_MENTIONED)


def test_curated_marker_is_flagged_curated():
    m_ = match_biomarker("Inclusion Criteria:\n* MSS tumors", "MSS")
    assert m_.curated is True


def test_uncurated_result_never_reaches_eligible_or_excluded():
    """An uncurated guess must never claim the confidence a reviewed marker's
    verdict carries — it has no negation handling and no reviewed synonyms."""
    texts = [
        "Inclusion Criteria:\n* EGFR mutation required",
        "Exclusion Criteria:\n* EGFR mutation",
        "Inclusion Criteria:\n* EGFR wild-type required",
    ]
    for t in texts:
        r = match_biomarker(t, "EGFR")
        assert r.status not in (ELIGIBLE, EXCLUDED), (
            f"uncurated match returned {r.status} for {t!r} — must be UNCLEAR/NOT MENTIONED only"
        )


# ------------------------------------------------- cross-module agreement


def _direction(support_eligible: bool, support_excluded: bool) -> str:
    if support_eligible and support_excluded:
        return "CONTRADICTORY"
    if support_eligible:
        return "ADMITS"
    if support_excluded:
        return "EXCLUDES"
    return "SILENT"


def _patient_direction(status: str) -> str:
    return {
        ELIGIBLE: "ADMITS", ELIGIBLE_BY_EXCLUSION: "ADMITS",
        EXCLUDED: "EXCLUDES", NOT_MENTIONED: "SILENT", UNCLEAR: "CONTRADICTORY",
    }[status]


def _gating_direction(status: str) -> str:
    return {
        G_REQUIRED: "ADMITS", G_ELIGIBLE_BY_EXCLUSION: "ADMITS",
        G_EXCLUDED: "EXCLUDES", G_NOT_MENTIONED: "SILENT",
    }[status]


NON_CONTRADICTORY_FIXTURES = [
    "Inclusion Criteria:\n* Microsatellite stable (MSS) tumors",
    "Inclusion Criteria:\n* Documented non-MSI-H disease",
    "Inclusion Criteria:\n* Tumors must be MSI-H (microsatellite instability-high)",
    "Exclusion Criteria:\n* Known MSI-H or dMMR",
    "Inclusion Criteria:\n* Age 18+\n* ECOG 0-1",
    ("Inclusion Criteria:\n* Documented NOT to have microsatellite "
     "instability-high (MSI-high) or mismatch repair deficient (dMMR) CRC "
     "by tissue-based analysis."),
    ("Inclusion Criteria:\n"
     "* The tumor must have been assessed for microsatellite instability "
     "high (MSI-H) or deficient mismatch repair (dMMR) status per a "
     "standard local testing method.\n\nExclusion Criteria:\n"
     "* Tumor is MSI-H/dMMR per a standard local testing method.\n"),
]


def test_the_two_modules_never_reach_opposite_conclusions():
    """The defect-5 regression: for text that does not genuinely contradict
    itself, biomarker.py's patient-side verdict and biomarker_gating.py's
    trial-side verdict for the SAME marker must point the SAME direction —
    both admit, both exclude, or both silent. They are allowed to differ in
    HOW confidently (gating.py has no UNCLEAR; biomarker.py's on-conflict
    policy differs — see markers.py's docstring), never in WHICH direction."""
    for text in NON_CONTRADICTORY_FIXTURES:
        patient = match_biomarker(text, "MSS")
        gating = gate_markers(text)["MSS"]
        pd, gd = _patient_direction(patient.status), _gating_direction(gating.status)
        if pd == "CONTRADICTORY":
            continue  # a genuine patient-side conflict has no gating equivalent to compare
        assert pd == gd, (
            f"disagreement on {text!r}: biomarker.py says {patient.status} ({pd}), "
            f"biomarker_gating.py says {gating.status} ({gd})"
        )


# ------------------------------------------------- supplementary-text fallback


def test_description_only_reached_when_eligibility_is_silent():
    m_ = match_biomarker(
        "Inclusion Criteria:\n* Age 18+", "MSS",
        detailed_description="Inclusion Criteria:\n* MSI-H required",
    )
    assert m_.status == EXCLUDED and m_.source == "detailed_description"


def test_eligibility_criteria_always_outranks_supplementary_text():
    """A clear eligibility-criteria statement must never be overridden by
    prose elsewhere, even prose that says the opposite."""
    m_ = match_biomarker(
        "Inclusion Criteria:\n* MSS tumors", "MSS",
        detailed_description="This study focuses on MSI-H disease.",
    )
    assert m_.status == ELIGIBLE and m_.source == "eligibility_criteria"


def test_keywords_are_the_last_resort_supplementary_source():
    m_ = match_biomarker("", "MSS", keywords=["Colorectal Cancer", "MSS", "Microsatellite stable"])
    assert m_.status == ELIGIBLE and m_.source == "keywords"


# ------------------------------------------------- the six-trial ground truth


NAMES = {
    "NCT05425940": "STELLAR-303", "NCT07228832": "HARMONi-GI3",
    "NCT05608044": "C-800-25", "NCT05405595": "ADG126-P001",
    "NCT06252649": "CodeBreaK 301", "NCT05253651": "MOUNTAINEER-03",
}


def _load_record(nct):
    return parse_study(json.loads((FIXTURES / f"ctgov_study_{nct}.json").read_text()))


def _mss_match(nct):
    r = _load_record(nct)
    return match_biomarker(
        r.eligibility_criteria, "MSS",
        detailed_description=r.detailed_description,
        brief_summary=r.brief_summary, keywords=r.keywords,
    )


def test_stellar_303_reaches_the_user_as_eligible_by_exclusion():
    assert _mss_match("NCT05425940").status == ELIGIBLE_BY_EXCLUSION


def test_c_800_25_reaches_the_user_as_eligible_by_exclusion():
    assert _mss_match("NCT05608044").status == ELIGIBLE_BY_EXCLUSION


def test_harmoni_gi3_reaches_the_user_as_eligible_by_exclusion():
    assert _mss_match("NCT07228832").status == ELIGIBLE_BY_EXCLUSION


def test_adg126_p001_reaches_the_user_via_its_detailed_description():
    m_ = _mss_match("NCT05405595")
    assert m_.is_candidate, f"ADG126-P001 must reach the user, got {m_.status}"
    assert m_.source == "detailed_description"
    assert "MSS" in m_.evidence


def test_codebreak_301_stays_not_mentioned_for_mss():
    """The pass criterion this task exists to protect: a trial gated purely on
    KRAS G12C, with no microsatellite-status text anywhere, must NOT come back
    eligible. Tuning that turns this into a false positive is a regression."""
    m_ = _mss_match("NCT06252649")
    assert m_.status == NOT_MENTIONED, f"got {m_.status} — must stay NOT MENTIONED"


def test_mountaineer_03_stays_not_mentioned_for_mss():
    """Same protection for the HER2/RAS-gated trial."""
    m_ = _mss_match("NCT05253651")
    assert m_.status == NOT_MENTIONED, f"got {m_.status} — must stay NOT MENTIONED"


def test_zero_inversions_across_all_six():
    """The non-negotiable pass criterion: no trial is ever labelled as
    requiring the marker it excludes."""
    inversions = [nct for nct in NAMES if _mss_match(nct).status == EXCLUDED]
    assert inversions == [], f"inverted: {[NAMES[n] for n in inversions]}"


def test_recall_is_six_of_six():
    reaching_user = [nct for nct in NAMES if _mss_match(nct).is_candidate]
    assert set(reaching_user) == {"NCT05425940", "NCT07228832", "NCT05608044", "NCT05405595"}, (
        f"expected exactly the four MSS-relevant trials to reach the user, got "
        f"{[NAMES[n] for n in reaching_user]}"
    )


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
