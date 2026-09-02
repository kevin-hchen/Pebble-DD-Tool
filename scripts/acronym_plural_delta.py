"""Measure the acronym-plural fix against the pre-fix matcher, store-wide.

Driven through `gate_markers`, per §23's lesson. The candidate set is exact
rather than heuristic: a trial can only change if some marker pattern matches an
acronym-plural somewhere in the text the matcher reads, so those are enumerated
directly and everything else is provably unaffected.

    python scripts/acronym_plural_delta.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, ".")

import medrag.markers as M  # noqa: E402
from medrag.biomarker_gating import gate_markers  # noqa: E402
from medrag.markers import MARKERS, _compiled, _is_acronym_plural  # noqa: E402

DB = "data/raw/trials.db"
DIRECTIONS = {"REQUIRED", "EXCLUDED", "ELIGIBLE_BY_EXCLUSION"}
_RES = {k: _compiled(m) for k, m in MARKERS.items()}

_shipped_matches = M._matches


def pre_fix_matches(rx, text):
    """`_matches` without the acronym-plural filter — i.e. plain finditer, which
    is what every call site did before this fix."""
    return list(rx.finditer(text or ""))


def has_acronym_plural(*texts) -> bool:
    for t in texts:
        if not t:
            continue
        for rx in _RES.values():
            for m in rx.finditer(t):
                if _is_acronym_plural(m.group(0)):
                    return True
    return False


def run(rec):
    return gate_markers(
        rec["eligibility_criteria"] or "",
        detailed_description=rec["detailed_description"] or "",
        brief_summary=rec["brief_summary"] or "",
        keywords=json.loads(rec["keywords"] or "[]"),
    )


def main() -> None:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM trials WHERE eligibility_criteria IS NOT NULL "
        "AND eligibility_criteria != ''").fetchall()

    candidates = [r for r in rows if has_acronym_plural(
        r["eligibility_criteria"], r["detailed_description"], r["brief_summary"],
        r["keywords"])]
    print(f"trials: {len(rows)}   candidates (text contains an acronym-plural "
          f"marker match): {len(candidates)}", flush=True)

    trans: Counter = Counter()
    changed: list[tuple] = []
    for rec in candidates:
        M._matches = pre_fix_matches
        before = run(rec)
        M._matches = _shipped_matches
        after = run(rec)
        for key in before:
            a, b = before[key].status, after[key].status
            if a != b:
                trans[(a, b)] += 1
                changed.append((rec["nct_id"], rec["brief_title"], key, a, b))
    M._matches = _shipped_matches

    print(f"\ntrials whose verdicts change: {len({c[0] for c in changed})}")
    print(f"total verdict changes: {len(changed)}\n")
    for (a, b), n in trans.most_common():
        print(f"  {a:>22} -> {b:<22} {n}")

    inversions = [c for c in changed if c[3] in DIRECTIONS and c[4] in DIRECTIONS]
    toward_required = [c for c in inversions if c[4] == "REQUIRED"]
    print(f"\ndirection-to-different-direction inversions: {len(inversions)}")
    print(f"  toward REQUIRED: {len(toward_required)}")

    print(f"\nevery changed verdict ({len(changed)}):")
    for nct, title, key, a, b in sorted(changed):
        print(f"  {nct}  {key:<11} {a:<22} -> {b:<22} | {title[:60]}")

    with open("/tmp/acronym_plural_changes.json", "w") as fh:
        json.dump([{"nct_id": n, "title": t, "marker": k, "before": a, "after": b}
                   for n, t, k, a, b in sorted(changed)], fh, indent=2)
    print("\nchange list -> /tmp/acronym_plural_changes.json")


if __name__ == "__main__":
    main()
