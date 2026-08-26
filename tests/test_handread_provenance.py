"""Every hand-read fixture states what was read to produce it.

A hand-read is the control this project falls back on when every automated gate
is clean. It is what caught the Stage B `NOT_ASSESSABLE` emission being wrong on
most of the records it fired on, after census/live parity, the six-trial MSS
ground truth, zero rank inversions and a green suite had all passed.

Which makes a silent defect in the READING method worse than no control at all.
The 28-trial hand-read was performed against a generated dossier that printed
the first 170 characters of each matching sentence, and two rows were labelled
from an excerpt that stopped just before the clause that decided them — both
blocks end "Participants with HER2 positive disease are not eligible for
enrollment". Two wrong labels, wrong in the same way, frozen into the fixture
everything downstream is graded against.

So: a fixture of hand-assigned labels must record its source span, either per
row or in a README beside it. This sweep DISCOVERS the fixtures rather than
listing them, so a hand-read added later is covered without anyone remembering
to add it here — the same reason test_phrasing.py discovers caveat modules.

See docs/DECISIONS.md, "A hand-read is performed against the complete record".

Run: python -m pytest tests/test_handread_provenance.py -q
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Direct runs do not load conftest.py, so the no-network guard is installed
# here too. See tests/netguard.py.
from tests import netguard  # noqa: E402

netguard.install()

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures"

#: A hand-read fixture is a JSON list of records carrying a per-row human
#: judgement and the reasoning for it. Detected by shape, not by name.
_JUDGEMENT_KEYS = ("verdict", "design", "label", "grade")
_REASON_KEYS = ("reason", "reasoning", "why")


def _hand_read_fixtures() -> list[Path]:
    found = []
    for path in sorted(FIXTURES.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(data, list) or not data:
            continue
        row = data[0]
        if not isinstance(row, dict):
            continue
        if any(k in row for k in _JUDGEMENT_KEYS) and any(k in row for k in _REASON_KEYS):
            found.append(path)
    return found


def test_the_sweep_finds_the_known_hand_read_fixtures():
    """Without this the sweep below passes vacuously if the shape test stops
    matching — which is how a discovery-based check quietly covers nothing."""
    names = {p.name for p in _hand_read_fixtures()}
    for expected in ("not_assessable_handread.json",
                     "diagnostic_ground_truth.json",
                     "diagnostic_confirmatory.json"):
        assert expected in names, (
            f"the hand-read sweep no longer finds {expected}. Either the fixture "
            f"changed shape or the detector is broken; found {sorted(names)}."
        )


def test_every_hand_read_fixture_states_what_was_read():
    """Per-row `source_span`, or a README beside it that states the span.

    Both forms are accepted because the two existing fixtures record it
    differently and neither is wrong: the 28 carry it per row because two rows
    were read differently from the other 26, while the diagnostic sets were all
    read the same way and say so once.
    """
    span_in_readme = re.compile(
        r"label from|read from|source span|title \+ abstract", re.IGNORECASE)

    for path in _hand_read_fixtures():
        rows = json.loads(path.read_text())
        per_row = all(str(r.get("source_span", "")).strip() for r in rows)
        if per_row:
            continue

        readmes = list(FIXTURES.glob(f"{path.stem.split('_confirmatory')[0]}*README*"))
        stated = any(span_in_readme.search(r.read_text()) for r in readmes)
        assert stated, (
            f"{path.name} records no source span — not per row, and no README "
            f"beside it states one (looked at {[r.name for r in readmes]}). A "
            "hand-read whose source is unknown cannot be audited for the "
            "excerpt-truncation failure that produced two wrong labels in "
            "not_assessable_handread.json. See docs/DECISIONS.md."
        )


def test_the_standing_rule_is_written_down_where_it_is_found():
    """The rule outlives this test file only if it is in the decision record."""
    text = (REPO / "docs" / "DECISIONS.md").read_text()
    assert "A hand-read is performed against the complete record" in text, (
        "docs/DECISIONS.md no longer states the complete-record rule. The two "
        "wrong labels it was written from are the evidence for it."
    )
    assert "NCT05700669" in text and "NCT06257758" in text, (
        "the rule no longer names the two records it was derived from; without "
        "them it reads as a generic instruction rather than a measured finding"
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
    print("\nall hand-read provenance checks passed" if not failures
          else f"\n{failures} failure(s)")
    raise SystemExit(1 if failures else 0)
