"""The 28 trials that moved into NOT_ASSESSABLE, hand-read and frozen.

Stage B (2161d73) shipped a verdict whose every automated gate was clean —
census/live parity, the six-trial MSS ground truth, zero rank inversions, full
suite green — and which was wrong on most of the records it fired on. The
control that worked was reading the records. This fixture is that read, written
down so the two fixes are measured against a frozen baseline rather than against
a number held in a transcript. It is the third time in this project something
load-bearing existed only in conversation.

Labels were assigned per trial from the trial's own eligibility text, before
either fix was written. Three verdicts:

  PROSE_SOURCED        the span came from detailed_description / brief_summary,
                       not eligibility_criteria. Fix 1 targets these.
  DIRECTION_SWALLOWED  eligibility DOES state a direction for that marker and a
                       filter consumed the sentence. Fix 2 targets these.
  BELONGS              the record raised the axis and genuinely stated nothing
                       comparable. These must SURVIVE both fixes.

The recorded split is pinned, not just the total, because the fixes were sized
from the split: a fix aimed at 12 prose cases that finds 9 has a different reach
than its author expected, and a total that matches while the split moves is a
finding rather than a confirmation.

The fixture is content-hashed and frozen. It is never edited to agree with a
later run — an edited-once fixture has no reconstructible provenance, the same
rule as tests/fixtures/diagnostic_confirmatory.json.

Run: python -m pytest tests/test_not_assessable_handread.py -q
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

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "not_assessable_handread.json"

#: sha256 as committed, alongside the labels and before either fix was written.
FIXTURE_SHA256 = "aa60fe194fd2420dd3398917366c76809fadfe629b620826682ac4a745d512a0"

VERDICTS = ("PROSE_SOURCED", "DIRECTION_SWALLOWED", "BELONGS")

#: The split as hand-read on the v13 census (commit 2161d73). Pinned so a later
#: re-read that disagrees has to say so in a commit rather than overwrite it.
RECORDED = {"PROSE_SOURCED": 9, "DIRECTION_SWALLOWED": 9, "BELONGS": 10}


def _load():
    return json.loads(FIXTURE.read_text())


def test_the_fixture_is_the_one_that_was_hand_read():
    actual = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert actual == FIXTURE_SHA256, (
        "not_assessable_handread.json has changed since it was frozen. These "
        "labels were read before the fixes existed; editing them to agree with a "
        "later run destroys the only baseline the fixes can be measured against. "
        "If a label is genuinely wrong, correct it in a commit that says which "
        "one and why, and update this hash in the same commit."
    )


def test_every_trial_carries_a_verdict_and_a_reason():
    for e in _load():
        assert e["verdict"] in VERDICTS, f"{e['nct_id']}: bad verdict {e['verdict']!r}"
        assert e["reason"].strip(), f"{e['nct_id']}: no reasoning recorded"
        assert e["not_assessable_markers"], f"{e['nct_id']}: no marker recorded"


def test_a_wrong_verdict_states_what_it_should_have_been():
    """A label saying only 'wrong' cannot be checked after a fix runs."""
    for e in _load():
        if e["verdict"] == "DIRECTION_SWALLOWED":
            assert e["correct_verdict_if_wrong"].strip(), (
                f"{e['nct_id']}: a swallowed direction must record the verdict "
                "the record actually supports, or the fix cannot be graded"
            )


def test_the_recorded_split_is_what_the_fixture_holds():
    counts = Counter(e["verdict"] for e in _load())
    assert dict(counts) == RECORDED, (
        f"the fixture's split {dict(counts)} no longer matches the recorded "
        f"{RECORDED}. The split is what the two fixes were sized from."
    )
    assert sum(RECORDED.values()) == 28


def test_the_prose_sourced_labels_agree_with_the_recorded_span_source():
    """Structural cross-check: a trial labelled PROSE_SOURCED must actually have
    no eligibility-sourced span, and one labelled DIRECTION_SWALLOWED must have
    one. Catches a mislabelled row without re-reading the trial."""
    for e in _load():
        srcs = set(e["span_sources"])
        if e["verdict"] == "PROSE_SOURCED":
            assert "eligibility_criteria" not in srcs, (
                f"{e['nct_id']} is labelled PROSE_SOURCED but its span came from "
                f"eligibility criteria ({srcs})"
            )
        if e["verdict"] == "DIRECTION_SWALLOWED":
            assert "eligibility_criteria" in srcs, (
                f"{e['nct_id']} is labelled DIRECTION_SWALLOWED but no span came "
                f"from eligibility criteria ({srcs})"
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
                print(f"FAIL  {name}: {exc}")
    print("\nall hand-read fixture checks passed" if not failures
          else f"\n{failures} failure(s)")
    raise SystemExit(1 if failures else 0)
