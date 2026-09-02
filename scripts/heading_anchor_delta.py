"""How many verdicts does the mid-line heading phrase actually change?

Measured by driving gate_markers twice -- once with the shipped iter_criteria,
once with a variant whose heading test is anchored at the start of the line --
rather than by counting lines and inferring. That is the §23 lesson applied:
when diffing a derived column, drive the function that derived it.

The patched variant here is a MEASUREMENT INSTRUMENT, not a proposed fix. It
exists to size the defect; what the fix should be is a separate decision.

    python scripts/heading_anchor_delta.py
"""
import json, re, sqlite3, sys
sys.path.insert(0, ".")
import medrag.markers as M
from medrag.biomarker_gating import gate_markers

_orig = M.iter_criteria

# Same function, one change: the heading phrase must START the line (after any
# enumeration marker or short qualifier), instead of appearing anywhere in it.
_HEAD = re.compile(r"^[\-\*•–—\d\.\)\(\s]*(?:key\s+|main\s+|participant\s+|patient\s+|subject\s+)?(inclusion|exclusion)\s+criteria", re.I)

def patched(text, default_section="unknown"):
    section = default_section
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _HEAD.match(line)
        if m:
            section = m.group(1).lower()
            rest = line.split(":", 1)[1].strip() if ":" in line else ""
            if rest:
                yield section, rest
            continue
        cleaned = re.sub(r"^[\-\*•–—\d\.\)\(\s]+", "", line)
        if cleaned:
            yield section, cleaned

conn = sqlite3.connect("file:data/raw/trials.db?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
_all = conn.execute("SELECT * FROM trials WHERE eligibility_criteria IS NOT NULL AND eligibility_criteria != ''").fetchall()
# Only trials with a heading phrase that does NOT start its line can change.
_mid = re.compile(r"^(?!\s*[-*\u2022\u2013\u2014\d.)(\s]*(?:key |main |participant |patient |subject )?(?:inclusion|exclusion)\s+criteria).*(?:inclusion|exclusion)\s+criteria", re.I)
rows = [r for r in _all if any(_mid.match(l.strip()) for l in r["eligibility_criteria"].splitlines() if l.strip())]
print(f"candidate trials (heading phrase appears mid-line): {len(rows)} of {len(_all)}", flush=True)

def run(rec):
    return gate_markers(rec["eligibility_criteria"],
                        detailed_description=rec["detailed_description"] or "",
                        brief_summary=rec["brief_summary"] or "",
                        keywords=json.loads(rec["keywords"] or "[]"))

from collections import Counter
trans = Counter(); changed_rows = 0; inversions = []
DIRS = {"REQUIRED", "EXCLUDED", "ELIGIBLE_BY_EXCLUSION"}
for rec in rows:
    M.iter_criteria = _orig
    a = run(rec)
    M.iter_criteria = patched
    b = run(rec)
    ch = False
    for k in a:
        if a[k].status != b[k].status:
            ch = True
            trans[(a[k].status, b[k].status)] += 1
            if a[k].status in DIRS and b[k].status in DIRS:
                inversions.append((rec["nct_id"], k, a[k].status, b[k].status))
    if ch:
        changed_rows += 1
M.iter_criteria = _orig

print(f"trials scanned: {len(rows)}   trials whose verdicts change: {changed_rows}")
print(f"total verdict changes: {sum(trans.values())}\n")
for (x, y), n in trans.most_common():
    print(f"  {x:>22} -> {y:<22} {n}")
print(f"\ndirection-to-different-direction: {len(inversions)}")
for nct, k, x, y in sorted(inversions)[:40]:
    print(f"  {nct}  {k:<12} {x:<22} -> {y}")
