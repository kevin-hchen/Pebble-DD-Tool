"""Funding, affiliation and conflict-of-interest signals, captured at ingest.

The independence axis asks a question the cited chunk usually cannot answer on
its own: is this source tied to the manufacturer? A paper's disclosure ("Funded
by Exact Sciences") sits in its Conclusions or a dedicated COI statement, while
the sentence an analyst cites ("sensitivity was 92.3%") sits in Results — a
different chunk. Judging linkage from the cited chunk alone therefore reads an
industry-funded pivotal study as INDEPENDENT, which is exactly backwards.

So the signal is computed once per document at ingest, from every disclosure
PubMed carries, and propagated onto every chunk — the same move evidence grading
makes. Whichever chunk is later cited, the funder travels with it.

Two things are derived and never conflated:

  * a searchable disclosure `blob` — affiliations, grants, the COI statement and
    any funding sentence scanned from the abstract — matched at verify time
    against the manufacturer's name to decide COMPANY-LINKED.
  * an `independent` flag — set ONLY on positive evidence of independence: a
    named non-industry funder, or an explicit no-conflict statement. Absence of a
    disclosure is not independence, so the flag stays False by default and the
    source reads NO DISCLOSURE rather than a flattering INDEPENDENT.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Phrases that introduce a funder in free text. Scanned across the whole abstract
# so a disclosure in any section is captured, not only the cited one.
FUNDING_PHRASES = (
    "funded by", "funding from", "funding was provided", "funding provided by",
    "supported by", "support from", "sponsored by", "sponsor was", "financial support",
    "employee of", "employees of", "employed by", "grant from", "grants from",
    "in collaboration with", "provided funding", "provided the funding",
)

# Explicit statements of independence. Their PRESENCE is positive evidence; their
# absence says nothing, which is the whole point of the correction.
NO_CONFLICT_PHRASES = (
    "no conflict of interest", "no conflicts of interest", "no competing interest",
    "no competing interests", "no competing financial interest",
    "no potential conflict", "no relevant financial", "no financial conflict",
    "declare no", "declares no", "declared no", "report no conflict",
    "reported no conflict", "have no conflict", "has no conflict",
    "without conflict of interest", "nothing to disclose", "none to disclose",
    "no relevant conflicts",
)

# Named non-industry funders — a positive independence signal. Deliberately public
# agencies and non-profits only; a company or a bare university affiliation is not
# here, because "who employs the authors" is not "who paid for the study".
PUBLIC_FUNDER_CUES = (
    "national institutes of health", "national institute of", "nih hhs", " nih ",
    "national cancer institute", " nci ", "nci nih", "medical research council",
    "wellcome", "cancer research uk", "european research council", "horizon 2020",
    "european commission", "european union", "howard hughes medical institute",
    "american cancer society", "department of veterans affairs", "veterans affairs",
    "centers for disease control", "world health organization", "gates foundation",
    "bill and melinda gates", "bill & melinda gates", "department of defense",
    "national science foundation", "public health service", "national health service",
    "u.s. department of", "us department of", "charitable", "nonprofit",
)

_MAX_BLOB = 2000  # bound per-chunk storage; matching only needs the tokens present


@dataclass(frozen=True)
class Disclosure:
    blob: str            # searchable disclosure text, for company matching
    independent: bool    # positive evidence of independence was found


def _sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def scan_funding_sentences(text: str) -> list[str]:
    """Sentences from free text (an abstract) that name or introduce a funder."""
    low_phrases = FUNDING_PHRASES
    out = []
    for sent in _sentences(text):
        low = sent.lower()
        if any(p in low for p in low_phrases):
            out.append(sent)
    return out


def _has_no_conflict(text: str) -> bool:
    low = (text or "").lower()
    return any(p in low for p in NO_CONFLICT_PHRASES)


def names_public_funder(text: str) -> bool:
    low = f" {(text or '').lower()} "
    return any(cue in low for cue in PUBLIC_FUNDER_CUES)


def disclosure_from_document(doc) -> Disclosure:
    """Derive the disclosure signal for a document from its stored metadata plus
    a scan of its abstract. Meta fields come from the efetch parser; the abstract
    scan also lets an index rebuilt over an older corpus still catch a funding
    line in the text."""
    meta = getattr(doc, "meta", {}) or {}
    affiliations = [a for a in (meta.get("affiliations") or []) if a]
    grants = [g for g in (meta.get("grants") or []) if g]
    coi = (meta.get("coi_statement") or "").strip()

    scanned = list(meta.get("funding_scan") or [])
    scanned += scan_funding_sentences(getattr(doc, "text", "") or "")
    scanned = list(dict.fromkeys(s for s in scanned if s))  # dedupe, keep order

    blob = " ".join([*affiliations, *grants, coi, *scanned]).strip()

    # Positive independence only. Author affiliations are excluded from the funder
    # test: they say where authors work, not who funded the work.
    funder_text = " ".join([*grants, coi, *scanned])
    independent = _has_no_conflict(coi) or _has_no_conflict(" ".join(scanned)) \
        or names_public_funder(funder_text)

    return Disclosure(blob=blob[:_MAX_BLOB], independent=independent)
