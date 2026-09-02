"""The diagnostic ground truth is FROZEN. This test is the freeze.

`tests/fixtures/diagnostic_ground_truth.json` holds 110 studies hand-read and
labelled before any grader existed. It is the set the diagnostic evidence
hierarchy will be validated against, and the whole value of it is that it was
written down first.

The rule, stated so it cannot be softened later: **nothing edits these labels
once grader output exists, including if the grader looks wrong.** A label
changed after seeing a disagreement is not a correction, it is the test set
being fitted to the thing under test, and the agreement number it produces
afterwards means nothing. If a label is genuinely wrong, that is a finding to
report alongside the measurement — not an edit.

The content hash below is what makes the rule enforceable rather than merely
stated. Editing any label, or any of the reasoning, changes it and fails here.

This is the same discipline `docs/DECISIONS.md` records for the combined-signal
experiment: the fresh held-out set is read before the classifier is written,
because the two failure patterns that motivated it were derived from the
previous set and grading on that set again would fit the test.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Direct runs do not load conftest.py, so the no-network guard is installed
# here too. See tests/netguard.py.
from tests import netguard  # noqa: E402

netguard.install()

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "diagnostic_ground_truth.json"
README = Path(__file__).resolve().parent / "fixtures" / "diagnostic_ground_truth.README.md"

#: sha256 of the fixture as committed, 14 August 2026, before any grader existed.
#: If this test fails and you did not mean to edit the labels, restore them. If
#: you DID mean to edit them, the measurement they support has to be re-run and
#: re-reported from scratch — a partially re-labelled set is worse than either.
FROZEN_SHA256 = "c53d6a33c5d5c1ebbe3f4b6bab56927a89712651e0d762685edbbcd6f34348b4"

#: The label vocabulary, fixed from Oxford CEBM levels for diagnosis, QUADAS-2
#: and STARD BEFORE the sample was drawn. See the README beside the fixture.
DESIGNS = {
    "SR_META_DTA", "CONSECUTIVE_COHORT", "NONCONSECUTIVE_COHORT",
    "CASE_CONTROL_TWO_GATE", "DIAGNOSTIC_RCT", "PROGNOSTIC_MODEL",
    "NOT_DIAGNOSTIC", "CANNOT_GRADE",
}


def _rows():
    return json.loads(FIXTURE.read_text())


def test_the_labels_are_frozen():
    """The pre-registration, enforced."""
    actual = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert actual == FROZEN_SHA256, (
        "the hand-read diagnostic labels have changed.\n"
        "  They are frozen: nothing edits them once grader output exists, including "
        "if the grader looks wrong.\n"
        "  A label changed after seeing a disagreement fits the test set to the thing "
        "under test, and every agreement number computed afterwards is meaningless.\n"
        "  If a label is genuinely wrong, report it beside the measurement instead."
    )


def test_every_record_carries_its_reasoning():
    """A label with no reason is unauditable — nobody can tell later whether it
    was a judgement or a slip, which is exactly what the freeze has to protect."""
    for row in _rows():
        assert row["reason"].strip(), f"{row['pmid']} has a label and no reasoning"
        assert row["design"] in DESIGNS, f"{row['pmid']}: unknown design {row['design']!r}"


def test_the_sample_spans_the_three_modalities():
    """One modality is not the category — the recurring failure `docs/SCOPE.md`
    names. A hierarchy validated only on IVD would be a hierarchy for IVD."""
    counts = Counter(r["modality"] for r in _rows())
    assert set(counts) == {"ivd", "imaging", "monitoring"}, counts
    assert min(counts.values()) >= 25, f"a modality is thin: {counts}"


def test_the_sample_is_large_enough_and_shares_nothing_with_the_audit_corpus():
    """Fresh set, not the 86 the problem was diagnosed on. Grading on those
    would be fitting to the sample that produced the hypothesis."""
    rows = _rows()
    assert len(rows) >= 60, f"only {len(rows)} studies hand-read"

    corpus = Path(__file__).resolve().parents[1] / "data" / "raw" / "corpus.jsonl"
    if not corpus.exists():
        return          # a fresh clone has no corpus; the fixture still stands
    held = set()
    for line in corpus.read_text().split("\n"):
        if line.strip():
            held.add(json.loads(line)["doc_id"])
    overlap = {r["pmid"] for r in rows} & held
    assert not overlap, f"these PMIDs are also in the audit corpus: {sorted(overlap)}"


def test_every_label_value_is_populated():
    """A hierarchy cannot be validated on tiers the sample does not contain.

    CASE_CONTROL_TWO_GATE and CANNOT_GRADE are named explicitly: the first is
    the tier this whole change turns on, and the second is the state that
    distinguishes "the design is weak" from "the record does not say".
    """
    counts = Counter(r["design"] for r in _rows())
    for design in DESIGNS:
        assert counts[design] > 0, (
            f"no study in the ground truth is labelled {design}, so nothing can "
            "validate how a grader treats it"
        )
    assert counts["CASE_CONTROL_TWO_GATE"] >= 3
    assert counts["CANNOT_GRADE"] >= 3


def test_the_reading_rule_and_its_sources_are_recorded():
    """The vocabulary came from published frameworks before the sample was
    drawn. Without that written down, a reader cannot tell whether the tiers
    were derived or reverse-engineered from what turned up."""
    text = README.read_text()
    for source in ("CEBM", "QUADAS-2", "STARD"):
        assert source in text, f"the label README no longer cites {source}"
    assert "title + abstract + PubMed publication types only" in text, (
        "the README no longer records that labels were read from exactly what a "
        "grader can see — without it the agreement measurement is not comparable"
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
    print("\nground truth frozen and valid" if not failures else f"\n{failures} failure(s)")
    raise SystemExit(1 if failures else 0)
