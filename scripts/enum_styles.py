"""Enumerate the enumeration styles that actually occur in registry eligibility
text, and how often each one hides inside a unit rather than starting a line.

`markers.iter_criteria` splits on newlines only. A style that appears at the
START of a line is therefore already separated; the same style appearing INSIDE
a yielded unit is not, and that is the defect under investigation. So every
style is counted twice — leading and intra-unit — and the intra-unit column is
the one that matters.

Two measurement traps, both hit on the first run of this script and both fixed
here rather than reported around:

  * leading markers must be read off the RAW lines. `iter_criteria` strips
    `^[-*•–—\\d.)( ]+` before yielding, so asking a yielded unit whether it
    starts with a marker measures the strip, not the corpus (it reported 99.57%
    prose).
  * a unit's style bucket must come from that UNIT's own matches. Reading a
    trial-level accumulator labels every later unit with every style seen
    earlier in the same trial.

Styles are read off the corpus, not written down from expectation: the
unclassified bucket exists so a style nobody predicted shows up as a number
rather than as silence.

    python scripts/enum_styles.py            # colorectal
    python scripts/enum_styles.py --all      # whole store
"""

from __future__ import annotations

import re
import sqlite3
import sys
from collections import Counter, defaultdict

sys.path.insert(0, ".")

from medrag.markers import MARKERS, _compiled, iter_criteria  # noqa: E402

DB = "data/raw/trials.db"

# Roman numerals only up to xx: eligibility sublists do not run longer, and a
# permissive pattern starts matching ordinary words ("mix.", "did.").
_ROMAN = r"(?:i|ii|iii|iv|v|vi|vii|viii|ix|x|xi|xii|xiii|xiv|xv|xvi|xvii|xviii|xix|xx)"

# The intra-unit patterns require a sentence boundary or a double space before
# the marker. Without that guard "iv." matches inside "...received iv. therapy"
# and the count measures the pattern rather than the corpus.
_BOUNDARY = r"(?:(?<=[.;:!?])\s+|(?<=\s\s))"

# Each style is (name, leading_regex, intra_unit_regex).
STYLES = [
    ("roman_lower",
     re.compile(rf"^{_ROMAN}[.)]\s", re.I),
     re.compile(rf"{_BOUNDARY}{_ROMAN}[.)]\s+(?=[A-Z(])")),
    ("roman_paren",
     re.compile(rf"^\({_ROMAN}\)\s", re.I),
     re.compile(rf"\({_ROMAN}\)\s+(?=[A-Z(])", re.I)),
    ("arabic_dot",
     re.compile(r"^\d{1,2}[.)]\s"),
     re.compile(rf"{_BOUNDARY}\d{{1,2}}[.)]\s+(?=[A-Z(])")),
    ("arabic_paren",
     re.compile(r"^\(\d{1,2}\)\s"),
     re.compile(r"\(\d{1,2}\)\s+(?=[A-Z(])")),
    # 'i' and 'v' are excluded from the lettered intra-unit class so a roman
    # numeral is not double-counted as a letter.
    ("lettered",
     re.compile(r"^[a-zA-Z][.)]\s"),
     re.compile(rf"{_BOUNDARY}[a-hj-uwyzA-HJ-UWYZ][.)]\s+(?=[A-Z(])")),
    ("lettered_paren",
     re.compile(r"^\([a-zA-Z]\)\s"),
     re.compile(r"\([a-hj-uwyzA-HJ-UWYZ]\)\s+(?=[A-Z(])")),
    ("bulleted",
     re.compile(r"^[•▪◦○*]\s"),
     re.compile(r"\s[•▪◦○*]\s+")),
    ("dash_led",
     re.compile(r"^[-–—]\s"),
     re.compile(r"(?:(?<=[.;])\s*|(?<=\s\s))[-–—]\s*(?=[A-Z(])")),
    ("semicolon",
     None,
     re.compile(r";\s+(?=\S)")),
]

_LEADING_ANY = re.compile(
    rf"^(?:\({_ROMAN}\)|{_ROMAN}[.)]|\(?\d{{1,2}}[.)]|\(?[a-zA-Z][.)]|[•▪◦○*]|[-–—])\s",
    re.I,
)

# A plain sentence boundary is not an enumeration style, but NCT06257758 mixes
# polarity across one with no marker at all, so it belongs in the same table or
# the table understates the problem.
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")

_EXCLUDING = re.compile(
    r"\b(are not eligible|is not eligible|not be eligible|will be excluded|"
    r"are excluded|is excluded|be excluded|must not|may not|cannot|can not|"
    r"ineligible|are not permitted|is not permitted|are not allowed|"
    r"not allowed|exclusion)\b",
    re.I,
)
_ADMITTING = re.compile(
    r"\b(must have|must be|are eligible|is eligible|will be eligible|"
    r"are required|is required|should have|must demonstrate|are enrolled|"
    r"are included|will be included|eligible to)\b",
    re.I,
)

_MARKER_RES = [_compiled(m) for m in MARKERS.values()]


def _mentions_marker(text: str) -> bool:
    return any(r.search(text) for r in _MARKER_RES)


def rows(conn, set_key):
    if set_key is None:
        return conn.execute(
            "SELECT nct_id, eligibility_criteria FROM trials "
            "WHERE eligibility_criteria IS NOT NULL AND eligibility_criteria != ''")
    return conn.execute(
        "SELECT t.nct_id, t.eligibility_criteria FROM trials t "
        "JOIN trial_query_sets q USING (nct_id) WHERE q.set_key = ? "
        "AND t.eligibility_criteria IS NOT NULL AND t.eligibility_criteria != ''",
        (set_key,))


def main() -> None:
    set_key = None if "--all" in sys.argv else "colorectal"
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    trials_leading: Counter = Counter()
    trials_intra: Counter = Counter()
    units_intra: Counter = Counter()
    lines_leading: Counter = Counter()

    total_trials = total_units = total_lines = 0
    prose_lines = 0
    multi_sentence_units = 0
    mixed_units = 0
    mixed_marker_units = 0
    marker_units = 0
    mixed_buckets: Counter = Counter()
    mixed_marker_buckets: Counter = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    marker_examples: list[str] = []

    for r in rows(conn, set_key):
        total_trials += 1
        text = r["eligibility_criteria"]
        seen_leading, seen_intra = set(), set()

        # Leading styles off the RAW lines, before iter_criteria strips them.
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            total_lines += 1
            hit = False
            for name, lead_re, _ in STYLES:
                if lead_re is not None and lead_re.match(line):
                    seen_leading.add(name)
                    lines_leading[name] += 1
                    hit = True
            if not hit and not _LEADING_ANY.match(line):
                prose_lines += 1

        for _section, unit in iter_criteria(text):
            total_units += 1
            sentences = _SENTENCE.split(unit)
            if len(sentences) > 1:
                multi_sentence_units += 1

            own_styles = set()
            for name, _lead, intra_re in STYLES:
                if intra_re.search(unit):
                    own_styles.add(name)
                    units_intra[name] += 1
            seen_intra |= own_styles

            has_marker = _mentions_marker(unit)
            if has_marker:
                marker_units += 1

            if _ADMITTING.search(unit) and _EXCLUDING.search(unit):
                mixed_units += 1
                bucket = ",".join(sorted(own_styles)) or (
                    "sentence_boundary_only" if len(sentences) > 1
                    else "no_separator_at_all")
                mixed_buckets[bucket] += 1
                if len(examples[bucket]) < 2:
                    examples[bucket].append(f"{r['nct_id']}: {unit[:190]}")
                if has_marker:
                    mixed_marker_units += 1
                    mixed_marker_buckets[bucket] += 1
                    if len(marker_examples) < 12:
                        marker_examples.append(f"{r['nct_id']} [{bucket}]: {unit[:190]}")

        for n in seen_leading:
            trials_leading[n] += 1
        for n in seen_intra:
            trials_intra[n] += 1

    label = set_key or "ALL SETS"
    print(f"population: {label}   trials with eligibility text: {total_trials}")
    print(f"non-blank lines: {total_lines}   units yielded by iter_criteria: {total_units}\n")

    print(f"{'style':<16} {'lines w/ leading':>17} {'trials w/ leading':>18} "
          f"{'units w/ INTRA':>15} {'trials w/ INTRA':>16}")
    for name, _l, _i in STYLES:
        print(f"{name:<16} {lines_leading[name]:>17} {trials_leading[name]:>18} "
              f"{units_intra[name]:>15} {trials_intra[name]:>16}")

    def pct(n, d):
        return f"{100.0 * n / d:.2f}%" if d else "-"

    print(f"\nlines with no leading marker at all:  {prose_lines:>7}  "
          f"{pct(prose_lines, total_lines)} of lines")
    print(f"units spanning >1 sentence:           {multi_sentence_units:>7}  "
          f"{pct(multi_sentence_units, total_units)} of units")
    print(f"units naming a curated marker:        {marker_units:>7}  "
          f"{pct(marker_units, total_units)} of units")
    print(f"units w/ BOTH admitting+excluding cue:{mixed_units:>7}  "
          f"{pct(mixed_units, total_units)} of units")
    print(f"  ... of those, naming a marker:      {mixed_marker_units:>7}  "
          f"{pct(mixed_marker_units, marker_units)} of marker units")

    print("\nmixed-polarity units by what separates the clauses "
          "(all / marker-bearing):")
    for bucket, n in mixed_buckets.most_common():
        print(f"  {bucket:<42} {n:>6} / {mixed_marker_buckets[bucket]:>4}")
        for e in examples[bucket][:1]:
            print(f"      e.g. {e}")

    print("\nmarker-bearing mixed-polarity units — the population that can "
          "actually flip a verdict:")
    for e in marker_examples:
        print(f"  {e}")


if __name__ == "__main__":
    main()
