"""Biomarker matching over trial eligibility text, from the patient's side.

The patient HAS the biomarker (say MSS) and wants to know whether a trial's
eligibility would let them in. Registry eligibility is free text, and the same
molecular status is written a dozen ways — MSS, microsatellite stable, pMMR,
proficient mismatch repair, non-MSI-H — and just as often expressed *indirectly*,
by excluding the opposite ("no MSI-high tumors"). All of those have to resolve
to the same status. The vocabulary and negation grammar live in `markers.py`;
this module owns the patient-facing reduction of that shared signal set —
see `markers.py`'s docstring for why this module's precedence differs from
`biomarker_gating.py`'s.

Rules that shape the design:

  * ALWAYS return the criterion sentence that decided it, and which text field
    it came from when that field is not the formal eligibility criteria. A
    filtered list with no shown evidence cannot be checked.

  * The opposite-excluded pattern ("excludes MSI-H") is now its own state,
    ELIGIBLE_BY_EXCLUSION, not folded into ELIGIBLE (it is a weaker, indirect
    statement and the reader should be able to tell) or into UNCLEAR (it is not
    ambiguous — an oncologist reads "excludes MSI-H" as strong evidence an MSS
    patient qualifies, and burying it in UNCLEAR is how STELLAR-303 and
    HARMONi-GI3 — Phase 3 trials central to this population — used to vanish
    from a patient's MSS search).

  * UNCLEAR is reserved for a genuine contradiction in the source text (both an
    eligible-leaning and an excluded-leaning signal present) or an unresolved
    mixed mention ("MSI-H or MSS accepted" in one sentence). A missed trial is
    worse than an uncertain one, so it is kept, flagged, never dropped.

Statuses are from the patient's perspective:

  ELIGIBLE               their biomarker is affirmatively named as includable
  ELIGIBLE BY EXCLUSION  the opposite biomarker is excluded, naming theirs indirectly
  EXCLUDED                the trial requires the opposite, or excludes their biomarker
  UNCLEAR                 the source text genuinely contradicts itself
  NOT MENTIONED            the eligibility text does not reference the biomarker at all
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import markers as _m
from .markers import SOURCE_LABELS, MarkerDef, iter_criteria  # noqa: F401  (re-export for callers)

ELIGIBLE = "ELIGIBLE"
ELIGIBLE_BY_EXCLUSION = "ELIGIBLE BY EXCLUSION"
EXCLUDED = "EXCLUDED"
UNCLEAR = "UNCLEAR"
NOT_MENTIONED = "NOT MENTIONED"


@dataclass
class BiomarkerMatch:
    status: str
    evidence: str        # the criterion sentence that decided the status
    biomarker: str        # canonical key when recognised, else the raw query
    curated: bool = True  # False for the uncurated literal-text fallback
    source: str = ""      # which text field the evidence came from

    @property
    def is_candidate(self) -> bool:
        """The patient could plausibly enter: their biomarker is named,
        indirectly named by excluding the opposite, or the determination is
        uncertain. EXCLUDED and NOT MENTIONED are not candidates (one is a hard
        no, the other is out of scope for a biomarker landscape)."""
        return self.status in (ELIGIBLE, ELIGIBLE_BY_EXCLUSION, UNCLEAR)

    def _labelled_evidence(self) -> str:
        if not self.evidence:
            return self.evidence
        if self.source and self.source != "eligibility_criteria":
            label = SOURCE_LABELS.get(self.source, self.source)
            return f"[from the trial's {label}, not formal eligibility criteria] {self.evidence}"
        return self.evidence


def resolve(biomarker: str) -> MarkerDef | None:
    """Map a user's biomarker string to a known, curated definition, or None."""
    return _m.resolve_marker(biomarker)


def match_biomarker(
    eligibility_text: str,
    biomarker: str,
    *,
    detailed_description: str = "",
    brief_summary: str = "",
    keywords=(),
    markers: dict[str, MarkerDef] | None = None,
) -> BiomarkerMatch:
    """Decide whether an MSS-type patient's biomarker admits them to this trial,
    and return the criterion sentence that decided it.

    The supplementary fields are consulted only when eligibility text (and each
    field before them) carries no signal at all — see `markers.collect_signals`
    and `markers.record_texts`. This is how ADG126-P001 (MSS stated only in its
    detailed description) reaches a verdict rather than reading as NOT MENTIONED.
    """
    mdef = _m.resolve_marker(biomarker, markers)
    if mdef is None:
        return _literal_match(eligibility_text, biomarker, detailed_description,
                              brief_summary, keywords)

    texts = _m.record_texts(eligibility_text, detailed_description, brief_summary, keywords)
    signals = _m.collect_signals(mdef, texts, markers)
    match = _reduce(mdef, signals)
    match.evidence = match._labelled_evidence()
    return match


def _reduce(mdef: MarkerDef, signals: list) -> BiomarkerMatch:
    """The patient-side precedence: a genuine conflict is UNCLEAR, never a pick.
    See markers.py's module docstring ("THE TWO POLICIES") for why this differs
    from biomarker_gating.py's REQUIRED-wins policy."""
    own_req, own_exc, opp_req, opp_exc = _m.split_signals(signals)
    eligible_leaning = own_req or opp_exc
    excluded_leaning = own_exc or opp_req

    if eligible_leaning and excluded_leaning:
        s = (own_exc or opp_req)[0]
        return BiomarkerMatch(UNCLEAR, s.span, mdef.key, True, s.source)
    if eligible_leaning:
        if own_req:
            s = own_req[0]
            return BiomarkerMatch(ELIGIBLE, s.span, mdef.key, True, s.source)
        s = opp_exc[0]
        return BiomarkerMatch(ELIGIBLE_BY_EXCLUSION, s.span, mdef.key, True, s.source)
    if excluded_leaning:
        s = (own_exc or opp_req)[0]
        return BiomarkerMatch(EXCLUDED, s.span, mdef.key, True, s.source)
    return BiomarkerMatch(NOT_MENTIONED, "", mdef.key, True)


def _literal_match(
    eligibility_text: str, biomarker: str, detailed_description: str = "",
    brief_summary: str = "", keywords=(),
) -> BiomarkerMatch:
    """Fallback for a biomarker with no reviewed entry in config/markers.yaml.
    No opposite to reason about and no negation handling, so a mention is
    reported as UNCLEAR with its sentence — honest about what it cannot tell —
    and never as a confident ELIGIBLE or EXCLUDED. `curated=False` marks it so a
    reader is never shown an unreviewed guess with the same weight as a
    reviewed marker's verdict."""
    term = re.sub(r"\s+", " ", (biomarker or "").strip())
    if not term:
        return BiomarkerMatch(NOT_MENTIONED, "", biomarker, curated=False)
    pattern = re.compile(re.escape(term), re.IGNORECASE)
    texts = _m.record_texts(eligibility_text, detailed_description, brief_summary, keywords)
    for source, text in texts.items():
        for _section, sentence in iter_criteria(text):
            if pattern.search(sentence):
                label = SOURCE_LABELS.get(source, source)
                evidence = (sentence if source == "eligibility_criteria"
                           else f"[from the trial's {label}, not formal eligibility criteria] {sentence}")
                return BiomarkerMatch(UNCLEAR, evidence, term, curated=False, source=source)
    return BiomarkerMatch(NOT_MENTIONED, "", term, curated=False)
