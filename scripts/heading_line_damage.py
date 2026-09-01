"""Measure the two `iter_criteria` heading-line defects found by hand-reading.

Neither is the under-splitting defect this work was scoped to. Both are in the
same function, both change marker verdicts, and both were found by reading
complete records rather than by any automated gate.

`iter_criteria` treats ANY line containing the substring "inclusion criteria" or
"exclusion criteria" as a section heading. Two consequences:

  A. SECTION FLIP. A criterion that merely mentions the phrase in passing -- eg
     "...provided that they meet other inclusion and exclusion criteria" -- sets
     the section state for every unit that follows. NCT07127822, whose title is
     "Assessing Iparomlimab and Tuvonralimab in Recurrent or Metastatic
     MSI-H/dMMR Gastric Cancer" and whose inclusion criterion 5 reads "Confirmed
     by PCR or NGS as microsatellite instability-high (MSI-H)", is read as
     MSI_H: EXCLUDED because of it.

  B. COLON TRUNCATION. On a line judged to be a heading, only the text after the
     FIRST colon is yielded; everything before it is discarded and never
     reaches any marker. On a single-line record that is most of the criteria.

    python scripts/heading_line_damage.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, ".")

from medrag.biomarker_gating import gate_markers  # noqa: E402
from medrag.markers import MARKERS, _compiled  # noqa: E402

DB = "data/raw/trials.db"
_MARKER_RES = {k: _compiled(m) for k, m in MARKERS.items()}


def classify_line(line: str):
    """Mirror iter_criteria's heading test, and say whether it is a real heading.

    A real heading is one where the phrase starts the line (modulo an
    enumeration marker or a qualifier like "Key" / "Participant"). Anything else
    is a criterion that happens to contain the phrase.
    """
    low = line.lower()
    for phrase in ("inclusion criteria", "exclusion criteria"):
        i = low.find(phrase)
        if i < 0:
            continue
        prefix = line[:i]
        # Allow a short leading qualifier: "Key Exclusion Criteria:",
        # "3. Inclusion Criteria", "Participant Exclusion Criteria".
        is_heading = len(prefix.strip(" -*0123456789.)(\t")) <= 14
        return phrase, is_heading, prefix
    return None, None, None


def main() -> None:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    flip_trials = set()
    truncation_trials = set()
    truncated_chars = 0
    truncated_marker_trials = set()
    flip_marker_trials = set()
    per_phrase: Counter = Counter()
    examples: list[str] = []

    rows = conn.execute(
        "SELECT nct_id, eligibility_criteria FROM trials "
        "WHERE eligibility_criteria IS NOT NULL AND eligibility_criteria != ''")
    for r in rows:
        nct, text = r["nct_id"], r["eligibility_criteria"]
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            phrase, is_heading, prefix = classify_line(line)
            if phrase is None:
                continue
            if not is_heading:
                flip_trials.add(nct)
                per_phrase[phrase] += 1
                if any(rx.search(line) for rx in _MARKER_RES.values()):
                    flip_marker_trials.add(nct)
                if len(examples) < 6:
                    examples.append(f"{nct}: ...{line[max(0, len(prefix) - 60):][:150]}")
            # Colon truncation applies whenever iter_criteria calls it a
            # heading, real or not: the text before the colon is dropped.
            if ":" in line:
                dropped = line.split(":", 1)[0]
                # The phrase itself is expected before the colon on a real
                # heading; only count text beyond it as lost.
                lost = dropped[len(prefix or ""):] if not is_heading else ""
                if not is_heading and len(dropped.strip()) > 25:
                    truncation_trials.add(nct)
                    truncated_chars += len(dropped)
                    if any(rx.search(dropped) for rx in _MARKER_RES.values()):
                        truncated_marker_trials.add(nct)
                del lost

    print(f"A. SECTION FLIP -- the phrase appears mid-line, in a criterion")
    print(f"   trials affected:                       {len(flip_trials):>7}")
    print(f"   ... whose flipping line names a marker:{len(flip_marker_trials):>7}")
    for p, n in per_phrase.most_common():
        print(f"   lines containing '{p}': {n}")
    print("\n   examples:")
    for e in examples:
        print(f"     {e}")

    print(f"\nB. COLON TRUNCATION -- text before the first colon is discarded")
    print(f"   trials where >25 chars of criterion text is dropped: "
          f"{len(truncation_trials):>7}")
    print(f"   ... where the dropped text names a marker:           "
          f"{len(truncated_marker_trials):>7}")
    print(f"   total characters discarded store-wide:               {truncated_chars:>7}")

    # The worked case, end to end.
    r = conn.execute(
        "SELECT * FROM trials WHERE nct_id = 'NCT07127822'").fetchone()
    flags = gate_markers(
        r["eligibility_criteria"], detailed_description=r["detailed_description"] or "",
        brief_summary=r["brief_summary"] or "",
        keywords=json.loads(r["keywords"] or "[]"))
    print("\nWORKED CASE  NCT07127822")
    print(f"   title:   {r['brief_title']}")
    print(f"   MSI_H:   {flags['MSI_H'].status}   (record says inclusion criterion 5: "
          f"'Confirmed by PCR or NGS as MSI-H')")
    print(f"   MSS:     {flags['MSS'].status}")


if __name__ == "__main__":
    main()
