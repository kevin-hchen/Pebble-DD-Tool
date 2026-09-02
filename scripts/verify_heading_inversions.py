"""Print every inversion the heading fix produces, with the record quoted.

§24's pre-registered bar is that each inversion is verified INDIVIDUALLY as a
correction rather than a new error — not sampled. This assembles what a human
needs to make that call for one inversion in one screen:

  * the trial title, which for a biomarker-gated trial usually states the gate
  * the span the PRE-FIX code decided on, and the section it thought it was in
  * the span the FIXED code decides on, and its section
  * every line of the record that names the marker, verbatim

It prints; it does not judge. The judgement goes in the report.

    python scripts/verify_heading_inversions.py [inversions.json]
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, ".")

import medrag.markers as M  # noqa: E402
from medrag.biomarker_gating import gate_markers  # noqa: E402
from medrag.markers import MARKERS, _compiled  # noqa: E402
from scripts.heading_fix_delta import pre_fix_iter_criteria  # noqa: E402

DB = "data/raw/trials.db"
IN = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/heading_fix_inversions.json")

_shipped = M.iter_criteria


def marker_lines(text: str, marker: str) -> list[str]:
    rx = _compiled(MARKERS[marker])
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line and rx.search(line):
            out.append(line)
    return out


def main() -> None:
    inversions = json.loads(IN.read_text())
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    toward_required = [i for i in inversions if i["after"] == "REQUIRED"]
    print(f"{len(inversions)} inversions; {len(toward_required)} toward REQUIRED\n")

    for n, inv in enumerate(inversions, 1):
        rec = conn.execute(
            "SELECT * FROM trials WHERE nct_id = ?", (inv["nct_id"],)).fetchone()
        kw = json.loads(rec["keywords"] or "[]")

        M.iter_criteria = pre_fix_iter_criteria
        before = gate_markers(
            rec["eligibility_criteria"] or "",
            detailed_description=rec["detailed_description"] or "",
            brief_summary=rec["brief_summary"] or "", keywords=kw)
        M.iter_criteria = _shipped
        after = gate_markers(
            rec["eligibility_criteria"] or "",
            detailed_description=rec["detailed_description"] or "",
            brief_summary=rec["brief_summary"] or "", keywords=kw)

        b, a = before[inv["marker"]], after[inv["marker"]]
        flag = "  <== TOWARD REQUIRED" if inv["after"] == "REQUIRED" else ""
        print("=" * 78)
        print(f"[{n}/{len(inversions)}] {inv['nct_id']}  {inv['marker']}  "
              f"{inv['before']} -> {inv['after']}{flag}")
        print(f"  title: {rec['brief_title']}")
        print(f"\n  PRE-FIX span  [{b.source}]:\n    {b.span[:400] or '(none)'}")
        print(f"\n  FIXED  span  [{a.source}]:\n    {a.span[:400] or '(none)'}")
        lines = marker_lines(rec["eligibility_criteria"], inv["marker"])
        print(f"\n  every eligibility line naming {inv['marker']} ({len(lines)}):")
        for ln in lines[:6]:
            print(f"    - {ln[:300]}")
        if len(lines) > 6:
            print(f"    ... and {len(lines) - 6} more")
        # The line that caused the flip, if any.
        mention = [ln.strip() for ln in (rec["eligibility_criteria"] or "").splitlines()
                   if re.search(r"(inclusion|exclusion)\s+criteri", ln, re.I)
                   and not re.match(r"^[\-\*•–—\s]*(?:\d{1,2}[.)]\s*)?"
                                    r"(?:key|main|major|general|additional|further|"
                                    r"specific|primary|secondary|patient|participant|"
                                    r"subject|donor|study|the)?\s*"
                                    r"(inclusion|exclusion)\s+criteri", ln.strip(), re.I)]
        if mention:
            print(f"\n  the mid-line mention that flipped the section:")
            for ln in mention[:2]:
                print(f"    ! {ln[:300]}")
        print()

    M.iter_criteria = _shipped


if __name__ == "__main__":
    main()
