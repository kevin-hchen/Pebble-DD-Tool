#!/usr/bin/env python3
"""Build the public data artifact: one command, versioned, checksummed.

    python scripts/build_artifact.py --out dist/

WHAT THIS PRODUCES AND WHY IT IS SHAPED THIS WAY

A directory (tar it if you like) holding a compacted copy of each store plus a
`manifest.json`. Three properties the deployment depends on:

  * **The snapshot date and content version live INSIDE each database**, in a
    `snapshot_meta` table, not in the filename. A filename is a label anyone can
    change with `mv`, and a deployment that renames `trials-2026-08-10.db` to
    `trials-current.db` has silently discarded the only record of how old it is.
    Reading the date out of the file itself cannot be defeated that way.

  * **Every file is checksummed and the checksums are published** in the
    manifest and in `SHA256SUMS`. A deployer verifies what they actually put on
    the server against what was built, rather than trusting that the copy
    completed.

  * **Reproducible given the same inputs.** `VACUUM INTO` is byte-deterministic
    (measured: three consecutive vacuums of the same source produce identical
    SHA-256), and the metadata written inside each database is derived from the
    DATA — snapshot date, row counts, content version — never from the wall
    clock. So two builds from the same stores produce byte-identical databases,
    and `--verify-reproducible` proves it by building twice and comparing.

    The build TIME is recorded in the manifest, outside the databases, precisely
    so it cannot make the databases differ.

    Worth stating plainly: this is reproducibility from the same source stores,
    not from nothing. The stores hold fetched registry data, and the registry
    changes daily — a fetch a week later is a different snapshot, correctly.
    What is guaranteed is that the artifact is a pure function of the stores it
    was built from, so a mismatch means the input changed, not the build.

WHY VACUUM INTO RATHER THAN A COPY

A copy of a live database can catch a write in progress, and copies the free
pages too. `VACUUM INTO` takes a read transaction, writes a compact consistent
copy, and never touches the source — so this is safe to run while the tool is
in use, and the result has no free-page bloat to ship.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

#: Bumped when the SHAPE of the artifact changes — a store added or removed, or
#: the metadata table's columns changed. Distinct from each store's own
#: `PRAGMA user_version` (its schema) and from the snapshot date (its age).
#: Three different questions, three different numbers.
ARTIFACT_VERSION = 1

MANIFEST_NAME = "manifest.json"
SUMS_NAME = "SHA256SUMS"

#: The stores the public service reads. `trials.db` is required — without it
#: there is no landscape and the service has nothing to serve. The FDA stores
#: are needed only by the memo and claims routes, which ship flagged off, so a
#: deployment may legitimately omit them.
STORES = [
    ("trials.db", True, "trials"),
    ("fda.db", False, "fda devices"),
    ("drugs.db", False, "fda drugs"),
]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot_date(conn: sqlite3.Connection) -> str:
    """The newest ingest timestamp in the file — what the data IS, not when the
    artifact was built. Derived from the data so it is deterministic, and so a
    rebuild of unchanged stores yields an unchanged database."""
    newest = ""
    for table in ("trials", "clearances", "applications", "pma", "recalls"):
        try:
            row = conn.execute(f"SELECT MAX(ingested_at) FROM {table}").fetchone()
        except sqlite3.Error:
            continue           # table absent in this store; the others still count
        if row and row[0] and str(row[0]) > newest:
            newest = str(row[0])
    return newest or "unknown"


def _row_counts(conn: sqlite3.Connection) -> dict:
    counts = {}
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "AND name NOT LIKE '%_fts%' ORDER BY name"):
        try:
            counts[name] = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        except sqlite3.Error:
            continue
    return counts


def _write_meta(path: Path, snapshot_date: str, counts: dict) -> None:
    """Stamp the snapshot date and content version INTO the database.

    This is the requirement that a filename cannot satisfy. `public/artifact.py`
    reads these values back at startup and refuses to serve a stale one; a
    deployer renaming the file changes nothing about what the app believes.
    """
    conn = sqlite3.connect(str(path))
    try:
        with conn:
            conn.execute("DROP TABLE IF EXISTS snapshot_meta")
            conn.execute(
                "CREATE TABLE snapshot_meta ("
                "  key TEXT PRIMARY KEY,"
                "  value TEXT"
                ")")
            conn.executemany(
                "INSERT INTO snapshot_meta (key, value) VALUES (?, ?)",
                [
                    ("artifact_version", str(ARTIFACT_VERSION)),
                    ("snapshot_date", snapshot_date),
                    ("row_counts", json.dumps(counts, sort_keys=True)),
                    # Deliberately NO build timestamp here. A wall-clock value
                    # inside the file would make two builds of identical inputs
                    # differ, destroying the reproducibility check. It lives in
                    # the manifest instead.
                ])
    finally:
        conn.close()
    # VACUUM again so the metadata table does not leave the file's page layout
    # dependent on insertion order — this is what keeps the output byte-stable.
    tmp = path.with_suffix(".compact")
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("VACUUM INTO ?", (str(tmp),))
    finally:
        conn.close()
    tmp.replace(path)


def build(source: Path, out: Path, quiet: bool = False) -> dict:
    def say(msg: str) -> None:
        if not quiet:
            print(msg)

    out.mkdir(parents=True, exist_ok=True)
    entries = []

    for filename, required, label in STORES:
        src = source / filename
        if not src.exists():
            if required:
                raise SystemExit(
                    f"error: {src} is missing and is required.\n"
                    f"  The public service cannot serve a landscape without it.\n"
                    f"  Build it first:  python -m medrag trials --condition \"...\"")
            say(f"  {label:<14} not present — skipping (optional; "
                f"only the flagged-off memo/claims routes read it)")
            continue

        dest = out / filename
        if dest.exists():
            dest.unlink()

        say(f"  {label:<14} compacting {filename}…")
        # Read-only on the source: this is safe to run while the tool is in use,
        # and cannot modify the stores it is copying.
        conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        try:
            snapshot_date = _snapshot_date(conn)
            counts = _row_counts(conn)
            conn.execute("VACUUM INTO ?", (str(dest),))
        finally:
            conn.close()

        _write_meta(dest, snapshot_date, counts)
        digest = _sha256(dest)
        size = dest.stat().st_size
        entries.append({
            "file": filename,
            "label": label,
            "required": required,
            "sha256": digest,
            "bytes": size,
            "snapshot_date": snapshot_date,
            "row_counts": counts,
        })
        say(f"  {label:<14} {size/1048576:>8.1f} MB  {digest[:16]}…  "
            f"snapshot {snapshot_date}")

    manifest = {
        "artifact_version": ARTIFACT_VERSION,
        # The one wall-clock value, and it lives OUT here so it cannot make two
        # builds of the same inputs differ.
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "snapshot_date": max((e["snapshot_date"] for e in entries), default="unknown"),
        "total_bytes": sum(e["bytes"] for e in entries),
        "files": entries,
    }
    (out / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (out / SUMS_NAME).write_text(
        "".join(f"{e['sha256']}  {e['file']}\n" for e in entries))

    say(f"\n  artifact {out}")
    say(f"  total {manifest['total_bytes']/1048576:.1f} MB "
        f"across {len(entries)} file(s), snapshot {manifest['snapshot_date']}")
    say(f"  verify with:  cd {out} && shasum -a 256 -c {SUMS_NAME}")
    return manifest


def verify_reproducible(source: Path, quiet: bool = False) -> bool:
    """Build twice into scratch directories and compare every database byte for
    byte. Proves the claim rather than asserting it."""
    import tempfile

    first = Path(tempfile.mkdtemp()) / "a"
    second = Path(tempfile.mkdtemp()) / "b"
    m1 = build(source, first, quiet=True)
    m2 = build(source, second, quiet=True)

    same = True
    for a, b in zip(m1["files"], m2["files"]):
        ok = a["sha256"] == b["sha256"]
        same &= ok
        if not quiet:
            print(f"  {a['file']:<14} {'identical' if ok else 'DIFFERS'}  {a['sha256'][:16]}…")
    shutil.rmtree(first.parent, ignore_errors=True)
    shutil.rmtree(second.parent, ignore_errors=True)
    return same


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", default="data/raw", type=Path,
                        help="directory holding the built stores (default: data/raw)")
    parser.add_argument("--out", default="dist/artifact", type=Path,
                        help="where to write the artifact (default: dist/artifact)")
    parser.add_argument("--verify-reproducible", action="store_true",
                        help="build twice and confirm the databases are byte-identical")
    args = parser.parse_args()

    source = (REPO / args.source) if not args.source.is_absolute() else args.source
    if not source.exists():
        print(f"error: no source directory at {source}", file=sys.stderr)
        return 1

    if args.verify_reproducible:
        print("Building twice to confirm reproducibility…")
        ok = verify_reproducible(source)
        print("\nreproducible" if ok else "\nNOT reproducible — the build is not a "
              "pure function of its inputs")
        return 0 if ok else 1

    print(f"Building public data artifact from {source}")
    build(source, (REPO / args.out) if not args.out.is_absolute() else args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
