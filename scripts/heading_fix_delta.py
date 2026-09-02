"""Measure the SHIPPED heading fix against the pre-fix `iter_criteria`.

`scripts/heading_anchor_delta.py` measured a start-anchored heading test as a
measurement instrument. The shipped fix is not that instrument: it also repairs
the colon-truncation half and declines to treat a combined
"inclusion/exclusion" heading as either section. So the §24 figures (81 changes,
71 inversions) do not carry over and are re-measured here, through
`gate_markers` — never by counting lines and inferring.

The pre-fix implementation is held verbatim below rather than reached for in
git, so this script keeps reproducing the same baseline after the fix lands.

    python scripts/heading_fix_delta.py            # store-wide
    python scripts/heading_fix_delta.py colorectal
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, ".")

import medrag.markers as M  # noqa: E402
from medrag.biomarker_gating import gate_markers  # noqa: E402

DB = "data/raw/trials.db"
DIRECTIONS = {"REQUIRED", "EXCLUDED", "ELIGIBLE_BY_EXCLUSION"}

_shipped = M.iter_criteria


def pre_fix_iter_criteria(text: str, default_section: str = "unknown"):
    """`markers.iter_criteria` exactly as it stood at commit 71a9fd1."""
    section = default_section
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if "inclusion criteria" in low:
            section = "inclusion"
            rest = line.split(":", 1)[1].strip() if ":" in line else ""
            if rest:
                yield section, rest
            continue
        if "exclusion criteria" in low:
            section = "exclusion"
            rest = line.split(":", 1)[1].strip() if ":" in line else ""
            if rest:
                yield section, rest
            continue
        cleaned = re.sub(r"^[\-\*•–—\d\.\)\(\s]+", "", line)
        if cleaned:
            yield section, cleaned


# Only a trial with a line the PRE-FIX code called a heading can change: with no
# such line both implementations take the `cleaned` path for every line.
_CANDIDATE = re.compile(r"(inclusion|exclusion)\s+criteri", re.IGNORECASE)


def run(rec):
    return gate_markers(
        rec["eligibility_criteria"] or "",
        detailed_description=rec["detailed_description"] or "",
        brief_summary=rec["brief_summary"] or "",
        keywords=json.loads(rec["keywords"] or "[]"),
    )


def main() -> None:
    set_key = sys.argv[1] if len(sys.argv) > 1 else None
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    if set_key:
        rows = conn.execute(
            "SELECT t.* FROM trials t JOIN trial_query_sets q USING (nct_id) "
            "WHERE q.set_key = ? AND t.eligibility_criteria IS NOT NULL "
            "AND t.eligibility_criteria != ''", (set_key,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM trials WHERE eligibility_criteria IS NOT NULL "
            "AND eligibility_criteria != ''").fetchall()

    candidates = [r for r in rows if _CANDIDATE.search(r["eligibility_criteria"])]
    print(f"population: {set_key or 'ALL'}   trials: {len(rows)}   "
          f"candidates: {len(candidates)}", flush=True)

    trans: Counter = Counter()
    inversions: list[tuple] = []
    changed_rows = 0

    for i, rec in enumerate(candidates):
        if i and i % 20000 == 0:
            print(f"   ... {i}/{len(candidates)}", flush=True)
        M.iter_criteria = pre_fix_iter_criteria
        before = run(rec)
        M.iter_criteria = _shipped
        after = run(rec)
        touched = False
        for key in before:
            a, b = before[key].status, after[key].status
            if a == b:
                continue
            touched = True
            trans[(a, b)] += 1
            if a in DIRECTIONS and b in DIRECTIONS:
                inversions.append((rec["nct_id"], key, a, b))
        if touched:
            changed_rows += 1
    M.iter_criteria = _shipped

    print(f"\ntrials whose verdicts change: {changed_rows}")
    print(f"total verdict changes: {sum(trans.values())}\n")
    for (a, b), n in trans.most_common():
        print(f"  {a:>22} -> {b:<22} {n}")

    toward_required = [i for i in inversions if i[3] == "REQUIRED"]
    print(f"\ndirection-to-different-direction inversions: {len(inversions)}")
    print(f"  toward REQUIRED: {len(toward_required)}")
    for nct, key, a, b in sorted(inversions):
        flag = "   <-- toward REQUIRED" if b == "REQUIRED" else ""
        print(f"  {nct}  {key:<12} {a:<22} -> {b}{flag}")

    with open("/tmp/heading_fix_inversions.json", "w") as fh:
        json.dump([{"nct_id": n, "marker": k, "before": a, "after": b}
                   for n, k, a, b in sorted(inversions)], fh, indent=2)
    print("\ninversion list -> /tmp/heading_fix_inversions.json")


if __name__ == "__main__":
    main()
