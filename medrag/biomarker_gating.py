"""Trial-side biomarker gating census over eligibility text.

This is the indication-first counterpart to `biomarker.py`. That module asks a
patient-side question — "does an MSS patient qualify for this trial?" and
answers ELIGIBLE / ELIGIBLE BY EXCLUSION / EXCLUDED / UNCLEAR / NOT MENTIONED.
This one asks a landscape question — "what does this trial gate on?" — and, for
each marker that gates colorectal trials, returns exactly one of:

    REQUIRED                the marker is named as an inclusion criterion (or
                             the paired opposite is excluded — see below)
    ELIGIBLE_BY_EXCLUSION    the trial excludes the paired opposite marker
                             without directly naming this one (only meaningful
                             for a marker with an `opposite`, like MSS/MSI-H)
    EXCLUDED                 the marker is named as an exclusion criterion, or
                             negated in an inclusion line ("non-MSI-H", "RAS
                             wild-type"), or the paired opposite is required
    NOT_MENTIONED             the eligibility text does not name the marker,
                             directly or via its opposite, at all

NOT_MENTIONED is the load-bearing state. It is NOT "eligible", it is "we could
not read a gate off the text" — the same distinction as ValidationReport.assessed
and NegativeEvidence.searched. Nothing downstream may fold a NOT_MENTIONED marker
into a REQUIRED (or EXCLUDED, or ELIGIBLE_BY_EXCLUSION) set; a landscape that
quietly counts unparsed trials as matching is worse than one that reports the
gap. There is a regression test.

The vocabulary, negation grammar, and signal collection are shared with
`biomarker.py` via `markers.py`. This module owns only the REDUCTION of that
shared signal set into the four states above, and its precedence on a genuine
conflict is REQUIRED-wins — see `markers.py`'s module docstring ("THE TWO
POLICIES") for why that is the correct, and deliberately different, choice from
`biomarker.py`'s UNCLEAR-on-conflict.

It is regex and keyword matching only, deliberately. The output is a filter that
narrows a count, and a hallucinated eligibility flag is unauditable — so the
matched span is stored beside every non-NOT_MENTIONED call and a human can check
it.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import markers as _m
from .markers import (  # noqa: F401  (re-exported for existing importers)
    ELIGIBLE_BY_EXCLUSION,
    EXCLUDED,
    MARKER_KEYS,
    MARKER_LABELS,
    MARKERS,
    NOT_MENTIONED,
    REQUIRED,
    MarkerDef,
)

EXPLICIT = "EXPLICIT"
SYNONYM = "SYNONYM"
NO_BASIS = "NONE"   # basis does not apply — status is not REQUIRED


@dataclass
class MarkerFlag:
    marker: str          # key, e.g. "MSI_H"
    status: str          # REQUIRED | ELIGIBLE_BY_EXCLUSION | EXCLUDED | NOT_MENTIONED
    span: str = ""       # the criterion sentence that decided it, for auditing
    source: str = ""     # which text field the span came from
    # Only meaningful when status == REQUIRED: did the winning sentence name
    # the marker by its own literal name (EXPLICIT) or only a synonym
    # (SYNONYM)? Read by the coverage statement (coverage.py), never by any
    # status decision. NO_BASIS otherwise, including for ELIGIBLE_BY_EXCLUSION
    # — "by exclusion" is already its own reported category, distinct from
    # this explicit/synonym split of REQUIRED.
    basis: str = NO_BASIS

    @property
    def label(self) -> str:
        return MARKER_LABELS.get(self.marker, self.marker)


def _reduce(mdef: MarkerDef, signals: list) -> MarkerFlag:
    """The trial-side precedence: REQUIRED wins on any conflict. A census feeds
    a count a human will narrow by reading the sample, so a false negative
    here hides a possibly relevant trial from that review entirely, while a
    false positive costs one extra glance — undercounting is the worse
    failure. `own_required` beats everything; `opp_excluded` (the paired
    opposite explicitly excluded — MSS's read of "excludes MSI-H") beats
    `own_excluded`/`opp_required` even on genuine conflict. See markers.py's
    module docstring for the full reasoning and how this differs from
    biomarker.py's UNCLEAR-on-conflict policy."""
    own_req, own_exc, opp_req, opp_exc = _m.split_signals(signals)
    if own_req:
        s = own_req[0]
        basis = EXPLICIT if _m.is_explicit_match(mdef, s.span) else SYNONYM
        return MarkerFlag(mdef.key, REQUIRED, s.span, s.source, basis)
    if opp_exc:
        s = opp_exc[0]
        return MarkerFlag(mdef.key, ELIGIBLE_BY_EXCLUSION, s.span, s.source)
    if own_exc or opp_req:
        s = (own_exc or opp_req)[0]
        return MarkerFlag(mdef.key, EXCLUDED, s.span, s.source)
    return MarkerFlag(mdef.key, NOT_MENTIONED)


def gate_markers(
    eligibility_text: str,
    *,
    detailed_description: str = "",
    brief_summary: str = "",
    keywords=(),
    markers: dict[str, MarkerDef] | None = None,
) -> dict[str, MarkerFlag]:
    """Classify every registered marker for one trial's eligibility text.

    Always returns all markers — a marker the text never names comes back
    NOT_MENTIONED, never omitted, so a caller cannot mistake absence for a miss.

    The supplementary fields are consulted only when the formal eligibility
    text (and each field before them) carries no signal for a given marker (see
    `markers.collect_signals`) — a trial's prose or keyword tags can fill a real
    gap (ADG126-P001 states MSS only in its detailed description) but never
    override a clear eligibility-criteria statement.
    """
    registry = MARKERS if markers is None else markers
    texts = _m.record_texts(eligibility_text, detailed_description, brief_summary, keywords)
    out: dict[str, MarkerFlag] = {}
    for key, mdef in registry.items():
        signals = _m.collect_signals(mdef, texts, registry)
        out[key] = _reduce(mdef, signals)
    return out


# Delimiters chosen so a LIKE '% KEY:STATUS %' filter cannot collide across
# markers (e.g. RAS vs KRAS_G12C). The string is space-padded on both ends.
def gating_tokens(flags: dict[str, MarkerFlag]) -> str:
    """A space-delimited, space-padded token string for SQL LIKE filtering:
    ' MSS:ELIGIBLE_BY_EXCLUSION MSI_H:EXCLUDED RAS:NOT_MENTIONED ... '."""
    return " " + " ".join(f"{k}:{flags[k].status}" for k in MARKER_KEYS) + " "


def gating_token(marker: str, status: str) -> str:
    """The exact substring a landscape filter matches against `gating_tokens`."""
    return f" {marker}:{status} "


def gating_basis_tokens(flags: dict[str, MarkerFlag]) -> str:
    """Same LIKE-token scheme as `gating_tokens`, one level down: only
    meaningful for a REQUIRED marker, whether the winning sentence used the
    marker's own name (EXPLICIT) or a synonym (SYNONYM). Stored so the
    coverage statement's 'N explicit, M by synonym' split is a SQL COUNT over
    a stored column, never a live re-scan of eligibility text."""
    return " " + " ".join(f"{k}:{flags[k].basis}" for k in MARKER_KEYS) + " "


def gating_basis_token(marker: str, basis: str) -> str:
    return f" {marker}:{basis} "
