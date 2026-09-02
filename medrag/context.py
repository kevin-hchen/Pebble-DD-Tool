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
from .fda.client import Clearance510k
from .fda.drugs import APPROVED, DrugApplication
from .fda.pma import PATHWAY_DE_NOVO, PATHWAY_PMA
from .trials.client import TrialRecord

TRIAL_LABEL = "TRIAL RECORD"
FDA_LABEL = "FDA RECORD"
# Drug approvals get their own label and their own identifier. "FDA RECORD" was
# fine while the only regulatory object was a 510(k), but a citation that reads
# `FDA RECORD — K123456` next to one that reads `FDA RECORD — BLA125514` invites
# the reader to treat a device clearance and a drug approval as the same kind of
# fact. They are not: one is substantial equivalence to a predicate, the other is
# a demonstration of safety and efficacy. The identifier is the application
# number, so a claim resolves to the exact application a reader can look up.
FDA_DRUG_LABEL = "FDA DRUG APPROVAL"
# Premarket APPROVAL gets its own label for the same reason drug approvals did.
# A 510(k) is substantial equivalence to a predicate; a PMA is approval on
# clinical evidence. One label spanning both would tell a reader that a Class II
# thermometer and a Class III defibrillator stand in the same relation to the FDA.
FDA_PMA_LABEL = "FDA DEVICE APPROVAL (PMA)"
# And a De Novo is neither: granted BECAUSE no predicate exists.
FDA_DE_NOVO_LABEL = "FDA DE NOVO AUTHORISATION"
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


def _fda_block(c: Clearance510k) -> str:
    """Render a 510(k) clearance as labelled fields. The regulatory facts a
    diligence question actually asks: what it is, its class, and when it cleared."""
    lines = [
        f"Device: {c.device_name}",
        f"510(k): {c.k_number}",
        f"Product code: {c.product_code or 'not stated'}",
        f"Device class: {c.device_class or 'not stated'}",
        f"Decision: {c.decision_description or c.decision_code or 'not stated'}"
        + (f" ({c.decision_date})" if c.decision_date else ""),
    ]
    if c.applicant:
        lines.append(f"Applicant: {c.applicant}")
    if c.clearance_type:
        lines.append(f"Clearance type: {c.clearance_type}")
    if c.regulation_number:
        lines.append(f"Regulation: {c.regulation_number}")
    return "\n".join(lines)


def _drug_block(a: DrugApplication) -> str:
    """Render a drugsFDA application as labelled fields.

    The status line is written so the model cannot soften it: a TENTATIVE
    APPROVAL says outright that it is not an approval, and an application whose
    products are all discontinued says that separately from whether it was ever
    approved. Both are facts the raw record states and prose routinely blurs.
    """
    lines = [
        f"Application: {a.display_number}",
        f"Sponsor: {a.sponsor_name or 'not stated'}",
        f"Approval status: {a.approval_status}",
    ]
    if a.approval_status == APPROVED and a.approval_date:
        lines.append(f"Original US approval date: {a.approval_date}")
    elif a.approval_status != APPROVED:
        lines.append("NOTE: this application is NOT an approval. A tentative approval "
                     "means the FDA found the application met requirements but could "
                     "not approve it, usually for patent or exclusivity reasons.")
    if a.all_ingredients:
        lines.append(f"Active ingredients: {', '.join(a.all_ingredients[:6])}")
    brands = sorted({p.brand_name for p in a.products if p.brand_name} | set(a.brand_names))
    if brands:
        lines.append(f"Brand name(s): {', '.join(brands[:6])}")
    if a.routes:
        lines.append(f"Route: {', '.join(a.routes[:4])}")
    if a.marketing_statuses:
        lines.append(f"Marketing status: {', '.join(a.marketing_statuses)}")
    if a.all_discontinued:
        lines.append("NOTE: every product under this application is marked Discontinued — "
                     "withdrawn from marketing, which is NOT the same as never approved.")
    if a.n_supplements:
        lines.append(f"Approved supplements: {a.n_supplements}"
                     + (f" (latest {a.latest_supplement_date})" if a.latest_supplement_date else ""))
    if a.review_priority:
        lines.append(f"Review priority: {a.review_priority}")
    if a.submission_class:
        lines.append(f"Submission class: {a.submission_class}")
    return "\n".join(lines)


def _pma_block(a) -> str:
    """Render a PMA application as labelled fields.

    The pathway line is first and explicit: this is approval on clinical
    evidence, not clearance by predicate equivalence, and the model must not be
    able to blur them. Device class is printed verbatim with a note that it is
    not inferred, because 7,177 PMA records are Class 2.
    """
    rep = a.representative
    lines = [
        f"Pathway: {PATHWAY_PMA} — approval supported by clinical evidence of "
        "safety and effectiveness. NOT a 510(k) clearance and NOT a finding of "
        "substantial equivalence to a predicate device.",
        f"PMA number: {a.pma_number}",
        f"Approval decision: {a.approval_state}",
    ]
    if a.approval_date:
        lines.append(f"Original approval date: {a.approval_date}")
    if not a.has_original_record:
        lines.append("NOTE: the original application record is not present in this "
                     "copy of the export — only supplements to it — so its decision "
                     "cannot be read from here.")
    if rep is not None:
        if rep.trade_name:
            lines.append(f"Trade name: {rep.trade_name}")
        if rep.generic_name:
            lines.append(f"Generic name: {rep.generic_name}")
        if rep.applicant:
            lines.append(f"Applicant: {rep.applicant}")
        if rep.product_code:
            lines.append(f"Product code: {rep.product_code}")
        lines.append(f"Device class (as filed, not inferred from the pathway): "
                     f"{rep.device_class or 'not stated'}")
        if rep.advisory_committee_description:
            lines.append(f"Advisory committee: {rep.advisory_committee_description}")
    if a.supplements:
        lines.append(f"Approved supplements on file: {len(a.supplements)}"
                     + (f" (latest {a.latest_supplement_date})"
                        if a.latest_supplement_date else ""))
    return "\n".join(lines)


def _de_novo_block(c: Clearance510k) -> str:
    """A De Novo authorisation, stated as what it is."""
    return "\n".join([
        f"Pathway: {PATHWAY_DE_NOVO} — granted BECAUSE no predicate device existed. "
        "This is NOT a finding of substantial equivalence and must not be described "
        "as one.",
        f"Submission: {c.k_number}",
        f"Device: {c.device_name or 'not stated'}",
        f"Decision date: {c.decision_date or 'not stated'}",
        f"Applicant: {c.applicant or 'not stated'}",
        f"Product code: {c.product_code or 'not stated'}",
        f"Device class (as filed): {c.device_class or 'not stated'}",
    ])


def build_evidence(
    trials: list[TrialRecord] | None = None,
    passages: list[Retrieved] | None = None,
    fda: list[Clearance510k] | None = None,
    drugs: list[DrugApplication] | None = None,
    pma: list | None = None,
    de_novo: list[Clearance510k] | None = None,
    max_chars: int = 12000,
) -> list[Evidence]:
    """Interleave the sources into one numbered list.

    Trials and FDA clearances come first: in diligence, what happened and what
    cleared outrank what was argued. A single numbering across all three so a
    citation [n] resolves the same everywhere — introducing a parallel scheme
    has been a bug twice.
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

    for c in fda or []:
        block = _fda_block(c)
        if used + len(block) > max_chars and items:
            break
        items.append(
            Evidence(
                index=index,
                kind=FDA_LABEL,
                identifier=c.k_number,
                text=block,
                title=c.device_name,
                url=c.url,
                citation=c.applicant,
                meta={"product_code": c.product_code, "device_class": c.device_class,
                      "decision": c.decision_description},
            )
        )
        used += len(block)
        index += 1

    for c in de_novo or []:
        block = _de_novo_block(c)
        if used + len(block) > max_chars and items:
            break
        items.append(Evidence(
            index=index, kind=FDA_DE_NOVO_LABEL, identifier=c.k_number, text=block,
            title=c.device_name, url=c.url, citation=c.applicant,
            meta={"pathway": PATHWAY_DE_NOVO, "product_code": c.product_code,
                  "device_class": c.device_class},
        ))
        used += len(block)
        index += 1

    for a in pma or []:
        block = _pma_block(a)
        if used + len(block) > max_chars and items:
            break
        rep = a.representative
        items.append(Evidence(
            index=index, kind=FDA_PMA_LABEL,
            # The PMA number, so a claim cites the exact application.
            identifier=a.pma_number, text=block,
            title=a.trade_name, url=a.url, citation=a.applicant,
            meta={"pathway": PATHWAY_PMA, "approval_state": a.approval_state,
                  "approval_date": a.approval_date,
                  "device_class": rep.device_class if rep else "",
                  "product_code": a.product_code,
                  "has_original_record": a.has_original_record},
        ))
        used += len(block)
        index += 1

    for a in drugs or []:
        block = _drug_block(a)
        if used + len(block) > max_chars and items:
            break
        items.append(
            Evidence(
                index=index,
                kind=FDA_DRUG_LABEL,
                # The application number in the form the FDA prints — "NDA 021923",
                # not a generic FDA RECORD — so a claim resolves to the exact
                # application an analyst can paste into Drugs@FDA.
                identifier=a.display_number,
                text=block,
                title=(sorted({p.brand_name for p in a.products if p.brand_name})
                       or a.brand_names or a.all_ingredients or [""])[0],
                url=a.url,
                citation=a.sponsor_name,
                meta={"approval_status": a.approval_status,
                      "approval_date": a.approval_date,
                      "application_type": a.application_type,
                      "is_approved": a.is_approved,
                      "all_discontinued": a.all_discontinued,
                      "ingredients": a.all_ingredients},
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
    fda = [e for e in evidence if e.kind == FDA_LABEL]
    drugs = [e for e in evidence if e.kind == FDA_DRUG_LABEL]
    pma = [e for e in evidence if e.kind == FDA_PMA_LABEL]
    de_novo = [e for e in evidence if e.kind == FDA_DE_NOVO_LABEL]
    tiers: dict[str, int] = {}
    for e in lit:
        if e.grade_tag:
            tiers[e.grade_tag] = tiers.get(e.grade_tag, 0) + 1
    return {
        "n_trials": len(trials),
        "n_literature": len(lit),
        "n_fda": len(fda),
        "n_fda_drug": len(drugs),
        # Counted apart from clearances: a PMA and a 510(k) are different
        # regulatory facts and a provenance line that merges them hides which.
        "n_fda_pma": len(pma),
        "n_fda_de_novo": len(de_novo),
        # Counted separately from n_fda_drug: an application in evidence is not
        # necessarily an approved one, and a reader scanning provenance should
        # not have to open each citation to find out.
        "n_fda_drug_approved": sum(1 for e in drugs if e.meta.get("is_approved")),
        "n_stopped_trials": sum(1 for e in trials if e.meta.get("stopped_early")),
        "evidence_tiers": tiers,
        # Answers "is this conclusion resting on case reports?" without the
        # reader having to audit every citation.
        "n_weak_evidence": sum(1 for e in lit if e.meta.get("evidence_rank", 8) >= 6),
    }
