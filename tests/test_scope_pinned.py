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
It does not — the device path is not equally gated. This file pins the target so
the gap stays measurable; a test that failed on the gap would have to be deleted
or weakened to ship anything, which is how a target gets quietly lowered to meet
the build instead of the other way round.

The page-length constraint is the worked example of the alternative, and of why
this file has a `RELEASED_CONSTRAINTS` list. It was released by the owner on
13 August 2026 rather than met, and the release is pinned as firmly as the
constraint was: the file must still name it, must carry the date, and must carry
the measurement it was decided against. A shorter constraint list on its own
cannot be told apart from someone deleting the line their build could not
satisfy.
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
    "Free to run.",
    "Usable by someone non-technical.",
)

#: A constraint the owner RELEASED, pinned as deliberately as it was pinned when
#: it was in force.
#:
#: "Two to three pages maximum" was a hard constraint until 13 August 2026. It
#: is not in HARD_CONSTRAINTS any more, and without this a future reader finds a
#: shorter list and cannot tell whether the line was decided away or quietly
#: dropped to make a failing test pass — which is exactly what
#: `test_every_hard_constraint_is_stated_verbatim`'s own failure message warns
#: about ("if this was deliberate, the owner changed what the tool is for and
#: this test should be updated in the same commit").
#:
#: So the release is now the thing under test: the file must SAY it was
#: released, must carry the date, and must carry the measurement the decision
#: was taken against. A silent re-tightening fails here too, which is the
#: property that makes this a pin rather than a deletion.
RELEASED_CONSTRAINTS = (
    ("Two to three pages maximum", "RELEASED 13 August 2026"),
)

#: The numbers the release was decided against. Pinned because a released
#: constraint with no measurement beside it is indistinguishable from one that
#: was found inconvenient.
RELEASE_EVIDENCE = (
    "**7 pages when nothing is found**",
    "**11 for a well-evidenced device**",
    "**24 for a well-evidenced drug**",
    "evidence blocks 46.3%",
    "source lists 26.4%",
    "headers and coverage 21.0%",
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


def test_a_released_constraint_says_so_rather_than_vanishing():
    """A shorter constraint list is ambiguous; a stated release is not.

    Pinning the release is what distinguishes "the owner decided this" from
    "someone deleted the line the build could not meet". Both produce a file
    without the constraint in it; only one of them is honest.
    """
    text = SCOPE.read_text()
    for constraint, marker in RELEASED_CONSTRAINTS:
        assert constraint in text, (
            f"docs/SCOPE.md no longer mentions {constraint!r} at all. A released "
            "constraint must remain visible with its release recorded — deleting it "
            "outright loses the fact that it was ever in force."
        )
        assert marker in text, (
            f"{constraint!r} appears in docs/SCOPE.md without {marker!r}. It must be "
            "readable as a decision with a date, not as a constraint that is simply "
            "no longer being met."
        )
        assert constraint not in "".join(HARD_CONSTRAINTS), (
            f"{constraint!r} is both released and still pinned as a hard constraint"
        )


def test_the_release_carries_the_measurement_it_was_decided_against():
    """A released constraint with no numbers beside it reads as a constraint
    that was found inconvenient."""
    text = SCOPE.read_text()
    for fact in RELEASE_EVIDENCE:
        assert fact in text, (
            f"docs/SCOPE.md no longer records {fact!r}. The page-length release was "
            "taken against a specific measurement, and dropping it leaves a future "
            "reader unable to tell what state the decision was made in."
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
