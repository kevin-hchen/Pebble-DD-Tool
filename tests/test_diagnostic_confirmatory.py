"""The confirmatory set: drawn fresh, labelled blind, graded once.

Separate from the development fixture and frozen the same way. The point of a
confirmatory set is that it is used ONCE — the grader was already finished and
frozen when these 40 were drawn, the labels were written and hashed before the
grader was run against them, and the result is the published number whatever it
turned out to be.

If the grader is changed after this point, this set is spent. The next
measurement needs a new draw; re-grading these 40 would measure fit, which is
what the development set already does.
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

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "diagnostic_confirmatory.json"
DEV = Path(__file__).resolve().parent / "fixtures" / "diagnostic_ground_truth.json"

#: sha256 as committed, written BEFORE the grader was run against it.
FROZEN_SHA256 = "5e7b3d9bf5519a3625c5e87ade33b45723a7c4682d9a9d7944b3b5635abc2011"

#: The published result. Recorded so a later change that quietly moves it has to
#: move this line too, in the same commit, deliberately.
RESULT = {"n": 40, "misroutes": 4, "declines": 5, "coverage": 35, "inversions": 0}


def test_the_confirmatory_labels_are_frozen():
    actual = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert actual == FROZEN_SHA256, (
        "the confirmatory labels have changed. They were written before the grader "
        "was run and are used once; editing them makes the published figure "
        "unreproducible and turns a confirmatory set into a development one."
    )


def test_it_shares_no_study_with_the_development_set():
    """Otherwise it confirms nothing — it re-measures what was fitted."""
    conf = {r["pmid"] for r in json.loads(FIXTURE.read_text())}
    dev = {r["pmid"] for r in json.loads(DEV.read_text())}
    assert not (conf & dev), f"overlap with the development set: {sorted(conf & dev)}"
    assert len(conf) == RESULT["n"]


def test_it_spans_the_three_modalities():
    counts = Counter(r["modality"] for r in json.loads(FIXTURE.read_text()))
    assert set(counts) == {"ivd", "imaging", "monitoring"}, counts
    assert min(counts.values()) >= 10, counts


def test_every_record_carries_its_reasoning():
    for row in json.loads(FIXTURE.read_text()):
        assert row["reason"].strip(), f"{row['pmid']} has a label and no reasoning"


def test_the_published_result_is_recorded_with_its_bars():
    """The figure in CAPABILITIES.md and the bars it was judged against, in one
    place, so neither can drift from the other silently."""
    caps = (Path(__file__).resolve().parents[1] / "docs" / "CAPABILITIES.md").read_text()
    assert "4 / 40  = 10.0%" in caps
    assert "35 / 40  = 87.5%" in caps
    assert "ordering inversions   0" in caps
    assert "9" in caps and "unmeasured" in caps.lower(), (
        "CAPABILITIES.md must state that tier assignment is unmeasured on this set"
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
                print(f"FAIL  {name}: {str(exc)[:180]}")
    print("\nconfirmatory set frozen" if not failures else f"\n{failures} failure(s)")
    raise SystemExit(1 if failures else 0)
