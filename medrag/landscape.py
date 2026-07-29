"""Trial landscape from the patient's perspective.

A different use case from asset diligence. Given a condition and a biomarker,
enumerate the trials a patient could actually enter: filter the registry to the
condition, screen each trial's eligibility for the biomarker, and show — for each
trial that admits or might admit the patient — where it runs, who to contact, and
the exact eligibility line that decided it.

Two things are deliberate:

  * A trial whose biomarker status cannot be read is kept as UNCLEAR, never
    dropped. A missed trial is worse than an uncertain one for someone looking
    for an option, so the uncertain ones stay in the table, flagged, with their
    eligibility sentence shown so a clinician can judge.

  * Trials that clearly require the OPPOSITE biomarker, and trials whose
    eligibility never mentions it, are not shown as candidates — but they are
    COUNTED, so the reader can see how much the biomarker filter set aside rather
    than wondering whether the search simply missed them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .biomarker import ELIGIBLE, EXCLUDED, NOT_MENTIONED, UNCLEAR, BiomarkerMatch, match_biomarker
from .trials.client import TrialRecord

# Statuses that mean a patient could enrol now (or soon). Everything else —
# active-not-recruiting, completed, terminated — cannot take a new patient, so
# those sort below the open trials even when the biomarker fits.
_ENROLLING = {"RECRUITING", "NOT_YET_RECRUITING", "ENROLLING_BY_INVITATION", "AVAILABLE"}


@dataclass
class LandscapeTrial:
    record: TrialRecord
    match: BiomarkerMatch
    nearest_location: dict | None = None
    proximity_tier: int = 0        # 3 city, 2 state, 1 country, 0 none/unmatched
    proximity_label: str = ""      # how the nearest location matched the query

    @property
    def is_enrolling(self) -> bool:
        return self.record.overall_status.upper() in _ENROLLING

    @property
    def contact(self) -> dict | None:
        """The most actionable contact for a patient: the coordinator at the
        nearest site if the trial lists one, otherwise the central study contact.
        A site coordinator beats the overall study chair for someone trying to
        enrol."""
        loc = self.nearest_location or (self.record.locations[0] if self.record.locations else None)
        if loc:
            for c in loc.get("contacts") or []:
                if c.get("email") or c.get("phone"):
                    return c
        return self.record.primary_contact


@dataclass
class TrialLandscape:
    condition: str
    biomarker: str
    location: str = ""
    trials: list[LandscapeTrial] = field(default_factory=list)   # ELIGIBLE + UNCLEAR
    n_condition: int = 0           # trials matched on condition, before biomarker screen
    n_eligible: int = 0
    n_unclear: int = 0
    n_excluded: int = 0            # require the opposite biomarker
    n_not_mentioned: int = 0       # eligibility never references the biomarker
    n_no_eligibility_text: int = 0  # subset of not_mentioned: nothing on file to screen
    warnings: list[str] = field(default_factory=list)

    def counts_line(self) -> str:
        return (
            f"{self.n_eligible} eligible, {self.n_unclear} unclear "
            f"(shown); {self.n_excluded} require the opposite biomarker, "
            f"{self.n_not_mentioned} do not mention it (not shown)"
        )


def _format_location(loc: dict) -> str:
    bits = [loc.get(k, "") for k in ("city", "state", "country")]
    return ", ".join(b for b in bits if b) or loc.get("facility", "") or "location not stated"


def _proximity(record: TrialRecord, location: str) -> tuple[dict | None, int, str]:
    """Rank a trial's sites against the patient's location. No geocoding — a
    substring match on city, then state, then country, which is enough to float
    a same-city or same-country site to the top and honest about doing no more."""
    locs = record.locations or []
    if not locs:
        return None, 0, ""
    if not location.strip():
        return locs[0], 0, ""

    q = location.strip().lower()
    best, best_tier, best_label = locs[0], 0, ""
    for loc in locs:
        for tier, key, label in ((3, "city", "city"), (2, "state", "state"),
                                 (1, "country", "country")):
            field_val = (loc.get(key) or "").lower()
            if field_val and (q in field_val or field_val in q):
                if tier > best_tier:
                    best, best_tier, best_label = loc, tier, f"same {label}"
                break
    return best, best_tier, best_label


def build_landscape(
    store,
    condition: str,
    biomarker: str,
    location: str = "",
    limit: int = 300,
) -> TrialLandscape:
    """Screen the condition's trials for the biomarker and assemble the landscape."""
    landscape = TrialLandscape(condition=condition, biomarker=biomarker, location=location)

    if store is None:
        landscape.warnings.append("trial store not found — run `medrag trials` first")
        return landscape

    records = store.query(condition=condition, limit=limit)
    # A structured condition filter can miss free-text phrasings; fall back to
    # the FTS search so a real trial is not lost to an exact-match gap.
    if not records:
        records = store.search(f"{condition} {biomarker}".strip(), limit=limit)
    landscape.n_condition = len(records)

    candidates: list[LandscapeTrial] = []
    for record in records:
        match = match_biomarker(record.eligibility_criteria, biomarker)
        if match.status == EXCLUDED:
            landscape.n_excluded += 1
            continue
        if match.status == NOT_MENTIONED:
            landscape.n_not_mentioned += 1
            if not (record.eligibility_criteria or "").strip():
                landscape.n_no_eligibility_text += 1
            continue

        loc, tier, label = _proximity(record, location)
        candidates.append(LandscapeTrial(
            record=record, match=match,
            nearest_location=loc, proximity_tier=tier, proximity_label=label,
        ))
        if match.status == ELIGIBLE:
            landscape.n_eligible += 1
        elif match.status == UNCLEAR:
            landscape.n_unclear += 1

    # Eligible before unclear; open-to-enrolment before closed; nearer before
    # farther; then a stable NCT order.
    status_rank = {ELIGIBLE: 0, UNCLEAR: 1}
    candidates.sort(key=lambda t: (
        status_rank.get(t.match.status, 2),
        0 if t.is_enrolling else 1,
        -t.proximity_tier,
        t.record.nct_id,
    ))
    landscape.trials = candidates

    if landscape.n_no_eligibility_text:
        landscape.warnings.append(
            f"{landscape.n_no_eligibility_text} condition trial(s) had no eligibility "
            "text on file and could not be screened for the biomarker — they are not "
            "shown. Check them directly on ClinicalTrials.gov."
        )
    return landscape
