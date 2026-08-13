"""`docs/SCOPE.md` is the definition every build choice is checked against.

Until this file existed, that definition was held nowhere — not in the working
tree, not in git history on any branch, not in stash. A parity audit that needed
to ask "what is this tool supposed to cover" had to reconstruct the answer from
the owner's messages, and reconstructing a definition is how a definition drifts.

So it is pinned the same way the public terms of use are pinned by
`tests/test_public_app.py::test_the_shipped_terms_state_each_retention_claim`,
and for the same reason. The terms are a promise to a visitor; this is a promise
about what the tool is for. Either can be edited in one commit by someone who
does not know it was load-bearing, and in both cases the edit looks like a
wording change and is not.

What is asserted here is deliberately narrow: the HARD CONSTRAINTS and the
drugs-and-devices-equally line, verbatim. Not the whole document. The prose
around them should be free to improve; the constraints are the part that decides
whether a build choice is in scope, and a silent change to one of them is a
silent change to what the tool is.

Deliberately NOT asserted: that the tool currently SATISFIES these constraints.
It does not — a memo is 7 to 24 pages against a two-to-three page constraint,
and the device path is not equally gated. This file pins the target so the gap
stays measurable; a test that failed on the gap would have to be deleted or
weakened to ship anything, which is how a target gets quietly lowered to meet
the build instead of the other way round.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Direct runs do not load conftest.py, so the no-network guard is installed
# here too. See tests/netguard.py.
from tests import netguard  # noqa: E402

netguard.install()

SCOPE = Path(__file__).resolve().parents[1] / "docs" / "SCOPE.md"

#: The hard constraints, verbatim. Each is quoted exactly as the file states it,
#: including the emphasis marks — the bold is part of what was written down, and
#: a constraint quietly de-emphasised is a constraint on its way out.
HARD_CONSTRAINTS = (
    "**IPs cannot leave.**",
    "**100% accurate to what the study actually says.**",
    "**Every claim cited, and cited cleanly.**",
    "**Two to three pages maximum**",
    "Free to run.",
    "Usable by someone non-technical.",
)

#: The scope line this whole audit turns on. Quoted in full rather than as a
#: keyword, because "drugs and devices" appearing somewhere in the file would
#: also be satisfied by "drugs and devices, drugs first".
DRUGS_AND_DEVICES = (
    "**Drugs and devices equally.** Not one primary and the other secondary. "
    "Both\npaths must be equally real, equally tested, equally gated."
)


def test_the_scope_file_exists():
    """It did not, which is the whole reason for this file."""
    assert SCOPE.exists(), (
        f"{SCOPE} is missing. It is the definition build choices are checked "
        "against; without it there is nothing to check them against."
    )


def test_every_hard_constraint_is_stated_verbatim():
    text = SCOPE.read_text()
    for constraint in HARD_CONSTRAINTS:
        assert constraint in text, (
            f"docs/SCOPE.md no longer states the hard constraint {constraint!r}. "
            "If this was deliberate, the owner changed what the tool is for and this "
            "test should be updated in the same commit; if it was not, restore it."
        )


def test_the_drugs_and_devices_line_is_stated_verbatim():
    """The single line the device-parity work is measured against.

    Weakening it to "drugs and devices" without "equally", or to "both paths
    must be real" without "equally tested, equally gated", would retire the
    standard while leaving the sentence looking intact.
    """
    text = SCOPE.read_text()
    assert DRUGS_AND_DEVICES in text, (
        "docs/SCOPE.md no longer states, verbatim, that drugs and devices are "
        "screened equally and that both paths must be equally real, tested and gated"
    )


def test_the_recurring_failure_is_named_with_its_three_examples():
    """The three worked examples are the evidence for the rule, and the rule
    reads as an abstraction without them: colorectal was one disease of 74, the
    bilirubin monitor one device of many, drugs one half of the domain."""
    text = SCOPE.read_text()
    for example in ("Colorectal cancer was", "neonatal bilirubin monitor is one device",
                    "Drugs are one half of the domain"):
        assert example in text, f"docs/SCOPE.md no longer names the example: {example!r}"


def test_the_public_requirements_state_the_unconditional_one():
    """"It cannot fail in the public eye" is the constraint that outranks the
    others when they conflict, so it is pinned separately."""
    assert "**It cannot fail in the public eye.**" in SCOPE.read_text()


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
    print("\nall scope tests passed" if not failures else f"\n{failures} failure(s)")
    raise SystemExit(1 if failures else 0)
