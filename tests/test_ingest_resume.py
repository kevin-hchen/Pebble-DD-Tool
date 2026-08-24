"""Tests for the ingest lifecycle (begin_ingest / record_coverage / the status
column) — the guard against a population that stopped growing because the
process died.

Every other completeness guard in this codebase fires on a RESPONSE. `run_query`
raises `IncompleteFetch` when pagination yields fewer studies than the registry's
own `countTotal`; `CoverageReport.errors` records a query that threw. None of
them can fire when the process is killed, because a killed process raises
nothing — the fetch simply stops, and a family holding 6,000 of 12,000 studies
sits in the store looking exactly like a finished one. That is the failure this
file exists to pin.

The properties, in the order they matter:

  1. A run interrupted after records are written but before the ingest is
     verified leaves a visible IN_PROGRESS marker, not a plausible lie.
  2. No surface renders such a family as a complete census — the coverage line
     says "PARTIAL INGEST", states N of M, and names the command that finishes
     it.
  3. COMPLETE is reachable only by counting the store back and checking it
     against the registry-reported total of every query in the set. Test 3 is
     what keeps tests 1 and 2 from passing vacuously: if nothing could ever be
     COMPLETE, "not complete" would prove nothing.
  4. `--max-records` truncation grades PARTIAL. This is the same silent-subset
     failure reached through a documented flag rather than a crash, and the old
     `CoverageReport.complete` — which looked only for errors — called it done.
  5. A pre-lifecycle (v8) database is re-graded from the numbers it already
     holds, and only a row whose own numbers prove completeness may be called
     COMPLETE.

No network: tests/netguard.py blocks sockets.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import netguard  # noqa: E402

netguard.install()

from medrag.coverage import build_coverage_statement, render_lines  # noqa: E402
from medrag.trials.client import TrialRecord  # noqa: E402
from medrag.trials.queries import (  # noqa: E402
    CONDITION,
    TERM,
    CoverageReport,
    QuerySet,
    QueryYield,
    TrialQuery,
)
from medrag.trials.store import (  # noqa: E402
    INGEST_COMPLETE,
    INGEST_IN_PROGRESS,
    INGEST_PARTIAL,
    STORE_VERSION,
    TrialStore,
    migrate_derived_columns,
    verify_ingest,
)

SET = QuerySet(
    key="colorectal", label="Colorectal cancer",
    queries=(TrialQuery(CONDITION, "colorectal cancer"), TrialQuery(TERM, "MSS colorectal")),
)


def _db() -> Path:
    return Path(tempfile.mkdtemp()) / "trials.db"


def _trials(n, start=1):
    return [
        TrialRecord(nct_id=f"NCT{i:05d}", brief_title=f"Trial {i}",
                    overall_status="RECRUITING", conditions=["Colorectal Cancer"],
                    eligibility_criteria="Inclusion Criteria:\n* MSS")
        for i in range(start, start + n)
    ]


def _report(n_unique, fetched, reported, term_fetched=None, term_reported=None, errors=()):
    """A CoverageReport shaped like one real fetch of SET."""
    term_fetched = n_unique if term_fetched is None else term_fetched
    term_reported = term_fetched if term_reported is None else term_reported
    return CoverageReport(
        set_key=SET.key, set_label=SET.label, curated=True,
        yields=[
            QueryYield(query=SET.queries[0], reported_total=reported, fetched=fetched,
                       new=fetched),
            QueryYield(query=SET.queries[1], reported_total=term_reported,
                       fetched=term_fetched, new=0),
        ],
        total_unique=n_unique,
        errors=list(errors),
    )


# ---------------------------------------------------------------- the crash


def test_a_run_killed_after_writing_records_leaves_an_in_progress_marker():
    """The crash case, reproduced exactly: the marker is written, the records
    land, and the process dies before anything verifies them. Nothing raises —
    which is the whole problem — so the only evidence must be the marker."""
    store = TrialStore(_db())
    store.begin_ingest(SET)
    store.upsert(_trials(6), set_key=SET.key)
    # No record_coverage: this is where the kill lands.

    cov = store.coverage(SET.key)
    assert cov is not None, "the family must be visible, not absent"
    assert cov["status"] == INGEST_IN_PROGRESS
    assert not cov["verified_complete"]
    assert store.count(query_set=SET.key) == 6, "the records really are in the store"
    assert [d["set_key"] for d in store.incomplete_sets()] == [SET.key]
    store.close()


def test_the_coverage_line_for_an_interrupted_ingest_says_partial_and_how_to_finish_it():
    """Property 2. The store knowing is not enough — a number nobody is warned
    about is read as a census."""
    store = TrialStore(_db())
    store.begin_ingest(SET)
    store.upsert(_trials(6), set_key=SET.key)
    # A prior complete run had recorded 12; the interrupted re-fetch kept those
    # numbers, so the line can state a denominator instead of only a count.
    store.conn.execute("UPDATE query_coverage SET total_unique = 12 WHERE set_key = ?",
                       (SET.key,))
    store.conn.commit()

    cs = build_coverage_statement(store, SET.key)
    text = " ".join(render_lines(cs))

    assert not cs.complete
    assert cs.partial_ingest
    assert "PARTIAL INGEST" in text
    assert "6 of 12 studies" in text, text
    assert "LOWER BOUND" in text
    assert 'python -m medrag trials --condition "colorectal"' in text
    store.close()


def test_an_interrupted_ingest_is_never_rendered_as_a_complete_census():
    """The negative form of the same property, checked against the words the
    complete branch actually uses — so a reworded complete line cannot start
    leaking into the partial one unnoticed."""
    store = TrialStore(_db())
    store.begin_ingest(SET)
    store.upsert(_trials(6), set_key=SET.key)

    text = " ".join(render_lines(build_coverage_statement(store, SET.key)))
    for forbidden in ("fetched to its full registry-reported total", "none capped",
                      "6 of 6 studies"):
        assert forbidden not in text, f"an unverified ingest rendered {forbidden!r}"
    store.close()


# ------------------------------------------------------- what clears it


def test_a_verified_ingest_is_marked_complete_and_reads_as_a_census():
    """Property 3, and the anti-vacuity guard for everything above: COMPLETE is
    reachable, so 'not complete' elsewhere is a real distinction."""
    store = TrialStore(_db())
    store.begin_ingest(SET)
    store.upsert(_trials(6), set_key=SET.key)
    outcome = store.record_coverage(_report(n_unique=6, fetched=6, reported=6))

    assert outcome.status == INGEST_COMPLETE, outcome.reasons
    assert outcome.reasons == []
    assert store.coverage(SET.key)["verified_complete"]
    assert store.incomplete_sets() == []

    cs = build_coverage_statement(store, SET.key)
    text = " ".join(render_lines(cs))
    assert cs.complete and not cs.partial_ingest
    assert "PARTIAL INGEST" not in text
    assert "6 of 6 studies" in text
    store.close()


def test_completion_is_decided_by_counting_the_store_not_by_trusting_the_report():
    """`record_coverage` counts the database rather than believing the number
    handed to it. A report claiming 12 unique trials over a store holding 6 is
    exactly the shape a lost write produces, and it must not grade COMPLETE."""
    store = TrialStore(_db())
    store.begin_ingest(SET)
    store.upsert(_trials(6), set_key=SET.key)
    outcome = store.record_coverage(_report(n_unique=12, fetched=12, reported=12))

    assert outcome.status == INGEST_PARTIAL
    assert outcome.held == 6
    assert any("holds 6" in r for r in outcome.reasons), outcome.reasons
    store.close()


def test_a_capped_fetch_is_not_complete_even_though_no_query_errored():
    """Property 4. `--max-records` suppresses `IncompleteFetch` by design, so
    nothing raises and no query records an error — the old completeness test
    (errors only) called this finished."""
    store = TrialStore(_db())
    store.begin_ingest(SET)
    store.upsert(_trials(200), set_key=SET.key)
    outcome = store.record_coverage(
        _report(n_unique=200, fetched=200, reported=10195,
                term_fetched=200, term_reported=391))

    assert outcome.status == INGEST_PARTIAL
    assert any("200 of 10,195" in r for r in outcome.reasons), outcome.reasons

    text = " ".join(render_lines(build_coverage_statement(store, SET.key)))
    assert "PARTIAL INGEST" in text
    store.close()


def test_a_query_with_no_recorded_registry_total_cannot_grade_complete():
    """Nothing on file to check against is not the same as checked and passed —
    the not-assessed-vs-nothing-found rule, at the query level."""
    store = TrialStore(_db())
    store.upsert(_trials(6), set_key=SET.key)
    outcome = store.record_coverage(_report(n_unique=6, fetched=6, reported=None))

    assert outcome.status == INGEST_PARTIAL
    assert any("no registry total" in r for r in outcome.reasons), outcome.reasons
    store.close()


def test_re_fetching_a_complete_family_knocks_it_back_to_in_progress_first():
    """A crash during a RE-fetch is the same failure as a crash during a first
    fetch: the moment new records start landing, the old recorded total stops
    describing the store. The previously recorded numbers survive, so the line
    can still state a denominator."""
    store = TrialStore(_db())
    store.begin_ingest(SET)
    store.upsert(_trials(6), set_key=SET.key)
    store.record_coverage(_report(n_unique=6, fetched=6, reported=6))
    assert store.coverage(SET.key)["status"] == INGEST_COMPLETE

    store.begin_ingest(SET)
    cov = store.coverage(SET.key)
    assert cov["status"] == INGEST_IN_PROGRESS
    assert cov["total_unique"] == 6, "the earlier numbers are kept, not erased"
    assert store.incomplete_sets(), "a re-fetch in flight is not a complete family"
    store.close()


def test_a_family_never_started_is_absent_rather_than_incomplete():
    """Never searched, searched-but-unfinished and searched-and-complete are
    three states. `incomplete_sets` reports the middle one only; a family with
    no row is not silently promoted into the backlog, and `coverage` returning
    None is what renders as 'this tool has not looked'."""
    store = TrialStore(_db())
    store.begin_ingest(SET)
    store.upsert(_trials(6), set_key=SET.key)
    store.record_coverage(_report(n_unique=6, fetched=6, reported=6))

    assert store.coverage("nsclc") is None
    assert [d["set_key"] for d in store.incomplete_sets()] == []
    assert not build_coverage_statement(store, "nsclc").ever_ingested
    store.close()


def test_incomplete_sets_lists_unfinished_families_before_finished_ones():
    """The resume order. Ranking finished families first would bury the ones
    the operator opened the list to find."""
    store = TrialStore(_db())
    store.begin_ingest(SET)
    store.upsert(_trials(6), set_key=SET.key)
    store.record_coverage(_report(n_unique=6, fetched=6, reported=6))

    other = QuerySet(key="nsclc", label="NSCLC",
                     queries=(TrialQuery(CONDITION, "non-small cell lung cancer"),))
    store.begin_ingest(other)
    store.upsert(_trials(3, start=900), set_key=other.key)

    states = store.ingest_states()
    assert [s["set_key"] for s in states] == ["nsclc", "colorectal"]
    assert [s["set_key"] for s in store.incomplete_sets()] == ["nsclc"]
    store.close()


# ------------------------------------------------------ the v8 backfill


def _v8_store(path: Path, *, fetched: int, reported: int, n_records: int):
    """A database in the pre-lifecycle shape: the v9 columns absent, a coverage
    row already written, and `user_version` set back to 8."""
    store = TrialStore(path)
    store.upsert(_trials(n_records), set_key=SET.key)
    store.record_coverage(_report(n_unique=n_records, fetched=fetched, reported=reported))
    store.close()

    conn = sqlite3.connect(str(path))
    with conn:
        # SQLite cannot drop a column on old versions; blanking them reproduces
        # what a v8 file holds, which is what the migration must read.
        conn.execute("UPDATE query_coverage SET status = NULL, held = NULL, started_at = NULL")
        conn.execute("PRAGMA user_version = 8")
    conn.close()


def test_the_backfill_grades_a_provably_complete_v8_family_complete():
    path = _db()
    _v8_store(path, fetched=6, reported=6, n_records=6)

    result = migrate_derived_columns(path)
    assert result["migrated"] and result["from_version"] == 8
    assert result["graded"] == [(SET.key, INGEST_COMPLETE)]

    store = TrialStore(path)
    assert store.coverage(SET.key)["verified_complete"]
    assert store.incomplete_sets() == []
    store.close()


def test_the_backfill_grades_a_v8_family_whose_own_numbers_fall_short_partial():
    """The measurement that decides a re-run has to come from the row itself.
    A query recorded as fetching 200 of 10,195 was never complete, whatever
    the absence of an error suggests."""
    path = _db()
    _v8_store(path, fetched=200, reported=10195, n_records=200)

    assert migrate_derived_columns(path)["graded"] == [(SET.key, INGEST_PARTIAL)]

    store = TrialStore(path)
    text = " ".join(render_lines(build_coverage_statement(store, SET.key)))
    assert "PARTIAL INGEST" in text
    store.close()


def test_the_backfill_does_not_re_derive_columns_an_v8_file_already_has():
    """A migration step must not run when its own version gap is already closed.

    v8 already holds correct `intervention_tokens`, so recomputing 15,000 blobs
    is wasted work and is how two steps start overwriting each other. The
    biomarker census is a different step with a different gap — v13 moved it —
    and it SHOULD run here, which the assertions below keep apart."""
    path = _db()
    _v8_store(path, fetched=6, reported=6, n_records=6)

    result = migrate_derived_columns(path)
    assert result["token_rows"] == 0, (
        "no intervention_tokens blob should be rewritten for a v8 file — that is "
        "the step whose version gap is already closed"
    )
    # The census IS recomputed, and must be: v13 changed the matcher by adding
    # NOT_ASSESSABLE, so a stored census computed under the old rules would
    # disagree with the live screen. That is the divergence the parity gate
    # exists to catch, so this asserts the recompute happened rather than
    # asserting the migration did nothing.
    assert result["census_rows"] == 6, result

    conn = sqlite3.connect(str(path))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == STORE_VERSION
    conn.close()


def test_a_store_that_is_already_current_is_left_alone():
    path = _db()
    store = TrialStore(path)
    store.close()
    result = migrate_derived_columns(path)
    assert not result["migrated"] and result["reason"] == "already current"


# ------------------------------------------------- the shared verifier


def test_one_verifier_decides_completeness_for_both_the_live_ingest_and_the_backfill():
    """`verify_ingest` is the single implementation, so a rule added to the live
    path cannot go missing from the backfill — the drift that put the marker
    vocabulary in markers.py after biomarker.py and biomarker_gating.py
    disagreed about the same trial."""
    ok = [{"query": "cond:x", "fetched": 10, "reported_total": 10, "error": ""}]
    assert verify_ingest(10, 10, ok, []) == (INGEST_COMPLETE, [])

    short = [{"query": "cond:x", "fetched": 4, "reported_total": 10, "error": ""}]
    status, reasons = verify_ingest(4, 4, short, [])
    assert status == INGEST_PARTIAL and reasons

    failed = [{"query": "cond:x", "fetched": 0, "reported_total": None,
               "error": "RuntimeError: timeout"}]
    assert verify_ingest(0, 0, failed, [])[0] == INGEST_PARTIAL


if __name__ == "__main__":
    import traceback

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except Exception:
                failures += 1
                print(f"FAIL  {name}")
                traceback.print_exc()
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
