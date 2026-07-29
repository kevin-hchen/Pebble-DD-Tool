"""Claim verification: the inverse of the memo flow.

The memo flow asks a fixed question set about an asset. This flow starts from
the founder's own assertions and checks each one against independent evidence.
It exists because the analyst's stated bottleneck is proving a technology
WITHOUT leaning on the company's own materials, so the whole point is to reach
past the deck to PubMed and the registry and report where the deck's claims
actually land.

Every claim is scored on TWO orthogonal axes, because they answer different
questions and one must never be allowed to hide the other:

  support        does the evidence back the claim?
                 SUPPORTED | PARTIALLY SUPPORTED | CONTRADICTED | NOT FOUND |
                 NOT VERIFIABLE | UNVERIFIED
  independence   whose evidence is it?
                 COMPANY-LINKED | NO DISCLOSURE | INDEPENDENT | MIXED (n of m) | N/A

A claim can be PARTIALLY SUPPORTED and COMPANY-LINKED, or SUPPORTED and NO
DISCLOSURE — very different findings, and both have to be visible at a glance.
Folding "the only support is the manufacturer's own study" into the support value
(the old SUPPORTED - COMPANY SOURCE) hid a real partial behind a scary label and a
company-linked support behind a reassuring one. Independence is its own column.

Crucially, the independence axis obeys the same rule as the support axis: absence
of a disclosure is not proof of independence, just as absence of evidence is not
contradiction. So NO DISCLOSURE is the honest default, INDEPENDENT is emitted only
on positive evidence (a named non-industry funder or an explicit no-conflict
statement) and is rare, and a mix reports its counts — MIXED (1 company-linked,
1 no disclosure). The linkage is read from a document-level disclosure signal
captured at ingest (see disclosures.py), so a "Funded by X" line in a paper's
Conclusions is seen even when its Results chunk is the one cited.

NOT FOUND and CONTRADICTED are never merged. Absence of evidence is not evidence
against, so NOT FOUND is decided deterministically when nothing was retrieved and
can never be turned into CONTRADICTED by the model.

NOT VERIFIABLE is decided at extraction, not verification: "best-in-class
accuracy" and "clinically proven" have no checkable assertion, and left alone
they would all come back NOT FOUND and drown the claims that matter. They are
flagged for the analyst to rewrite or drop during the edit step, and recorded
rather than discarded — "the deck makes four claims that cannot be checked" is
itself a finding.

The deterministic overlays sit on top of the model's judgement, because none of
them should depend on a model getting them right:

  * an empty retrieval is NOT FOUND, full stop — the model is not even consulted
  * a numeric claim whose figure is not grounded in its own cited support is
    downgraded to PARTIALLY SUPPORTED, with both figures shown
  * independence is computed from the structured sponsor fields, never asked of
    the model that is also judging support

Confidentiality: deck-derived claims are more sensitive than an asset name, so
nothing leaves the machine until the caller has seen exactly what would be sent
and to which provider, and confirmed it for this run. A setting chosen once weeks
ago is not consent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .config import Config, load_config
from .context import LIT_LABEL, TRIAL_LABEL, Evidence, build_evidence, render_context
from .providers import effective_provider, make_client
from .validation import extract_figures, figure_grounded

# ---------------------------------------------------------------- support axis

SUPPORTED = "SUPPORTED"
PARTIAL = "PARTIALLY SUPPORTED"
CONTRADICTED = "CONTRADICTED"
NOT_FOUND = "NOT FOUND"
# No specific, checkable assertion — decided at extraction, not verification.
NOT_VERIFIABLE = "NOT VERIFIABLE"
# Evidence was retrieved but no model was available to judge it. Distinct from
# NOT FOUND on purpose, for the same reason ValidationReport.assessed exists.
UNVERIFIED = "UNVERIFIED"

# Display order: strongest-signal-for-diligence first, then the two "could not
# judge" states last.
SUPPORT_VALUES = (SUPPORTED, PARTIAL, CONTRADICTED, NOT_FOUND, NOT_VERIFIABLE, UNVERIFIED)

# What the model is allowed to return on the support axis. Independence and the
# numeric downgrade are decided in code, so the model never sees them.
_MODEL_VERDICTS = {
    "supported": SUPPORTED,
    "partially_supported": PARTIAL,
    "partial": PARTIAL,
    "contradicted": CONTRADICTED,
    "not_found": NOT_FOUND,
}

# ---------------------------------------------------------------- independence axis
#
# The axis answers "whose evidence is it?" and it obeys the same rule as the
# support axis: absence of a disclosure is not proof of independence, exactly as
# absence of evidence is not contradiction. So the honest default is NO DISCLOSURE,
# and INDEPENDENT is emitted ONLY on positive evidence — a named non-industry
# funder or an explicit no-conflict statement — which in practice is rare.

COMPANY_LINKED = "COMPANY-LINKED"   # a disclosure ties this source to the manufacturer
NO_DISCLOSURE = "NO DISCLOSURE"     # nothing found either way — the honest default
INDEPENDENT = "INDEPENDENT"         # positive evidence of independence (rare)
MIXED = "MIXED"                     # a mix across the cited sources, with counts
INDEP_NA = "N/A"                    # nothing cited to assess

INDEPENDENCE_VALUES = (COMPANY_LINKED, MIXED, NO_DISCLOSURE, INDEPENDENT, INDEP_NA)
# Per-source linkage outcomes (the three a single source can land on).
_SOURCE_LINKAGES = (COMPANY_LINKED, INDEPENDENT, NO_DISCLOSURE)


# ---------------------------------------------------------------- company source

# Corporate-form and generic sector words carry no identity. Stripping them lets
# "Example Therapeutics" match a sponsor field of "Example Therapeutics, Inc."
# while still requiring the distinctive token ("example") to line up.
_ORG_NOISE = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "companies",
    "ltd", "limited", "llc", "lp", "llp", "plc", "gmbh", "ag", "sa", "sas",
    "nv", "bv", "as", "oy", "ab", "spa", "srl", "kk",
    "pharma", "pharmaceutical", "pharmaceuticals", "therapeutics", "therapeutic",
    "biosciences", "bioscience", "sciences", "science", "biotech", "biopharma",
    "biopharmaceuticals", "bio", "biologics", "medical", "medicines", "medicine",
    "health", "healthcare", "laboratories", "laboratory", "labs", "lab",
    "holdings", "holding", "group", "international", "global", "technologies",
    "technology", "diagnostics", "genomics", "oncology", "the", "and", "of",
}


def _org_tokens(text: str) -> set[str]:
    """Significant, lower-cased word tokens, corporate noise removed."""
    raw = [t for t in "".join(c if c.isalnum() else " " for c in text.lower()).split() if t]
    return {t for t in raw if t not in _ORG_NOISE and len(t) > 1}


def _org_matches(company_tokens: set[str], text: str) -> bool:
    """True when every distinctive token of the company appears in `text`.

    Requiring ALL tokens (not any) keeps a two-word firm like "Boehringer
    Ingelheim" from matching on a stray "boehringer" alone, while a single
    distinctive token still matches its own sponsor field.
    """
    if not company_tokens:
        return False
    return company_tokens <= _org_tokens(text)


def is_company_source(ev: Evidence, company: str) -> bool:
    """Is this evidence item authored or funded by the manufacturer?

    Trials carry the answer in structured fields — lead sponsor and
    collaborators — which is authoritative. For literature the answer comes from
    the document-level disclosure signal captured at ingest (affiliations, grants,
    the COI statement, funding sentences from anywhere in the abstract), so a
    "Funded by X" line in the Conclusions is seen even when the Results chunk is
    the one cited. The cited chunk's own text is still checked too.
    """
    company_tokens = _org_tokens(company)
    if not company_tokens:
        return False

    if ev.kind == TRIAL_LABEL:
        candidates = [ev.meta.get("lead_sponsor", ""), ev.citation]
        candidates += ev.meta.get("collaborators", []) or []
        return any(_org_matches(company_tokens, c) for c in candidates if c)

    disclosure = ev.meta.get("disclosure", "")
    return _org_matches(company_tokens, " ".join([ev.title, ev.citation, ev.text, disclosure]))


def source_linkage(ev: Evidence, company: str) -> str:
    """Classify one source's relationship to the manufacturer, honestly.

    COMPANY-LINKED when a disclosure ties it to the manufacturer. INDEPENDENT
    only on POSITIVE evidence of independence — a trial names a sponsor that is
    not the manufacturer, or a paper carries a named non-industry funder or an
    explicit no-conflict statement. Otherwise NO DISCLOSURE: nothing was found
    either way, which is not the same as independence and must not be reported as
    it."""
    if is_company_source(ev, company):
        return COMPANY_LINKED
    if _has_independence_evidence(ev):
        return INDEPENDENT
    return NO_DISCLOSURE


def _has_independence_evidence(ev: Evidence) -> bool:
    if ev.kind == TRIAL_LABEL:
        # A registry record always names its sponsor; a sponsor that is not the
        # manufacturer (is_company_source already returned False) is positive
        # evidence the trial is independent of it.
        return bool(ev.meta.get("lead_sponsor") or ev.citation)
    return bool(ev.meta.get("disclosure_independent"))


# ---------------------------------------------------------------- confidentiality


@dataclass
class TransmissionNotice:
    """Exactly what a verification run would send off the machine, and where."""

    local: bool               # True => nothing leaves this computer
    provider_key: str
    provider_label: str
    endpoint: str
    items: list[str]          # the exact texts that would be transmitted
    kind: str = "claims"      # "claims" or "deck text"

    offline: bool = False     # local because outbound network is hard-blocked

    def render(self) -> str:
        if self.local:
            why = (
                "offline mode is on"
                if self.offline
                else f"the provider is '{self.provider_key}'"
            )
            return (
                f"Nothing will leave this machine: {why}, so the {self.kind} stay "
                "local. No confirmation needed."
            )
        lines = [
            f"About to transmit {len(self.items)} {self.kind} item(s) to an "
            "external service before any verdict can be produced.",
            "",
            f"  Provider: {self.provider_label}",
            f"  Endpoint: {self.endpoint}",
            "",
            f"Exactly this text will be sent to {self.provider_key}:",
        ]
        for i, item in enumerate(self.items, 1):
            snippet = item.strip().replace("\n", " ")
            lines.append(f"  {i}. {snippet}")
        lines += [
            "",
            "Deck content is confidential. Confirm you want to send it for this "
            "run before proceeding.",
        ]
        return "\n".join(lines)


def transmission_notice(cfg: Config, items: list[str], kind: str = "claims") -> TransmissionNotice:
    provider = effective_provider(cfg)
    # A local provider or offline mode means the model runs here (Ollama) or not
    # at all (none); either way the text does not leave the machine.
    offline = bool(getattr(cfg, "offline", False))
    local = offline or provider.key in ("none", "ollama")
    endpoint = "this machine" if local else (provider.base_url or "https://api.openai.com/v1")
    return TransmissionNotice(
        local=local,
        provider_key=provider.key,
        provider_label=provider.label,
        endpoint=endpoint,
        items=list(items),
        kind=kind,
        offline=offline,
    )


def requires_confirmation(cfg: Config) -> bool:
    """True when running the verifier would transmit text to an external service."""
    return not transmission_notice(cfg, []).local


class ConfirmationRequired(Exception):
    """Raised when a run would transmit off-machine but was not confirmed.

    Carries the notice so a caller can show the user precisely what would be
    sent rather than a bare 'permission denied'.
    """

    def __init__(self, notice: TransmissionNotice):
        self.notice = notice
        super().__init__("transmission not confirmed for this run")


# ---------------------------------------------------------------- extraction & triage


@dataclass
class ExtractedClaim:
    """One candidate claim, with the extraction-time verifiability decision.

    `verifiable` is False for statements with no checkable content ("best-in-class
    accuracy"); `reason` says why, so the analyst can decide to rewrite or drop it
    during the edit step. Carried as data through the edit boundary so the
    decision the analyst saw is the decision that is recorded."""

    text: str
    verifiable: bool = True
    reason: str = ""


EXTRACTION_PROMPT = """You are preparing to fact-check a company's pitch deck for an \
investment diligence review. Extract the deck's CLAIMS — the assertions a skeptical \
analyst would want verified against independent evidence — and, for each, decide \
whether it is actually checkable.

Include claims about efficacy, safety, mechanism, performance figures, comparisons \
to standard of care or competitors, regulatory status, and trial results. Each \
claim must stand alone and be checkable on its own, with any number preserved \
exactly as written.

For each claim set "verifiable" to false when it makes NO specific, checkable \
assertion — marketing language with no measurable content such as "best-in-class \
accuracy", "clinically proven", "revolutionary", "trusted by leading clinicians", \
"gold-standard". When verifiable is false, give a one-line reason. A claim is \
verifiable when it names a number, a measurable outcome, a specific comparison, or \
a concrete regulatory fact that could be checked against independent evidence.

Exclude vision statements, market-size projections, team biographies, and \
financial asks entirely — do not return them at all.

Deck text:
{deck}

Return JSON only:
{{"claims": [{{"text": "<one self-contained claim>", "verifiable": true|false, \
"reason": "<why it cannot be checked, if verifiable is false>"}}]}}"""


TRIAGE_PROMPT = """You are screening claims before fact-checking them for an \
investment diligence review. For each claim, decide whether it makes a specific, \
checkable assertion that could be verified against independent evidence.

Mark "verifiable": false for marketing language with no measurable content — \
"best-in-class accuracy", "clinically proven", "revolutionary", "trusted by \
clinicians", "gold-standard" — and give a one-line reason. Mark it true when the \
claim names a number, a measurable outcome, a specific comparison, or a concrete \
regulatory fact.

Claims, in order:
{claims}

Return JSON only, one assessment per claim IN THE SAME ORDER:
{{"assessments": [{{"verifiable": true|false, "reason": "<why not, if false>"}}]}}"""


def _load_json(client, cfg: Config, prompt: str) -> dict:
    resp = client.chat.completions.create(
        model=cfg.chat_model,
        temperature=0.0,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(resp.choices[0].message.content)


def extract_claims(deck_text: str, cfg: Config | None = None,
                   confirmed: bool = False) -> list[ExtractedClaim]:
    """Pull candidate claims out of raw deck text with one model call.

    Refuses to transmit the deck until the run is confirmed. Each claim carries
    its verifiability decision so the caller can show it during the edit step;
    the caller MUST show the list to the analyst before any verification runs —
    the analyst owns what gets checked.
    """
    cfg = cfg or load_config()
    notice = transmission_notice(cfg, [deck_text], kind="deck text")
    if not notice.local and not confirmed:
        raise ConfirmationRequired(notice)

    client = make_client(cfg)
    if client is None:
        raise RuntimeError(
            "extracting claims from a deck needs an AI provider. Configure one, or "
            "supply the claims directly, one per line."
        )

    payload = _load_json(client, cfg, EXTRACTION_PROMPT.format(deck=deck_text))
    claims: list[ExtractedClaim] = []
    for c in payload.get("claims") or []:
        if isinstance(c, str):
            text, verifiable, reason = c.strip(), True, ""
        else:
            text = str(c.get("text", "")).strip()
            verifiable = bool(c.get("verifiable", True))
            reason = str(c.get("reason", "")).strip()
        if text:
            claims.append(ExtractedClaim(text=text, verifiable=verifiable, reason=reason))
    return claims


def triage_claims(claim_texts: list[str], cfg: Config, client) -> list[ExtractedClaim]:
    """Assess verifiability for already-extracted plain claims (the file / paste
    path, where there was no extraction step to decide it). One model call for
    the whole batch. On any error, fail open: treat every claim as verifiable
    rather than silently marking a real claim NOT VERIFIABLE."""
    items = [ExtractedClaim(text=t) for t in claim_texts]
    if not items or client is None:
        return items

    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(claim_texts, 1))
    try:
        payload = _load_json(client, cfg, TRIAGE_PROMPT.format(claims=numbered))
        assessments = payload.get("assessments") or []
    except Exception:
        return items

    for item, a in zip(items, assessments):
        if isinstance(a, dict):
            item.verifiable = bool(a.get("verifiable", True))
            item.reason = str(a.get("reason", "")).strip()
    return items


def parse_claims_text(text: str) -> list[str]:
    """One claim per non-blank line; '#' lines are comments. Used by the file
    input path so an analyst can hand-curate a claims list in any editor."""
    claims = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        claims.append(line)
    return claims


# ---------------------------------------------------------------- classification

CLASSIFY_PROMPT = """You are verifying one claim from a company's pitch deck against \
independent evidence for an investment diligence review. Judge ONLY whether the \
excerpts below support the claim on the science — ignore who funded or authored \
them, which is assessed separately.

Claim under verification: {claim}

Choose exactly one verdict:
- "supported": the excerpts independently state what the claim asserts.
- "partially_supported": the excerpts point the same direction but the \
magnitude, population, endpoint, or conditions differ from the claim.
- "contradicted": the excerpts state the opposite, fail to replicate it, or \
report a null or negative result on the same question.
- "not_found": the excerpts do not address this claim either way.

Critical distinction: "not_found" and "contradicted" are different findings and \
must never be swapped. Absence of relevant evidence is NOT evidence against the \
claim. If the excerpts simply do not speak to the claim, that is "not_found".

Rules:
1. Use ONLY the excerpts. Cite every excerpt you rely on by its [n] marker.
2. Preserve every number exactly as written.
3. Be concise: one or two sentences of rationale.

Excerpts:
{context}

Return JSON only:
{{"verdict": "supported|partially_supported|contradicted|not_found", \
"citations": [<n>, ...], "rationale": "<one or two sentences>"}}"""


@dataclass
class ClaimVerdict:
    claim: str
    support: str
    independence: str = INDEP_NA
    evidence: list[Evidence] = field(default_factory=list)
    citations: list[int] = field(default_factory=list)
    rationale: str = ""
    company_sources: list[int] = field(default_factory=list)  # cited, company-linked
    # Counts across the cited SUPPORTING sources, summing to source_count.
    n_company: int = 0
    n_independent: int = 0
    n_no_disclosure: int = 0
    source_count: int = 0
    claim_figures: list[str] = field(default_factory=list)
    source_figures: list[str] = field(default_factory=list)
    assessed: bool = True
    model: str = "none"
    note: str = ""

    @property
    def cited_evidence(self) -> list[Evidence]:
        by_index = {e.index: e for e in self.evidence}
        return [by_index[i] for i in self.citations if i in by_index]

    def independence_display(self) -> str:
        """The independence label with its breakdown. For a single verdict it
        reads 'COMPANY-LINKED (1 of 1)' or 'NO DISCLOSURE (2 of 2)'; for a mix it
        spells out the split so a reader is never left to assume independence."""
        if self.independence == INDEP_NA or self.source_count == 0:
            return INDEP_NA
        if self.independence == MIXED:
            parts = []
            if self.n_company:
                parts.append(f"{self.n_company} company-linked")
            if self.n_independent:
                parts.append(f"{self.n_independent} independent")
            if self.n_no_disclosure:
                parts.append(f"{self.n_no_disclosure} no disclosure")
            return f"MIXED ({', '.join(parts)})"
        k = {COMPANY_LINKED: self.n_company, INDEPENDENT: self.n_independent,
             NO_DISCLOSURE: self.n_no_disclosure}.get(self.independence, 0)
        return f"{self.independence} ({k} of {self.source_count})"


_UNSET = object()


def classify_claim(
    claim: str,
    evidence: list[Evidence],
    cfg: Config,
    company: str = "",
    client: object = _UNSET,
) -> ClaimVerdict:
    """Classify one claim against its already-assembled evidence.

    Retrieval, verifiability triage, and the confirmation gate live in
    ClaimVerifier; this function only judges support and independence. It assumes
    the caller has already cleared transmission and confirmed the claim is
    checkable, so it is also the seam the tests drive directly with a mocked
    client.
    """
    # Deterministic and model-free: nothing retrieved is NOT FOUND, and the model
    # is never given the chance to turn that into CONTRADICTED.
    if not evidence:
        return ClaimVerdict(claim=claim, support=NOT_FOUND, evidence=[], model="none")

    if client is _UNSET:
        client = make_client(cfg)
    if client is None:
        # Evidence exists but nothing can judge it. Not a verdict — an honest
        # "unassessed", kept distinct from NOT FOUND on purpose.
        return ClaimVerdict(
            claim=claim,
            support=UNVERIFIED,
            evidence=evidence,
            assessed=False,
            model="none",
            note="No model available to classify this claim; evidence is listed but unjudged.",
        )

    by_index = {e.index: e for e in evidence}
    try:
        payload = _load_json(
            client, cfg, CLASSIFY_PROMPT.format(claim=claim, context=render_context(evidence))
        )
    except Exception:
        return ClaimVerdict(
            claim=claim,
            support=UNVERIFIED,
            evidence=evidence,
            assessed=False,
            model="none",
            note="The model response could not be parsed; this claim was not classified.",
        )

    token = str(payload.get("verdict", "")).strip().lower()
    support = _MODEL_VERDICTS.get(token)
    if support is None:
        return ClaimVerdict(
            claim=claim,
            support=UNVERIFIED,
            evidence=evidence,
            assessed=False,
            model="none",
            note=f"The model returned an unrecognised verdict ({token!r}); not classified.",
        )

    # Keep only citations that point at evidence actually retrieved, mirroring
    # the memo numbering exactly — same assembled list, no parallel scheme.
    raw_citations = payload.get("citations") or []
    citations: list[int] = []
    for c in raw_citations:
        try:
            n = int(c)
        except (TypeError, ValueError):
            continue
        if n in by_index and n not in citations:
            citations.append(n)

    result = ClaimVerdict(
        claim=claim,
        support=support,
        evidence=evidence,
        citations=citations,
        rationale=str(payload.get("rationale", "")).strip(),
        model=cfg.chat_model,
    )

    _apply_numeric_downgrade(result, by_index)
    _apply_independence(result, by_index, company)
    return result


def _apply_numeric_downgrade(result: ClaimVerdict, by_index: dict[int, Evidence]) -> None:
    """A SUPPORTED numeric claim whose figure is not grounded in its own cited
    support is really PARTIALLY SUPPORTED — the direction is right but the number
    is not. 95% claimed against an 89% source is the canonical case."""
    if result.support != SUPPORTED or not result.citations:
        return

    claim_figs = extract_figures(result.claim)
    if not claim_figs:
        return

    source_text = " ".join(by_index[i].text for i in result.citations)
    source_figs = extract_figures(source_text)
    ungrounded = sorted(f for f in claim_figs if not figure_grounded(f, source_figs))
    if not ungrounded:
        return

    result.support = PARTIAL
    result.claim_figures = ungrounded
    result.source_figures = sorted(source_figs)
    result.note = (
        "Direction supported, but the claimed figure is not grounded in the cited "
        "evidence."
    )


def _apply_independence(result: ClaimVerdict, by_index: dict[int, Evidence],
                        company: str) -> None:
    """Independence is a proportion over the SUPPORTING citations, classified from
    the disclosure signals — never asked of the model that is also judging
    support. Only supported/partial verdicts have supporting evidence to assess;
    for everything else independence is N/A.

    The honest default is NO DISCLOSURE: a source with no funding or conflict
    disclosure is not evidence of independence. INDEPENDENT is emitted only on
    positive evidence. One manufacturer-funded study alone is COMPANY-LINKED
    (1 of 1); reporting it as a clean pass is the exact failure this axis exists
    to prevent."""
    if result.support not in (SUPPORTED, PARTIAL):
        result.independence = INDEP_NA
        return

    m = len(result.citations)
    if m == 0:
        # Supported but nothing cited: cannot assess whose evidence it is.
        result.independence = INDEP_NA
        return

    linkages = [source_linkage(by_index[i], company) for i in result.citations]
    result.company_sources = [i for i, link in zip(result.citations, linkages)
                              if link == COMPANY_LINKED]
    result.n_company = sum(1 for link in linkages if link == COMPANY_LINKED)
    result.n_independent = sum(1 for link in linkages if link == INDEPENDENT)
    result.n_no_disclosure = sum(1 for link in linkages if link == NO_DISCLOSURE)
    result.source_count = m

    if result.n_company == m:
        result.independence = COMPANY_LINKED
        result.note = (result.note + " " if result.note else "") + (
            "Every supporting source is tied to the manufacturer by a disclosed "
            "funding or affiliation link."
        )
    elif result.n_independent == m:
        result.independence = INDEPENDENT
    elif result.n_no_disclosure == m:
        result.independence = NO_DISCLOSURE
        result.note = (result.note + " " if result.note else "") + (
            "No funding or conflict disclosure was found for the supporting "
            "source(s); independence is unverified, not established."
        )
    else:
        result.independence = MIXED
        result.note = (result.note + " " if result.note else "") + (
            f"Of {m} supporting sources, {result.n_company} are company-linked and "
            f"{result.n_no_disclosure} carry no disclosure."
        )


def not_verifiable_verdict(claim: str, reason: str = "") -> ClaimVerdict:
    """A recorded verdict for a claim with no checkable assertion. Assessed (we
    reached a determination), never retrieved or sent to the model."""
    return ClaimVerdict(
        claim=claim,
        support=NOT_VERIFIABLE,
        independence=INDEP_NA,
        assessed=True,
        model="none",
        note=reason or "No specific, checkable assertion to verify.",
    )


# ---------------------------------------------------------------- report


@dataclass
class ClaimReport:
    asset: str
    indication: str
    company: str
    verdicts: list[ClaimVerdict] = field(default_factory=list)
    model: str = ""
    embedder: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def all_evidence(self) -> list[Evidence]:
        return [e for v in self.verdicts for e in v.evidence]

    def support_counts(self) -> dict:
        out = {v: 0 for v in SUPPORT_VALUES}
        for v in self.verdicts:
            out[v.support] = out.get(v.support, 0) + 1
        return out

    def independence_counts(self) -> dict:
        out = {v: 0 for v in INDEPENDENCE_VALUES}
        for v in self.verdicts:
            out[v.independence] = out.get(v.independence, 0) + 1
        return out


# ---------------------------------------------------------------- verifier


class ClaimVerifier:
    """Retrieve evidence for each claim and classify it, behind the confirmation
    gate. Structurally a sibling of DiligenceRunner: same optional dual stores,
    same warnings surfaced rather than swallowed."""

    def __init__(self, cfg: Config | None = None, rag=None, trial_store=None):
        self.cfg = cfg or load_config()
        self.rag = rag
        self.warnings: list[str] = []

        if self.rag is None:
            try:
                from .pipeline import MedRAG

                self.rag = MedRAG(self.cfg)
            except (FileNotFoundError, RuntimeError) as exc:
                self.warnings.append(f"literature index unavailable: {exc}")

        self.trial_store = trial_store
        if self.trial_store is None:
            from .pipeline import TRIALS_DB
            from .trials.store import TrialStore, TrialStoreSchemaError

            db = self.cfg.raw_dir / TRIALS_DB
            if db.exists():
                # A stale trials.db degrades to literature-only rather than
                # crashing the verification run.
                try:
                    self.trial_store = TrialStore(db)
                except TrialStoreSchemaError as exc:
                    self.warnings.append(str(exc).splitlines()[0])
            else:
                self.warnings.append("trial store not found — run `medrag trials` first")

    def notice(self, claims: list) -> TransmissionNotice:
        return transmission_notice(self.cfg, [_claim_text(c) for c in claims], kind="claims")

    def _retrieve(self, claim: str, asset: str, indication: str, k: int) -> list[Evidence]:
        trials = []
        if self.trial_store is not None:
            trials = self.trial_store.query(
                intervention=asset or None, condition=indication or None, limit=k
            )
            # Structured filters can legitimately return nothing; fall back to
            # free text so a checkable claim is not silently starved of registry
            # context.
            if not trials:
                trials = self.trial_store.search(f"{asset} {indication} {claim}".strip(), limit=k)

        passages = self.rag.retriever.retrieve(claim, k=k) if self.rag else []
        return build_evidence(
            trials=trials, passages=passages, max_chars=self.cfg.max_context_chars
        )

    def verify(
        self,
        claims: list,
        asset: str = "",
        indication: str = "",
        company: str = "",
        confirmed: bool = False,
        progress: bool = False,
    ) -> ClaimReport:
        notice = self.notice(claims)
        # The gate: for a remote provider, nothing is retrieved or sent until the
        # run is confirmed. Raising here — before any store or model is touched —
        # is what makes "no transmission before confirmation" true rather than
        # merely intended.
        if not notice.local and not confirmed:
            raise ConfirmationRequired(notice)

        client = make_client(self.cfg)

        # Normalise input. Already-tagged ExtractedClaims (the deck-extraction
        # path, decided during the edit step) are honoured as-is; bare strings
        # (a claims file or a direct paste) are triaged now so a vague claim is
        # caught wherever it enters rather than only via extraction.
        items, to_triage = [], []
        for c in claims:
            if isinstance(c, ExtractedClaim):
                items.append(c)
            else:
                ec = ExtractedClaim(text=str(c))
                items.append(ec)
                to_triage.append(ec)
        if to_triage and client is not None:
            tagged = triage_claims([e.text for e in to_triage], self.cfg, client)
            for ec, t in zip(to_triage, tagged):
                ec.verifiable, ec.reason = t.verifiable, t.reason

        verdicts: list[ClaimVerdict] = []
        for i, item in enumerate(items, 1):
            if progress:
                print(f"[medrag] verifying {i}/{len(items)}")
            if not item.verifiable:
                verdicts.append(not_verifiable_verdict(item.text, item.reason))
                continue
            evidence = self._retrieve(item.text, asset, indication, self.cfg.top_k)
            verdicts.append(
                classify_claim(item.text, evidence, self.cfg, company=company, client=client)
            )

        return ClaimReport(
            asset=asset,
            indication=indication,
            company=company,
            verdicts=verdicts,
            model=self.cfg.chat_model,
            embedder=self.rag.embedder.name if self.rag else "none",
            warnings=list(self.warnings),
        )

    def close(self) -> None:
        if self.trial_store is not None:
            self.trial_store.close()


def _claim_text(c) -> str:
    return c.text if isinstance(c, ExtractedClaim) else str(c)
