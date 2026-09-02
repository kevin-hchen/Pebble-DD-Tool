"""`FINGERPRINT_SOURCES` must cover everything a precomputed row depends on.

The version gate is what `CAPABILITIES.md` advertises as making the public
service refuse to start on a stale artifact. A file missing from the list is a
hole in exactly that promise: the artifact ships precomputed answers, the code
that produced them has changed, and the fingerprint matches anyway.

`medrag/trials/store.py` was missing, and the way it was missing is the reason
this file traces rather than reads imports. `build_landscape(store, ...)` takes
the store as a PARAMETER. No import of `trials/store.py` appears anywhere in
the landscape call graph — I checked, with an AST walk over the transitive
first-party closure from `precompute.py` and `landscape.py`, and it returns 11
modules with `trials/store.py` not among them. Every row nonetheless comes out
of that file's SQL. A static import check would have passed while the hole was
open, which makes it a check that only looks like one.

So the check RUNS a real `build_landscape` under `sys.settrace` and records
which files executed. Dependency injection, deferred imports inside functions,
and dispatch through an object all show up, because execution is the thing
being measured rather than a proxy for it.

What this cannot catch, stated rather than left implied:

  * a file that only executes on a code path this fixture does not reach — a
    marker with no matching trial, an error branch. The fixture is built to
    exercise the ordinary path end to end (screen, rank, count, coverage) for
    that reason, and `test_the_trace_actually_reached_the_interesting_modules`
    fails if the trace goes quiet, which is the vacuity guard.
  * a YAML config, which is data and never executes. Configs are listed by hand
    and checked here only for existence. The rule for them is the same and has
    to be applied by a human: if editing it changes a row, it belongs.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Direct runs do not load conftest.py, so the no-network guard is installed
# here too. See tests/netguard.py.
from tests import netguard  # noqa: E402

netguard.install()

from medrag.landscape import build_landscape  # noqa: E402
from medrag.precompute import FINGERPRINT_SOURCES, code_version  # noqa: E402
from medrag.trials.client import TrialRecord  # noqa: E402
from medrag.trials.store import TrialStore  # noqa: E402

REPO = Path(__file__).resolve().parents[1]

#: Files that execute during a landscape build but cannot change a row.
#: Documented individually, because "excluded" with no reason is how a real
#: dependency gets excused. Getting one of these wrong costs an unnecessary
#: rebuild; the dangerous direction — a real dependency omitted — is what the
#: trace makes impossible.
EXEMPT = {
    # Dataclass and config plumbing: `Config` carries paths and flags, and a
    # landscape row is not a function of any of them. Listed rather than
    # silently skipped so a future field that DOES affect a row is a visible
    # decision to remove this line.
    "medrag/config.py",
    # Provider selection. No model runs anywhere in a landscape build — the
    # whole point of the precompute is that it is deterministic SQL and Python.
    "medrag/providers.py",
    "medrag/__init__.py",
    "medrag/trials/__init__.py",
}


def _store() -> TrialStore:
    """A small store that exercises the whole ordinary path: an admitting
    trial, an excluded one, and one the screen has to read text for."""
    store = TrialStore(Path(tempfile.mkdtemp()) / "t.db")
    store.upsert(
        [
            TrialRecord(
                nct_id="NCT_MSS", brief_title="MSS colorectal study",
                overall_status="RECRUITING", phase="Phase 2",
                conditions=["Colorectal Neoplasms"], interventions=["Drug A"],
                intervention_types=["DRUG"], enrollment_count=200,
                allocation="RANDOMIZED", start_date="2025-01-01",
                eligibility_criteria=(
                    "Inclusion Criteria: Microsatellite stable (MSS) colorectal cancer. "
                    "Exclusion Criteria: prior therapy."),
                locations=[{"facility": "A", "city": "Boston", "state": "MA",
                            "country": "United States", "status": "", "contacts": []}],
            ),
            TrialRecord(
                nct_id="NCT_MSI", brief_title="MSI-H colorectal study",
                overall_status="RECRUITING", phase="Phase 3",
                conditions=["Colorectal Neoplasms"], interventions=["Drug B"],
                intervention_types=["DRUG"],
                eligibility_criteria=(
                    "Inclusion Criteria: MSI-H or dMMR tumours only."),
            ),
        ],
        set_key="colorectal",
        provenance={"NCT_MSS": ["cond:colorectal cancer"],
                    "NCT_MSI": ["cond:colorectal cancer"]},
    )
    return store


def _traced_files() -> set[str]:
    """Repo-relative `medrag/` files that execute during a real build."""
    seen: set[str] = set()

    def tracer(frame, event, arg):
        if event != "call":
            return None
        try:
            path = Path(frame.f_code.co_filename).resolve()
            rel = path.relative_to(REPO)
        except (ValueError, OSError):
            return None
        if rel.parts and rel.parts[0] == "medrag" and rel.suffix == ".py":
            seen.add(str(rel))
        return None

    store = _store()
    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        build_landscape(store, condition="colorectal", biomarker="MSS",
                        query_set="colorectal", use_precomputed=False)
    finally:
        sys.settrace(previous)
        store.close()
    return seen


def test_every_executed_module_is_fingerprinted():
    """The structural check. Adding a module to the landscape path without
    adding it to FINGERPRINT_SOURCES fails here."""
    executed = _traced_files()
    listed = set(FINGERPRINT_SOURCES)
    missing = sorted(executed - listed - EXEMPT)
    assert not missing, (
        "these files execute during a landscape build and are not in "
        f"FINGERPRINT_SOURCES: {missing}. Either they can change a precomputed "
        "row — add them — or they provably cannot, in which case add them to "
        "EXEMPT with the reason, because an unexplained exclusion is how a real "
        "dependency gets excused."
    )


def test_the_trace_actually_reached_the_interesting_modules():
    """Anti-vacuity. A trace that reached nothing would pass the test above.

    `trials/store.py` is named explicitly: it is the file the whole exercise was
    about, it is reached only through an injected object, and if the fixture
    ever stops touching it this check stops proving anything.
    """
    executed = _traced_files()
    for required in ("medrag/landscape.py", "medrag/trials/store.py",
                     "medrag/biomarker.py", "medrag/markers.py",
                     "medrag/ranking.py"):
        assert required in executed, (
            f"{required} did not execute — the fixture no longer exercises the "
            "path this test is supposed to be measuring"
        )


def test_the_injected_store_is_invisible_to_an_import_check():
    """Pins WHY this file traces, so nobody replaces it with a cheaper check.

    If `landscape.py` ever imports the store directly this test fails, and the
    honest response is to notice that an import check would then have been
    sufficient — not to delete the assertion.
    """
    source = (REPO / "medrag" / "landscape.py").read_text()
    for form in ("from .trials.store import", "from medrag.trials.store import",
                 "import medrag.trials.store"):
        assert form not in source, (
            "landscape.py now imports the trial store directly. The trace-based "
            "check still works, but the reasoning in this file's docstring — that "
            "no import-graph check could see the store — no longer holds and "
            "should be rewritten rather than left to mislead."
        )


def test_every_listed_source_exists():
    """A path typo silently weakens the fingerprint rather than breaking it:
    `code_version` folds a missing file in as the literal b"<absent>", so a
    renamed module keeps producing a stable hash while no longer being
    watched."""
    for rel in FINGERPRINT_SOURCES:
        assert (REPO / rel).exists(), f"{rel} is fingerprinted but does not exist"


def test_the_fingerprint_changes_when_a_listed_file_changes():
    """The mechanism, end to end, on the file that was missing."""
    target = REPO / "medrag" / "trials" / "store.py"
    before = code_version()
    original = target.read_bytes()
    try:
        target.write_bytes(original + b"\n# fingerprint probe\n")
        assert code_version() != before, \
            "editing a fingerprinted file did not change the fingerprint"
    finally:
        target.write_bytes(original)
    assert code_version() == before, "the probe was not cleanly reverted"


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
    print("\nall fingerprint tests passed" if not failures else f"\n{failures} failure(s)")
    raise SystemExit(1 if failures else 0)
