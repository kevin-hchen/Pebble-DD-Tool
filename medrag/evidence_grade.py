"""Evidence grading by study design, assigned at ingest.

If a case report retrieves at the same weight as a registered Phase 3, an
analyst cannot trust any of the output. PubMed already carries publication type
in its metadata, so the tier is free at ingest - no model call, no inference.

The tier is used twice, deliberately:
  1. as a ranking boost during retrieval, so stronger designs surface first
  2. as a visible tag in the output, so a reader can discount appropriately

Point 2 matters more than point 1. Reranking silently changes what an analyst
sees; a visible tag lets them disagree with the ranking.
"""

from __future__ import annotations

from dataclasses import dataclass

# Ordered strongest to weakest. Rank 1 is the strongest evidence.
TIERS = [
    ("meta-analysis", 1, "Meta-analysis"),
    ("systematic-review", 2, "Systematic review"),
    ("rct", 3, "Randomized controlled trial"),
    ("cohort", 4, "Cohort study"),
    ("case-control", 5, "Case-control study"),
    ("case-series", 6, "Case series or report"),
    ("narrative", 7, "Narrative review, editorial or commentary"),
    ("unclassified", 8, "Unclassified"),
]

_RANK = {key: rank for key, rank, _ in TIERS}
_LABEL = {key: label for key, _, label in TIERS}
UNCLASSIFIED = "unclassified"

# PubMed publication types, lowercased, mapped to a tier key. Checked in tier
# order so that a record typed both "Meta-Analysis" and "Review" grades as the
# meta-analysis - the stronger claim wins.
_PUBTYPE_MAP: dict[str, str] = {
    "meta-analysis": "meta-analysis",
    "systematic review": "systematic-review",
    "randomized controlled trial": "rct",
    "controlled clinical trial": "rct",
    "clinical trial, phase iii": "rct",
    "clinical trial, phase ii": "rct",
    "pragmatic clinical trial": "rct",
    "equivalence trial": "rct",
    "adaptive clinical trial": "rct",
    "clinical trial": "cohort",
    "observational study": "cohort",
    "comparative study": "cohort",
    "multicenter study": "cohort",
    "validation study": "cohort",
    "case-control studies": "case-control",
    "case reports": "case-series",
    "review": "narrative",
    "editorial": "narrative",
    "comment": "narrative",
    "letter": "narrative",
    "news": "narrative",
    "historical article": "narrative",
}

# Fallback cues from title text, used only when publication types are absent or
# unhelpful. Weaker signal than metadata, so it never overrides a real type.
_TITLE_CUES = [
    ("meta-analysis", "meta-analysis"),
    ("systematic review", "systematic-review"),
    ("randomised controlled", "rct"),
    ("randomized controlled", "rct"),
    ("double-blind", "rct"),
    ("placebo-controlled", "rct"),
    ("cohort study", "cohort"),
    ("prospective cohort", "cohort"),
    ("case-control", "case-control"),
    ("case report", "case-series"),
    ("case series", "case-series"),
]


@dataclass(frozen=True)
class Grade:
    key: str
    rank: int
    label: str
    source: str  # metadata | title | default

    @property
    def tag(self) -> str:
        """Short visible marker for memo output, e.g. [RCT]."""
        return {
            "meta-analysis": "META-ANALYSIS",
            "systematic-review": "SYS REVIEW",
            "rct": "RCT",
            "cohort": "COHORT",
            "case-control": "CASE-CONTROL",
            "case-series": "CASE REPORT",
            "narrative": "NARRATIVE",
            "unclassified": "UNGRADED",
        }[self.key]

    @property
    def is_weak(self) -> bool:
        """Case reports and opinion pieces. Flagged so a memo can say when its
        evidence base is thin rather than presenting everything as equivalent."""
        return self.rank >= 6


def _grade(key: str, source: str) -> Grade:
    return Grade(key=key, rank=_RANK[key], label=_LABEL[key], source=source)


def grade_publication(publication_types: list[str] | None, title: str = "") -> Grade:
    """Assign an evidence tier from PubMed publication types, falling back to
    title cues, then to unclassified."""
    types = {t.strip().lower() for t in (publication_types or []) if t and t.strip()}

    if types:
        best: str | None = None
        for pubtype in types:
            key = _PUBTYPE_MAP.get(pubtype)
            if key and (best is None or _RANK[key] < _RANK[best]):
                best = key
        if best:
            return _grade(best, "metadata")

    lowered = (title or "").lower()
    for cue, key in _TITLE_CUES:
        if cue in lowered:
            return _grade(key, "title")

    return _grade(UNCLASSIFIED, "default")


def grade_document(doc) -> Grade:
    """Grade a Document using its stored PubMed metadata.

    Trial registry records are not graded here - a registry entry is a fact
    about what was run, not a published finding, and forcing it onto the same
    scale would imply a comparison that does not hold.
    """
    meta = getattr(doc, "meta", {}) or {}
    return grade_publication(meta.get("publication_types"), getattr(doc, "title", ""))


# Multiplier applied to cosine similarity during reranking. Deliberately gentle:
# a meta-analysis that is off-topic should still lose to an on-topic case report,
# so grading reorders near-ties rather than overriding relevance.
_BOOST = {
    "meta-analysis": 1.20,
    "systematic-review": 1.16,
    "rct": 1.12,
    "cohort": 1.04,
    "case-control": 1.00,
    "case-series": 0.92,
    "narrative": 0.88,
    "unclassified": 1.00,
}


def boost_for(grade_key: str) -> float:
    return _BOOST.get(grade_key, 1.0)


def rerank_by_evidence(retrieved: list, weight: float = 1.0) -> list:
    """Reorder retrieved passages by score x evidence boost.

    weight=0 disables grading entirely, which is what the eval harness uses to
    measure recall before and after. Original scores are preserved on the object
    so the memo can still show true cosine similarity.
    """
    if weight <= 0:
        return list(retrieved)

    def key(item):
        grade_key = getattr(item.chunk, "evidence_key", UNCLASSIFIED)
        boost = boost_for(grade_key)
        adjusted = 1.0 + (boost - 1.0) * weight
        return item.score * adjusted

    return sorted(retrieved, key=key, reverse=True)


def distribution(grades: list[Grade]) -> dict[str, int]:
    """Counts by tier label, strongest first. Answers the diligence question
    'what is the evidence quality distribution here'."""
    counts: dict[str, int] = {}
    for g in sorted(grades, key=lambda g: g.rank):
        counts[g.label] = counts.get(g.label, 0) + 1
    return counts
