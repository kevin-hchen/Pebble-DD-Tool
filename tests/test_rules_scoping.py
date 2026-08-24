"""A path-scoped rule that matches nothing is worse than no rule at all.

`.claude/rules/*.md` files carry `paths:` frontmatter. Claude Code loads them
only when it reads a file matching one of the globs, so an invariant moved out
of `CLAUDE.md` and into a rule scoped to a path that no longer exists is
**silently absent**: nothing errors, nothing warns, and the guard is simply
gone. That failure mode is invisible in exactly the way this repository's
whole discipline is written against — a renamed module or a moved config file
retires the rule without anyone deciding to.

So: every glob in every rule must match at least one file that exists today,
and every rule file must be tracked by git. An untracked rule is the same
failure at a different layer — it works perfectly on the machine of whoever
wrote it and does not exist for anybody else, so the invariant is enforced for
one person and silently absent for the rest of the team and for CI.

The second check is the one that keeps the split honest rather than merely
working. `CLAUDE.md` promises that a rule may live in `.claude/rules/` only if
it cannot be violated from outside its own glob. A rule file that names a module
outside its own scope is either mis-scoped or holds an invariant that belonged
in the always-loaded core, so the file must state which files it applies to and
must not reach past them.

Run: python -m pytest tests/test_rules_scoping.py -q
     (or: python tests/test_rules_scoping.py)
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Direct runs do not load conftest.py, so the no-network guard is installed
# here too. See tests/netguard.py.
from tests import netguard  # noqa: E402

netguard.install()

REPO = Path(__file__).resolve().parents[1]
RULES_DIR = REPO / ".claude" / "rules"


def _rule_files() -> list[Path]:
    return sorted(RULES_DIR.rglob("*.md"))


def _paths_frontmatter(text: str) -> list[str] | None:
    """Return the `paths:` globs, or None if the file has no frontmatter.

    Parsed by hand rather than with PyYAML so this test states the exact
    on-disk shape Claude Code was verified to read: a `---` fence as the very
    first line, a `paths:` key, and `- "glob"` list items.
    """
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 3)
    if end == -1:
        return None
    front = text[4:end + 1]
    if not re.search(r"^paths:\s*$", front, re.MULTILINE):
        return None
    return re.findall(r'^\s*-\s*"([^"]+)"\s*$', front, re.MULTILINE)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(("git", *args), cwd=REPO, capture_output=True, text=True)


def _in_a_git_work_tree() -> bool:
    return (REPO / ".git").exists() and _git("rev-parse", "--git-dir").returncode == 0


def test_every_rule_file_is_tracked_by_git():
    """A rule that is not in the repository is enforced for exactly one person.

    `.claude/rules/` is easy to leave untracked: it is a dot-directory, it sits
    beside `settings.local.json` which IS conventionally ignored, and nothing in
    a normal test run touches git. The rule then works on the author's machine
    and is simply absent for every teammate and every CI run — the same silent
    absence as a glob that matches nothing, one layer down.
    """
    if not _in_a_git_work_tree():
        # A source export with no .git cannot answer the question. Say so rather
        # than passing: a check that quietly succeeds when it could not run is
        # the vacuous-coverage failure this suite guards against elsewhere.
        import pytest

        pytest.skip("not a git work tree, so tracked-ness cannot be checked here")

    untracked = []
    for path in _rule_files():
        rel = path.relative_to(REPO).as_posix()
        if _git("ls-files", "--error-unmatch", rel).returncode != 0:
            untracked.append(rel)

    assert not untracked, (
        f"rule files not tracked by git: {untracked}. They load only on the "
        "machine they were written on, so the invariants they hold are absent "
        "for everyone else. Run `git add .claude/rules/`."
    )


def test_no_rule_file_is_hidden_by_a_gitignore():
    """Tracked beats ignored, so this is about the NEXT rule file, not these.

    If something ignores `.claude/**`, a rule added later is silently not staged
    by `git add` and nobody notices until an invariant goes missing. The ignore
    may live in the repo's .gitignore, in `.git/info/exclude`, or in the
    author's global config — this reports whichever matched, which is why it
    uses `check-ignore -v` rather than just its exit code.
    """
    if not _in_a_git_work_tree():
        import pytest

        pytest.skip("not a git work tree, so ignore rules cannot be checked here")

    rules_dir = RULES_DIR.relative_to(REPO).as_posix()
    probe = f"{rules_dir}/a-rule-added-later.md"
    result = _git("check-ignore", "-v", "--no-index", probe)
    assert result.returncode != 0, (
        f"a new file in {rules_dir}/ would be ignored by: {result.stdout.strip()!r}. "
        "`git add` would silently skip it, so the next invariant moved out of "
        "CLAUDE.md would be enforced for nobody."
    )


def test_the_git_checks_are_not_vacuous():
    """Both checks above pass if `_git` always returns success, and skip if
    `_in_a_git_work_tree` is wrong. Drive each against a known answer."""
    if not _in_a_git_work_tree():
        import pytest

        pytest.skip("not a git work tree")

    assert _git("ls-files", "--error-unmatch", "CLAUDE.md").returncode == 0, (
        "git reports a file known to be tracked as untracked"
    )
    assert _git("ls-files", "--error-unmatch", "no_such_file_xyz.md").returncode != 0, (
        "git reports a nonexistent file as tracked, so the check cannot fail"
    )


def test_there_are_rule_files_to_check():
    """Without this the sweep below passes vacuously on an empty directory —
    which is also what it would do if the rules were deleted."""
    assert RULES_DIR.is_dir(), (
        f"{RULES_DIR} is missing. CLAUDE.md was trimmed on the promise that the "
        "scoped invariants live there."
    )
    assert _rule_files(), f"no rule files in {RULES_DIR}"


def test_every_rule_declares_the_paths_it_is_scoped_to():
    """A rule with no `paths:` loads unconditionally, which silently undoes the
    split: the content goes back to costing every session, but it now lives
    somewhere nobody reading CLAUDE.md will look."""
    for path in _rule_files():
        globs = _paths_frontmatter(path.read_text())
        assert globs is not None, (
            f"{path.relative_to(REPO)} has no `paths:` frontmatter, so it loads in "
            "every session. Either scope it, or move its content back into "
            "CLAUDE.md where the always-loaded invariants are reviewed together."
        )
        assert globs, f"{path.relative_to(REPO)} declares `paths:` with no globs"


def test_every_paths_glob_matches_a_file_that_exists():
    """The check this file exists for.

    A rule scoped to `medrag/fda/**` after `fda/` is renamed does not warn, does
    not error, and never loads. The invariant is gone and the file that held it
    still looks fine.
    """
    stale = []
    for path in _rule_files():
        for glob in _paths_frontmatter(path.read_text()) or []:
            if not any(REPO.glob(glob)):
                stale.append(f"{path.relative_to(REPO)} → {glob!r}")

    assert not stale, (
        "path-scoped rules whose globs match nothing in the repository: "
        f"{stale}. Such a rule NEVER loads, so the invariant it holds is "
        "silently unenforced. Either fix the glob or move the invariant back "
        "into CLAUDE.md."
    )


def test_the_glob_check_would_actually_catch_a_stale_path():
    """The sweep above passes if `_paths_frontmatter` silently returns [] for
    every file. Drive the matcher directly on a path that cannot exist."""
    assert not any(REPO.glob("medrag/no_such_package/**")), (
        "the glob matcher reports matches for a directory that does not exist"
    )
    assert any(REPO.glob("medrag/fda/**")), (
        "the glob matcher finds nothing under a directory that does exist"
    )


def test_no_rule_reaches_past_its_own_scope():
    """The rule for what may be scoped, checked rather than trusted.

    A scoped rule may only hold invariants that cannot be violated from outside
    its glob. If a rule file instructs the reader about a module it is not
    scoped to, that invariant fires only when someone is already in the right
    place — and is silent for the person who would actually break it.

    Every rule file carries a `## What stays in CLAUDE.md` section, whose whole
    job is to name the out-of-scope modules that are deliberately NOT covered
    here. That section is excluded from the check; everything before it is the
    instructive part and must stay inside the glob.
    """
    # Modules whose names appearing as an instruction would mean the rule is
    # constraining code it is not loaded for.
    watched = ("memo.py", "diligence.py", "claims.py", "landscape_memo.py",
               "table_render.py", "app.py", "context.py", "validation.py")

    offenders = []
    for path in _rule_files():
        body = path.read_text()
        marker = "## What stays in CLAUDE.md"
        assert marker in body, (
            f"{path.relative_to(REPO)} has no {marker!r} section. Every scoped rule "
            "states what it deliberately does not cover, so the split is legible "
            "from inside the rule as well as from CLAUDE.md."
        )
        # Everything before that section is instructive and must stay in scope;
        # the section itself exists to name out-of-scope modules.
        instructive = body.split(marker)[0]
        for module in watched:
            if module in instructive:
                offenders.append(f"{path.relative_to(REPO)} instructs about {module}")

    assert not offenders, (
        f"{offenders}. A scoped rule that constrains a module outside its own "
        "`paths:` is silent for whoever edits that module. Move the invariant "
        "back into CLAUDE.md."
    )


def test_the_core_file_says_the_scoped_rules_exist():
    """A reader of CLAUDE.md must be able to tell that some invariants are held
    elsewhere, or the always-loaded file reads as the complete list."""
    text = (REPO / "CLAUDE.md").read_text()
    assert ".claude/rules/" in text, (
        "CLAUDE.md does not mention .claude/rules/, so it reads as the complete "
        "set of invariants when it is not"
    )
    for path in _rule_files():
        assert path.name in text, (
            f"{path.name} is not named in CLAUDE.md. Every scoped rule is listed "
            "there so a reader knows what is held out of the always-loaded core."
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
    print("\nall rule-scoping checks passed" if not failures else f"\n{failures} failure(s)")
    raise SystemExit(1 if failures else 0)
