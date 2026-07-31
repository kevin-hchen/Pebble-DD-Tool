"""Trial-side biomarker gating census over eligibility text.

This is the indication-first counterpart to `biomarker.py`. That module asks a
patient-side question — "does an MSS patient qualify for this trial?" and answers
ELIGIBLE / EXCLUDED / UNCLEAR. This one asks a landscape question — "what does
this trial gate on?" — and, for each marker that gates colorectal trials, returns
exactly one of:

    REQUIRED       the marker is named as an inclusion criterion
    EXCLUDED       the marker is named as an exclusion criterion (or negated in
                   an inclusion line, e.g. "non-MSI-H")
    NOT_MENTIONED  the eligibility text does not name the marker at all

NOT_MENTIONED is the load-bearing state. It is NOT "eligible", it is "we could
not read a gate off the text" — the same distinction as ValidationReport.assessed
and NegativeEvidence.searched. Nothing downstream may fold a NOT_MENTIONED marker
into a REQUIRED (or EXCLUDED) set; a landscape that quietly counts unparsed trials
as matching is worse than one that reports the gap. There is a regression test.

It is regex and keyword matching only, deliberately. The output is a filter that
narrows a count, and a hallucinated eligibility flag is unauditable — so the
matched span is stored beside every REQUIRED/EXCLUDED call and a human can check
it. Section detection (Inclusion vs Exclusion) reuses `biomarker.py` so there is
one implementation of that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .biomarker import _iter_criteria  # one implementation of section splitting

REQUIRED = "REQUIRED"
EXCLUDED = "EXCLUDED"
NOT_MENTIONED = "NOT_MENTIONED"

# The markers that actually gate colorectal-cancer trials. The key is a SQL- and
# token-safe identifier (no spaces or colons); `label` is what a human reads.
# `patterns` name the marker; they do not encode direction — REQUIRED vs EXCLUDED
# is decided from the section and from an in-line negation.


@dataclass(frozen=True)
class MarkerDef:
    key: str
    label: str
    patterns: tuple[str, ...]


MARKERS: tuple[MarkerDef, ...] = (
    MarkerDef("MSS", "MSS / pMMR", (
        r"\bMSS\b", r"microsatellite[\s\-]?stable", r"\bpMMR\b",
        r"proficient\s+mismatch[\s\-]?repair", r"mismatch[\s\-]?repair[\s\-]?proficient",
    )),
    MarkerDef("MSI_H", "MSI-H / dMMR", (
        r"\bMSI[\s\-]?H(?:igh)?\b", r"microsatellite\s+instabilit\w*[\s\-]?high",
        r"\bdMMR\b", r"deficient\s+mismatch[\s\-]?repair", r"mismatch[\s\-]?repair[\s\-]?deficient",
    )),
    MarkerDef("RAS", "RAS (KRAS/NRAS)", (
        r"\bK?RAS\b", r"\bNRAS\b",
    )),
    MarkerDef("BRAF_V600E", "BRAF V600E", (
        r"BRAF[\s\-]?V600E?", r"\bV600E\b",
    )),
    MarkerDef("HER2_AMP", "HER2 amplification", (
        r"HER2[\s\-]?(?:amplif\w*|positive|\bpos\b|\+)", r"ERBB2[\s\-]?amplif\w*",
    )),
    MarkerDef("KRAS_G12C", "KRAS G12C", (
        r"KRAS[\s\-]?G12C", r"\bG12C\b",
    )),
    MarkerDef("KRAS_G12D", "KRAS G12D", (
        r"KRAS[\s\-]?G12D", r"\bG12D\b",
    )),
)

MARKER_KEYS = tuple(m.key for m in MARKERS)
MARKER_LABELS = {m.key: m.label for m in MARKERS}

_COMPILED = {m.key: re.compile("|".join(m.patterns), re.IGNORECASE) for m in MARKERS}

# A negation immediately before the marker flips an inclusion mention to
# EXCLUDED: "non-MSI-H" required means MSI-H is what the trial does NOT want.
_NEGATION = re.compile(r"(?:\bnon[\s\-]?|\bnot\s+|\bno\s+|\bwithout\s+|\babsence of\s+)$",
                       re.IGNORECASE)


@dataclass
class MarkerFlag:
    marker: str          # key, e.g. "MSI_H"
    status: str          # REQUIRED | EXCLUDED | NOT_MENTIONED
    span: str = ""       # the criterion sentence that decided it, for auditing

    @property
    def label(self) -> str:
        return MARKER_LABELS.get(self.marker, self.marker)


def _negated(sentence: str, match_start: int) -> bool:
    """Is the marker match negated by the words right before it?"""
    return bool(_NEGATION.search(sentence[:match_start]))


def gate_markers(eligibility_text: str) -> dict[str, MarkerFlag]:
    """Classify every registered marker for one trial's eligibility text.

    Always returns all markers — a marker the text never names comes back
    NOT_MENTIONED, never omitted, so a caller cannot mistake absence for a miss.
    On a genuine conflict (a marker named as both required and excluded) REQUIRED
    wins, because for "which trials require marker X" a false negative hides a real
    trial; the span records which sentence was chosen.
    """
    signals: dict[str, list[tuple[str, str]]] = {m.key: [] for m in MARKERS}

    for section, sentence in _iter_criteria(eligibility_text):
        for mkey, rx in _COMPILED.items():
            m = rx.search(sentence)
            if not m:
                continue
            negated = _negated(sentence, m.start())
            if section == "exclusion" or negated:
                status = EXCLUDED
            elif section == "inclusion":
                status = REQUIRED
            else:
                # No heading and no negation: a bare marker mention in eligibility
                # is overwhelmingly a requirement. Recorded with its span so it is
                # auditable rather than hidden.
                status = REQUIRED
            signals[mkey].append((status, sentence.strip()))

    out: dict[str, MarkerFlag] = {}
    for mkey in MARKER_KEYS:
        hits = signals[mkey]
        if not hits:
            out[mkey] = MarkerFlag(mkey, NOT_MENTIONED)
            continue
        required = next((s for st, s in hits if st == REQUIRED), None)
        if required is not None:
            out[mkey] = MarkerFlag(mkey, REQUIRED, required)
        else:
            out[mkey] = MarkerFlag(mkey, EXCLUDED, hits[0][1])
    return out


# Delimiters chosen so a LIKE '% KEY:STATUS %' filter cannot collide across
# markers (e.g. RAS vs KRAS_G12C). The string is space-padded on both ends.
def gating_tokens(flags: dict[str, MarkerFlag]) -> str:
    """A space-delimited, space-padded token string for SQL LIKE filtering:
    ' MSS:REQUIRED MSI_H:EXCLUDED RAS:NOT_MENTIONED ... '."""
    return " " + " ".join(f"{k}:{flags[k].status}" for k in MARKER_KEYS) + " "


def gating_token(marker: str, status: str) -> str:
    """The exact substring a landscape filter matches against `gating_tokens`."""
    return f" {marker}:{status} "
