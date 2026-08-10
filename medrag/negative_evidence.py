"""The negative-evidence pass.

RAG confirms whatever you ask it. For diligence the valuable output is the
evidence AGAINST the thesis, and it will not appear on its own - it has to be
its own stage with its own output section.

Two halves, doing different jobs:

  Deterministic - facts from databases, which cannot hallucinate and cannot be
  talked out of a result: trials that stopped early (TERMINATED / WITHDRAWN /
  SUSPENDED, with whyStopped attached), and, for a device, its FDA recalls and
  MAUDE adverse-event reports. A recall is a fact from a database, not a model
  judgement, so it lives here beside the stopped trials — but on its own lines in
  the memo, because a recall and a halted trial are different failure modes and
  deserve different framing.

  Model - a second call over the retrieved literature whose ONLY instruction is
  to find findings that contradict, fail to replicate, or fail to support the
  claim. It is given explicit permission to return nothing, because a forced
  contradiction prompt will invent one, and an invented contradiction in a
  diligence memo is worse than silence: it burns credibility on the one section
  a partner will actually read closely.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .config import Config
from .context import Evidence, render_context
from .fda.client import AdverseEvent, Recall
from .providers import make_client
from .trials.client import TrialRecord

CONTRADICTION_PROMPT = """You are auditing a claim about a biomedical asset for an \
investment diligence memo. Your ONLY task is to find evidence in the excerpts \
below that WEAKENS the claim.

Claim under audit: {claim}

Report only:
- findings that directly contradict the claim
- failures to replicate it
- null or negative results on the same question
- material limitations that undercut it: tiny samples, surrogate endpoints \
standing in for clinical ones, very short follow-up, single-centre design, \
sponsor-run analyses, populations that do not generalise
- safety or tolerability signals that offset the claimed benefit

Rules:
1. Use ONLY the excerpts. Cite each point by its [n] marker.
2. If the excerpts contain NO contradicting or undercutting evidence, return an \
empty findings list. This is a normal and useful result. Do NOT manufacture a \
weakness to fill the section, and do NOT restate the claim's own caveats as if \
they were contrary findings.
3. Do not soften. State the strongest honest version of each point.
4. Quantitative detail must be preserved exactly as written.

Excerpts:
{context}

Return JSON only:
{{"findings": [{{"point": "<one sentence>", "citation": <n>, "kind": \
"contradiction|non-replication|null-result|limitation|safety"}}]}}"""


#: The two arms get their OWN budgets rather than competing for one, and the
#: sizes were set by measurement, not by splitting 25 in half.
#:
#: They answer different questions — "has this compound failed anywhere, in any
#: disease" and "what has failed in this disease" — and the first is the
#: higher-value, harder-to-find answer. On the live colorectal store the
#: indication arm holds 1,336 stopped trials against an intervention arm of
#: 2-93, and 89% of the indication arm carries a stated reason, so the old
#: shared sort (reason-stated, then NCT ID) decayed to alphabetical order across
#: a pool the indication arm outnumbered by 15-600x.
#:
#: THE FIRST SPLIT TRIED WAS WRONG, AND THE MEASUREMENT SAID SO. Reserving 15
#: of the 25 for the intervention arm looked protective and was not: for
#: "encorafenib and cetuximab" the old shared budget happened to yield 20
#: intervention rows, so a 15-row reservation DROPPED 5 of them, and across five
#: real assets 23 intervention-derived trials that had been shown stopped being
#: shown. A reservation is a ceiling as well as a floor, and putting a ceiling
#: on the high-value arm is the opposite of the intent.
#:
#: So the arms are sized by their nature instead. The intervention arm is
#: BOUNDED BY THE WORLD — a compound has as many trials as it has, 93 at the
#: top of what was measured — and every one of them is a direct answer, so it
#: gets the whole original budget and can never lose a row it used to show. The
#: indication arm is effectively UNBOUNDED (1,336 and rising with the fetch), it
#: is context rather than a finding about this asset, and no sample size makes a
#: 1,336-row pool representative — so it gets a small fixed budget and states
#: its denominator. Worst case a section grows from 25 rows to 35.
#:
#: Deliberately no spillover between them. Letting an empty intervention arm
#: inflate the indication arm to 35 rows trades a bigger memo for more rows of a
#: sample that was already unrepresentative at 10; the coverage line carries
#: that information honestly in one sentence instead.
INTERVENTION_BUDGET = 25
INDICATION_BUDGET = 10

#: How many candidates to pull per arm before ordering and truncating. Larger
#: than the budget so "trials with a stated reason first" ranks over a real
#: window rather than over exactly the rows that will be shown; bounded so a
#: 1,336-trial arm is never materialised in full for a 35-row section.
_CANDIDATE_WINDOW = (INTERVENTION_BUDGET + INDICATION_BUDGET) * 2

INTERVENTION_ARM = "intervention"
INDICATION_ARM = "indication"


@dataclass
class StoppedTrial:
    """A trial that stopped early. Deterministic; no model judgement."""

    record: TrialRecord
    #: Which arm(s) surfaced it. A trial of this compound IN this indication is
    #: found by both, and is attributed to the intervention arm because that is
    #: the question it answers most directly.
    arms: tuple[str, ...] = (INTERVENTION_ARM,)

    @property
    def from_intervention(self) -> bool:
        return INTERVENTION_ARM in self.arms

    @property
    def reason(self) -> str:
        return self.record.why_stopped or "not stated by sponsor"

    @property
    def reason_is_stated(self) -> bool:
        return bool(self.record.why_stopped)

    def line(self) -> str:
        r = self.record
        head = f"{r.nct_id} — {r.overall_status}"
        if r.phase:
            head += f", {r.phase}"
        if r.enrollment_count is not None:
            head += f", n={r.enrollment_count}"
        if r.lead_sponsor:
            head += f" ({r.lead_sponsor})"
        return f"{head}\n    Reason: {self.reason}"


@dataclass
class Finding:
    point: str
    citation: int
    kind: str = "limitation"
    identifier: str = ""
    grade_tag: str = ""


@dataclass
class NegativeEvidence:
    claim: str = ""
    #: The full sweep, with its per-arm denominators. `stopped_trials` below
    #: reads from it, so every renderer keeps working while the counts a memo
    #: must state stay reachable.
    stopped: "StoppedTrialSweep" = field(default_factory=lambda: StoppedTrialSweep())
    recalls: list[Recall] = field(default_factory=list)
    adverse_events: list[AdverseEvent] = field(default_factory=list)
    event_totals: dict = field(default_factory=dict)   # event_type -> count in store
    findings: list[Finding] = field(default_factory=list)
    product_code: str = ""
    model: str = ""
    searched: bool = True          # False when the model half could not run
    fda_searched: bool = True      # False when no FDA store was available to check
    note: str = ""

    @property
    def stopped_trials(self) -> list[StoppedTrial]:
        """What gets printed. Kept as the name every renderer already uses."""
        return self.stopped.trials

    @property
    def is_empty(self) -> bool:
        return not (self.stopped_trials or self.recalls or self.adverse_events or self.findings)

    @property
    def events_shown_of(self) -> int:
        """Total adverse-event reports in the store for this device — the memo
        shows a handful and must say of how many, never imply it saw them all."""
        return sum(self.event_totals.values())

    def summary(self) -> str:
        if not self.searched:
            return "Not assessed — no model available for the contradiction pass."
        if self.is_empty:
            # Say which way the silence cuts. "Nothing found" and "nothing exists"
            # are different claims and the memo must not blur them.
            return (
                "No contradicting evidence found in the retrieved set. This is not "
                "the same as no contradicting evidence existing — it bounds the "
                "search, not the literature."
            )
        bits = []
        if self.stopped_trials:
            stated = sum(1 for s in self.stopped_trials if s.reason_is_stated)
            # "of N" is not decoration here: this is a capped sample of a pool
            # that reaches 1,336 on a real indication.
            bits.append(
                f"{len(self.stopped_trials)} of {self.stopped.n_total} trial(s) stopped "
                f"early shown ({stated} with a stated reason)"
            )
        if self.recalls:
            bits.append(f"{len(self.recalls)} FDA recall(s)")
        if self.adverse_events:
            bits.append(
                f"{len(self.adverse_events)} of {self.events_shown_of} adverse-event "
                "report(s) shown"
            )
        if self.findings:
            bits.append(f"{len(self.findings)} contradicting or undercutting finding(s)")
        return "; ".join(bits)


def _recency_key(start_date: str) -> int:
    """Sort key placing the most recently started trial first, and a trial with
    no start date on file last rather than first — an absent date is not a
    recent one, the same not-found-is-not-a-value rule this codebase applies to
    disclosures and biomarkers."""
    digits = "".join(c for c in (start_date or "") if c.isdigit())[:8]
    if not digits:
        return 1                      # sorts after every negative value below
    return -int(digits.ljust(8, "0"))


@dataclass
class StoppedTrialSweep:
    """What the deterministic half found, per arm, with its denominators.

    `trials` is what gets printed; every other field exists so a reader is never
    shown a capped sample that looks like a complete answer. `store.query`
    returns rows with no denominator, and this sweep used to inherit that: 25
    shown of 1,336 and 25 shown of 25 rendered identically.
    """
    trials: list[StoppedTrial] = field(default_factory=list)
    n_intervention_total: int = 0     # stopped trials of this compound, anywhere
    n_indication_total: int = 0       # stopped trials in this indication
    n_total: int = 0                  # unique union of the two arms
    searched_intervention: bool = False   # False => no asset given, NOT "none found"
    searched_indication: bool = False

    @property
    def n_shown(self) -> int:
        return len(self.trials)

    @property
    def n_shown_intervention(self) -> int:
        return sum(1 for t in self.trials if t.from_intervention)

    @property
    def n_shown_indication(self) -> int:
        return sum(1 for t in self.trials if not t.from_intervention)

    def coverage_line(self) -> str:
        """One line naming the split and both denominators — the same discipline
        the trial landscape applies to its own capped sample. Rendered by the
        Markdown memo and the PDF from this single function so the two cannot
        disagree."""
        if not (self.searched_intervention or self.searched_indication):
            return ""
        arms = []
        if self.searched_intervention:
            arms.append(f"{self.n_shown_intervention} of {self.n_intervention_total} "
                        "stopped trial(s) of this compound in any indication")
        if self.searched_indication:
            arms.append(f"{self.n_shown_indication} of {self.n_indication_total} "
                        "stopped trial(s) in this indication")
        held = self.n_total - self.n_shown
        line = f"Showing {self.n_shown} of {self.n_total}: " + "; ".join(arms) + "."
        if held > 0:
            line += (f" {held} stopped trial(s) are not listed — each arm has its own "
                     "budget, so a large indication pool cannot crowd out a trial of "
                     "the compound itself.")
        return line


def find_stopped_trials(
    store,
    intervention: str | None = None,
    condition: str | None = None,
    limit: int = 25,
    query_set: str | None = None,
) -> StoppedTrialSweep:
    """Deterministic half. Pure SQL over the registry.

    Intervention and indication are searched as alternatives, never ANDed. A
    trial of the same compound terminated in a DIFFERENT indication is among the
    most valuable things a diligence pass can surface - the molecule failed
    somewhere, and requiring the indication to match would hide it. Widening to
    OR risks a few loosely related trials, which a reader can dismiss; narrowing
    to AND risks silence, which they cannot detect.

    TWO ARMS, TWO BUDGETS, TWO DENOMINATORS

    The indication arm selects by `query_set`, like every other consumer in this
    codebase. It used to run `LOWER(conditions) LIKE '%<indication>%'`, which was
    exempted from the three condition-matching fixes on the reasoning that a
    substring is loose and this sweep only ever wants to widen. The measurement
    says the opposite: on the live colorectal store the substring saw 557 of
    1,336 stopped trials and missed 779 of them (58%), because "Colorectal
    Neoplasms" does not contain "colorectal cancer". A sweep whose whole purpose
    is exhaustiveness was the least exhaustive path in the tool.

    Fixing that made the indication arm 2.4x larger, which is exactly the
    condition under which a shared budget starts hiding the intervention arm, so
    the arms no longer share one. See INTERVENTION_BUDGET.
    """
    sweep = StoppedTrialSweep()
    if store is None:
        return sweep

    sweep.searched_intervention = bool(intervention)
    sweep.searched_indication = bool(query_set or condition)

    if not intervention and not (query_set or condition):
        # No handle at all: report the registry-wide stopped set, unsplit.
        records = store.stopped_trials(limit=limit)
        sweep.trials = [StoppedTrial(record=r, arms=()) for r in records]
        sweep.n_total = len(records)
        return sweep

    def arm(**kw) -> list[TrialRecord]:
        return store.stopped_trials(limit=_CANDIDATE_WINDOW, **kw) if any(kw.values()) else []

    iv_records = arm(intervention=intervention)
    ind_records = arm(query_set=query_set) if query_set else arm(condition=condition)

    if intervention:
        sweep.n_intervention_total = store.stopped_trials_total(intervention=intervention)
    if query_set or condition:
        sweep.n_indication_total = store.stopped_trials_total(
            query_set=query_set, condition=None if query_set else condition)

    # A trial found by both arms belongs to the intervention arm: it answers
    # "has this compound failed" as well as "what has failed here", and the
    # first is the question that is harder to answer any other way.
    iv_ids = {r.nct_id for r in iv_records}
    ind_only = [r for r in ind_records if r.nct_id not in iv_ids]

    # Stated reasons first, then most recently started, within each arm. Done per
    # arm rather than over the merged pool, because a merged sort is what let the
    # larger arm decide the order of both.
    #
    # The recency tiebreak is not cosmetic. NCT ID was the old tiebreak and 89%
    # of the indication arm carries a stated reason, so the sort decayed to
    # alphabetical — which for NCT IDs means OLDEST-registered first, the exact
    # opposite of what a diligence reader wants, and it made "which 25 of 81"
    # depend on how many candidates happened to be fetched. Ordering by the same
    # key the SQL already uses makes the candidate window invisible: fetching 70
    # and showing 25 yields the same 25 as fetching 25.
    def by_information(records):
        return sorted(records, key=lambda r: (not bool(r.why_stopped),
                                              _recency_key(r.start_date), r.nct_id))

    iv_sorted, ind_sorted = by_information(iv_records), by_information(ind_only)

    # Fixed per-arm budgets, no spillover — see INTERVENTION_BUDGET for why the
    # intervention arm keeps the whole original budget rather than a share of it.
    iv_take = min(len(iv_sorted), INTERVENTION_BUDGET)
    ind_take = min(len(ind_sorted), INDICATION_BUDGET)

    both_ids = {r.nct_id for r in ind_records} & iv_ids
    trials = [
        StoppedTrial(record=r,
                     arms=(INTERVENTION_ARM, INDICATION_ARM) if r.nct_id in both_ids
                     else (INTERVENTION_ARM,))
        for r in iv_sorted[:iv_take]
    ] + [
        StoppedTrial(record=r, arms=(INDICATION_ARM,)) for r in ind_sorted[:ind_take]
    ]

    # The union total counts each trial once, so the two arm totals overlapping
    # cannot inflate it.
    overlap = store.stopped_trials_total(
        intervention=intervention, query_set=query_set,
        condition=None if query_set else condition,
    ) if (intervention and (query_set or condition)) else 0
    sweep.n_total = (sweep.n_intervention_total + sweep.n_indication_total - overlap)

    # Presentation order across the merged, already-budgeted list: stated reason
    # first, then the compound's own trials, then NCT. This decides only how the
    # already-selected rows READ, never which rows survive the cap.
    sweep.trials = sorted(
        trials, key=lambda t: (not t.reason_is_stated, not t.from_intervention,
                               t.record.nct_id))
    return sweep


def _resolve_product_codes(fda_store, product_code, device_name) -> list[str]:
    if product_code:
        return [product_code.upper()]
    if device_name:
        return fda_store.product_codes_for_device(device_name)
    return []


def find_device_recalls(fda_store, product_code=None, device_name=None,
                        limit: int = 25) -> list[Recall]:
    """Deterministic half for devices. Recalls for the product code(s) OR the
    device description — never the manufacturer, which fragments on live data.
    Pure SQL; no model judgement."""
    if fda_store is None:
        return []
    seen: set[str] = set()
    out: list[Recall] = []
    for code in _resolve_product_codes(fda_store, product_code, device_name):
        for r in fda_store.recalls(product_code=code, limit=limit):
            if r.recall_number not in seen:
                seen.add(r.recall_number)
                out.append(r)
    if device_name:
        for r in fda_store.recalls(device_name=device_name, limit=limit):
            if r.recall_number not in seen:
                seen.add(r.recall_number)
                out.append(r)
    return out[:limit]


def find_adverse_events(fda_store, product_code=None, device_name=None,
                        limit: int = 15) -> tuple[list[AdverseEvent], dict]:
    """MAUDE reports for the device, worst-severity first, hard-capped. Returns
    the shown events and the per-type totals in the store, so the memo can say
    '3 of 812 shown' rather than implying the list is complete."""
    if fda_store is None:
        return [], {}
    events: list[AdverseEvent] = []
    totals: dict[str, int] = {}
    seen: set[str] = set()
    for code in _resolve_product_codes(fda_store, product_code, device_name):
        for et, n in fda_store.event_counts(code).items():
            totals[et] = totals.get(et, 0) + n
        for e in fda_store.events(product_code=code, limit=limit):
            if e.report_number not in seen:
                seen.add(e.report_number)
                events.append(e)
    events.sort(key=lambda e: (e.severity_rank, e.date_received), reverse=False)
    return events[:limit], totals


class ContradictionHunter:
    """Model half. Separate call, separate prompt, permission to find nothing."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = make_client(cfg)

    def hunt(self, claim: str, evidence: list[Evidence]) -> tuple[list[Finding], str, bool]:
        """Findings must cite the same numbering the memo renders. Callers pass
        the already-assembled evidence list rather than raw passages, because
        rebuilding it here numbered literature from 1 while the memo numbered
        trials first, so a finding citing [4] pointed at the wrong record."""
        if not evidence:
            return [], "", True
        if self.client is None:
            # Better to declare the section unassessed than to imply a clean bill
            # of health that nothing actually checked.
            return [], "", False

        prompt = CONTRADICTION_PROMPT.format(claim=claim, context=render_context(evidence))
        by_index = {e.index: e for e in evidence}

        try:
            resp = self.client.chat.completions.create(
                model=self.cfg.chat_model,
                temperature=0.0,
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": prompt}],
            )
            payload = json.loads(resp.choices[0].message.content)
        except Exception:
            return [], "", False

        findings: list[Finding] = []
        for raw in payload.get("findings") or []:
            point = str(raw.get("point", "")).strip()
            if not point:
                continue
            try:
                citation = int(raw.get("citation"))
            except (TypeError, ValueError):
                continue
            # Drop findings citing sources that were never retrieved rather than
            # letting an unverifiable point into the memo.
            source = by_index.get(citation)
            if source is None:
                continue
            findings.append(
                Finding(
                    point=point,
                    citation=citation,
                    kind=str(raw.get("kind", "limitation")),
                    identifier=source.identifier,
                    grade_tag=source.grade_tag,
                )
            )

        return findings, self.cfg.chat_model, True


def run_negative_pass(
    claim: str,
    cfg: Config,
    evidence: list[Evidence] | None = None,
    trial_store=None,
    intervention: str | None = None,
    condition: str | None = None,
    fda_store=None,
    product_code: str | None = None,
    device_name: str | None = None,
    max_events: int = 15,
    query_set: str | None = None,
) -> NegativeEvidence:
    """Run every half and assemble the section.

    The deterministic halves — stopped trials, and (for a device) FDA recalls and
    adverse events — always run when their store is present. The FDA device_name
    defaults to the trial intervention, since the asset name is the same string.

    `query_set` selects the indication arm of the stopped-trial sweep. A caller
    that passes only `condition` still works, but selects by substring and will
    under-count badly — see `find_stopped_trials`."""
    sweep = find_stopped_trials(trial_store, intervention=intervention,
                                condition=condition, query_set=query_set)
    stopped = sweep.trials

    device = device_name or intervention
    recalls = find_device_recalls(fda_store, product_code=product_code, device_name=device)
    events, event_totals = find_adverse_events(
        fda_store, product_code=product_code, device_name=device, limit=max_events)

    findings, model, searched = ContradictionHunter(cfg).hunt(claim, evidence or [])

    note = ""
    if stopped and not any(s.reason_is_stated for s in stopped):
        note = (
            "Trials stopped early but no sponsor filed a reason. Absence of a stated "
            "reason is not evidence that the reason was benign."
        )

    return NegativeEvidence(
        claim=claim,
        stopped=sweep,
        recalls=recalls,
        adverse_events=events,
        event_totals=event_totals,
        findings=findings,
        product_code=product_code or "",
        model=model,
        searched=searched,
        # Distinct from "no recalls found": no FDA store means we did not check.
        fda_searched=fda_store is not None,
        note=note,
    )
