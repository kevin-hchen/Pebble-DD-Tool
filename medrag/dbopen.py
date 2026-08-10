"""How a SQLite store is opened for reading, in one place.

Written because every store in this tool WROTE to its own database just to be
opened. `TrialStore.__init__` ran `executescript(SCHEMA)`, then
`PRAGMA user_version = N`, then `commit()` — on every open, including one that
only ever runs SELECTs. Three consequences, all of which bite a public
deployment:

  * A reader takes a WRITE lock. Opening the store while an ingest is running
    fails outright with "database is locked", so a visitor's search dies
    because someone refreshed the data. Concurrent read during write is the
    normal case for a public site, not an edge case.
  * On a read-only filesystem the constructor does not start AT ALL. Not
    degraded — the first `mkdir` or `executescript` raises and the page never
    renders.
  * A public web app had write access to its own data. Nothing in the read path
    intends to write, so that access exists only to be misused.

The fix belongs here rather than on the mount: making the mount writable to get
a read-only app to start hands that app permission it should never hold, and
the next person to read the deployment config would have no way to know the
write access was an accident.

TWO READ MODES, AND THE DIFFERENCE IS NOT COSMETIC
--------------------------------------------------

`mode=ro` alone opens read-only but still uses SQLite's normal locking, so the
reader sees a consistent snapshot and PICKS UP a concurrent writer's commits.
It needs the filesystem to be writable enough for lock/WAL files even though
the database itself is never modified.

`immutable=1` additionally promises SQLite that the file CANNOT change, which
lets it skip locking entirely. That promise is what makes a genuinely read-only
mount work — no lock file, no `-wal`, no `-shm`, nothing to create. It is also a
promise that is false the moment an ingest touches the file, and SQLite's own
documentation says the result is then undefined.

Measured on this database (2026-08-10), a connection held open across a writer's
commit:

    mode=ro              501 rows -> 2501 rows   (sees the ingest)
    mode=ro&immutable=1  501 rows ->  501 rows   (frozen at open)

So the two requirements — "serve the previous snapshot while an ingest runs" and
"work on a read-only mount" — are answered by DIFFERENT flags, and collapsing
them into one would either break concurrent reads or risk undefined reads. The
default is the safe one; `immutable=True` is an explicit assertion by the
deployer that this file is a frozen artefact nothing will write to.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

#: How long a connection waits for a lock before giving up. Generous, because
#: the alternative a user sees is the page failing outright; an ingest's commit
#: is far shorter than this, so in practice the wait is milliseconds.
BUSY_TIMEOUT_MS = 10_000


class ReadOnlyStoreError(RuntimeError):
    """A write was attempted against a store opened for reading.

    Raised in plain language rather than letting SQLite's own
    "attempt to write a readonly database" reach a user, and raised BEFORE any
    work is done so a partial write is impossible. This is a fail-closed guard
    in the same family as `crypto.write_secure` refusing to write plaintext:
    the read path is not supposed to write, so if it tries, that is a bug to
    surface rather than a condition to recover from.
    """


class MissingDatabaseError(RuntimeError):
    """Opened for reading and the file is not there.

    A distinct error because a read-only open must NEVER create the database.
    The writable path creates on demand, which is right for an ingest and
    exactly wrong for a public reader: it would silently produce an empty store
    and every query against it would answer "nothing found" for a question
    nobody had searched — the not-assessed-vs-nothing-found confusion this
    codebase guards against everywhere else, arriving through the file layer.
    """


def connect_read_only(path: str | Path, *, immutable: bool = False) -> sqlite3.Connection:
    """Open a SQLite file for reading, with no possibility of writing it.

    `immutable=True` asserts the file is a frozen snapshot — required on a
    read-only mount, wrong while an ingest may be running. See the module
    docstring for the measurement behind that split.
    """
    path = Path(path)
    if not path.exists():
        raise MissingDatabaseError(
            f"no database at {path}. A read-only store never creates one, because "
            "an empty store answers every question with 'nothing found' — which "
            "reads as a finding rather than as an absence."
        )
    # quote() so a path containing '?' or '#' cannot alter the URI's meaning.
    uri = f"file:{quote(str(path))}?mode=ro"
    if immutable:
        uri += "&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    # A reader that meets a momentary lock should wait, not fail. Without this
    # SQLite gives up instantly and a visitor sees "database is locked" for a
    # contention that resolves in milliseconds.
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def prepare_writable(conn: sqlite3.Connection) -> None:
    """Put a writable connection into the journal mode that lets readers and a
    writer coexist, and give it a lock timeout.

    WAL is not a performance tweak here, it is the fix for a defect this
    codebase's own concurrency test found. In the default rollback-journal mode
    a reader holds a SHARED lock for the length of its query and a writer needs
    an EXCLUSIVE one, so the two block each other: a public page polling the
    store starved an ingest until it hung outright, and an ingest mid-commit
    made every reader fail with "database is locked". Read-during-write is the
    normal case for a served snapshot, not an edge case.

    Under WAL a writer appends to the -wal file while readers continue against
    the last committed snapshot. Neither blocks the other, which is exactly the
    requirement: serve the previous snapshot happily while an ingest runs.

    `journal_mode` is a persistent property of the file, so this runs once per
    open and is a no-op on a database already in WAL. It needs write access,
    which is why it lives on the writable path only — a read-only connection
    inherits whatever mode the file was shipped in.
    """
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")


def refuse_write(store: object, operation: str) -> None:
    """Raise if `store` was opened read-only. Called at the TOP of every write.

    Takes the operation name so the message says what was attempted; a bare
    "read-only" tells whoever hits it nothing about which call to look at.
    """
    if getattr(store, "read_only", False):
        raise ReadOnlyStoreError(
            f"{type(store).__name__}.{operation}() tried to write, but this store was "
            "opened read-only. Nothing on a read path should write to the database — "
            "if this is an ingest, open the store without read_only=True; if it is a "
            "page or a memo run, this is the bug."
        )
