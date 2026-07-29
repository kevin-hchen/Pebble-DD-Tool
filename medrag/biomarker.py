"""Biomarker matching over trial eligibility text, from the patient's side.

The patient HAS the biomarker (say MSS) and wants to know whether a trial's
eligibility would let them in. Registry eligibility is free text, and the same
molecular status is written a dozen ways — MSS, microsatellite stable, pMMR,
proficient mismatch repair, non-MSI-H — and just as often expressed *indirectly*,
by excluding the opposite ("no MSI-high tumors"). All of those have to resolve to
the same status.

Two rules shape the design:

  * ALWAYS return the criterion sentence that decided it. A filtered list with no
    shown evidence cannot be checked, and a patient (or their oncologist) must be
    able to read the exact line and judge it themselves.

  * When inclusion vs exclusion cannot be determined, the answer is UNCLEAR, not
    a silent drop. A missed trial is worse than an uncertain one here — the
    indirect "excludes MSI-H" phrasing implies the patient probably qualifies,
    but "probably" is UNCLEAR, and the patient sees the sentence and decides.

Statuses are from the patient's perspective:

  ELIGIBLE       their biomarker is affirmatively named as includable
  EXCLUDED       the trial requires the opposite, or excludes their biomarker
  UNCLEAR        the biomarker area is referenced but eligibility can't be read
                 off it (indirect phrasing, or an unlabelled criterion)
  NOT MENTIONED  the eligibility text does not reference the biomarker at all
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ELIGIBLE = "ELIGIBLE"
EXCLUDED = "EXCLUDED"
UNCLEAR = "UNCLEAR"
NOT_MENTIONED = "NOT MENTIONED"

# Cues that flip an unlabelled criterion into an inclusion or exclusion reading
# when there is no "Inclusion/Exclusion Criteria" heading to lean on.
_EXCL_CUES = re.compile(
    r"\b(exclu|must not|cannot|can not|ineligible|not eligible|without|"
    r"absence of|no evidence of|negative for|no known)\b",
    re.IGNORECASE,
)
_INCL_CUES = re.compile(
    r"\b(required|require|must have|must be|positive for|documented|confirmed|"
    r"eligible if|only if|known to be)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BiomarkerDef:
    key: str
    aliases: tuple[str, ...]     # user-facing names that resolve to this def
    positive: tuple[str, ...]    # regexes stating the patient's own biomarker
    opposite: tuple[str, ...]    # regexes for the opposite biomarker


# Microsatellite status. "non-MSI-H" is a POSITIVE variant (it means the patient
# is not MSI-high, i.e. MSS/MSI-L), and it deliberately appears before the bare
# MSI-H opposite pattern is tested — the matcher strips positive spans first so
# "non-MSI-H" is never misread as the MSI-H it contains.
MSS = BiomarkerDef(
    key="MSS",
    aliases=(
        "mss", "microsatellite stable", "microsatellite-stable", "microsatellite stability",
        "pmmr", "proficient mismatch repair", "mismatch repair proficient",
        "non-msi-h", "non msi-h", "not msi-h", "msi stable", "msi-stable",
    ),
    positive=(
        r"\bMSS\b",
        r"microsatellite[\s\-]?stable",
        r"\bpMMR\b",
        r"proficient\s+mismatch[\s\-]?repair",
        r"mismatch[\s\-]?repair[\s\-]?proficient",
        r"non[\s\-]?MSI[\s\-]?H(?:igh)?",
    ),
    opposite=(
        r"\bMSI[\s\-]?H(?:igh)?\b",
        r"microsatellite\s+instabilit\w*[\s\-]?high",
        r"\bdMMR\b",
        r"deficient\s+mismatch[\s\-]?repair",
        r"mismatch[\s\-]?repair[\s\-]?deficient",
    ),
)

_REGISTRY = (MSS,)


@dataclass
class BiomarkerMatch:
    status: str
    evidence: str        # the criterion sentence that decided the status
    biomarker: str       # canonical key when recognised, else the raw query

    @property
    def is_candidate(self) -> bool:
        """The patient could plausibly enter: their biomarker is named or the
        determination is uncertain. EXCLUDED and NOT MENTIONED are not candidates
        (one is a hard no, the other is out of scope for a biomarker landscape)."""
        return self.status in (ELIGIBLE, UNCLEAR)


def resolve(biomarker: str) -> BiomarkerDef | None:
    """Map a user's biomarker string to a known definition, or None."""
    norm = re.sub(r"\s+", " ", (biomarker or "").strip().lower())
    if not norm:
        return None
    for bdef in _REGISTRY:
        if norm == bdef.key.lower() or norm in bdef.aliases:
            return bdef
        # Tolerate a query that is one of the aliases embedded in a longer phrase.
        if any(a in norm or norm in a for a in bdef.aliases):
            return bdef
    return None


def _iter_criteria(text: str):
    """Yield (section, criterion_sentence) for each eligibility line.

    Tracks the Inclusion/Exclusion headings the registry almost always emits, and
    strips bullet and numbering markers so the returned sentence reads cleanly."""
    section = "unknown"
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


def _context(section: str, sentence: str) -> str:
    if section in ("inclusion", "exclusion"):
        return section
    if _EXCL_CUES.search(sentence):
        return "exclusion"
    if _INCL_CUES.search(sentence):
        return "inclusion"
    return "unknown"


def _outcome(context: str, has_positive: bool, has_opposite: bool) -> str:
    if has_positive and has_opposite:
        # e.g. "MSI-H or MSS accepted" — the patient may well qualify, but one
        # sentence naming both cannot be read cleanly. Keep it, flagged.
        return UNCLEAR
    if has_positive:
        # Their biomarker is named: excluded only if the criterion excludes it.
        return EXCLUDED if context == "exclusion" else ELIGIBLE
    # Only the opposite is named.
    if context == "inclusion":
        return EXCLUDED          # the trial requires the opposite -> patient is out
    return UNCLEAR               # excluded-opposite or unlabelled -> indirect


def match_biomarker(eligibility_text: str, biomarker: str) -> BiomarkerMatch:
    """Decide whether an MSS-type patient's biomarker admits them to this trial,
    and return the criterion sentence that decided it."""
    bdef = resolve(biomarker)
    if bdef is None:
        return _literal_match(eligibility_text, biomarker)

    pos_re = re.compile("|".join(bdef.positive), re.IGNORECASE)
    opp_re = re.compile("|".join(bdef.opposite), re.IGNORECASE)

    signals: list[tuple[str, str]] = []
    for section, sentence in _iter_criteria(eligibility_text):
        has_pos = bool(pos_re.search(sentence))
        # Strip positive spans before testing the opposite, so "non-MSI-H" (a
        # positive variant) is not counted as the MSI-H it literally contains.
        without_pos = pos_re.sub(" ", sentence)
        has_opp = bool(opp_re.search(without_pos))
        if not has_pos and not has_opp:
            continue
        signals.append((_outcome(_context(section, sentence), has_pos, has_opp), sentence))

    return _aggregate(signals, bdef.key)


def _aggregate(signals: list[tuple[str, str]], key: str) -> BiomarkerMatch:
    if not signals:
        return BiomarkerMatch(NOT_MENTIONED, "", key)

    statuses = {s for s, _ in signals}
    # A trial that both names the patient's biomarker as includable AND excludes
    # it elsewhere is contradictory; surface the exclusion sentence and call it
    # UNCLEAR rather than pick a side.
    if ELIGIBLE in statuses and EXCLUDED in statuses:
        excl = next(sent for st, sent in signals if st == EXCLUDED)
        return BiomarkerMatch(UNCLEAR, excl, key)

    for want in (ELIGIBLE, EXCLUDED, UNCLEAR):
        for status, sentence in signals:
            if status == want:
                return BiomarkerMatch(want, sentence, key)
    return BiomarkerMatch(NOT_MENTIONED, "", key)


def _literal_match(eligibility_text: str, biomarker: str) -> BiomarkerMatch:
    """Fallback for a biomarker with no registered variants. Without an 'opposite'
    to reason about, a mention is reported as UNCLEAR with its sentence — honest,
    since we cannot tell inclusion from exclusion for an arbitrary term."""
    term = re.sub(r"\s+", " ", (biomarker or "").strip())
    if not term:
        return BiomarkerMatch(NOT_MENTIONED, "", biomarker)
    pattern = re.compile(re.escape(term), re.IGNORECASE)
    for _section, sentence in _iter_criteria(eligibility_text):
        if pattern.search(sentence):
            return BiomarkerMatch(UNCLEAR, sentence, term)
    return BiomarkerMatch(NOT_MENTIONED, "", term)
