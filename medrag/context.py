"""Assemble prompt context from two kinds of evidence, labelled by provenance.

The model conflates a registry record with a review article unless told which is
which, and then citations point at the wrong kind of evidence - a memo claiming
"a trial showed X [3]" where [3] is a narrative review is worse than no citation.

Every item therefore carries an explicit source kind, TRIAL RECORD or LITERATURE,
and an identifier the analyst can verify without re-running anything: an NCT ID
or a PMID.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .documents import Retrieved
from .trials.client import TrialRecord

TRIAL_LABEL = "TRIAL RECORD"
LIT_LABEL = "LITERATURE"


@dataclass
class Evidence:
    """One numbered, citable item in the assembled context."""

    index: int
    kind: str                 # TRIAL RECORD | LITERATURE
    identifier: str           # NCT id or PMID
    text: str
    title: str = ""
    url: str = ""
    citation: str = ""
    score: float | None = None
    meta: dict = field(default_factory=dict)

    grade_tag: str = ""       # evidence tier for literature, e.g. RCT

    def render(self) -> str:
        head = f"[{self.index}] ({self.kind} — {self.identifier}"
        # The tier goes in the context, not only the bibliography: the model
        # should know it is reading a case report before it weighs the claim.
        head += f" — {self.grade_tag})" if self.grade_tag else ")"
        return f"{head}\n{self.text}"

    def bib_line(self) -> str:
        parts = [f"[{self.index}]", f"({self.kind})"]
        if self.grade_tag:
            parts.append(f"[{self.grade_tag}]")
        if self.title:
            parts.append(self.title)
        if self.citation:
            parts.append(f"— {self.citation}")
        # Identifiers are backticked so both Markdown and the PDF render them in
        # a monospace family with tabular numerals - a PMID or NCT is a machine
        # key, not prose, and readers scan them as a column.
        parts.append(f"`{self.identifier}`")
        if self.url:
            parts.append(self.url)
        return " ".join(parts)


def _trial_block(t: TrialRecord) -> str:
    """Render a trial as labelled fields, not prose.

    Field-per-line survives truncation legibly and keeps the model from
    paraphrasing a status into something softer than TERMINATED.
    """
    lines = [
        f"Title: {t.brief_title}",
        f"Phase: {t.phase or 'not stated'}",
        f"Status: {t.overall_status or 'not stated'}",
    ]
    if t.enrollment_count is not None:
        etype = f" ({t.enrollment_type.lower()})" if t.enrollment_type else ""
        lines.append(f"Enrollment: {t.enrollment_count}{etype}")
    if t.lead_sponsor:
        sclass = f" ({t.sponsor_class})" if t.sponsor_class else ""
        lines.append(f"Sponsor: {t.lead_sponsor}{sclass}")
    if t.conditions:
        lines.append(f"Conditions: {', '.join(t.conditions[:6])}")
    if t.interventions:
        lines.append(f"Interventions: {', '.join(t.interventions[:6])}")
    if t.start_date or t.primary_completion_date:
        lines.append(
            f"Dates: start {t.start_date or '?'}, primary completion "
            f"{t.primary_completion_date or '?'}"
        )
    if t.why_stopped:
        lines.append(f"WHY STOPPED: {t.why_stopped}")
    elif t.stopped_early:
        # Say so explicitly. Silence here reads as "no reason to worry" when it
        # actually means the sponsor filed nothing.
        lines.append("WHY STOPPED: not stated by sponsor")
    return "\n".join(lines)


def build_evidence(
    trials: list[TrialRecord] | None = None,
    passages: list[Retrieved] | None = None,
    max_chars: int = 12000,
) -> list[Evidence]:
    """Interleave the two sources into one numbered list.

    Trials come first: in diligence, what happened outranks what was argued.
    """
    items: list[Evidence] = []
    index = 1
    used = 0

    for t in trials or []:
        block = _trial_block(t)
        if used + len(block) > max_chars and items:
            break
        items.append(
            Evidence(
                index=index,
                kind=TRIAL_LABEL,
                identifier=t.nct_id,
                text=block,
                title=t.brief_title,
                url=t.url,
                citation=t.lead_sponsor,
                meta={"status": t.overall_status, "phase": t.phase,
                      "stopped_early": t.stopped_early,
                      # Carried so the claim verifier can flag company-authored
                      # evidence from the structured sponsor fields rather than
                      # guessing an affiliation out of prose.
                      "lead_sponsor": t.lead_sponsor,
                      "sponsor_class": t.sponsor_class,
                      "collaborators": list(t.collaborators)},
            )
        )
        used += len(block)
        index += 1

    for r in passages or []:
        pmid = r.chunk.doc_id
        block = r.chunk.text
        if used + len(block) > max_chars and items:
            break
        items.append(
            Evidence(
                index=index,
                kind=LIT_LABEL,
                identifier=f"PMID {pmid}" if pmid.isdigit() else pmid,
                text=block,
                title=r.chunk.title,
                url=r.chunk.url,
                citation=r.chunk.citation,
                score=r.score,
                grade_tag=getattr(r.chunk, "evidence_tag", ""),
                meta={
                    "section": r.chunk.section,
                    "evidence_key": getattr(r.chunk, "evidence_key", "unclassified"),
                    "evidence_rank": getattr(r.chunk, "evidence_rank", 8),
                    # The document-level funder signal, carried so independence is
                    # judged from the whole record, not the one cited chunk.
                    "disclosure": getattr(r.chunk, "disclosure", ""),
                    "disclosure_independent": getattr(r.chunk, "disclosure_independent", False),
                },
            )
        )
        used += len(block)
        index += 1

    return items


def render_context(evidence: list[Evidence]) -> str:
    return "\n\n".join(e.render() for e in evidence)


def render_bibliography(evidence: list[Evidence]) -> str:
    return "\n".join(e.bib_line() for e in evidence)


def provenance_summary(evidence: list[Evidence]) -> dict:
    """Counts by source kind - surfaced so a reader can see the evidence mix
    at a glance rather than inferring it from the citations."""
    trials = [e for e in evidence if e.kind == TRIAL_LABEL]
    lit = [e for e in evidence if e.kind == LIT_LABEL]
    tiers: dict[str, int] = {}
    for e in lit:
        if e.grade_tag:
            tiers[e.grade_tag] = tiers.get(e.grade_tag, 0) + 1
    return {
        "n_trials": len(trials),
        "n_literature": len(lit),
        "n_stopped_trials": sum(1 for e in trials if e.meta.get("stopped_early")),
        "evidence_tiers": tiers,
        # Answers "is this conclusion resting on case reports?" without the
        # reader having to audit every citation.
        "n_weak_evidence": sum(1 for e in lit if e.meta.get("evidence_rank", 8) >= 6),
    }
