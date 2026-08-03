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
    query_set: str = ""            # the fetch that defined this population
    population_total: int = 0      # trials the ingest holds for that set
    coverage: dict | None = None   # what was searched; None means never ingested
    n_condition: int = 0           # trials screened, before the biomarker screen
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
    limit: int | None = None,
    query_set: str | None = None,
) -> TrialLandscape:
    """Screen the fetched population for the biomarker and assemble the landscape.

    The population is whatever the INGEST went and got for this indication,
    selected by its recorded query set — a structured fact stamped on each record
    at fetch time. It is deliberately not re-derived here: the previous version
    re-ran `LOWER(conditions) LIKE '%colorectal cancer%'`, which used different
    logic from the fetch and discarded trials the ingest had deliberately
    retrieved (MOUNTAINEER-03 registers "Colorectal Neoplasms" and does not
    contain the substring). Narrowing belongs to the fetch; the local layer
    filters on structured facts only.

    `limit=None` means screen the whole population. A cap is honoured but always
    reported, because a silently truncated landscape is indistinguishable from a
    complete one.
    """
    landscape = TrialLandscape(condition=condition, biomarker=biomarker, location=location)

    if store is None:
        landscape.warnings.append("trial store not found — run `medrag trials` first")
        return landscape

    if query_set is None:
        from .trials.queries import resolve_query_set
        query_set = resolve_query_set(condition).key

    landscape.query_set = query_set
    landscape.population_total = store.count(query_set=query_set)
    landscape.coverage = store.coverage(query_set)
    records = store.query(query_set=query_set, limit=limit or landscape.population_total or 1)

    if not records:
        landscape.warnings.append(
            f"nothing has been ingested for “{condition}” yet (query set "
            f"“{query_set}”). Run `medrag trials --condition \"{condition}\"` first."
        )
    landscape.n_condition = len(records)
    if landscape.population_total > len(records):
        landscape.warnings.append(
            f"only {len(records)} of {landscape.population_total} ingested trials were "
            "screened because a row limit was set — the counts below are of the "
            "screened subset, not the whole population."
        )

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

    cov = landscape.coverage
    if cov:
        # A known-partial coverage has to be stated. An absent basket trial and a
        # non-existent one look identical in the output otherwise.
        if cov.get("basket_caveat"):
            landscape.warnings.append(cov["basket_caveat"])
        if cov.get("errors"):
            landscape.warnings.append(
                "This ingest did not complete every planned query, so the list below "
                "may be missing trials: " + "; ".join(cov["errors"])
            )
        if not cov.get("curated", True):
            landscape.warnings.append(
                f"No reviewed synonym set exists for “{condition}”, so only that exact "
                "phrase was searched. Trials registering the indication differently may "
                "be missing. Add a set to config/trial_queries.yaml to fix this."
            )
    elif landscape.n_condition:
        landscape.warnings.append(
            "These trials were ingested before coverage was recorded, so what was "
            "searched for them is unknown. Re-run the ingest to establish it."
        )
    return landscape
