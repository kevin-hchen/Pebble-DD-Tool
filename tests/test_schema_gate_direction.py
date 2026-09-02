"""The schema gate must diagnose which SIDE is stale, because the remedies are
opposite and one of them is destructive.

A store OLDER than the code is missing columns; depending on the gap the remedy
is a migration, a targeted backfill, or a delete-and-re-ingest. A store NEWER
than the code is missing nothing at all — a later revision wrote it, so it holds
more than the code can read. The remedy there is to move the CODE, and the store
must be kept.

Until this file existed the gate had no newer-than branch. The final `else`
caught it, so a v13 store opened by v12 code reported "built by an older
version" — backwards — and instructed `rm <store>` followed by a re-ingest.
That was reachable on this machine, not hypothetically: a 1.9 GB, 241,298-record
verified store sat at v13 while HEAD declared v12, and anyone on HEAD who
followed the message would have destroyed it to recover data it already held.

A destructive instruction on a wrong diagnosis is worse than a crash. A crash
stops the operator; this one moves them confidently in the wrong direction.

Both directions are asserted here, and each half carries a negative control, so
neither can pass by the gate simply refusing everything with one message.

Run: python -m pytest tests/test_schema_gate_direction.py -q
     (or: python tests/test_schema_gate_direction.py)
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Direct runs do not load conftest.py, so the no-network guard is installed
# here too. See tests/netguard.py.
from tests import netguard  # noqa: E402

netguard.install()

from medrag.trials.store import (  # noqa: E402
    STORE_VERSION,
    TrialStore,
    TrialStoreSchemaError,
)


def _store_at_version(version: int) -> Path:
    """A real store, stamped to an arbitrary schema version."""
    path = Path(tempfile.mkdtemp()) / "trials.db"
    TrialStore(path).close()
    conn = sqlite3.connect(str(path))
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    conn.close()
    return path


def _refusal(version: int) -> str:
    path = _store_at_version(version)
    try:
        TrialStore(path)
    except TrialStoreSchemaError as exc:
        return str(exc)
    raise AssertionError(
        f"a store at schema v{version} must be refused by v{STORE_VERSION} code, "
        "not silently read"
    )


# --------------------------------------------------------------------- newer

def test_a_store_newer_than_the_code_is_never_told_to_delete_it():
    """The failure this file exists for.

    The store is intact; the operator must keep it. Any instruction to remove or
    re-fetch it is a wrong answer that reads like a correct one.
    """
    msg = _refusal(STORE_VERSION + 1)

    assert "rm " not in msg, (
        "a store NEWER than the code was told to delete itself. It lacks "
        f"nothing — it holds more than v{STORE_VERSION} code can read. "
        f"Message was: {msg!r}"
    )
    for destructive in ("re-ingest", "re-fetch", "delete it"):
        assert destructive not in msg.lower().replace("no re-fetch", ""), (
            f"the newer-store message proposes {destructive!r}, which would "
            f"destroy a complete store. Message was: {msg!r}"
        )


def test_a_store_newer_than_the_code_says_so_rather_than_calling_it_older():
    """The diagnosis, not just the remedy.

    "Built by an older version (schema v13, current is v12)" is false on its
    face, and an operator who believes it will reach for the wrong fix even if
    the remedy line is right.
    """
    msg = _refusal(STORE_VERSION + 1)

    assert "NEWER" in msg, (
        f"a v{STORE_VERSION + 1} store opened by v{STORE_VERSION} code must be "
        f"described as newer. Message was: {msg!r}"
    )
    assert "older version" not in msg, (
        "the newer-store message still calls the store older, which is the "
        f"backwards diagnosis this branch exists to fix. Message was: {msg!r}"
    )
    assert str(STORE_VERSION + 1) in msg and str(STORE_VERSION) in msg, (
        "the message must state both versions so the operator can tell which "
        "revision to check out"
    )


def test_the_newer_store_remedy_points_at_the_code():
    """Keeping the store is only half an answer; the operator needs the next
    step. `git log -S` finds the revision that declared that schema version, and
    the message says what it means when history contains none — which is exactly
    the case when the store was written by uncommitted work."""
    msg = _refusal(STORE_VERSION + 1)
    assert "git log -S" in msg, f"no way to find the matching code: {msg!r}"
    assert "uncommitted" in msg, (
        "the message does not cover the case where no committed revision "
        "declares that schema version, which is when this fires in practice"
    )


# --------------------------------------------------------------------- older

def test_a_store_older_than_the_code_still_gets_its_destructive_remedy():
    """The negative control for the checks above.

    They would all pass if the gate stopped proposing a re-ingest for ANY
    version. A genuinely stale store with unrecoverable columns must still be
    told to delete and re-ingest — that remedy is correct in that direction, and
    suppressing it would be a different bug wearing this fix's clothes.
    """
    msg = _refusal(1)

    assert "rm " in msg, (
        "a genuinely stale store must still be given the re-ingest remedy; "
        f"got {msg!r}"
    )
    assert "older version" in msg, (
        f"a v1 store opened by v{STORE_VERSION} code IS older and must say so; "
        f"got {msg!r}"
    )
    assert "NEWER" not in msg, "an older store must not be described as newer"


def test_a_backfillable_store_is_told_to_migrate_not_to_delete():
    """Second negative control, on the middle case: v12 is backfillable, so its
    remedy is a local recomputation. If this started proposing `rm` the
    newer-store fix would look fine while the gate got worse elsewhere."""
    msg = _refusal(12) if STORE_VERSION != 12 else _refusal(10)
    assert "--migrate" in msg or "--backfill" in msg, (
        f"a backfillable store must be offered a local recomputation; got {msg!r}"
    )
    assert "rm " not in msg, (
        f"a backfillable store must not be told to delete itself; got {msg!r}"
    )


def test_a_current_store_is_not_refused_at_all():
    """The last control: every assertion above is satisfied by a gate that
    refuses everything."""
    path = _store_at_version(STORE_VERSION)
    store = TrialStore(path)
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == STORE_VERSION
    store.close()


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
    print("\nall schema-gate direction tests passed" if not failures
          else f"\n{failures} failure(s)")
    raise SystemExit(1 if failures else 0)
