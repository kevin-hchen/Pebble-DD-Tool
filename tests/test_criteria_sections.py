"""How `iter_criteria` decides which section a criterion sits in — including
what it currently gets WRONG.

RATIONALE §24. `iter_criteria` tests `"exclusion criteria" in line`, so any
criterion that MENTIONS the phrase becomes a section heading; and on a heading
line it yields only the text after the first colon, discarding whatever came
before. The two compound: a mid-line mention makes the line a "heading", which
then truncates it.

That is not cosmetic. `markers._context` assigns polarity per unit from the
section tag and `collect_signals` classifies every marker match inside against
it, so a flipped section inverts every marker after it.

THIS FILE PINS THE DEFECT RATHER THAN THE FIX. A start-anchored heading test was
built and measured and REJECTED — it traded 13 corrections for 16 new errors, 13
of them toward REQUIRED, on trials whose own titles contradicted the verdict it
produced. §24 records why. So the `KNOWN_DEFECT` tests below assert what the code
does TODAY, and each one's docstring states what it SHOULD do.

They are written to FAIL LOUDLY the moment somebody fixes §24 — that is the
point. A limit with no executable trace is a limit somebody assumes was handled.
When the third attempt lands, invert these and delete the KNOWN_DEFECT prefix.

The worked case is a real record, captured to
`tests/fixtures/heading_defect_records.json` rather than reconstructed, because
a synthetic reproduction of a parse bug tends to encode someone's theory of the
bug instead of the input that produces it.

Run: python tests/test_criteria_sections.py   (also runs under pytest)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Direct runs do not load conftest.py, so the no-network guard is installed
# here too. See tests/netguard.py.
from tests import netguard  # noqa: E402

netguard.install()

from medrag.biomarker_gating import gate_markers  # noqa: E402
from medrag.markers import iter_criteria  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _records():
    return {r["nct_id"]: r
            for r in json.loads((FIXTURES / "heading_defect_records.json").read_text())}


def _sections(text):
    return [s for s, _unit in iter_criteria(text)]


# --------------------------------------------------------- what already works


def test_the_heading_forms_the_registry_actually_writes_are_recognised():
    """These hold today and must keep holding through any §24 fix.

    Read off the corpus, not invented: 465,393 of the 503,936 lines carrying the
    phrase open the line with no qualifier at all, and the short tail that
    remains is led by key / additional / main / general.
    """
    for line, expected in [
        ("Inclusion Criteria:", "inclusion"),
        ("INCLUSION CRITERIA:", "inclusion"),
        ("Exclusion Criteria", "exclusion"),
        ("Key Exclusion Criteria:", "exclusion"),
        ("Main Inclusion Criteria:", "inclusion"),
        ("* Exclusion Criteria:", "exclusion"),
        ("- Inclusion Criteria:", "inclusion"),
        ("3. Inclusion Criteria:", "inclusion"),
        ("a) Exclusion Criteria:", "exclusion"),
        ("Participant Exclusion Criteria", "exclusion"),
        ("Additional Inclusion Criteria:", "inclusion"),
    ]:
        got = _sections(f"{line}\nSome criterion text here")
        assert got and got[-1] == expected, (
            f"{line!r} stopped being read as a {expected} heading; the unit "
            f"after it is tagged {got[-1] if got else '(nothing yielded)'}"
        )


# ------------------------------------------- the defect, pinned as it stands
#
# Every test below asserts BEHAVIOUR THAT IS WRONG. Read the docstring for what
# the right answer is. See the module docstring before changing any of them.


def test_KNOWN_DEFECT_a_criterion_that_mentions_the_phrase_flips_the_section():
    """SHOULD read `MSI_H: REQUIRED`, `MSS: EXCLUDED`.

    NCT07127822 enrols MSI-H/dMMR gastric cancer and says so in inclusion
    criterion 5 ("Confirmed by PCR or NGS as microsatellite instability-high").
    Its criterion 7 ends "...provided that they meet other inclusion and
    exclusion criteria;", which is read as an exclusion heading — so the trial
    is recorded as EXCLUDING the marker it exists to recruit, and the landscape
    screen hides it from exactly that population.
    """
    rec = _records()["NCT07127822"]
    flags = gate_markers(rec["eligibility_criteria"])

    assert flags["MSI_H"].status == "EXCLUDED", (
        "§24 may have been fixed — MSI_H no longer reads EXCLUDED. If so this "
        "test has done its job: invert it to assert REQUIRED and drop the "
        "KNOWN_DEFECT prefix."
    )
    assert flags["MSS"].status == "ELIGIBLE_BY_EXCLUSION", flags["MSS"].status


def test_KNOWN_DEFECT_criteria_before_the_mentioning_line_are_discarded():
    """SHOULD keep every criterion. Criteria 1-4 of NCT07127822 sit before the
    first colon of the line that is misread as a heading, so they are dropped
    entirely — they reach no marker, no FTS index and no reader. Store-wide this
    discards 537,578 characters of criterion text across 3,705 trials."""
    rec = _records()["NCT07127822"]
    units = " ".join(u for _s, u in iter_criteria(rec["eligibility_criteria"]))

    for fragment in ("Voluntarily willing to participate",
                     "Age ≥18 years",
                     "Expected survival time",
                     "unresectable locally advanced"):
        assert fragment not in units, (
            f"{fragment!r} now survives segmentation — the truncation half of "
            "§24 may be fixed. Invert this test."
        )


def test_KNOWN_DEFECT_a_heading_written_without_a_colon_loses_its_criteria():
    """SHOULD yield ("inclusion", "Patients must have HER2 negative disease").

    The colon-only rule yields nothing at all here, so every criterion on the
    line vanishes silently — no marker, no reader, no warning.
    """
    assert list(iter_criteria(
        "Inclusion Criteria - Patients must have HER2 negative disease")) == []


def test_KNOWN_DEFECT_a_statement_about_the_criteria_is_read_as_a_heading():
    """SHOULD leave both lines "unknown". "There are no exclusion criteria" is a
    claim about the trial, not a section marker; treating it as one silently
    relabels everything that follows it as exclusion."""
    text = ("There are no exclusion criteria for this study.\n"
            "Patients must have MSS disease.")
    assert _sections(text) == ["exclusion"], _sections(text)


def test_KNOWN_DEFECT_a_combined_heading_resolves_to_exclusion():
    """SHOULD settle NEITHER section — "inclusion/exclusion criteria" names both.

    It resolves to whichever branch is tested first, which is how "Other
    protocol-defined inclusion/exclusion criteria apply" comes to mean exclusion
    for every unit after it. 3,599 such lines store-wide.
    """
    text = ("Inclusion Criteria:\n"
            "* Patients must have MSS disease\n"
            "* Other protocol-defined inclusion/exclusion criteria apply\n"
            "* Patients must have measurable disease")
    assert _sections(text) == ["inclusion", "exclusion"], _sections(text)


def test_KNOWN_DEFECT_a_mentioning_line_is_consumed_rather_than_kept():
    """SHOULD yield the line as an ordinary inclusion criterion. It is instead
    treated as a heading, and having no colon it yields nothing — so a real
    criterion disappears."""
    text = ("Inclusion Criteria:\n"
            "* Agree to provide tissue, provided that they meet other "
            "inclusion and exclusion criteria")
    assert list(iter_criteria(text)) == []


def test_KNOWN_DEFECT_the_singular_criterion_is_not_recognised_as_a_heading():
    """SHOULD read "Inclusion Criterion:" as an inclusion heading. The substring
    test looks for "criteria" and the singular form never matches, so the
    section stays at whatever preceded it."""
    assert _sections("Inclusion Criterion:\nSome criterion text here") == [
        "unknown", "unknown"]


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
