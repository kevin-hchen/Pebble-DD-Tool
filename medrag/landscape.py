"""Trial landscape from the patient's perspective.

A different use case from asset diligence. Given a condition and a biomarker,
enumerate the trials a patient could actually enter: filter the registry to the
condition, screen each trial's eligibility for the biomarker, and show — for each
trial that admits or might admit the patient — where it runs, who to contact, and
the exact eligibility line that decided it.

Four things are deliberate:

  * A trial whose biomarker status cannot be read is kept as UNCLEAR, never
    dropped. A missed trial is worse than an uncertain one for someone looking
    for an option, so the uncertain ones stay in the table, flagged, with their
    eligibility sentence shown so a clinician can judge.

  * Trials that clearly require the OPPOSITE biomarker, and trials whose
    eligibility never mentions it, are not shown as candidates — but they are
    COUNTED, so the reader can see how much the biomarker filter set aside rather
    than wondering whether the search simply missed them.

  * HOW a trial states its eligibility is a column, not a rank. Every admitting
    state — ELIGIBLE, ELIGIBLE BY EXCLUSION, UNCLEAR — competes in one ranked
    list, scored by ranking.py. Grouping by state first, which is what this did
    before, meant a trial naming MSS explicitly outranked EVERY trial that
    states it by excluding MSI-H, however much larger or later-phase the second
    one was: on the live colorectal store that put STELLAR-303 and C-800-25 —
    both Phase 3-scale, both central to this disease, both by-exclusion — below
    five hundred trials that matter less. The state is still shown on every
    row, with the criterion sentence behind it, because an oncologist reads
    "excludes MSI-H" and "requires MSS" differently and is entitled to.

  * The printed list is CAPPED, and says what the cap left out. An 800-row
    table is not an answer to "what could this patient enter". The cap lives
    here rather than in each renderer, so the Streamlit page, the Markdown and
    the PDF cannot show different rows — the same reason coverage.render_lines
    is the only function that turns a coverage statement into text.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import ranking
from .biomarker import (
    ELIGIBLE,
    ELIGIBLE_BY_EXCLUSION,
    EXCLUDED,
    NOT_MENTIONED,
    UNCLEAR,
    BiomarkerMatch,
    match_biomarker,
    resolve,
)
from .coverage import CoverageStatement, build_coverage_statement
from .trials.client import TrialRecord

# Statuses that mean a patient could enrol now (or soon). Everything else —
# active-not-recruiting, completed, terminated — cannot take a new patient.
# This is no longer a sort key: ranking.py's `status` signal scores exactly
# this distinction, in points, on a line the row prints. It stays as a property
# because the page and the memo both label rows with it.
_ENROLLING = {"RECRUITING", "NOT_YET_RECRUITING", "ENROLLING_BY_INVITATION", "AVAILABLE"}

# How many ranked trials are printed, on every surface.
#
# 30 is not derived from anything about patients; it is the number the
# diligence memo's aggregate sections already use (config/landscape.yaml's
# sample cap), adopted here so the two capped trial tables this tool prints
# agree on how much of a population a sample should be. That makes "why does
# the page show more than the memo" a question nobody has to ask. It is an
# editorial call, flagged as one: whoever owns config/landscape.yaml owns this
# number too, and the row count that follows a cap change is the only thing
# that changes with it — the counts, the coverage statement and the ranking
# are all computed over the whole population regardless.
DEFAULT_SHOW_LIMIT = 30


@dataclass
class LandscapeTrial:
    record: TrialRecord
    match: BiomarkerMatch
    nearest_location: dict | None = None
    proximity_tier: int = 0        # 3 city, 2 state, 1 country, 0 none/unmatched
    proximity_label: str = ""      # how the nearest location matched the query
    ranking: ranking.Ranking | None = None   # why this row sits where it does

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
    trials: list[LandscapeTrial] = field(default_factory=list)   # the PRINTED rows, ranked
    query_set: str = ""            # the fetch that defined this population
    population_total: int = 0      # trials the ingest holds for that set
    coverage: dict | None = None   # what was searched; None means never ingested
    n_condition: int = 0           # trials screened, before the biomarker screen
    n_eligible: int = 0
    n_eligible_by_exclusion: int = 0  # opposite marker excluded, this one named only indirectly
    n_unclear: int = 0             # genuine contradiction in the source text
    n_excluded: int = 0            # require the opposite biomarker
    n_not_mentioned: int = 0       # eligibility never references the biomarker
    n_no_eligibility_text: int = 0  # subset of not_mentioned: nothing on file to screen
    n_candidates: int = 0          # admitting trials found, before the display cap
    show_limit: int = 0            # the cap that produced `trials`; 0 means uncapped
    ranked_out_by_state: dict[str, int] = field(default_factory=dict)
    biomarker_curated: bool = True  # False when the biomarker has no config/markers.yaml entry
    coverage_statement: CoverageStatement | None = None  # see coverage.py
    warnings: list[str] = field(default_factory=list)

    @property
    def n_ranked_out(self) -> int:
        """Admitting trials the cap kept off the page. Never inferred from a
        difference the reader has to compute themselves."""
        return max(0, self.n_candidates - len(self.trials))

    def counts_line(self) -> str:
        return (
            f"{self.n_eligible} eligible, {self.n_eligible_by_exclusion} eligible by "
            f"exclusion, {self.n_unclear} unclear (shown); {self.n_excluded} require "
            f"the opposite biomarker, {self.n_not_mentioned} do not mention it (not shown)"
        )

    def sample_lines(self) -> list[str]:
        """What is printed, out of what, and what the cap set aside — the ONLY
        function that says so, called verbatim by the Streamlit page, the
        Markdown and the PDF. Same construction as coverage.render_lines: three
        surfaces cannot disagree about a number none of them computes.

        The state breakdown of the excluded rows is the part that has to be
        there. "770 not listed" invites the assumption that the ones held back
        are the weaker ELIGIBLE-BY-EXCLUSION and UNCLEAR rows; naming the count
        per state shows whether that is true, and on the live colorectal store
        it is not.
        """
        if not self.n_candidates:
            return []
        if not self.n_ranked_out:
            return [
                f"Showing all {len(self.trials)} trial(s) whose eligibility admits this "
                "patient, ranked by config/ranking.yaml — every row states its own score."
            ]
        held = ", ".join(
            f"{n} {state.lower()}" for state, n in self.ranked_out_by_state.items() if n
        )
        return [
            f"Showing the {len(self.trials)} highest-ranked of {self.n_candidates} trials "
            f"whose eligibility admits this patient. {self.n_ranked_out} are not listed"
            + (f" ({held})" if held else "")
            + " — this is a capped sample, not the whole set.",
            "Rank is config/ranking.yaml's deterministic score (no model involved) and "
            "every printed row states which signals fired for it. How a trial states "
            "eligibility — directly, or by excluding the opposite marker — is a column, "
            "not a rank: the counts above and the coverage statement cover all "
            f"{self.n_candidates}.",
        ]


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
    show_limit: int | None = DEFAULT_SHOW_LIMIT,
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

    `limit` and `show_limit` are different caps and the distinction matters.
    `limit` narrows what is SCREENED, so it changes every count on the object
    and is a testing lever, not a normal setting. `show_limit` narrows only what
    is PRINTED: the whole population is still screened, counted and ranked, and
    `sample_lines()` states what the cap held back. `show_limit=None` prints
    everything, which is how a caller asks for the full ranked list.
    """
    landscape = TrialLandscape(condition=condition, biomarker=biomarker, location=location)
    mdef = resolve(biomarker)
    landscape.biomarker_curated = mdef is not None
    if not landscape.biomarker_curated:
        landscape.warnings.append(
            f"“{biomarker}” has no reviewed synonym or negation handling in "
            "config/markers.yaml, so this is a generic text search: it can only ever "
            "mark a trial UNCLEAR or NOT MENTIONED, never a confident ELIGIBLE or "
            "EXCLUDED, and it will not catch synonyms or indirect ('excludes the "
            "opposite') phrasing the way a reviewed marker would."
        )

    if store is None:
        landscape.warnings.append("trial store not found — run `medrag trials` first")
        return landscape

    if query_set is None:
        from .trials.queries import resolve_query_set
        query_set = resolve_query_set(condition).key

    landscape.query_set = query_set
    landscape.population_total = store.count(query_set=query_set)
    landscape.coverage = store.coverage(query_set)
    landscape.coverage_statement = build_coverage_statement(
        store, query_set, marker=mdef.key if mdef else None,
    )
    # THE CENSUS PREFILTER. For a CURATED marker, narrow in SQL to the trials the
    # ingest-time census says could admit it, and live-screen only those. The
    # excluded/not-mentioned counts then come from the census (real SQL COUNTs
    # over the whole population) rather than from screening every record to
    # discard it.
    #
    # Measured: colorectal screened 12,095 records and took ~10.5s; it now
    # screens 826 and takes well under a second. Proven equivalent across all 74
    # families and 7 markers before shipping — see tests/test_census_live_parity.py.
    #
    # An UNCURATED marker cannot use it: there is no census for a marker the
    # config does not know, so it still screens the whole population, which is
    # correct and is why that path is unchanged.
    prefiltered = bool(mdef) and not limit
    if prefiltered:
        records = store.query(query_set=query_set, admits_marker=mdef.key,
                              limit=landscape.population_total or 1)
        census = store.biomarker_counts(mdef.key, query_set=query_set)
    else:
        records = store.query(query_set=query_set,
                              limit=limit or landscape.population_total or 1)
        census = None

    if not records:
        landscape.warnings.append(
            f"nothing has been ingested for “{condition}” yet (query set "
            f"“{query_set}”). Run `medrag trials --condition \"{condition}\"` first."
        )
    # `n_condition` is the population this landscape was drawn from, and it must
    # not silently become "the prefiltered subset" — the page prints it as the
    # denominator ("None of the N trials screened for X"). With the prefilter on,
    # the population is still the whole query set; only the live screen is
    # narrowed.
    landscape.n_condition = (landscape.population_total if prefiltered else len(records))
    if landscape.population_total > len(records):
        landscape.warnings.append(
            f"only {len(records)} of {landscape.population_total} ingested trials were "
            "screened because a row limit was set — the counts below are of the "
            "screened subset, not the whole population."
        )

    candidates: list[LandscapeTrial] = []
    for record in records:
        match = match_biomarker(
            record.eligibility_criteria, biomarker,
            detailed_description=record.detailed_description,
            brief_summary=record.brief_summary,
            keywords=record.keywords,
        )
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
        elif match.status == ELIGIBLE_BY_EXCLUSION:
            landscape.n_eligible_by_exclusion += 1
        elif match.status == UNCLEAR:
            landscape.n_unclear += 1

    # One ranked list across every admitting state. Biomarker state is NOT a
    # sort key — see the module docstring for the two Phase 3 trials that cost.
    # Ties break on NCT ID rather than on a second unscored signal, the same
    # rule store.landscape() follows: anything that decided a row's position has
    # to appear in the explain() line the row prints, and a genuine tie means
    # the scored signals ran out.
    rank_cfg = ranking.load_ranking_config()
    provenance = store.found_by_map(query_set=query_set) if candidates else {}
    for t in candidates:
        t.ranking = ranking.score_record(
            t.record, provenance.get(t.record.nct_id, []), rank_cfg,
            # Distance is only a question when the patient asked it. With the
            # location box empty the signal is not evaluated at all, so it
            # cannot print a meaningless "no site nearby" on every row.
            proximity_tier=t.proximity_tier if location.strip() else None,
        )
    candidates.sort(key=lambda t: (-t.ranking.score, t.record.nct_id))

    if prefiltered and census is not None:
        # These records were never screened (that is the point), so their counts
        # come from the census — real SQL COUNTs over the whole population, not a
        # number inferred from what the prefilter happened to return. Without
        # this the page would report 0 excluded and 0 not-mentioned, which reads
        # as "nothing was ruled out" rather than "these were ruled out in SQL".
        landscape.n_excluded = census.get("EXCLUDED", 0)
        landscape.n_not_mentioned = census.get("NOT_MENTIONED", 0)
        # Counted in SQL, not inferred: these records are never loaded, and
        # reporting 0 would say "every trial had text to screen" for a
        # population where hundreds have none.
        landscape.n_no_eligibility_text = store.count_without_eligibility(
            query_set=query_set)

    landscape.n_candidates = len(candidates)
    landscape.show_limit = show_limit or 0
    landscape.trials = candidates[:show_limit] if show_limit else candidates
    landscape.ranked_out_by_state = {
        state: sum(1 for t in candidates[len(landscape.trials):] if t.match.status == state)
        for state in (ELIGIBLE, ELIGIBLE_BY_EXCLUSION, UNCLEAR)
    }

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
