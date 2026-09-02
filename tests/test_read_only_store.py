"""Tests for the read-only store path — the blocker for serving this on a
public website.

Every store here WROTE to its own database just to be opened:
`executescript(SCHEMA)`, `PRAGMA user_version = N`, `commit()`, on every open
including one that only ever runs SELECTs. Three consequences, each pinned
below:

  1. A reader took a WRITE lock, so opening the store during an ingest failed
     with "database is locked" — a visitor's search dying because someone
     refreshed the data.
  2. On a read-only filesystem the constructor did not start AT ALL, so the app
     could not be deployed against a read-only volume.
  3. A public web app held write access to its own data, which nothing on the
     read path ever needed.

And the property that matters most for a public deployment, tested here rather
than assumed: **a read path never fetches**. A stranger's search must not make
the server pull from ClinicalTrials.gov or PubMed on their behalf.

The concurrency test in this file found a REAL defect rather than confirming a
belief: with SQLite's default rollback journal, a reader's SHARED lock and a
writer's EXCLUSIVE lock exclude each other, and a polling reader starved the
ingest until it hung outright. That is why the writable path now sets WAL. The
test is written so it fails again if WAL is removed.

No network: tests/netguard.py blocks sockets — which is itself part of the
proof for the no-fetch tests, since an attempted fetch would raise there.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import netguard  # noqa: E402

netguard.install()

from medrag.config import Config  # noqa: E402
from medrag.dbopen import (  # noqa: E402
    MissingDatabaseError,
    ReadOnlyStoreError,
    connect_read_only,
)
from medrag.trials.client import TrialRecord  # noqa: E402
from medrag.trials.store import TrialStore  # noqa: E402


def _records(a, b):
    return [
        TrialRecord(nct_id=f"NCT{i:06d}", brief_title=f"Trial {i}",
                    overall_status="RECRUITING", conditions=["Colorectal Cancer"],
                    eligibility_criteria="Inclusion Criteria:\n* MSS")
        for i in range(a, b)
    ]


def _seeded(n=20) -> Path:
    path = Path(tempfile.mkdtemp()) / "trials.db"
    store = TrialStore(path)
    store.upsert(_records(1, n + 1), set_key="colorectal")
    store.close()
    return path


# ------------------------------------------------- the constructor's writes


def test_a_read_only_open_leaves_the_file_byte_identical():
    """The direct form of the bug: opening the store used to modify it. Checked
    on bytes and mtime rather than on behaviour, because the whole complaint is
    that a read was indistinguishable from a write at the filesystem."""
    path = _seeded()
    before, before_mtime = path.read_bytes(), path.stat().st_mtime

    store = TrialStore(path, read_only=True)
    store.count()
    store.query(limit=5)
    store.landscape(query_set="colorectal", sample_limit=3)
    store.close()

    assert path.read_bytes() == before, "a read-only open modified the database"
    assert path.stat().st_mtime == before_mtime


def test_a_read_only_store_serves_the_same_answers_as_a_writable_one():
    """Anti-vacuity: 'it does not write' would be trivially satisfiable by a
    store that also does not read."""
    path = _seeded(30)
    rw, ro = TrialStore(path), TrialStore(path, read_only=True)
    assert ro.count() == rw.count() == 30
    assert ro.count(query_set="colorectal") == rw.count(query_set="colorectal")
    assert len(ro.query(limit=10)) == len(rw.query(limit=10)) == 10
    assert (ro.landscape(query_set="colorectal", sample_limit=5)["total"]
            == rw.landscape(query_set="colorectal", sample_limit=5)["total"])
    rw.close()
    ro.close()


def test_every_write_is_refused_by_name_and_before_it_does_anything():
    path = _seeded()
    store = TrialStore(path, read_only=True)
    before = path.read_bytes()

    qset = type("Q", (), {"key": "colorectal", "label": "CRC", "curated": True})()
    for name, call in [
        ("upsert", lambda: store.upsert(_records(100, 110), set_key="colorectal")),
        ("begin_ingest", lambda: store.begin_ingest(qset)),
    ]:
        try:
            call()
            raise AssertionError(f"{name} was allowed to write a read-only store")
        except ReadOnlyStoreError as exc:
            assert name in str(exc), "the error must name the operation attempted"

    assert path.read_bytes() == before, "a refused write still changed the file"
    store.close()


def test_a_read_only_open_never_creates_a_missing_database():
    """An auto-created empty store would answer every question 'nothing found'
    for a question nobody searched — the not-assessed-vs-nothing-found rule,
    arriving through the file layer."""
    missing = Path(tempfile.mkdtemp()) / "absent.db"
    try:
        TrialStore(missing, read_only=True)
        raise AssertionError("a missing database was opened rather than refused")
    except MissingDatabaseError:
        pass
    assert not missing.exists(), "the read-only path created the file"


def test_a_read_only_open_does_not_create_the_parent_directory():
    """`mkdir` in the constructor is a separate failure from the schema write:
    it raises earlier, at page import, on a read-only volume."""
    root = Path(tempfile.mkdtemp())
    nested = root / "does" / "not" / "exist" / "trials.db"
    try:
        TrialStore(nested, read_only=True)
    except MissingDatabaseError:
        pass
    assert not (root / "does").exists(), "the read-only path created directories"


# ------------------------------------------------------ read-only filesystem


def test_a_database_with_no_write_permission_opens_read_only():
    """The 444 half of the deployment shape. The directory is made read-only
    too, because SQLite may want to create a journal beside the file and a
    writable directory would hide that."""
    path = _seeded()
    os.chmod(path, 0o444)
    os.chmod(path.parent, 0o555)
    try:
        store = TrialStore(path, read_only=True, immutable=True)
        assert store.count() == 20
        store.close()

        # And the writable path is the one that cannot cope — which is the
        # point: the read-only mode is not decoration, it is the only way in.
        try:
            TrialStore(path)
            opened_writable = True
        except Exception:
            opened_writable = False
        assert not opened_writable, \
            "a writable open succeeded on a read-only file; the guard proves nothing"
    finally:
        os.chmod(path.parent, 0o755)
        os.chmod(path, 0o644)


def test_immutable_mode_creates_no_new_file_of_its_own():
    """`immutable=1` is what lets a genuinely read-only mount work: SQLite skips
    locking, so the open needs to create nothing — no lock file, no -wal, no
    -shm.

    Asserted as "no NEW siblings", not "no siblings at all": the writable path
    now uses WAL, so a database created by an ingest already has -wal and -shm
    beside it before any reader arrives. Those belong to the writer. (For a
    deployment snapshot they should be checkpointed away so the .db file is
    self-contained — see docs/RUNBOOK.md.)
    """
    path = _seeded()
    before = {p.name for p in path.parent.iterdir()}

    conn = connect_read_only(path, immutable=True)
    assert conn.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 20
    conn.close()

    after = {p.name for p in path.parent.iterdir()}
    assert after == before, f"the read-only open created {after - before}"


# ---------------------------------------------------------- concurrency


def test_a_reader_serves_the_previous_snapshot_while_an_ingest_writes():
    """The requirement stated plainly, and the test that found the real bug.

    Under the default rollback journal this deadlocks: the reader's SHARED lock
    blocks the writer's EXCLUSIVE one, the ingest hangs, and every reader that
    catches a commit window fails with "database is locked". Under WAL the
    writer appends while readers keep answering from the last commit.

    Asserts BOTH directions, because either alone can be satisfied by starving
    the other: the ingest must finish, AND no read may fail.
    """
    path = _seeded(200)
    reads: list[int] = []
    errors: list[str] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                store = TrialStore(path, read_only=True)
                reads.append(store.count(query_set="colorectal"))
                store.close()
            except Exception as exc:            # noqa: BLE001 - recorded, then asserted on
                errors.append(f"{type(exc).__name__}: {exc}")
            time.sleep(0.005)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        time.sleep(0.1)
        started = time.time()
        writer = TrialStore(path)
        for batch in range(4):
            writer.upsert(_records(1000 + batch * 100, 1100 + batch * 100),
                          set_key="colorectal")
        writer.close()
        elapsed = time.time() - started
    finally:
        time.sleep(0.1)
        stop.set()
        thread.join(timeout=10)

    assert not errors, f"readers failed during an ingest: {errors[:3]}"
    assert reads, "the reader never ran; the test proves nothing"
    assert elapsed < 30, f"the ingest was starved by readers ({elapsed:.0f}s)"
    # The old snapshot was served throughout, and the new one afterwards.
    assert min(reads) == 200
    assert max(reads) > 200, "the writer's commits never became visible to readers"


def test_the_writable_path_uses_wal_so_the_test_above_cannot_silently_regress():
    """Pins the mechanism, not just the symptom. The concurrency test is timing
    sensitive by nature; this one fails deterministically if WAL is removed."""
    path = _seeded(5)
    store = TrialStore(path)
    assert store.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert store.conn.execute("PRAGMA busy_timeout").fetchone()[0] > 0
    store.close()


def test_a_read_only_connection_also_waits_rather_than_failing_on_a_lock():
    path = _seeded(5)
    conn = connect_read_only(path)
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] > 0
    conn.close()


# ------------------------------------------------- no fetch on a read path


def test_read_only_mode_declines_to_fetch_rather_than_reaching_the_network():
    """The public-site property. `ensure_data` is what both app.py and the
    claims page call, and on a miss it fetched PubMed and the registry. In
    read-only mode it must return without touching either — proven by the
    netguard, which raises on any outbound socket."""
    from medrag.autoload import ensure_data

    cfg = Config(data_dir=Path(tempfile.mkdtemp()))
    cfg.read_only = True

    report = ensure_data(cfg, asset="botensilimab", indication="colorectal cancer")

    assert report.read_only is True
    assert report.literature_added == 0 and report.trials_added == 0
    assert not report.errors, f"read-only mode should not error, it should decline: {report.errors}"


def test_declining_to_fetch_is_worded_as_absence_not_as_a_finding():
    """"Nothing was loaded" and "nothing exists" must not read the same. The
    summary is what a user sees, so the distinction has to be in the words."""
    from medrag.autoload import LoadReport

    text = LoadReport(read_only=True).summary()
    assert "does not fetch" in text
    assert "NOT a finding" in text
    # And it must not be confusable with the already-loaded message.
    assert text != LoadReport(skipped=True).summary()


def test_read_only_mode_outranks_an_explicit_force_refresh():
    """A caller asking to re-download is asking for something this deployment
    does not do. If `force` won, the landscape page's "re-download" checkbox
    would be a public button that fetches tens of thousands of studies."""
    from medrag.autoload import ensure_data

    cfg = Config(data_dir=Path(tempfile.mkdtemp()))
    cfg.read_only = True
    report = ensure_data(cfg, asset="x", indication="y", force=True)
    assert report.read_only is True and report.trials_added == 0


def test_read_only_implies_offline_and_drops_the_api_key():
    """Enforced once at load, not remembered at each call site."""
    from medrag.config import load_config

    keys = ("MEDRAG_READ_ONLY", "MEDRAG_OFFLINE", "MEDRAG_PROVIDER", "OPENAI_API_KEY")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        os.environ["MEDRAG_READ_ONLY"] = "1"
        os.environ["MEDRAG_PROVIDER"] = "openai"
        os.environ["OPENAI_API_KEY"] = "sk-should-be-dropped"
        os.environ.pop("MEDRAG_OFFLINE", None)
        cfg = load_config()
        assert cfg.read_only is True
        assert cfg.offline is True, "read-only must imply offline"
        assert cfg.openai_api_key is None, "a read-only deployment must not hold a key"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_ensure_dirs_creates_nothing_in_read_only_mode():
    """Called unconditionally at module scope by all three pages, and not
    wrapped — so on a read-only volume it killed the page at import."""
    root = Path(tempfile.mkdtemp()) / "data"
    cfg = Config(data_dir=root)
    cfg.read_only = True
    cfg.ensure_dirs()
    assert not root.exists(), "ensure_dirs created directories in read-only mode"

    cfg.read_only = False
    cfg.ensure_dirs()
    assert root.exists(), "ensure_dirs stopped working in normal mode"


def test_the_config_repr_still_hides_secrets_with_the_new_field():
    cfg = Config(openai_api_key="sk-secret", passphrase="hunter2")
    text = repr(cfg)
    assert "read_only=" in text
    assert "sk-secret" not in text and "hunter2" not in text


# --------------------------------------------- the other two stores


def test_the_fda_stores_have_the_same_read_only_contract():
    """The defect was identical in all three constructors, so the fix has to be
    checked in all three — a guard that exists in one store and not the others
    is the drift this codebase keeps having to undo."""
    from medrag.fda.drug_store import DrugStore
    from medrag.fda.store import FDAStore

    for cls, name in ((FDAStore, "fda.db"), (DrugStore, "drugs.db")):
        path = Path(tempfile.mkdtemp()) / name
        cls(path).close()                      # create it once, writably
        before = path.read_bytes()

        store = cls(path, read_only=True)
        assert store.read_only is True
        store.close()
        assert path.read_bytes() == before, f"{cls.__name__} wrote on a read-only open"

        try:
            cls(Path(tempfile.mkdtemp()) / "absent.db", read_only=True)
            raise AssertionError(f"{cls.__name__} created a missing database")
        except MissingDatabaseError:
            pass


def test_the_fda_stores_refuse_writes_when_read_only():
    from medrag.fda.drug_store import DrugStore
    from medrag.fda.store import FDAStore

    path = Path(tempfile.mkdtemp()) / "fda.db"
    FDAStore(path).close()
    store = FDAStore(path, read_only=True)
    try:
        store.set_category_total("FRN", 1148)
        raise AssertionError("set_category_total wrote to a read-only store")
    except ReadOnlyStoreError:
        pass
    store.close()

    path = Path(tempfile.mkdtemp()) / "drugs.db"
    DrugStore(path).close()
    store = DrugStore(path, read_only=True)
    try:
        store.record_search("pembrolizumab", 12, [])
        raise AssertionError("record_search wrote to a read-only store")
    except ReadOnlyStoreError:
        pass
    store.close()


def test_a_stale_schema_is_still_refused_on_a_read_only_open():
    """Fail-closed must not be something the read path skips: querying columns
    that are not there is a worse failure than refusing to open."""
    from medrag.trials.store import TrialStoreSchemaError

    path = _seeded(3)
    conn = sqlite3.connect(str(path))
    with conn:
        conn.execute("PRAGMA user_version = 2")
    conn.close()

    try:
        TrialStore(path, read_only=True)
        raise AssertionError("a stale schema opened read-only without complaint")
    except TrialStoreSchemaError as exc:
        assert "older version" in str(exc)


def test_a_read_only_store_reports_itself_as_read_only():
    """`refuse_write` keys off this attribute, and so can a caller deciding
    whether to offer a refresh button."""
    path = _seeded(3)
    assert TrialStore(path, read_only=True).read_only is True
    assert TrialStore(path).read_only is False


def test_the_seeded_fixture_is_not_accidentally_world_writable():
    """Not the point of this file, but a public deployment is exactly where a
    0644 database would matter, and the writable path chmods on create."""
    path = _seeded(2)
    mode = stat.S_IMODE(path.stat().st_mode)
    assert not mode & stat.S_IWOTH, f"database is world-writable: {oct(mode)}"


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
