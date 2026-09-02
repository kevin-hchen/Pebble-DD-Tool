"""`CLAUDE.md` is read in full at the start of every session, so its size is a
tax on every task in this repository.

It had reached 2,405 lines and 155 KB — a measurable share of why sessions ran
out of context mid-task. The trim moved the long-form reasoning, the
measurements, and the account of how each defect was found into
`docs/RATIONALE.md`, verbatim, and left `CLAUDE.md` holding the invariants plus
one sentence of why each.

Nothing was deleted, and that is the property this file protects. Three checks:

  * the line budget, so growth goes to `RATIONALE.md` rather than back here;
  * every test `CLAUDE.md` points at by name actually exists, so "→ test_x"
    cannot rot into a promise nothing keeps — the trim replaced a lot of prose
    with those pointers, and a pointer to a deleted test is worse than the
    prose was;
  * `RATIONALE.md` still holds every section `CLAUDE.md` sends a reader to.

The budget is set a little above where the trim landed, not at it: a test that
fails on one added line trains people to raise the number rather than think
about where the line belongs.

Run: python -m pytest tests/test_claude_md.py -q  (or: python tests/test_claude_md.py)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Direct runs do not load conftest.py, so the no-network guard is installed
# here too. See tests/netguard.py.
from tests import netguard  # noqa: E402

netguard.install()

REPO = Path(__file__).resolve().parents[1]
CLAUDE_MD = REPO / "CLAUDE.md"
RATIONALE = REPO / "docs" / "RATIONALE.md"
TESTS_DIR = REPO / "tests"

#: Lines, counting only what loads in EVERY session. The trim landed at 544
#: after the path-scoped split; headroom is for an invariant a future change
#: genuinely adds, not for narrative moving back in.
#:
#: Note what this does NOT count: `.claude/rules/*.md` with `paths:` frontmatter
#: load only when Claude reads a matching file. tests/test_rules_scoping.py
#: fails if a rule loses its frontmatter, which is the way that split could
#: silently put content back into every session without this number moving.
MAX_LINES = 580


def _test_names_defined_in_the_suite() -> set[str]:
    names: set[str] = set()
    for path in TESTS_DIR.glob("test_*.py"):
        names |= set(re.findall(r"^\s*def (test_\w+)", path.read_text(), re.MULTILINE))
    return names


def test_claude_md_stays_within_its_line_budget():
    """Every session pays for this file before it does any work.

    If this fails because an invariant was added, add it and raise the budget in
    the same commit. If it fails because reasoning, a measurement, or the story
    of a bug crept back in, that belongs in docs/RATIONALE.md instead.
    """
    lines = CLAUDE_MD.read_text().count("\n") + 1
    assert lines <= MAX_LINES, (
        f"CLAUDE.md is {lines} lines, over its {MAX_LINES}-line budget. It is read "
        "in full at the start of every session. Long-form reasoning, measurements "
        "and the history of a bug go in docs/RATIONALE.md; only the invariant and "
        "one sentence of why belong here."
    )


def test_the_rationale_file_exists_and_is_where_the_long_form_went():
    """A pointer to a file nobody wrote is worse than the prose it replaced."""
    assert RATIONALE.exists(), (
        "docs/RATIONALE.md is missing. CLAUDE.md was trimmed on the promise that "
        "the reasoning moved there rather than being deleted."
    )
    assert "docs/RATIONALE.md" in CLAUDE_MD.read_text(), (
        "CLAUDE.md no longer points at docs/RATIONALE.md, so a reader has no way "
        "to reach the reasoning behind the invariants it states"
    )
    # The trim is only honest if the long form is actually longer than the
    # summary. If RATIONALE.md ever shrinks below CLAUDE.md, something was
    # dropped rather than moved.
    assert RATIONALE.read_text().count("\n") > CLAUDE_MD.read_text().count("\n"), (
        "docs/RATIONALE.md is now shorter than CLAUDE.md — the long-form record "
        "has been trimmed too, which is the deletion this arrangement exists to "
        "prevent"
    )


def test_every_rationale_section_claude_md_cites_actually_exists():
    """CLAUDE.md replaced whole paragraphs with 'RATIONALE §N'."""
    cited = {int(n) for n in re.findall(r"RATIONALE §(\d+)", CLAUDE_MD.read_text())}
    assert cited, "CLAUDE.md cites no RATIONALE sections at all — check the citation format"

    present = {int(n) for n in re.findall(r"^## (\d+)\.", RATIONALE.read_text(), re.MULTILINE)}
    missing = sorted(cited - present)
    assert not missing, (
        f"CLAUDE.md sends readers to RATIONALE sections that do not exist: {missing}. "
        f"docs/RATIONALE.md currently defines {sorted(present)}."
    )


def test_every_test_claude_md_names_exists_in_the_suite():
    """The trim's largest category was 'becomes a test, and CLAUDE.md points at
    the test by name'. That is only better than prose while the name resolves.

    Names are matched against every `def test_*` in tests/, not against a list,
    so a renamed test fails here rather than leaving a dangling pointer.
    """
    text = CLAUDE_MD.read_text()
    # Only the backticked references are pointers; prose like "a test that
    # renders an artefact" is not, and neither are file paths.
    cited = set(re.findall(r"`(test_\w+)`", text))
    assert len(cited) > 20, (
        f"CLAUDE.md names only {len(cited)} tests. The trim traded prose for "
        "pointers; if the pointers are gone the invariants are unenforced again."
    )

    defined = _test_names_defined_in_the_suite()
    missing = sorted(cited - defined)
    assert not missing, (
        f"CLAUDE.md points at tests that do not exist: {missing}. Either the test "
        "was renamed (update CLAUDE.md in the same commit) or it was deleted, in "
        "which case the invariant it pinned is now unenforced and the prose has to "
        "come back."
    )


def test_the_name_check_would_actually_catch_a_dangling_pointer():
    """The check above passes if `cited` is empty for a formatting reason, and
    passes if `defined` accidentally contains everything. Drive both halves."""
    defined = _test_names_defined_in_the_suite()
    assert "test_claude_md_stays_within_its_line_budget" in defined, (
        "the suite scan does not find a test defined in this very file"
    )
    assert "test_a_name_no_test_in_this_repo_uses" not in defined, (
        "the suite scan matches names that are not defined anywhere"
    )


def test_the_master_rule_is_still_stated_in_claude_md():
    """One rule the whole file is an application of, and the one that has been a
    real bug more than once. It survived the trim by being promoted to its own
    section; a future trim must not fold it back into a bullet."""
    # Whitespace-normalised: these are prose, and where a sentence happens to
    # wrap is not part of the rule. A pin that breaks on reflowing a paragraph
    # gets "fixed" by weakening the pin.
    text = " ".join(CLAUDE_MD.read_text().replace("*", "").split())
    for phrase in (
        '"Not assessed", "nothing found" and "found against" are three different',
        "absence is never rendered as a negative finding",
        "a caveat must not contain the claim it denies",
    ):
        assert phrase in text, (
            f"CLAUDE.md no longer states: {phrase!r}. This is the rule the rest of "
            "the file applies; it is not a candidate for the rationale file."
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
    print("\nall CLAUDE.md pins passed" if not failures else f"\n{failures} failure(s)")
    raise SystemExit(1 if failures else 0)
