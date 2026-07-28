"""The negative-evidence pass.

RAG confirms whatever you ask it. For diligence the valuable output is the
evidence AGAINST the thesis, and it will not appear on its own - it has to be
its own stage with its own output section.

Two halves, doing different jobs:

  Deterministic - any trial on the same intervention or target with status
  TERMINATED, WITHDRAWN or SUSPENDED, with whyStopped attached. A database
  query. It cannot hallucinate and it cannot be talked out of a result.

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
    findings: list[Finding] = field(default_factory=list)
    model: str = ""
    searched: bool = True          # False when the model half could not run
    note: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.stopped_trials and not self.findings

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
) -> NegativeEvidence:
    """Run both halves and assemble the section."""
    stopped = find_stopped_trials(trial_store, intervention=intervention, condition=condition)
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
        findings=findings,
        model=model,
        searched=searched,
        note=note,
    )
