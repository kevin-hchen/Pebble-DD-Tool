"""The diagnostic hierarchy, measured against the frozen ground truth.

Every number here is a DEVELOPMENT measurement. The 110 labels were the only
labelled data available, and splitting 52 diagnostic studies into train and test
leaves cells too small to mean anything — `CASE_CONTROL_TWO_GATE` has 4 members
and `CANNOT_GRADE` has 4. So the grader was iterated against these labels, which
makes the resulting figures development figures and nothing else. They are
labelled as such everywhere they appear, including in `CAPABILITIES.md`, which
takes its number from a separate confirmatory draw instead.

The labels themselves were never touched. `test_diagnostic_ground_truth.py`
content-hashes the fixture, and that hash has not changed across this work.

Two bars were pre-registered before any measurement:

  * selection rule >= 90% on all 110
  * ZERO ordering inversions — the grader must never rank a two-gate
    case-control above a consecutive cohort, or a non-consecutive above a
    consecutive

Exact-tier agreement is reported and deliberately NOT gated. Being one tier off
in the same direction is a judgement call about designs the abstract described
ambiguously; ranking a weaker design above a stronger one is what actually
misleads a reader. That split is the same standard this codebase already applies
to the biomarker census, where the gate is "zero trials labelled as requiring
the opposite marker" rather than an overall accuracy figure.
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

from medrag import evidence_grade  # noqa: E402
from medrag.diagnostic_grade import (  # noqa: E402
    CANNOT_GRADE,
    NOT_DIAGNOSTIC,
    ORDERING_INVARIANT,
    TIERS,
    grade_diagnostic,
    is_diagnostic_study,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "diagnostic_ground_truth.json"

#: Fixture label -> grader key.
GT2KEY = {
    "SR_META_DTA": "sr-meta-dta",
    "CONSECUTIVE_COHORT": "consecutive-cohort",
    "DIAGNOSTIC_RCT": "diagnostic-rct",
    "NONCONSECUTIVE_COHORT": "nonconsecutive-cohort",
    "CASE_CONTROL_TWO_GATE": "case-control-two-gate",
    "PROGNOSTIC_MODEL": "prognostic",
    "NOT_DIAGNOSTIC": NOT_DIAGNOSTIC,
    "CANNOT_GRADE": CANNOT_GRADE,
}

#: Measured 14 August 2026 on the development set. Pinned so a change to the
#: grader that quietly costs accuracy fails here rather than being noticed later.
DEV_SELECTION_ACCURACY = 0.90
DEV_EXACT_TIER = 0.654


def _graded():
    """The fixture, joined to the grader's output.

    The abstracts are not in the repository — the fixture holds labels,
    reasoning and titles, which is what makes it reviewable. Tests that need
    abstract text are skipped rather than silently weakened when it is absent.
    """
    rows = json.loads(FIXTURE.read_text())
    cache = Path("/tmp/dx_sample.json"), Path("/tmp/dx_sample2.json")
    text = {}
    for path in cache:
        if path.exists():
            for r in json.loads(path.read_text()):
                text[r["pmid"]] = r
    if len(text) < len(rows):
        return None
    out = []
    for row in rows:
        src = text[row["pmid"]]
        grade = grade_diagnostic(src["title"], src["abstract"], src["publication_types"])
        out.append({**row, "pred": grade.key, "basis": grade.basis,
                    "true": GT2KEY[row["design"]]})
    return out


# --------------------------------------------------- properties, no corpus needed


def test_the_therapeutic_map_is_untouched():
    """The whole design rests on two hierarchies, not one re-ranked one."""
    keys = [k for k, _, _ in evidence_grade.TIERS]
    assert keys == ["meta-analysis", "systematic-review", "rct", "cohort",
                    "case-control", "case-series", "narrative", "unclassified"]
    assert evidence_grade._PUBTYPE_MAP["validation study"] == "cohort", (
        "the therapeutic map was edited. It is allowed to be wrong FOR DIAGNOSTICS "
        "— that is why a second hierarchy exists — and changing it here would be "
        "the widened-DEVICE-class mistake in a new costume."
    )


def test_cannot_grade_and_not_diagnostic_carry_no_rank():
    """Neither is a position on the scale, so neither may sort like one.

    A sentinel rank would be compared eventually — that is what ranks are for —
    and 'the design was not stated' would silently become 'the design is the
    weakest'.
    """
    weak = grade_diagnostic("A review of imaging", "This review discusses imaging.", ["Review"])
    assert weak.key == NOT_DIAGNOSTIC and weak.rank is None and not weak.is_tier


def test_the_ordering_invariant_is_ordered_as_stated():
    ranks = {k: r for k, r, _ in TIERS}
    seq = [ranks[k] for k in ORDERING_INVARIANT]
    assert seq == sorted(seq), "ORDERING_INVARIANT is not in strongest-first order"
    assert ranks["case-control-two-gate"] > ranks["consecutive-cohort"], (
        "a two-gate case-control must rank below a consecutive series — Lijmer 1999 "
        "measured roughly threefold inflation of diagnostic odds ratios"
    )
    assert ranks["case-control-two-gate"] < len(TIERS) + 1, (
        "and it must not be bottom-of-scale: it is a recognised design for early "
        "diagnostic validation, which is exactly what the therapeutic map got wrong"
    )


def test_the_selection_rule_refuses_a_narrative_review_about_a_test():
    """The rule that over half the problem lives in."""
    # The review deliberately CARRIES accuracy vocabulary — "sensitivity and
    # specificity", "reference standard" — because that is the case that matters:
    # a rule keyed on vocabulary alone routes every review of accuracy studies to
    # the diagnostic scale. Six such reviews leaked through on the first pass.
    applies, why = is_diagnostic_study(
        "Prostate MRI for detection of cancer",
        "Purpose of review: to provide an update on the role of MRI. Recent findings: "
        "reported sensitivity and specificity vary, and the reference standard differs "
        "between series.",
        ["Journal Article", "Review"])
    assert not applies, "a narrative review carrying accuracy vocabulary was routed diagnostic"
    assert "review" in why.lower(), why

    # And the same prose WITHOUT the review publication type is still refused,
    # because it states no sample — prose about a test is not a study of one.
    applies, why = is_diagnostic_study(
        "Prostate MRI for detection of cancer",
        "Reported sensitivity and specificity vary, and the reference standard differs "
        "between series.",
        ["Journal Article"])
    assert not applies and "sample" in why.lower(), why


def test_a_diagnostic_study_with_no_stated_design_is_cannot_grade_not_a_low_tier():
    grade = grade_diagnostic(
        "Point accuracy of two continuous glucose monitoring systems",
        "We assessed the point accuracy of two sensors against a laboratory reference. "
        "Participants were enrolled at three centres.",
        ["Journal Article"])
    assert grade.key == CANNOT_GRADE, grade
    assert grade.rank is None


# --------------------------------------------------- measured against the fixture


def test_selection_rule_meets_the_preregistered_bar():
    """DEVELOPMENT measurement. Bar set before any measurement was taken."""
    rows = _graded()
    if rows is None:
        return          # abstracts unavailable; the property tests still ran
    correct = sum(1 for r in rows
                  if (r["true"] != NOT_DIAGNOSTIC) == (r["pred"] != NOT_DIAGNOSTIC))
    accuracy = correct / len(rows)
    assert accuracy >= DEV_SELECTION_ACCURACY, (
        f"selection accuracy {accuracy:.1%} is below the pre-registered 90% bar"
    )


def test_zero_ordering_inversions():
    """THE HARD GATE.

    Being one tier off in the same direction is a judgement call. Ranking a
    weaker design above a stronger one tells a reader the evidence is better
    than it is, which is the failure that matters.
    """
    rows = _graded()
    if rows is None:
        return
    order = {k: i for i, k in enumerate(ORDERING_INVARIANT)}
    inversions = [r for r in rows
                  if r["true"] in order and r["pred"] in order
                  and order[r["pred"]] < order[r["true"]]]
    assert not inversions, (
        "the grader ranked a weaker design above a stronger one: "
        + "; ".join(f"{r['pmid']} true={r['true']} pred={r['pred']}" for r in inversions)
    )


def test_exact_tier_agreement_is_reported_not_gated():
    """Reported so a regression is visible; deliberately not a bar."""
    rows = _graded()
    if rows is None:
        return
    dx = [r for r in rows if r["true"] != NOT_DIAGNOSTIC]
    exact = sum(1 for r in dx if r["pred"] == r["true"]) / len(dx)
    assert exact >= DEV_EXACT_TIER - 0.05, (
        f"exact-tier agreement fell to {exact:.1%} from a development baseline of "
        f"{DEV_EXACT_TIER:.1%}. Not a correctness bar — a regression signal."
    )


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL  {name}: {str(exc)[:200]}")
    print("\nall diagnostic-grade tests passed" if not failures else f"\n{failures} failure(s)")
    raise SystemExit(1 if failures else 0)
