"""Query routing between the structured trial store and semantic literature search.

Two stores means something has to decide which one answers a question. A single
embedding space over both makes the system worse, not better: registry facts get
blurred into prose and the filters stop being filters.

One cheap classification call decides STRUCTURED, SEMANTIC, or BOTH. A rule-based
classifier backs it up and runs alone when no model is available, so routing
never becomes a hard dependency on the API. The rules are also what the model is
checked against in tests - a router that silently degrades to "always BOTH" would
look like it works while costing double and diluting every answer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum

from .config import Config
from .providers import make_client


class Route(str, Enum):
    STRUCTURED = "structured"   # registry facts: who ran it, what phase, did it stop
    SEMANTIC = "semantic"       # mechanism, efficacy, safety findings in the literature
    BOTH = "both"


# Vocabulary that names registry fields rather than biology.
_STRUCTURED_HINTS = re.compile(
    r"\b(trial|trials|study|studies|nct\d*|phase\s*(?:1|2|3|4|i{1,3}v?)|"
    r"terminated|withdrawn|suspended|discontinued|halted|stopped|failed|"
    r"recruiting|enrolling|enrollment|enrolment|sponsor|sponsored|"
    r"competitor|competitors|who else|anyone else|pipeline|registry|"
    r"completion|readout|timeline)\b",
    re.IGNORECASE,
)

# Vocabulary that asks what something means or does.
#
# Split from the topic nouns below on purpose. "Who else has run trials on this
# mechanism?" is a registry question that happens to contain the word mechanism;
# treating that noun as a semantic trigger routed it to BOTH, which doubles cost
# and dilutes the answer with literature nobody asked for. A semantic route needs
# the question to actually ASK about the science, not merely name it.
_SEMANTIC_HINTS = re.compile(
    r"\b(efficacy|effective|effectiveness|outcome|outcomes|evidence|"
    r"hazard ratio|odds ratio|relative risk|confidence interval|endpoint|biomarker|"
    r"safety|adverse|toxicity|tolerability|dose|dosing|"
    r"why does|why is|how does|how do|what is|what does|what are|"
    r"mechanism of action|meta-?analysis|systematic review|literature|"
    r"published|reported|rationale)\b",
    re.IGNORECASE,
)

# Topic nouns: enough to route a question with no other signal to literature,
# never enough on their own to pull a registry question into BOTH.
_TOPIC_NOUNS = re.compile(r"\b(mechanism|pathway|target|indication|biology)\b", re.IGNORECASE)

# Regulatory / device vocabulary that names the openFDA store's fields. This is
# an ORTHOGONAL signal, not a fourth Route value: a question can want clearances
# AND the literature. It only decides whether the FDA store is consulted.
_REGULATORY_HINTS = re.compile(
    r"\b(510\s*\(?k\)?|clearance|cleared|premarket|pma|de\s*novo|fda|"
    r"recall|recalled|adverse event|maude|product code|device class|"
    r"predicate|substantially equivalent|regulatory|approval|approved|"
    r"class\s*(?:i{1,3}|1|2|3)\s+device)\b",
    re.IGNORECASE,
)

ROUTER_PROMPT = """Classify this question about a biomedical asset.

STRUCTURED - answerable from a clinical trial registry: which trials exist, their \
phase, status, sponsor, enrollment, dates, or why a trial stopped.
SEMANTIC - answerable from published literature: mechanism, efficacy findings, \
safety signals, effect sizes, or scientific rationale.
BOTH - genuinely needs registry facts AND published findings.

Prefer STRUCTURED or SEMANTIC over BOTH. Choose BOTH only when the question \
cannot be answered without each kind of evidence.

Separately, set "regulatory" true if the question touches FDA clearance, device \
class, product code, recalls, or adverse events — an openFDA store answers those.

Question: {question}

Reply with JSON only: {{"route": "structured|semantic|both", "regulatory": \
true|false, "reason": "<8 words>"}}"""


@dataclass
class RoutingDecision:
    route: Route
    reason: str = ""
    method: str = "rules"          # rules | llm | llm-fallback
    filters: dict = field(default_factory=dict)
    needs_regulatory: bool = False  # consult the openFDA store as well

    @property
    def needs_trials(self) -> bool:
        return self.route in (Route.STRUCTURED, Route.BOTH)

    @property
    def needs_literature(self) -> bool:
        return self.route in (Route.SEMANTIC, Route.BOTH)


# Phrases like "phase 3" / "phase III" -> the store's "Phase 3" form.
_PHASE_RE = re.compile(r"\bphase\s*(1|2|3|4|i{1,3}v?)\b", re.IGNORECASE)
_ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4"}


def extract_filters(question: str) -> dict:
    """Pull structured filters the question states outright.

    Only unambiguous signals: a stated phase, an explicit stop-status word, or an
    NCT ID. Guessing the intervention from free text belongs to the store's
    text search, not here - a wrong filter silently returns nothing, which is
    worse than no filter at all.
    """
    filters: dict = {}

    m = _PHASE_RE.search(question)
    if m:
        token = m.group(1).lower()
        filters["phase"] = f"Phase {_ROMAN.get(token, token)}"

    if re.search(r"\b(terminated|withdrawn|suspended|discontinued|halted|stopped|failed)\b",
                 question, re.IGNORECASE):
        filters["stopped_only"] = True

    ncts = re.findall(r"\bNCT\d{8}\b", question, re.IGNORECASE)
    if ncts:
        filters["nct_ids"] = [n.upper() for n in ncts]

    # An FDA product code stated outright ("product code FRN"). Only when named
    # as such — a bare three-letter token is far too broad to guess from.
    pc = re.search(r"\bproduct code\s+([A-Za-z]{3})\b", question, re.IGNORECASE)
    if pc:
        filters["product_code"] = pc.group(1).upper()

    return filters


def classify_by_rules(question: str) -> RoutingDecision:
    structured = bool(_STRUCTURED_HINTS.search(question))
    semantic = bool(_SEMANTIC_HINTS.search(question))
    regulatory = bool(_REGULATORY_HINTS.search(question))

    if structured and semantic:
        route, reason = Route.BOTH, "registry and literature terms present"
    elif structured:
        route, reason = Route.STRUCTURED, "registry vocabulary"
    elif semantic or _TOPIC_NOUNS.search(question):
        route, reason = Route.SEMANTIC, "literature vocabulary"
    elif regulatory:
        # A purely regulatory question (clearances, recalls) has no trial or
        # literature signal; route it structured so the registry is also checked,
        # and let needs_regulatory pull in the FDA store.
        route, reason = Route.STRUCTURED, "regulatory vocabulary"
    else:
        # Unmatched questions go to literature: it is the larger, more forgiving
        # store, and a missed registry filter degrades worse than a broad search.
        route, reason = Route.SEMANTIC, "no registry signal; default to literature"

    return RoutingDecision(route=route, reason=reason, method="rules",
                           filters=extract_filters(question), needs_regulatory=regulatory)


class Router:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = make_client(cfg)

    def route(self, question: str) -> RoutingDecision:
        rules = classify_by_rules(question)
        if self.client is None:
            return rules

        try:
            resp = self.client.chat.completions.create(
                model=self.cfg.chat_model,
                temperature=0.0,
                max_tokens=60,
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": ROUTER_PROMPT.format(question=question)}],
            )
            payload = json.loads(resp.choices[0].message.content)
            route = Route(str(payload.get("route", "")).strip().lower())
            return RoutingDecision(
                route=route,
                reason=str(payload.get("reason", ""))[:80],
                method="llm",
                filters=rules.filters,  # regex filters are more reliable than the model's
                # OR with the regex: the model may miss a regulatory cue, but a
                # false positive only costs one extra structured lookup.
                needs_regulatory=bool(payload.get("regulatory")) or rules.needs_regulatory,
            )
        except Exception:
            # Bad JSON, unknown enum value, API error - fall back rather than fail.
            rules.method = "llm-fallback"
            return rules
