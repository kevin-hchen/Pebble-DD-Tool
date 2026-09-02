"""Is the non-Latin-script marker defect LIVE in the ClinicalTrials.gov store,
or still prospective?

Demonstrated on constructed registry-shaped text: CJK eligibility text does not
reach silence. `markers._context` returns "unknown" because the English cue
regexes match nothing, the English negation grammar matches nothing, and
`_classify` falls through to REQUIRED — on records that EXCLUDE the marker. And
`\\b` fails when a Latin-script marker name abuts a CJK character, because Python
treats ideographs as word characters, so "HER2阳性" does not match `\\bHER2\\b` and
the record comes back NOT_MENTIONED while plainly naming the marker.

This measures whether either is happening inside the store we actually hold.

    python scripts/nonlatin_census.py
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, ".")

from medrag.biomarker_gating import gate_markers  # noqa: E402
from medrag.markers import MARKERS, _compiled, _is_acronym_plural  # noqa: E402

DB = "data/raw/trials.db"
DIRECTIONS = {"REQUIRED", "EXCLUDED", "ELIGIBLE_BY_EXCLUSION"}

# Script families, by codepoint range. Latin, digits, punctuation and the
# general symbol blocks are deliberately absent — this asks "what non-Latin
# writing system is present", not "what characters are present".
SCRIPTS = {
    "Han (Chinese/Kanji)": [(0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF)],
    "Hiragana":            [(0x3040, 0x309F)],
    "Katakana":            [(0x30A0, 0x30FF)],
    "Hangul (Korean)":     [(0x1100, 0x11FF), (0xAC00, 0xD7AF)],
    "Cyrillic":            [(0x0400, 0x04FF)],
    "Greek":               [(0x0370, 0x03FF)],
    "Arabic":              [(0x0600, 0x06FF)],
    "Hebrew":              [(0x0590, 0x05FF)],
    "Devanagari":          [(0x0900, 0x097F)],
    "Thai":                [(0x0E00, 0x0E7F)],
}
_SCRIPT_RES = {
    name: re.compile("[" + "".join(f"\\u{lo:04x}-\\u{hi:04x}" for lo, hi in rs) + "]")
    for name, rs in SCRIPTS.items()
}

# A single character of a script is not that script being USED. Greek is the
# case that forces this: a first pass counted 10,072 "Greek" trials, and every
# one was "TGF-β", "β-hCG", "α", "μL" — Greek letters as scientific symbols
# inside ordinary English. The question here is whether eligibility text is
# WRITTEN in a non-Latin script, so a trial qualifies only with a run of
# consecutive characters and a meaningful total.
_MIN_RUN = 4
_MIN_TOTAL = 20
_RUN_RES = {name: re.compile(rx.pattern + "{%d,}" % _MIN_RUN)
            for name, rx in _SCRIPT_RES.items()}

# CJK-adjacent characters that break `\b`: ideographs, kana, hangul, and the
# fullwidth/CJK punctuation blocks that surround them in real records.
_WORDY_NONLATIN = re.compile(
    r"[㐀-䶿一-鿿豈-﫿぀-ゟ゠-ヿ"
    r"ᄀ-ᇿ가-힯Ѐ-ӿ]")

_MARKER_RES = {k: _compiled(m) for k, m in MARKERS.items()}
# The same patterns with the word-boundary anchors removed, so a match that `\b`
# suppresses can be seen.
_LOOSE_RES = {
    k: re.compile("|".join(p.replace(r"\b", "") for p in m.positive), re.IGNORECASE)
    for k, m in MARKERS.items()
}


def scripts_in(text: str) -> set[str]:
    """Script families the text is actually WRITTEN in — see `_MIN_RUN`."""
    out = set()
    for name, rx in _SCRIPT_RES.items():
        if (_RUN_RES[name].search(text)
                and len(rx.findall(text)) >= _MIN_TOTAL):
            out.add(name)
    return out


def symbol_only_scripts(text: str) -> set[str]:
    """Present but never in a run — scientific symbols, not a writing system."""
    return {name for name, rx in _SCRIPT_RES.items()
            if rx.search(text) and name not in scripts_in(text)}


def boundary_suppressed(text: str) -> list[tuple[str, str]]:
    """Marker names present in `text` that the shipped pattern cannot see
    because a non-Latin word character sits against them."""
    out = []
    for key, loose in _LOOSE_RES.items():
        strict = _MARKER_RES[key]
        for m in loose.finditer(text):
            tok = m.group(0)
            if _is_acronym_plural(tok):
                continue
            before = text[m.start() - 1] if m.start() else ""
            after = text[m.end()] if m.end() < len(text) else ""
            if not (_WORDY_NONLATIN.match(before or " ")
                    or _WORDY_NONLATIN.match(after or " ")):
                continue
            # Present to a reader, invisible to the matcher.
            if not any(sm.group(0) == tok and sm.start() == m.start()
                       for sm in strict.finditer(text)):
                out.append((key, text[max(0, m.start() - 25):m.end() + 25]))
    return out


def main() -> None:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM trials WHERE eligibility_criteria IS NOT NULL "
        "AND eligibility_criteria != ''").fetchall()

    by_script: Counter = Counter()
    symbol_only: Counter = Counter()
    trials_nonlatin = []
    for r in rows:
        found = scripts_in(r["eligibility_criteria"])
        for s in symbol_only_scripts(r["eligibility_criteria"]):
            symbol_only[s] += 1
        if found:
            for s in found:
                by_script[s] += 1
            trials_nonlatin.append((r, found))

    print(f"trials with eligibility text: {len(rows)}")
    print(f"trials whose eligibility text is WRITTEN in a non-Latin script: "
          f"{len(trials_nonlatin)}\n")
    print(f"{'script family':22} {'written in':>11} {'symbols only':>13}")
    for name in SCRIPTS:
        print(f"  {name:20} {by_script[name]:>11} {symbol_only[name]:>13}")
    print("\n'symbols only' = the script appears but never in a run of "
          f"{_MIN_RUN}+ characters — Greek letters as scientific notation "
          "(TGF-β, β-hCG, α, μL), not a writing system in use.")

    # --- do those trials carry marker verdicts, and which way ---
    verdicts: Counter = Counter()
    carrying = []
    for r, found in trials_nonlatin:
        flags = gate_markers(
            r["eligibility_criteria"] or "",
            detailed_description=r["detailed_description"] or "",
            brief_summary=r["brief_summary"] or "",
            keywords=json.loads(r["keywords"] or "[]"))
        live = {k: v for k, v in flags.items() if v.status != "NOT_MENTIONED"}
        for k, v in live.items():
            verdicts[v.status] += 1
        if live:
            carrying.append((r, found, live))

    print(f"\nof those, trials carrying at least one marker verdict: "
          f"{len(carrying)}")
    print("verdicts by direction:")
    for status in ("REQUIRED", "EXCLUDED", "ELIGIBLE_BY_EXCLUSION",
                   "NOT_ASSESSABLE"):
        mark = "   <-- the dangerous direction" if status == "REQUIRED" else ""
        print(f"  {status:22} {verdicts[status]:>5}{mark}")

    if carrying:
        print("\nevery non-Latin trial carrying a verdict:")
        for r, found, live in carrying:
            print(f"\n  {r['nct_id']}  [{', '.join(sorted(found))}]")
            print(f"    title: {r['brief_title'][:88]}")
            for k, v in sorted(live.items()):
                print(f"    {k:<11} {v.status:<22} source={v.source}")
                print(f"        span: {v.span[:150]}")

    # --- the \b boundary failure ---
    suppressed = []
    for r, found in trials_nonlatin:
        hits = boundary_suppressed(r["eligibility_criteria"] or "")
        if hits:
            suppressed.append((r, hits))

    print(f"\n\ntrials where a marker name abuts a non-Latin character and the "
          f"\\b anchor suppresses it: {len(suppressed)}")
    for r, hits in suppressed[:25]:
        print(f"\n  {r['nct_id']}  {r['brief_title'][:70]}")
        for key, ctx in hits[:3]:
            print(f"    {key}: ...{ctx}...")
    if len(suppressed) > 25:
        print(f"\n  ... and {len(suppressed) - 25} more")


if __name__ == "__main__":
    main()
