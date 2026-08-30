"""Diff the STORED biomarker census against what the current code derives.

Drives `biomarker_gating.gate_markers` — the reducer that actually built the
column. RATIONALE §23 records what happens when a derived column is diffed
against an ad-hoc re-reading instead: an earlier measurement reported 102
inversions where the real figure was 6.

    python scripts/census_diff.py colorectal
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, ".")

from medrag.biomarker_gating import gate_markers  # noqa: E402
from medrag.markers import (  # noqa: E402
    ELIGIBLE_BY_EXCLUSION,
    EXCLUDED,
    NOT_ASSESSABLE,
    NOT_MENTIONED,
    REQUIRED,
)

DB = "data/raw/trials.db"

# A "direction" is a claim about which way the trial gates. NOT_MENTIONED and
# NOT_ASSESSABLE are both absences of one, and must never be counted as
# inversions of each other.
DIRECTIONS = {REQUIRED, EXCLUDED, ELIGIBLE_BY_EXCLUSION}


def parse_stored(blob: str) -> dict[str, str]:
    out = {}
    for tok in (blob or "").split():
        if ":" in tok:
            k, _, v = tok.partition(":")
            out[k] = v
    return out


def main(set_key: str = "colorectal") -> None:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT t.* FROM trials t JOIN trial_query_sets q USING (nct_id) "
        "WHERE q.set_key = ?",
        (set_key,),
    ).fetchall()

    transitions: Counter = Counter()
    inversions: list[tuple] = []
    n_changed_rows = 0

    for r in rows:
        stored = parse_stored(r["biomarker_gating"])
        live = gate_markers(
            r["eligibility_criteria"] or "",
            detailed_description=r["detailed_description"] or "",
            brief_summary=r["brief_summary"] or "",
            # keywords is stored as a JSON array; the ingest passes it decoded
            # (trials/store.py). Splitting the raw string instead silently
            # changes the input and manufactures a diff.
            keywords=json.loads(r["keywords"] or "[]"),
        )
        changed = False
        for key, flag in live.items():
            was = stored.get(key)
            if was is None or was == flag.status:
                continue
            changed = True
            transitions[(key if False else was, flag.status)] += 1
            if was in DIRECTIONS and flag.status in DIRECTIONS:
                inversions.append((r["nct_id"], key, was, flag.status))
        if changed:
            n_changed_rows += 1

    print(f"set: {set_key}   trials: {len(rows)}   rows changed: {n_changed_rows}")
    print(f"total verdict changes: {sum(transitions.values())}")
    print("\nby transition:")
    for (a, b), n in sorted(transitions.items(), key=lambda kv: -kv[1]):
        print(f"  {a:>22} -> {b:<22} {n}")

    print(f"\ndirection-to-different-direction inversions: {len(inversions)}")
    toward_required = [i for i in inversions if i[3] == REQUIRED]
    print(f"  toward REQUIRED: {len(toward_required)}")
    for nct, key, a, b in sorted(inversions):
        mark = "  <-- toward REQUIRED" if b == REQUIRED else ""
        print(f"  {nct}  {key:<12} {a:<22} -> {b}{mark}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "colorectal")
