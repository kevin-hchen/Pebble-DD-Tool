"""Draw the criteria-segmentation ground-truth sample, stratified by separator
class, and emit a dossier that prints COMPLETE records.

The label vocabulary and the unit definition were fixed first, in
`tests/fixtures/criteria_segmentation.README.md`, and committed before this
script ran.

Two properties this script exists to guarantee:

  * **The draw is deterministic.** Sorted by nct_id then by unit index, taken on
    an even stride. No RNG, so re-running reproduces the same sample and a
    disagreement about a label is about the label.
  * **The dossier is not truncated.** Every unit is printed verbatim and whole,
    beside the complete eligibility text of its trial. The 28-trial hand-read
    was performed against 170-character excerpts and two rows were labelled from
    an excerpt that stopped before the clause that decided them — and a trailing
    exclusion clause is precisely what this sample is drawn to find. See
    docs/DECISIONS.md.

    python scripts/draw_segmentation_sample.py
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, ".")

from medrag.markers import MARKERS, _compiled, iter_criteria  # noqa: E402
from scripts.enum_styles import (  # noqa: E402
    _ADMITTING,
    _EXCLUDING,
    _SENTENCE,
    STYLES,
)

DB = "data/raw/trials.db"
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/segsample")

_ENUM_STYLES = [(n, r) for n, _l, r in STYLES if n != "semicolon"]
_SEMI = next(r for n, _l, r in STYLES if n == "semicolon")

_MARKER_RES = {k: _compiled(m) for k, m in MARKERS.items()}

# Sizes. ENUMERATION is taken whole because it is the smallest class and holds a
# named acceptance case; NONE is over-sampled relative to its share because it
# is the residual and the residual is the thing most likely to be assumed away.
TARGET = {
    "MIXED/ENUMERATION": 12,
    "MIXED/SEMICOLON": 10,
    "MIXED/SENTENCE": 10,
    "MIXED/NONE": 10,
    "OVERSPLIT_RISK/SENTENCE": 10,
    "OVERSPLIT_RISK/SEMICOLON": 8,
}

# Forced in: one acceptance case per separator class. The first two are named in
# the brief; the second two are the first record of their class in nct_id order,
# chosen by that rule rather than by what they contain.
FORCED = {"NCT05700669", "NCT06257758"}


def markers_in(text: str) -> list[str]:
    return [k for k, r in _MARKER_RES.items() if r.search(text)]


def separator_class(unit: str) -> str:
    """Precedence: an enumeration marker outranks a semicolon outranks a plain
    sentence boundary. A unit with none of the three has two clauses inside one
    sentence with nothing between them, which no splitting rule reaches."""
    if any(r.search(unit) for _n, r in _ENUM_STYLES):
        return "ENUMERATION"
    if _SEMI.search(unit):
        return "SEMICOLON"
    if len(_SENTENCE.split(unit)) > 1:
        return "SENTENCE"
    return "NONE"


def main() -> None:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    sets: dict[str, list[str]] = {}
    for r in conn.execute("SELECT nct_id, set_key FROM trial_query_sets"):
        sets.setdefault(r["nct_id"], []).append(r["set_key"])

    pools: dict[str, list[dict]] = {k: [] for k in TARGET}

    rows = conn.execute(
        "SELECT nct_id, eligibility_criteria FROM trials "
        "WHERE eligibility_criteria IS NOT NULL AND eligibility_criteria != '' "
        "ORDER BY nct_id")
    for r in rows:
        for idx, (section, unit) in enumerate(iter_criteria(r["eligibility_criteria"])):
            named = markers_in(unit)
            if not named:
                continue
            mixed = bool(_ADMITTING.search(unit) and _EXCLUDING.search(unit))
            cls = separator_class(unit)
            if mixed:
                key = f"MIXED/{cls}"
            elif cls in ("SENTENCE", "SEMICOLON"):
                key = f"OVERSPLIT_RISK/{cls}"
            else:
                continue
            if key not in pools:
                continue
            pools[key].append({
                "nct_id": r["nct_id"],
                "set_key": ",".join(sorted(sets.get(r["nct_id"], []))),
                "unit_index": idx,
                "section": section,
                "unit": unit,
                "separator_class": cls if mixed else "NOT_MIXED",
                "observed_separator": cls,
                "markers_named": named,
            })

    OUT.mkdir(parents=True, exist_ok=True)
    drawn: list[dict] = []

    for key, want in TARGET.items():
        pool = pools[key]
        forced = [p for p in pool if p["nct_id"] in FORCED]
        rest = [p for p in pool if p["nct_id"] not in FORCED]
        take = forced[:]
        room = max(0, want - len(take))
        if room:
            if len(rest) <= room:
                take += rest
            else:
                # Even stride over the nct_id-sorted pool: deterministic, and it
                # spreads the draw across registration eras rather than
                # clustering on whichever decade happens to sort first.
                step = len(rest) / room
                take += [rest[int(i * step)] for i in range(room)]
        for p in take:
            p["stratum"] = key.split("/")[0]
        drawn += take
        print(f"{key:<28} pool {len(pool):>5}   drawn {len(take):>3}"
              f"   forced {len(forced)}")

    (OUT / "sample.json").write_text(json.dumps(drawn, indent=2))
    print(f"\ntotal drawn: {len(drawn)}  ->  {OUT/'sample.json'}")

    # One dossier per stratum, so the reading is done in passes rather than in
    # one scroll. Complete units, complete eligibility text, no elision.
    by_key: dict[str, list[dict]] = {}
    for d in drawn:
        by_key.setdefault(f"{d['stratum']}/{d['observed_separator']}", []).append(d)

    for key, items in by_key.items():
        fname = key.replace("/", "__") + ".txt"
        lines = [f"# {key}   ({len(items)} units)", ""]
        for i, d in enumerate(items, 1):
            full = conn.execute(
                "SELECT brief_title, eligibility_criteria FROM trials WHERE nct_id = ?",
                (d["nct_id"],)).fetchone()
            lines += [
                "=" * 78,
                f"[{i}/{len(items)}] {d['nct_id']}   sets: {d['set_key']}",
                f"title: {full['brief_title']}",
                f"section as tagged by iter_criteria: {d['section']}   "
                f"unit_index: {d['unit_index']}",
                f"markers named: {', '.join(d['markers_named'])}",
                "",
                "---- THE UNIT, VERBATIM AND COMPLETE ----",
                d["unit"],
                "",
                "---- COMPLETE eligibility_criteria FOR THE RECORD ----",
                full["eligibility_criteria"],
                "",
            ]
        (OUT / fname).write_text("\n".join(lines))
        print(f"  wrote {fname}")


if __name__ == "__main__":
    main()
