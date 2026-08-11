"""Precomputed landscape results, baked into the artifact at build time.

WHY THIS AND NOT A CACHE

A request-time cache of search results would take repeat searches close to zero,
and colorectal+MSS will be the most repeated search on the site. But a cache
holding results is a record that a search happened, and the terms say nothing is
retained between requests. Precomputing at BUILD time has the same effect on the
common case with no retention surface at all: the answers are static data
shipped inside the artifact, computed before any visitor exists, so there is
nothing to key, nothing to expire, and no terms amendment needed.

WHAT IS PRECOMPUTED, AND WHAT IS NOT

Condition x curated marker only — 74 families x 7 markers. NOT location:
proximity is combinatorial across free-text place names, and it is a ranking
pass over rows that are ALREADY selected, so it is applied per request to the
precomputed candidate set.

Anything outside that grid — a condition with no ingested family, an uncurated
biomarker — has no precomputed answer and falls through to the live path at the
live cost. That is a deliberate miss, not a failure: the live path is correct,
merely slower.

THE VERSION GATE

Precomputed answers are produced by build-time code. If the serving code changes
and the artifact does not, precomputed and live answers diverge silently — which
is exactly the failure the census/live gate just demonstrated, arriving from the
other direction.

So the precompute is stamped with `code_version`, a fingerprint of every source
file and config that can change the answer. A different fingerprint at startup
means the artifact was built by different code, and `verify_precompute` refuses
to serve it. On top of that — because a fingerprint proves the bytes matched, not
that the behaviour did — startup RE-RUNS a sample of precomputed pairs through
the live path and compares. Mismatch fails closed with the reason.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: Every file whose contents can change a landscape answer. A change to any one
#: of them invalidates every precomputed result, which is the point: the
#: fingerprint is not a version someone remembers to bump, it is derived.
FINGERPRINT_SOURCES = (
    "medrag/markers.py",
    "medrag/biomarker.py",
    "medrag/biomarker_gating.py",
    "medrag/landscape.py",
    "medrag/ranking.py",
    "medrag/precompute.py",
    "config/markers.yaml",
    "config/ranking.yaml",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS precomputed_landscape (
    set_key   TEXT NOT NULL,
    marker    TEXT NOT NULL,
    rank      INTEGER NOT NULL,
    nct_id    TEXT NOT NULL,
    status    TEXT,
    evidence  TEXT,
    explain   TEXT,
    PRIMARY KEY (set_key, marker, rank)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS precomputed_counts (
    set_key             TEXT NOT NULL,
    marker              TEXT NOT NULL,
    population          INTEGER,
    n_eligible          INTEGER,
    n_by_exclusion      INTEGER,
    n_unclear           INTEGER,
    n_excluded          INTEGER,
    n_not_mentioned     INTEGER,
    n_no_eligibility    INTEGER,
    n_candidates        INTEGER,
    PRIMARY KEY (set_key, marker)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS precompute_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def code_version(repo: Path | None = None) -> str:
    """A fingerprint of everything that can change a landscape answer.

    Derived, never hand-maintained: a version constant someone has to remember
    to bump is a version that is wrong exactly when it matters. A missing file
    is folded in by name so a deletion changes the fingerprint too.
    """
    root = Path(repo or REPO)
    digest = hashlib.sha256()
    for rel in FINGERPRINT_SOURCES:
        path = root / rel
        digest.update(rel.encode())
        digest.update(path.read_bytes() if path.exists() else b"<absent>")
    return digest.hexdigest()[:16]


@dataclass(frozen=True)
class PrecomputedRow:
    nct_id: str
    status: str
    evidence: str
    explain: str


@dataclass(frozen=True)
class PrecomputedLandscape:
    set_key: str
    marker: str
    rows: list[PrecomputedRow] = field(default_factory=list)
    counts: dict = field(default_factory=dict)


def install_schema(conn) -> None:
    conn.executescript(SCHEMA)


def write(conn, set_key: str, marker: str, landscape) -> int:
    """Store one landscape's ordered admitting rows and its counts.

    `landscape` must have been built with `show_limit=None` and NO location, so
    what is stored is the full ranked candidate list under the location-free
    ranking. A request supplying a location re-ranks these rows; a request
    without one uses them as they are.
    """
    conn.execute("DELETE FROM precomputed_landscape WHERE set_key=? AND marker=?",
                 (set_key, marker))
    conn.execute("DELETE FROM precomputed_counts WHERE set_key=? AND marker=?",
                 (set_key, marker))
    rows = [
        (set_key, marker, i, t.record.nct_id, t.match.status,
         t.match.evidence or "", t.ranking.explain() if t.ranking else "")
        for i, t in enumerate(landscape.trials)
    ]
    conn.executemany(
        "INSERT INTO precomputed_landscape (set_key, marker, rank, nct_id, status, "
        "evidence, explain) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    conn.execute(
        "INSERT INTO precomputed_counts (set_key, marker, population, n_eligible, "
        "n_by_exclusion, n_unclear, n_excluded, n_not_mentioned, n_no_eligibility, "
        "n_candidates) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (set_key, marker, landscape.n_condition, landscape.n_eligible,
         landscape.n_eligible_by_exclusion, landscape.n_unclear,
         landscape.n_excluded, landscape.n_not_mentioned,
         landscape.n_no_eligibility_text, landscape.n_candidates))
    return len(rows)


def stamp(conn, n_pairs: int, n_rows: int, repo: Path | None = None) -> str:
    version = code_version(repo)
    conn.execute("DELETE FROM precompute_meta")
    conn.executemany(
        "INSERT INTO precompute_meta (key, value) VALUES (?, ?)",
        [("code_version", version), ("pairs", str(n_pairs)), ("rows", str(n_rows))])
    return version


def stored_version(conn) -> str:
    try:
        row = conn.execute(
            "SELECT value FROM precompute_meta WHERE key='code_version'").fetchone()
    except Exception:                                   # noqa: BLE001 - table absent
        return ""
    return str(row[0]) if row else ""


def lookup(conn, set_key: str, marker: str) -> PrecomputedLandscape | None:
    """The precomputed answer for one (family, marker), or None.

    None is an ordinary outcome — an un-precomputed pair falls through to the
    live path — so this never raises on a missing table.
    """
    try:
        counts = conn.execute(
            "SELECT * FROM precomputed_counts WHERE set_key=? AND marker=?",
            (set_key, marker)).fetchone()
    except Exception:                                   # noqa: BLE001
        return None
    if counts is None:
        return None
    rows = conn.execute(
        "SELECT nct_id, status, evidence, explain FROM precomputed_landscape "
        "WHERE set_key=? AND marker=? ORDER BY rank", (set_key, marker)).fetchall()
    return PrecomputedLandscape(
        set_key=set_key, marker=marker,
        rows=[PrecomputedRow(r["nct_id"], r["status"], r["evidence"], r["explain"])
              for r in rows],
        counts=dict(counts),
    )


def pairs(conn) -> list[tuple[str, str]]:
    try:
        return [(r["set_key"], r["marker"]) for r in conn.execute(
            "SELECT set_key, marker FROM precomputed_counts ORDER BY set_key, marker")]
    except Exception:                                   # noqa: BLE001
        return []


def verify_sample(store, sample: int = 3, repo: Path | None = None) -> list[str]:
    """Re-run a sample of precomputed pairs through the LIVE path and compare.

    Returns a list of problems; empty means the sample agreed. The caller
    decides what to do — the public service refuses to start.

    Two checks, and the second is the one that matters. The fingerprint proves
    the artifact was built from these bytes; re-running proves the answers still
    agree, which is a stronger and more expensive claim. A fingerprint alone
    would pass an artifact built by identical code against a DIFFERENT store.

    The sample is deliberately small and deterministic (the largest families,
    which are both the most-used and the most likely to expose an ordering
    difference), because this runs on every process start.
    """
    from .precompute import code_version as _cv  # local: keeps import graph flat

    problems: list[str] = []
    stored = stored_version(store.conn)
    if not stored:
        return ["the artifact carries no precompute_meta stamp"]

    current = _cv(repo)
    if stored != current:
        return [
            f"precomputed results were built by code version {stored}, this process is "
            f"{current}. The serving code has changed since the artifact was built, so "
            "precomputed and live answers may differ.\n"
            "  Rebuild the artifact:  python scripts/build_artifact.py --out dist/artifact"
        ]

    from .landscape import build_landscape

    checked = 0
    for set_key, marker in pairs(store.conn):
        if checked >= sample:
            break
        pre = lookup(store.conn, set_key, marker)
        if pre is None or not pre.rows:
            continue
        checked += 1
        live = build_landscape(store, condition=set_key, biomarker=marker,
                               query_set=set_key, show_limit=None, use_precomputed=False)
        live_ids = [t.record.nct_id for t in live.trials]
        pre_ids = [r.nct_id for r in pre.rows]
        if live_ids != pre_ids:
            problems.append(
                f"{set_key}/{marker}: precomputed {len(pre_ids)} rows, live path returns "
                f"{len(live_ids)}, and they are not the same ordered list "
                f"(first difference at position "
                f"{next((i for i, (a, b) in enumerate(zip(pre_ids, live_ids)) if a != b), 'length')})")
        for field_name, pre_value, live_value in (
                ("n_excluded", pre.counts.get("n_excluded"), live.n_excluded),
                ("n_not_mentioned", pre.counts.get("n_not_mentioned"), live.n_not_mentioned),
                ("population", pre.counts.get("population"), live.n_condition)):
            if pre_value != live_value:
                problems.append(
                    f"{set_key}/{marker}: precomputed {field_name}={pre_value}, "
                    f"live={live_value}")
    if not checked:
        problems.append("no precomputed pair could be verified — the artifact stamps a "
                        "version but stores no results")
    return problems


def build_all(store, families: list[str], markers: list[str], progress=None) -> dict:
    """Compute every (family, marker) landscape and store it. Build-time only."""
    from .landscape import build_landscape

    install_schema(store.conn)
    n_pairs = n_rows = 0
    for set_key in families:
        for marker in markers:
            landscape = build_landscape(
                store, condition=set_key, biomarker=marker, query_set=set_key,
                show_limit=None, use_precomputed=False)
            # A pair with no admitting trial is still stored: "computed, and the
            # answer is none" must be distinguishable from "not precomputed", or
            # the serving path would fall through to the live route for every
            # empty pair and pay full cost to rediscover an empty answer.
            n_rows += write(store.conn, set_key, marker, landscape)
            n_pairs += 1
            if progress:
                progress(set_key, marker, len(landscape.trials))
    version = stamp(store.conn, n_pairs, n_rows)
    store.conn.commit()
    return {"pairs": n_pairs, "rows": n_rows, "code_version": version}


def counts_to_landscape(pre: PrecomputedLandscape, landscape) -> None:
    """Copy precomputed counts onto a landscape object."""
    c = pre.counts
    landscape.n_condition = c.get("population") or 0
    landscape.n_eligible = c.get("n_eligible") or 0
    landscape.n_eligible_by_exclusion = c.get("n_by_exclusion") or 0
    landscape.n_unclear = c.get("n_unclear") or 0
    landscape.n_excluded = c.get("n_excluded") or 0
    landscape.n_not_mentioned = c.get("n_not_mentioned") or 0
    landscape.n_no_eligibility_text = c.get("n_no_eligibility") or 0
    landscape.n_candidates = c.get("n_candidates") or 0


def json_summary(conn) -> str:
    return json.dumps({"code_version": stored_version(conn), "pairs": len(pairs(conn))})
