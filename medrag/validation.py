"""Faithfulness checks: does the answer actually follow from the retrieved text?

Three cheap, deterministic signals, run on every answer:

* citation coverage - share of substantive sentences carrying an [n] marker
* citation validity - do all markers point at sources that were retrieved
* numeric grounding  - every number in the answer must appear in a cited chunk

Numeric grounding catches the failure mode that matters most clinically: a
hallucinated hazard ratio or dose reads exactly like a real one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .documents import Retrieved
from .generator import Answer, parse_citation_indices

_NUM_RE = re.compile(r"(?<![\w\[])(\d+(?:\.\d+)?)\s*(%|mg|kg|ml|mmhg|years?|months?|weeks?|days?)?")
# Broader than a single [n]: matches any bracket cluster containing only digits,
# commas, spaces, and hyphens/en-dashes. `_numbers` strips these so ranges like
# [11-20] cannot leak "11" and "20" into the claim's figure set.
_CITE_CLUSTER_RE = re.compile(r"\[[\d\s,\-–]+\]")
# Sentences that make no factual claim don't need a citation.
_HEDGE_RE = re.compile(
    r"^\s*(the (retrieved|available|indexed|provided|given) (excerpts?|literature|passages?)|"
    r"no relevant|this (does not|doesn't)|in summary|overall,|however,|note that|"
    r"these (findings|excerpts)|"
    r"what is missing|there is no evidence in the (provided|given))",
    re.IGNORECASE,
)


@dataclass
class ValidationReport:
    citation_coverage: float = 0.0
    invalid_citations: list[int] = field(default_factory=list)
    ungrounded_numbers: list[str] = field(default_factory=list)
    n_claims: int = 0
    n_cited: int = 0
    # False when nothing was actually checked: no evidence, or the answer came
    # from a path with no model. Without this, a section nobody validated
    # reported a clean pass, which is the same false-negative-dressed-as-a-pass
    # this module exists to prevent.
    assessed: bool = True
    reason: str = ""

    @property
    def passed(self) -> bool:
        if not self.assessed:
            return False
        return (
            not self.invalid_citations
            and not self.ungrounded_numbers
            and (self.citation_coverage >= 0.8 or self.n_claims == 0)
        )

    def summary(self) -> str:
        if not self.assessed:
            return f"NOT ASSESSED — {self.reason or 'nothing to check'}"
        if self.passed:
            if self.n_claims == 0:
                return "PASS — no claim-bearing sentences to check"
            return f"PASS — {self.n_cited}/{self.n_claims} claims cited, all figures grounded"
        issues = []
        if self.citation_coverage < 0.8 and self.n_claims:
            issues.append(f"{self.n_claims - self.n_cited} uncited claim(s)")
        if self.invalid_citations:
            issues.append(f"invalid citation(s) {self.invalid_citations}")
        if self.ungrounded_numbers:
            issues.append(f"ungrounded figure(s) {self.ungrounded_numbers}")
        return "REVIEW — " + "; ".join(issues)


def _split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text)
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z(\[])", text) if s.strip()]


def _numbers(text: str) -> set[str]:
    text = _CITE_CLUSTER_RE.sub(" ", text)  # citation markers are not claims
    return {m.group(1) for m in _NUM_RE.finditer(text)}


# Answers produced without a model. Nothing to validate: the text either
# reproduces the sources verbatim or reports that nothing was retrieved.
_NO_MODEL = {"extractive-fallback", "extractive", "none"}


def _numbered_sources(
    evidence: list | None,
    retrieved: list[Retrieved] | None,
    answer: Answer,
) -> list[tuple[int, str]]:
    """The numbered items the model actually saw, as (marker, text).

    Must mirror the assembled context exactly. Trial records are numbered ahead
    of literature, so validating against literature alone shifted every marker
    and flagged real citations as invalid while missing figures that came from
    the registry.
    """
    if evidence:
        return [(e.index, e.text) for e in evidence]
    source = retrieved if retrieved is not None else answer.sources
    return [(i, r.chunk.text) for i, r in enumerate(source or [], 1)]


def validate_answer(
    answer: Answer,
    retrieved: list[Retrieved] | None = None,
    evidence: list | None = None,
) -> ValidationReport:
    """Check an answer against the evidence it was given.

    Pass `evidence` (the assembled, provenance-labelled list) wherever both
    stores are in play. `retrieved` remains supported for the literature-only
    path.
    """
    items = _numbered_sources(evidence, retrieved, answer)
    report = ValidationReport()

    if not answer.text.strip():
        report.assessed = False
        report.reason = "the answer was empty"
        return report
    if not items:
        report.assessed = False
        report.reason = "no evidence was retrieved for this question"
        return report
    if answer.model in _NO_MODEL:
        report.assessed = False
        report.reason = "no model ran, so there is no generated claim to check"
        return report

    valid_range = {index for index, _ in items}
    report.invalid_citations = sorted(answer.cited_indices - valid_range)

    # Citation coverage over claim-bearing sentences only.
    for sent in _split_sentences(answer.text):
        if len(sent) < 25 or _HEDGE_RE.match(sent) or sent.startswith("*("):
            continue
        report.n_claims += 1
        if parse_citation_indices(sent):
            report.n_cited += 1
    report.citation_coverage = report.n_cited / report.n_claims if report.n_claims else 1.0

    # Every figure in the answer must appear somewhere in the cited evidence -
    # including trial records, where enrollment counts and dates live.
    corpus_numbers: set[str] = set()
    for _, text in items:
        corpus_numbers |= _numbers(text)
    for num in _numbers(answer.text):
        if num in corpus_numbers:
            continue
        # tolerate rounding: 12.3 grounded by 12.34, and small ordinals
        if float(num) <= 10 and "." not in num:
            continue
        if any(c.startswith(num) or num.startswith(c) for c in corpus_numbers):
            continue
        report.ungrounded_numbers.append(num)

    return report
