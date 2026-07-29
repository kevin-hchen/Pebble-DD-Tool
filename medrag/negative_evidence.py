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


@dataclass
class StoppedTrial:
    """A trial that stopped early. Deterministic; no model judgement."""

    record: TrialRecord

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
    stopped_trials: list[StoppedTrial] = field(default_factory=list)
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
            bits.append(
                f"{len(self.stopped_trials)} trial(s) stopped early "
                f"({stated} with a stated reason)"
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


def find_stopped_trials(
    store,
    intervention: str | None = None,
    condition: str | None = None,
    limit: int = 25,
) -> list[StoppedTrial]:
    """Deterministic half. Pure SQL over the registry.

    Intervention and condition are searched as alternatives, never ANDed. A
    trial of the same compound terminated in a DIFFERENT indication is among the
    most valuable things a diligence pass can surface - the molecule failed
    somewhere, and requiring the indication to match would hide it. Widening to
    OR risks a few loosely related trials, which a reader can dismiss; narrowing
    to AND risks silence, which they cannot detect.
    """
    if store is None:
        return []

    records: list[TrialRecord] = []
    seen: set[str] = set()
    for key in ("intervention", "condition"):
        value = intervention if key == "intervention" else condition
        if not value:
            continue
        for r in store.stopped_trials(**{key: value}, limit=limit):
            if r.nct_id not in seen:
                seen.add(r.nct_id)
                records.append(r)

    if not intervention and not condition:
        records = store.stopped_trials(limit=limit)

    # Trials with a stated reason first: they carry more information.
    records.sort(key=lambda r: (not bool(r.why_stopped), r.nct_id))
    return [StoppedTrial(record=r) for r in records[:limit]]


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
) -> NegativeEvidence:
    """Run every half and assemble the section.

    The deterministic halves — stopped trials, and (for a device) FDA recalls and
    adverse events — always run when their store is present. The FDA device_name
    defaults to the trial intervention, since the asset name is the same string."""
    stopped = find_stopped_trials(trial_store, intervention=intervention, condition=condition)

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
        stopped_trials=stopped,
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
