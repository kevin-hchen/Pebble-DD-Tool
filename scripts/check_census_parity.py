"""THE FULL-STORE CENSUS/LIVE PARITY SWEEP.

The exhaustive version of tests/test_census_live_parity.py: every family in the
real store, every curated marker, every record. Slow (~14 minutes over 241k
trials, 2.15M comparisons) so it is a script rather than a test — the fixture
version runs in CI, this one runs before shipping a change to either matcher.

    python scripts/check_census_parity.py

Exits non-zero on any divergence. Its first run found 124, all from
markers.resolve_marker substring-matching a query onto the wrong marker.

THE GATE: does the ingest-time census admit exactly what the live matcher admits?

For every family and every curated marker, compare:
  LIVE     match_biomarker(...) status in {ELIGIBLE, ELIGIBLE_BY_EXCLUSION, UNCLEAR}
  CENSUS   biomarker_gating token in {REQUIRED, ELIGIBLE_BY_EXCLUSION}
Any divergence means one of the two is wrong.
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from medrag.biomarker import ELIGIBLE, ELIGIBLE_BY_EXCLUSION, UNCLEAR, match_biomarker
from medrag.biomarker_gating import MARKER_KEYS, gating_token
from medrag.trials.store import TrialStore

ADMIT_LIVE = {ELIGIBLE, ELIGIBLE_BY_EXCLUSION, UNCLEAR}

s = TrialStore(os.getenv("MEDRAG_TRIALS_DB", "data/raw/trials.db"), read_only=True)
fams = [r[0] for r in s.conn.execute(
    "select set_key, count(*) c from trial_query_sets group by set_key order by c desc")]

report = {"families": len(fams), "markers": list(MARKER_KEYS), "divergences": [], "compared": 0}
t0 = time.time()
for fi, fam in enumerate(fams, 1):
    rows = s.conn.execute(
        "SELECT nct_id, eligibility_criteria, detailed_description, brief_summary, "
        "keywords, biomarker_gating FROM trials WHERE nct_id IN "
        "(SELECT nct_id FROM trial_query_sets WHERE set_key = ?)", (fam,)).fetchall()
    for marker in MARKER_KEYS:
        live, census = set(), set()
        req = gating_token(marker, "REQUIRED")
        exc = gating_token(marker, "ELIGIBLE_BY_EXCLUSION")
        for r in rows:
            m = match_biomarker(r["eligibility_criteria"], marker,
                                detailed_description=r["detailed_description"],
                                brief_summary=r["brief_summary"],
                                keywords=json.loads(r["keywords"] or "[]"))
            if m.status in ADMIT_LIVE:
                live.add(r["nct_id"])
            g = r["biomarker_gating"] or ""
            if req in g or exc in g:
                census.add(r["nct_id"])
        report["compared"] += len(rows)
        if live != census:
            only_live = sorted(live - census)[:5]
            only_census = sorted(census - live)[:5]
            report["divergences"].append({
                "family": fam, "marker": marker,
                "n_live": len(live), "n_census": len(census),
                "live_not_census": only_live, "census_not_live": only_census,
                "n_live_not_census": len(live - census),
                "n_census_not_live": len(census - live)})
    print(f"  [{fi}/{len(fams)}] {fam:<24} rows={len(rows):>6} "
          f"divergences so far={len(report['divergences'])}  ({time.time()-t0:.0f}s)", flush=True)
s.close()
report["seconds"] = round(time.time()-t0, 1)
json.dump(report, open(os.getenv("PARITY_REPORT", "/tmp/parity_report.json"),"w"), indent=2)
print("\nDIVERGENCES:", len(report["divergences"]))
for d in report["divergences"][:10]:
    print("  ", d)

sys.exit(1 if report["divergences"] else 0)
