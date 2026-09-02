"""The coverage statement: what was searched, what was not, and what matched —
stated on every landscape output, not buried in a footnote.

A curated list of six trials says "here are six trials". This says "here is
the whole registry, here is what matched, here is what we cannot see" — and
that is the single most defensible line this tool produces, because every
number in it traces to a stored count or the registry's own reported total,
never to what happened to render in a capped sample.

THREE LINES, ONE SOURCE OF TRUTH

`render_lines()` is the only place that turns a `CoverageStatement` into text,
and every surface — the Streamlit page, the Markdown memo, the PDF — calls it
verbatim. That is what makes "same numbers in all three" true by construction
rather than by three people remembering to keep three copies in sync, which is
exactly the kind of drift this codebase has been bitten by before
(`biomarker.py`/`biomarker_gating.py` disagreeing was an entire prior task).

WHY THE FIRST LINE IS NOT A LITERAL "X OF Y" AGAINST A REGISTRY-WIDE TOTAL

There is no single registry-reported number for "matching this query set" —
a query set is a UNION of several independent registry queries (MeSH-expanded
condition synonyms plus full-text term searches), each with its OWN reported
total, and those totals overlap heavily by design (a MeSH-expanded condition
query already returns most of what its synonyms would). Summing the
per-query reported totals and presenting that sum as "the total" would
double- and triple-count every trial the queries agree on — on the real
colorectal set that sum is roughly 4.5x the actual unique population, which
would be a materially misleading number to put on a page whose entire point is
not to mislead.

So the first line asserts the property that actually matters and is actually
true: `IncompleteFetch` (trials/client.py) already guarantees, at ingest
time, that every query in the set was fetched to its own full reported
total — an ingest that could not complete this raises rather than silently
holding a subset. What this line states is "N unique studies, and every query
that produced them completed" (or, if `query_coverage.errors` is non-empty,
exactly which queries did not, and that the count is a lower bound as a
result). That is a stronger, more precise claim than a synthetic "X of Y"
fraction against a number that does not correspond to anything a reader could
go verify on the registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from .biomarker_gating import MARKER_LABELS
from .markers import MARKERS

DEFAULT_REGISTRIES_CONFIG = Path(__file__).resolve().parent.parent / "config" / "registries.yaml"


@dataclass(frozen=True)
class RegistryConfig:
    searched_name: str
    not_searched: tuple[str, ...]
    caveat: str


def load_registries_config(path: str | Path | None = None) -> RegistryConfig:
    """Which registries this tool can and cannot see — config/registries.yaml,
    not code, for the same reason trial_queries.yaml and markers.yaml are
    config: this is a fact about the world and about this build, and someone
    extending registry coverage should be able to move one line, not find and
    edit a string literal."""
    p = Path(path or DEFAULT_REGISTRIES_CONFIG)
    if not p.exists():
        return RegistryConfig(searched_name="ClinicalTrials.gov", not_searched=(), caveat="")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    searched = data.get("searched") or []
    name = searched[0]["name"] if searched else "ClinicalTrials.gov"
    return RegistryConfig(
        searched_name=name,
        not_searched=tuple(data.get("not_searched") or ()),
        caveat=data.get("caveat") or "",
    )


@dataclass
class BiomarkerCoverage:
    """The third line's data: how a specific marker's admitting population
    breaks down, over the SAME population `fetched_total` describes — never a
    narrower, section-specific filter — a diligence section can additionally
    narrow by status or phase beyond the query set (e.g. "MSS/pMMR trials
    specifically" filters to RECRUITING/NOT_YET_RECRUITING). When it does,
    `population_total` differs from `CoverageStatement.fetched_total`, and
    `scope_note` names the extra filter so a reader is never left reconciling
    two different totals on the same statement without being told why they
    differ."""
    marker_key: str
    label: str
    population_total: int        # the population THIS breakdown is over
    scope_note: str              # "" if population_total == fetched_total
    explicit: int
    by_synonym: int
    by_exclusion: int
    opposite_label: str          # "" if the marker has no paired opposite
    opposite_count: int          # trials requiring the opposite marker instead
    not_mentioned: int

    @property
    def eligible_total(self) -> int:
        return self.explicit + self.by_synonym + self.by_exclusion


@dataclass
class CoverageStatement:
    registry: str
    fetched_total: int                 # unique studies held for this query set
    n_condition_queries: int
    n_term_queries: int
    fetch_date: datetime | None
    complete: bool                     # every query in the set completed
    incomplete_queries: list[str]      # query labels that did NOT complete, if any
    not_searched: tuple[str, ...]
    not_searched_caveat: str
    ever_ingested: bool = True         # False => "not searched" for THIS query set, not "found nothing"
    biomarker: BiomarkerCoverage | None = None
    basket_caveat: str = ""
    # An ingest that started and never verified — the crash case. Distinct from
    # `complete=False`, which means the fetch finished and a query failed inside
    # it. Both are lower bounds; only this one has a re-run as the remedy.
    partial_ingest: bool = False
    set_key: str = ""
    recorded_total: int = 0            # what the interrupted fetch had recorded, if anything


def _fmt_date(dt: datetime | None) -> str:
    return dt.strftime("%d %b %Y") if dt else "date not on file"


def _remedy(cs: "CoverageStatement") -> str:
    """The exact command that finishes an unfinished ingest. Written once, so
    every incomplete branch below points at the same thing — a coverage line
    that reports a shortfall without saying how to close it leaves the reader
    knowing only that the number is wrong."""
    return (f'python -m medrag trials --condition "{cs.set_key}"' if cs.set_key
            else "re-run the ingest for this indication")


def biomarker_coverage_from_counts(
    marker: str,
    gating_counts: dict[str, int],
    basis_counts: dict[str, int],
    population_total: int,
    scope_note: str = "",
) -> BiomarkerCoverage:
    """Build a BiomarkerCoverage from counts a caller already computed — used
    by `store.landscape()`, which needs the breakdown over ITS OWN filtered
    population (a diligence section can narrow by status/phase beyond the
    query set), not the bare query-set population `build_coverage_statement`
    assumes. `gating_counts` is `store.biomarker_counts()`'s shape,
    `basis_counts` is `store.biomarker_basis_counts()`'s shape."""
    mdef = MARKERS.get(marker)
    opp_key = mdef.opposite if mdef else ""
    return BiomarkerCoverage(
        marker_key=marker,
        label=MARKER_LABELS.get(marker, marker),
        population_total=population_total,
        scope_note=scope_note,
        explicit=basis_counts.get("EXPLICIT", 0),
        by_synonym=basis_counts.get("SYNONYM", 0),
        by_exclusion=gating_counts.get("ELIGIBLE_BY_EXCLUSION", 0),
        opposite_label=MARKER_LABELS.get(opp_key, opp_key) if opp_key else "",
        opposite_count=gating_counts.get("EXCLUDED", 0),
        not_mentioned=gating_counts.get("NOT_MENTIONED", 0),
    )


def registry_coverage_statement(
    cov: dict | None,
    fetched_total: int,
    registries: RegistryConfig | None = None,
    biomarker: BiomarkerCoverage | None = None,
) -> CoverageStatement:
    """The registry-level half of a coverage statement — what was searched,
    what was not, the fetch date, whether every query completed — built from a
    `store.coverage(query_set)` row and a population count a caller already
    has. Shared by `build_coverage_statement` (the simple, whole-query-set
    case) and `store.landscape()` (which needs the biomarker breakdown scoped
    to its own, possibly status/phase-narrowed population, computed by the
    caller and passed in as `biomarker`) so there is one place that decides
    what "searched" means, not two that could drift apart.
    """
    registries = registries or load_registries_config()

    if cov is None:
        return CoverageStatement(
            registry=registries.searched_name, fetched_total=0,
            n_condition_queries=0, n_term_queries=0, fetch_date=None,
            complete=False, incomplete_queries=[],
            not_searched=registries.not_searched, not_searched_caveat=registries.caveat,
            ever_ingested=False,
        )

    yields = cov.get("yields") or []
    n_cond = sum(1 for y in yields if y["query"].startswith("cond:"))
    n_term = sum(1 for y in yields if y["query"].startswith("term:"))
    incomplete = [y["query"] for y in yields if y.get("error")]

    updated_at = cov.get("updated_at")
    fetch_date = None
    if updated_at:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                fetch_date = datetime.strptime(updated_at.split(".")[0], fmt)
                break
            except ValueError:
                continue

    # `verified_complete` is the store's own grade — the stored count checked
    # against what the fetch produced, and every query against its
    # registry-reported total. An older row that predates the grading has no
    # status and is graded PARTIAL there, so it arrives here as unverified,
    # which is the honest reading: nothing on file proves it finished.
    verified = bool(cov.get("verified_complete"))

    return CoverageStatement(
        registry=registries.searched_name,
        fetched_total=fetched_total,
        n_condition_queries=n_cond,
        n_term_queries=n_term,
        fetch_date=fetch_date,
        complete=verified and not incomplete and not cov.get("errors"),
        incomplete_queries=incomplete,
        not_searched=registries.not_searched,
        not_searched_caveat=registries.caveat,
        ever_ingested=True,
        biomarker=biomarker,
        basket_caveat=cov.get("basket_caveat") or "",
        partial_ingest=not verified,
        set_key=cov.get("set_key") or "",
        recorded_total=cov.get("total_unique") or 0,
    )


def build_coverage_statement(
    store,
    query_set: str,
    marker: str | None = None,
    registries: RegistryConfig | None = None,
) -> CoverageStatement:
    """Assemble the coverage statement entirely from stored counts and the
    registry's own reported totals — the query_coverage row written at ingest
    and biomarker census columns written at ingest — never from a retrieved
    sample. Returns a statement with `ever_ingested=False` when this query set
    was never fetched at all, which a renderer must show differently from "we
    searched and found nothing" — the same not-searched-vs-nothing-found rule
    `ValidationReport.assessed` and `NegativeEvidence.searched` already apply.

    For the simple case: the whole query-set population, no status/phase
    narrowing. `store.landscape()` builds its own statement directly via
    `registry_coverage_statement` when a section narrows further.
    """
    registries = registries or load_registries_config()
    cov = store.coverage(query_set)
    fetched_total = store.count(query_set=query_set)

    biomarker_cov = None
    if marker and cov is not None:
        counts = store.biomarker_counts(marker, query_set=query_set)
        basis = store.biomarker_basis_counts(marker, query_set=query_set)
        biomarker_cov = biomarker_coverage_from_counts(
            marker, counts, basis, population_total=fetched_total, scope_note="",
        )

    return registry_coverage_statement(cov, fetched_total, registries, biomarker=biomarker_cov)


def render_lines(cs: CoverageStatement) -> list[str]:
    """The three (or four, with the basket caveat) lines, as plain text. The
    ONLY function that turns a CoverageStatement into words — every surface
    (Streamlit, Markdown, PDF) calls this and nothing else, so the numbers
    cannot drift between them."""
    lines: list[str] = []

    if not cs.ever_ingested:
        lines.append(
            f"Searched: nothing has been fetched from {cs.registry} for this indication yet — "
            "this is NOT a finding that no matching studies exist, only that this tool has not "
            "looked. Run an ingest first."
        )
    elif cs.incomplete_queries:
        # A named failing query outranks the generic unverified line below: it
        # says WHICH part of the set is missing, which the reader can act on.
        # Both end in the same place — a lower bound and a re-run — so nothing
        # is lost by preferring the specific one.
        n_queries = cs.n_condition_queries + cs.n_term_queries
        bad = ", ".join(cs.incomplete_queries)
        lines.append(
            f"Searched: {cs.registry} — {cs.fetched_total:,} studies found, but "
            f"{len(cs.incomplete_queries)} of {n_queries} queries did NOT complete ({bad}) — "
            f"this count is a LOWER BOUND, not exhaustive. Fetched {_fmt_date(cs.fetch_date)}. "
            f"Re-run to complete: {_remedy(cs)}."
        )
    elif cs.partial_ingest:
        # Checked BEFORE `complete`, because an ingest that never finished has
        # nothing to be complete about. The numbers it did record are shown
        # rather than suppressed — "N of M" is the only thing that tells a
        # reader how much is missing — but the sentence leads with the fact
        # that they were never verified, so the pair cannot be read as a census.
        #
        # This is the branch a crash lands in. There is no failing query to name
        # because no query failed: the process died, or a cap truncated it, and
        # the only evidence anything is wrong is the marker that was never
        # cleared.
        against = (f"{cs.fetched_total:,} of {cs.recorded_total:,} studies"
                   if cs.recorded_total else f"{cs.fetched_total:,} studies")
        lines.append(
            f"Searched: {cs.registry} — PARTIAL INGEST, {against}. This fetch was started "
            "and never verified against the registry's own reported totals, so the store "
            "may hold only part of this population and this count is a LOWER BOUND. "
            f"Re-run to complete: {_remedy(cs)}."
        )
    elif cs.complete:
        n_queries = cs.n_condition_queries + cs.n_term_queries
        q_word = "query" if n_queries == 1 else "queries"
        lines.append(
            f"Searched: {cs.registry} — {cs.fetched_total:,} of {cs.fetched_total:,} studies "
            f"found (every one of {n_queries} {q_word} — {cs.n_condition_queries} condition, "
            f"{cs.n_term_queries} full-text — fetched to its full registry-reported total, "
            f"none capped), fetched {_fmt_date(cs.fetch_date)}."
        )
    else:
        # Not verified, no failing query, and not flagged partial — the store
        # graded it incomplete for a reason it did not attribute to a query.
        # Say the bound rather than pick a story.
        lines.append(
            f"Searched: {cs.registry} — {cs.fetched_total:,} studies found, not verified "
            f"as exhaustive. This count is a LOWER BOUND. Fetched {_fmt_date(cs.fetch_date)}. "
            f"Re-run to complete: {_remedy(cs)}."
        )

    if cs.not_searched:
        line = f"Not searched: {', '.join(cs.not_searched)}."
        if cs.not_searched_caveat:
            line += f" {cs.not_searched_caveat}"
        lines.append(line)

    if cs.basket_caveat:
        lines.append(cs.basket_caveat)

    bm = cs.biomarker
    if bm and cs.ever_ingested:
        eligible_bits = [f"{bm.explicit:,} explicit", f"{bm.by_exclusion:,} by exclusion",
                         f"{bm.by_synonym:,} by synonym"]
        opp_bit = (f"{bm.opposite_count:,} {bm.opposite_label}" if bm.opposite_label
                  else f"{bm.opposite_count:,} require the opposite")
        scope = f" ({bm.scope_note})" if bm.scope_note else ""
        lines.append(
            f"Of {bm.population_total:,}{scope}: {bm.eligible_total:,} {bm.label}-eligible "
            f"({', '.join(eligible_bits)}), {opp_bit}, {bm.not_mentioned:,} not mentioned."
        )

    return lines
