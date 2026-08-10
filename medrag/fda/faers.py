"""FAERS — spontaneous adverse-event reports, and the guard around reading them.

This module is written guard-first on purpose. FAERS is the single most likely
source in this tool to produce a confidently misleading memo, because it yields
large, specific-looking numbers that invite exactly the reading they cannot
support. The counts are easy; saying what they are not is the work.

WHAT A FAERS COUNT IS
=====================

A count of REPORTS SUBMITTED — by clinicians, pharmacists, lawyers, consumers
and manufacturers — in which a drug and an event were mentioned together. That
is all. Every sentence below is a constraint the renderer enforces, not advice
in a docstring.

**There is no denominator.** FAERS does not know how many people took the drug.
A count is therefore never a rate, an incidence, a frequency or a risk, and no
two drugs' counts are comparable without an exposure denominator that this
source does not contain.

**Reporting is biased, and the biases are large.** Volume rises with media
attention, with litigation, and with recency of launch (a well-documented
reporting effect in the first years after approval). A widely-litigated drug and
a quietly-used one can differ by orders of magnitude in reports for reasons that
have nothing to do with either drug's safety.

**A report asserts an association; it does not establish one.** The FDA's own
field reference says of `drugcharacterization`: "Reported role of the drug in
the adverse event report. **These values are not validated by FDA.**" And
"Suspect" is defined as "the drug was considered *by the reporter* to be the
cause". Nothing in FAERS is an FDA finding of causation.

**The drug may not even be suspected.** `drugcharacterization` 2 is
"Concomitant (the drug was reported as being taken along with the suspect
drug)". A report counted for a drug may be a report about a different drug
entirely, in which this one was merely also present.

**Reported events include the disease.** Measured, the most co-reported event
for pembrolizumab is MALIGNANT NEOPLASM PROGRESSION (12,012 reports) — the
cancer progressing, which is the indication, not a drug effect. Any renderer
that prints a top-reactions table without saying this invites the reader to
read disease progression as toxicity.

**Duplicates are partly handled and partly not.** openFDA shows only the most
recent version of a resubmitted report (`duplicate` field), but independent
duplicate submissions of the same underlying case by different reporters are not
detected.

RETRIEVAL: CACHED AGGREGATES, NOT A MIRROR AND NOT LIVE-PER-MEMO
================================================================

FAERS is 20,692,690 reports and 113 GB across 1,767 partitions, so a local
mirror is out. Querying live per memo breaks offline mode and makes a memo
unreproducible. The third answer is what this module does: ask openFDA for
server-side `count` aggregations — a memo needs report counts and co-reported
event frequencies, not 20.7M individual reports — and cache each aggregate with
the timestamp it was retrieved. The cache IS the mirror: bounded, reproducible,
and offline-capable for anything already fetched. `--offline` reads the cache
and refuses to fetch, naming the asset that has none rather than returning a
silent zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Verbatim from the FDA field reference (open.fda.gov/fields/drugevent.yaml,
#: retrieved 2026-08-06). Not paraphrased, for the same reason the PMA decision
#: codes are not: the meanings carry the caveats.
DRUG_ROLE = {
    "1": "Suspect (the drug was considered by the reporter to be the cause)",
    "2": "Concomitant (the drug was reported as being taken along with the suspect drug)",
    "3": "Interacting (the drug was considered by the reporter to have interacted "
         "with the suspect drug)",
}
REPORTER_QUALIFICATION = {
    "1": "Physician",
    "2": "Pharmacist",
    "3": "Other health professional",
    "4": "Lawyer",
    "5": "Consumer or non-health professional",
}
SERIOUSNESS = {
    "1": "The adverse event resulted in death, a life threatening condition, "
         "hospitalization, disability, congenital anomaly, or other serious condition",
    "2": "The adverse event did not result in any of the above",
}
#: FDA: "Reported role of the drug in the adverse event report. These values are
#: not validated by FDA."
ROLE_NOT_VALIDATED = ("Reported role of the drug in the adverse event report. "
                      "These values are not validated by FDA.")

#: The interpretive block. Rendered on EVERY FAERS section, deterministically,
#: in full. Not a footnote and not conditional on the numbers being large.
WHAT_THIS_IS = (
    "These are counts of adverse-event REPORTS submitted to the FDA in which this "
    "drug and an event were mentioned together. A report is an association asserted "
    "by whoever filed it."
)
WHAT_THIS_IS_NOT = (
    # Worded to avoid the nouns it is denying. "not incidences, not rates" shares
    # a substring with the claim it rules out, so a downstream text match — or a
    # reader skimming — sees the claim rather than its negation. The same lesson
    # as the drugsFDA "is unapproved" draft, and the concrete phrasing is clearer
    # for a non-epidemiologist anyway.
    "FAERS has NO DENOMINATOR — it does not record how many people took the drug — "
    "so these counts cannot say how often an event happens, how likely it is, or "
    "what share of patients experienced it, and counts for two different drugs "
    "cannot be compared.",
    "Reporting volume is biased by media attention, by litigation, and by how "
    "recently a drug launched. A larger count can reflect a bigger news story or a "
    "bigger lawsuit rather than a bigger safety problem.",
    "Nothing here establishes causation. The FDA does not validate the reported "
    "role of a drug in a report, and a drug counted here may have been recorded as "
    "merely concomitant — present alongside the drug actually suspected.",
    "Reported events include the underlying disease. Progression of the condition "
    "being treated is among the most commonly reported events for oncology drugs, "
    "and it is not a drug effect.",
    "openFDA shows only the most recent version of a resubmitted report, but "
    "independent duplicate submissions of the same case are not detected.",
)

#: What absence means. Same four-way construction as drugsFDA and PMA, worded so
#: no substring reads as a safety claim when quoted out of context.
FAERS_ABSENCE_MEANINGS = (
    "no report naming this drug has been submitted to FAERS",
    "reports exist but name the drug by a spelling or trade name this search did "
    "not match — the product-name field is free text and the FDA states it is not "
    "systematically normalised",
    "the drug is recent, or little used, so few reports have accumulated",
    "reports were filed with a regulator other than the FDA, which FAERS does not "
    "cover",
)

#: The FAERS-specific version of the not-searched declaration.
NOT_SEARCHED = (
    "individual report narratives (this section reads aggregate counts only)",
    "FAERS quarterly extract files, which contain fields openFDA does not expose",
    "adverse-event systems outside the US (EudraVigilance, MHRA Yellow Card, "
    "Japan's PMDA)",
)


@dataclass
class TermCount:
    """One aggregate bucket: a term and how many reports carried it."""
    term: str
    count: int
    label: str = ""          # decoded meaning where the FDA publishes one

    @property
    def display(self) -> str:
        return self.label or self.term


@dataclass
class FAERSAnswer:
    """Cached FAERS aggregates for one asset, plus everything needed to read them.

    Deliberately has no field called `risk`, `rate`, `incidence` or `frequency`,
    and no method that divides one count by another. The numbers it holds are
    report counts and nothing else.
    """
    asset: str
    searched: bool = False              # False => never fetched AND nothing cached
    retrieved_at: str = ""              # when the aggregate was taken from openFDA
    matched_field: str = ""             # which field the count came from
    n_reports: int = 0                  # both fields ORed — the headline count
    n_reports_normalised: int = 0       # the normalised openfda block alone
    n_reports_free_text: int = 0        # the un-normalised product-name field alone
    reactions: list[TermCount] = field(default_factory=list)
    seriousness: list[TermCount] = field(default_factory=list)
    reporter: list[TermCount] = field(default_factory=list)
    drug_role: list[TermCount] = field(default_factory=list)
    n_death_reports: int = 0
    faers_total: int = 0                # the whole database, for scale
    offline_miss: bool = False          # asked offline, nothing cached
    absence_meanings: tuple[str, ...] = FAERS_ABSENCE_MEANINGS

    @property
    def found(self) -> bool:
        return self.n_reports > 0 or self.n_reports_free_text > 0

    @property
    def n_suspect(self) -> int:
        return next((t.count for t in self.drug_role if t.term == "1"), 0)

    @property
    def n_concomitant(self) -> int:
        return next((t.count for t in self.drug_role if t.term == "2"), 0)

    @property
    def n_consumer_reports(self) -> int:
        return next((t.count for t in self.reporter if t.term == "5"), 0)

    # ------------------------------------------------------------ rendering

    def statement(self) -> str:
        if self.offline_miss:
            return (f"FAERS was NOT consulted for “{self.asset}”: offline mode is on and "
                    "no cached aggregate exists for this asset. Run without --offline to "
                    "fetch one. This is not a finding about the drug.")
        if not self.searched:
            return (f"FAERS was NOT consulted for “{self.asset}”. This is not a finding "
                    "about the drug.")
        if not self.found:
            reasons = "; ".join(self.absence_meanings)
            return (
                f"No FAERS report matching “{self.asset}” was found. Absence here says "
                f"nothing either way about the drug's safety: it is equally consistent "
                f"with {len(self.absence_meanings)} different situations — {reasons}."
            )
        return (f"FAERS holds {self.n_reports:,} adverse-event report(s) naming "
                f"“{self.asset}”, out of {self.faers_total:,} reports in the database.")

    def axis_what_it_is(self) -> str:
        return "What this number is: " + WHAT_THIS_IS

    def axis_what_it_is_not(self) -> list[str]:
        return ["What this number is NOT: " + WHAT_THIS_IS_NOT[0]] + [
            "  " + line for line in WHAT_THIS_IS_NOT[1:]]

    def axis_role(self) -> str:
        """Suspect versus concomitant — whether the reporter blamed this drug."""
        if not self.drug_role:
            return ""
        bits = [f"{t.count:,} {t.display}" for t in self.drug_role]
        line = "Reported role of this drug: " + "; ".join(bits) + "."
        if self.n_concomitant:
            line += (f" {self.n_concomitant:,} of these report(s) record it as "
                     "concomitant — present alongside the drug the reporter actually "
                     "suspected, not itself suspected.")
        line += f" {ROLE_NOT_VALIDATED}"
        return line

    def axis_reporter(self) -> str:
        if not self.reporter:
            return ""
        bits = [f"{t.count:,} {t.display}" for t in self.reporter]
        line = "Who filed the reports: " + "; ".join(bits) + "."
        if self.n_consumer_reports:
            line += (" Consumer reports are not clinically adjudicated and a lawyer-filed "
                     "report is a litigation artefact before it is a safety signal.")
        return line

    def axis_seriousness(self) -> str:
        if not self.seriousness:
            return ""
        bits = [f"{t.count:,} {t.display}" for t in self.seriousness]
        line = "Reported seriousness: " + "; ".join(bits) + "."
        if self.n_death_reports:
            line += (f" {self.n_death_reports:,} report(s) record a fatal outcome. That "
                     "is the outcome the reporter recorded for the patient, not a finding "
                     "that the drug caused the death, and for an oncology drug the "
                     "underlying disease is frequently fatal on its own.")
        return line

    def axis_reactions(self) -> str:
        if not self.reactions:
            return ""
        top = "; ".join(f"{t.term} ({t.count:,})" for t in self.reactions[:10])
        return ("Most co-reported events (MedDRA terms, British-English spellings): "
                + top + ". These are events REPORTED alongside the drug, ranked by how "
                "often they were reported — not by how often they occur, and not "
                "adjusted for anything. Progression of the treated disease commonly "
                "appears in this list and is not a drug effect.")

    def axis_matching(self) -> str:
        """The linkage caveat, with the measured numbers for THIS asset."""
        line = ("Matched on the normalised drug block OR the free-text product name, "
                "ORed rather than either alone. ")
        if self.n_reports_normalised or self.n_reports_free_text:
            line += (f"Separately: {self.n_reports_normalised:,} report(s) via the "
                     f"normalised block, {self.n_reports_free_text:,} via the free-text "
                     "product name — a field the FDA states is \"not systematically "
                     "normalized\" and \"may contain misspellings\". ")
        if not self.n_reports_normalised and self.n_reports_free_text:
            line += ("Every report here was found ONLY by the free-text name, so this "
                     "count rests entirely on an un-normalised field and is approximate. ")
        line += ("Across the whole database 88.8% of reports carry the normalised block "
                 "and 11.2% carry only a free-text name, and for some assets the gap is "
                 "far larger — irinotecan returns 3.7x more reports on the free-text "
                 "field. Any count here is a LOWER BOUND on the reports that exist.")
        return line

    def coverage_lines(self) -> list[str]:
        lines = []
        if self.offline_miss:
            lines.append("Searched: nothing — offline mode with no cached aggregate for "
                         "this asset.")
        elif self.searched:
            lines.append(
                "Searched: openFDA FAERS via server-side aggregate counts"
                + (f", retrieved {self.retrieved_at[:19]}" if self.retrieved_at else "")
                + f", against {self.faers_total:,} reports. Individual reports are not "
                  "downloaded: the database is 20.7M reports and 113 GB, so this section "
                  "reads aggregates and caches them.")
        else:
            lines.append("Searched: nothing — FAERS was not consulted.")
        lines.append("Not searched: " + "; ".join(NOT_SEARCHED) + ".")
        return lines

    def render_lines(self) -> list[str]:
        """THE only function that turns FAERS numbers into prose.

        The interpretive block is rendered in FULL, always, before any count is
        broken down — not as a footnote and not conditional on the numbers being
        large. A model is never asked to summarise this object.
        """
        lines = [self.statement()]
        if self.found:
            lines.append(self.axis_what_it_is())
            lines.extend(self.axis_what_it_is_not())
            for axis in (self.axis_role(), self.axis_reporter(), self.axis_seriousness(),
                         self.axis_reactions(), self.axis_matching()):
                if axis:
                    lines.append(axis)
        lines.extend(self.coverage_lines())
        return lines


# ------------------------------------------------------------------ retrieval

#: The aggregates a memo actually needs. Each is one server-side `count` call.
#: `occurcountry` is deliberately absent: it returns HTTP 500 on this endpoint
#: (measured), and a field that 500s is recorded here as unavailable rather than
#: retried forever.
AGGREGATES = (
    ("reactions", "patient.reaction.reactionmeddrapt.exact", None),
    ("seriousness", "serious", SERIOUSNESS),
    ("reporter", "primarysource.qualification", REPORTER_QUALIFICATION),
    ("drug_role", "patient.drug.drugcharacterization", DRUG_ROLE),
)

#: Fields the drug name is matched against, best-linked first. Measured over the
#: whole database: openfda.generic_name 88.8%, medicinalproduct 100% (free text).
MATCH_FIELD = "patient.drug.openfda.generic_name"
FREE_TEXT_FIELD = "patient.drug.medicinalproduct"


def _search_for(asset: str, field_name: str) -> str:
    """An openFDA search clause for an asset, expanded through the ONE matcher.

    `agents.parse_asset` so a brand name or development code reaches the same
    reports the generic does — the same alias table the trial, drug and device
    stores use.
    """
    from .. import agents

    query = agents.parse_asset(asset)
    forms = [f for term in query.terms for f in term.forms]
    if not forms:
        return ""
    return " OR ".join(f'{field_name}:"{f}"' for f in dict.fromkeys(forms))


def fetch_aggregates(asset: str, timeout: int = 45, offline: bool = False) -> FAERSAnswer:
    """Pull the aggregate counts for one asset from openFDA.

    Never called when a cached aggregate exists and is wanted — see
    `DrugStore.faers_answer`. Raises in offline mode rather than returning an
    empty answer that would read as "no reports".
    """
    from datetime import datetime, timezone

    from .client import _fetch_json, _total  # the one throttled transport

    if offline:
        raise RuntimeError(
            f"offline mode is enabled: refusing to fetch FAERS aggregates for "
            f"“{asset}”. Run without --offline once to cache them.")

    normalised = _search_for(asset, MATCH_FIELD)
    free = _search_for(asset, FREE_TEXT_FIELD)
    if not normalised and not free:
        return FAERSAnswer(asset=asset, searched=True)

    # BOTH fields, ORed. Matching the normalised block alone under-counts badly
    # and sometimes completely: measured, irinotecan returns 12,783 reports
    # normalised against 47,829 free-text (3.74x), and botensilimab and
    # zanzalintinib — both investigational — return ZERO normalised and 1 and 36
    # free-text respectively. Reporting zero for those would be a false clean
    # safety impression from a matching artefact, which is the failure this
    # codebase guards everywhere. Same widen-rather-than-narrow rule as the
    # stopped-trial sweep; the two counts stay separately visible so a reader can
    # see how much of the total rests on the un-normalised field.
    search = " OR ".join(s for s in (normalised, free) if s)

    answer = FAERSAnswer(
        asset=asset, searched=True,
        matched_field=f"{MATCH_FIELD} OR {FREE_TEXT_FIELD}",
        retrieved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        faers_total=_total("drug/event", "_exists_:safetyreportid", timeout, offline) or 0,
        n_reports=_total("drug/event", search, timeout, offline) or 0,
    )
    if normalised:
        answer.n_reports_normalised = _total("drug/event", normalised, timeout, offline) or 0
    if free:
        answer.n_reports_free_text = _total("drug/event", free, timeout, offline) or 0

    if not answer.n_reports:
        return answer

    for attr, field_name, decode in AGGREGATES:
        payload = _fetch_json("drug/event", {"search": search, "count": field_name,
                                             "limit": 25}, timeout)
        buckets = []
        for row in (payload or {}).get("results") or []:
            term = str(row.get("term", ""))
            if decode is None:
                label = ""                      # free MedDRA text, nothing to decode
            else:
                # A coded field whose value the FDA data dictionary does not
                # define is labelled as undefined, never printed as a bare
                # number a reader would take for a category. Same rule as the
                # undocumented PMA decision codes.
                label = decode.get(term) or (
                    f"code {term}, which the FDA data dictionary does not define")
            buckets.append(TermCount(term=term, count=int(row.get("count", 0) or 0),
                                     label=label))
        setattr(answer, attr, buckets)

    # `seriousnessdeath` is documented as "1 if the adverse event resulted in
    # death, and absent otherwise", so only value 1 is counted as a fatal outcome.
    death = _total("drug/event", f"({search}) AND seriousnessdeath:1", timeout, offline)
    answer.n_death_reports = death or 0
    return answer


__all__ = [
    "FAERSAnswer", "TermCount", "DRUG_ROLE", "REPORTER_QUALIFICATION", "SERIOUSNESS",
    "WHAT_THIS_IS", "WHAT_THIS_IS_NOT", "FAERS_ABSENCE_MEANINGS", "NOT_SEARCHED",
    "ROLE_NOT_VALIDATED", "AGGREGATES", "MATCH_FIELD", "FREE_TEXT_FIELD",
    "fetch_aggregates",
]
