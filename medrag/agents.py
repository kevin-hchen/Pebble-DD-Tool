"""Shared drug/agent name matching — the one implementation, for every store.

WHAT THIS REPLACES

`store.query(intervention=asset)` matched with `LOWER(interventions) LIKE
'%<asset>%'` over a JSON array rendered as one string. The asset field takes a
human-typed phrase and most oncology assets are combinations, so the common
case was:

    asset  = "botensilimab and balstilimab"
    column = '["Botensilimab", "Balstilimab", "Oxaliplatin", ...]'

The literal phrase "botensilimab and balstilimab" appears nowhere in that
string — the array separator sits between the two agents — so the match
returned nothing, the structured query returned empty, and every caller fell
through to free-text search without saying so. This is the same defect as the
three condition-matching bugs CLAUDE.md records, in a fourth place: a substring
test over free text, standing in for a match against a structured fact.

THE RULE, STATED ONCE

A drug name is a structured fact, so it is matched as one: the array is parsed,
each element is split into agent phrases, each phrase is normalised to a token,
and matching is set membership over those tokens. Never a substring over the
joined string. `token_blob()` writes those tokens into a space-padded column at
ingest — the same scheme `query_sets` and `biomarker_gating` already use, for
the same reason: SQL should filter on a controlled vocabulary, not on prose.

CONJUNCTION IS AND, NOT A PHRASE

"botensilimab and balstilimab" is a request for trials carrying BOTH agents, so
`parse_asset` splits it into terms and `AssetQuery.matches` requires every one.
This is stricter than the old behaviour and that is the point — but it is also
the way this can newly return zero (see MissedCases below), so
`AssetQuery.term_reports` carries per-term counts and a caller can say WHICH
term collapsed the AND rather than presenting an empty result as a finding.

ALIASES ARE EXPANDED AT QUERY TIME, NOT BAKED IN AT INGEST

The stored tokens are the registry's own surface forms, normalised but not
canonicalised. Alias expansion (generic <-> brand <-> development code) happens
when a query is built, by ORing an agent's known forms together. This is a
deliberate difference from `biomarker_gating`, whose verdicts ARE baked in at
ingest and need a re-ingest when `config/markers.yaml` changes: adding
"AGEN1181" to `config/agents.yaml` takes effect on the next query, against the
database already on disk. A vocabulary edit that requires a 12,000-record
re-fetch is a vocabulary edit nobody makes.

DESIGNED FOR THE SECOND CALLER

`fda/` needs ingredient matching for drugsFDA, which is why nothing in this
module knows what a trial is. The record side takes an iterable of free-text
names; the query side takes a typed phrase. Two independently-maintained
matchers is exactly the drift that produced the biomarker bug — see
`markers.py`'s docstring for that story.

SALTS AND ESTERS, AND WHY NOTHING IS STRIPPED

drugsFDA writes the salt into the ingredient name: "IRINOTECAN HYDROCHLORIDE",
"LEUCOVORIN CALCIUM", "HYDROCODONE BITARTRATE". Measured on the live
`products.active_ingredients.name` vocabulary, 568 of the 1,000 most-used
ingredient names are multi-word and the trailing word is overwhelmingly a
counterion — HYDROCHLORIDE alone accounts for 181 of them.

One direction already worked: a record's word-level tokens mean a query for
"irinotecan" matches "IRINOTECAN HYDROCHLORIDE". The reverse did not — a deck
writing "leucovorin calcium" produced the single token `leucovorincalcium`,
which a record listing bare "LEUCOVORIN" does not carry. `_salt_base` closes
that by ALSO emitting the base name, on both sides.

It ADDS a reading rather than replacing one, the same decision the hyphen rule
already makes, because stripping is unsafe here: the trailing word is not always
a counterion. "POTASSIUM CHLORIDE" and "SODIUM CHLORIDE" are the drug, not a
salt of one, and "ETHINYL ESTRADIOL" carries its modifier in FRONT. Stripping
would turn potassium chloride into potassium — a different substance. Emitting
both leaves the full name matchable and adds the base as an alternative, so no
query that worked before stops working. The salt vocabulary is curated in
`config/agents.yaml`, derived from that measurement rather than from memory.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "agents.yaml"

# Separators that mean "another agent follows" on BOTH sides — inside a registry
# intervention string and inside a typed asset phrase. Deliberately the same
# list for both, because "Botensilimab + Balstilimab" is written by sponsors and
# analysts alike.
_SPLIT = re.compile(
    # Every character in this class is attested in the live colorectal store,
    # not anticipated. The CJK comma is NCT06115733's "Fruquintinib、
    # Capecitabine Tablets"; ± is NCT02948985's "treated with FOLFIRI±cetuximab";
    # the colon is NCT02271464's "Maintenance:BEVACIZUMAB"; the equals sign is
    # NCT00500292's "FOLFOX regimen=oxaliplatin, fluorouracil". A splitter that
    # only knows ASCII commas drops the second agent of each of them.
    r"\s*(?:[,;+&/|:=±、，；]|\band\b|\bplus\b|\bwith\b|\bversus\b|\bvs\.?\b)\s*",
    re.IGNORECASE,
)

# Hyphens join an agent to something else at least as often as they belong to
# its name: "Aflibercept-FOLFIRI", "FOLFIRI-cetuximab", "Bevacizumab-IRDye800CW",
# "l-Leucovorin". Splitting on them alone would break the development codes
# where the hyphen IS part of the name ("BAY 73-4506", "MK-3475"), so a
# hyphenated word contributes BOTH readings — the joined form and each part —
# and whichever one a query names is the one that matches.
_HYPHEN = re.compile(r"[-–—‐·]")

# Words that are never an agent name. Kept SHORT and obviously-noise on purpose:
# an over-eager stopword list silently drops real drugs, which is the failure
# this module exists to stop. Dose units and routes only.
_STOPWORDS = frozenset("""
a an the of in for to on at by or as is are be
mg mcg ug kg ml mm cm iu unit units dose doses dosing
iv po sc im oral orally intravenous intravenously infusion injection
daily weekly monthly cycle cycles day days week weeks month months year years
""".split())

# Registry names carry trademark marks and bracketed asides; neither is part of
# the name. Parentheticals are NOT discarded — "Botensilimab (AGEN1181)" states
# the development code, which is exactly the alias a deck is likely to use — so
# they are pulled out as additional candidate names instead.
_PARENTHETICAL = re.compile(r"[（(\[]([^）)\]]{2,60})[）)\]]")
_TRADEMARK = re.compile(r"[®™©]")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _load_salts(path: str | Path | None = None) -> frozenset[str]:
    """The curated counterion/ester vocabulary from config/agents.yaml."""
    p = Path(path or DEFAULT_CONFIG)
    if not p.exists():
        return frozenset()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return frozenset(
        n for n in (normalize_name(str(s)) for s in (data.get("salt_forms") or [])) if n
    )


def normalize_name(text: str) -> str:
    """One agent name to its comparison key: lowercase, alphanumerics only.

    Punctuation is removed rather than mapped to a space, so the many ways a
    registry writes the same development code collapse together — "AGEN-1181",
    "AGEN 1181" and "AGEN1181" are one token, and so are "PD-1" and "PD1".
    """
    return _NON_ALNUM.sub("", _TRADEMARK.sub("", text or "").lower())


SALTS = _load_salts()


def _salt_base(words: list[str], salts: frozenset[str] | None = None) -> str:
    """The name with a trailing counterion removed, or "" when there is none.

    Only the LAST word is considered, and only when at least one word would
    remain — "SODIUM CHLORIDE" keeps both readings via the normal word tokens,
    but this never reduces a name to nothing. Returns "" rather than the input
    when no salt is present, so callers can tell "no salt form" from "same
    name".
    """
    salts = SALTS if salts is None else salts
    if len(words) < 2:
        return ""
    if normalize_name(words[-1]) not in salts:
        return ""
    return normalize_name(" ".join(words[:-1]))


def _phrases(text: str) -> list[str]:
    """Split one free-text intervention string into candidate agent phrases,
    keeping any parenthetical aside as a candidate in its own right."""
    text = _TRADEMARK.sub("", text or "")
    # A parenthetical is split on the same separators as the rest, because it
    # routinely holds more than one alias: "Regorafenib (Stivarga, BAY73-4506)"
    # names the brand and the development code inside one aside.
    extra = [m.group(1) for m in _PARENTHETICAL.finditer(text)]
    stripped = _PARENTHETICAL.sub(" ", text)
    out = []
    for source in [stripped, *extra]:
        for chunk in _SPLIT.split(source):
            chunk = chunk.strip()
            if chunk:
                out.append(chunk)
    return out


def name_tokens(text: str) -> set[str]:
    """Every token one free-text name contributes.

    Both the whole normalised phrase AND its individual words, because registry
    intervention strings are not always just a drug name: "Preoperative hepatic
    and regional arterial chemotherapy using oxaliplatin, MMC and FUDR" only
    yields `oxaliplatin` at the word level, and dropping word tokens would lose
    it. Words shorter than three characters are skipped — "5" and "LV" carry no
    signal on their own and match everything.
    """
    tokens: set[str] = set()
    for phrase in _phrases(text):
        words = [w for w in re.split(r"\s+", phrase) if w]
        # The whole-phrase token exists for multi-word names — "trastuzumab
        # deruxtecan", "trifluridine tipiracil". Past four words a phrase is
        # prose, not a name ("Preoperative hepatic and regional arterial
        # chemotherapy using oxaliplatin"), and joining it produces a token
        # nothing will ever match, so only its words are kept.
        whole = normalize_name(phrase)
        if whole and len(whole) >= 3 and len(words) <= 4:
            tokens.add(whole)
        # "IRINOTECAN HYDROCHLORIDE" also answers to "irinotecan". Added, never
        # substituted — see the module docstring on why stripping is unsafe.
        base = _salt_base(words)
        if len(base) >= 3:
            tokens.add(base)
        for word in words:
            for part in [word, *_HYPHEN.split(word)]:
                w = normalize_name(part)
                if len(w) >= 3 and w not in _STOPWORDS:
                    tokens.add(w)
    return tokens


def record_tokens(names) -> set[str]:
    """Every token a record's PARSED name array contributes. This is the record
    side of the contract, and it takes the array — never a joined string."""
    tokens: set[str] = set()
    for n in names or []:
        tokens |= name_tokens(n)
    return tokens


def token_blob(names) -> str:
    """The space-padded token string stored in a column, for `% token %` LIKE
    filtering — the same scheme as `query_sets` and `biomarker_gating`. Sorted
    so the stored value is stable across re-ingests of the same record and a
    diff of the database shows real changes only."""
    tokens = sorted(record_tokens(names))
    return (" " + " ".join(tokens) + " ") if tokens else ""


# ------------------------------------------------------------------ the alias table


@dataclass(frozen=True)
class AgentDef:
    """One agent and every name it goes by. `key` is the canonical generic name
    in normalised form; `forms` is every normalised surface form including it."""
    key: str
    label: str
    forms: frozenset[str]
    kind_of_form: dict = field(default_factory=dict)   # form -> generic|brand|code


def load_agents(path: str | Path | None = None) -> dict[str, AgentDef]:
    """Read the curated alias table. A missing or malformed file yields an empty
    registry: every agent then matches on its typed name alone, which is
    degraded (no brand/code cross-reference) but not broken — the same failure
    posture `load_markers` takes."""
    p = Path(path or DEFAULT_CONFIG)
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    out: dict[str, AgentDef] = {}
    for name, spec in (data.get("agents") or {}).items():
        spec = spec or {}
        kinds: dict[str, str] = {}
        forms = set()
        for form, kind in (
            [(name, "generic")]
            + [(b, "brand") for b in (spec.get("brands") or [])]
            + [(c, "code") for c in (spec.get("codes") or [])]
            + [(s, "generic") for s in (spec.get("synonyms") or [])]
        ):
            n = normalize_name(str(form))
            if n:
                forms.add(n)
                kinds.setdefault(n, kind)
        if not forms:
            continue
        key = normalize_name(name)
        out[key] = AgentDef(key=key, label=str(name), forms=frozenset(forms),
                            kind_of_form=kinds)
    return out


# Loaded once. Callers wanting a different table pass it explicitly, exactly as
# markers.py does.
AGENTS = load_agents()


def _index(agents: dict[str, AgentDef]) -> dict[str, AgentDef]:
    """Every known surface form -> its agent, so a typed brand name resolves."""
    idx: dict[str, AgentDef] = {}
    for a in agents.values():
        for f in a.forms:
            idx.setdefault(f, a)
    return idx


def expand_aliases(term: str, agents: dict[str, AgentDef] | None = None) -> tuple[set[str], bool]:
    """Every normalised form a typed term should also match, and whether the
    term resolved to a curated agent at all.

    The second half of that return is the point: a term with no entry in
    `config/agents.yaml` matches only the string as typed, and a caller must be
    able to tell that apart from a curated agent that genuinely has one name.
    Same rule as `BiomarkerMatch.curated` — an uncurated guess and a reviewed
    verdict must never look equally confident.
    """
    agents = AGENTS if agents is None else agents
    key = normalize_name(term)
    if not key:
        return set(), False
    idx = _index(agents)
    found = idx.get(key)
    if found:
        return set(found.forms), True

    # A typed salt form resolves to the base drug's entry when it has one:
    # "leucovorin calcium" is the curated agent "leucovorin". Checked only after
    # the literal name misses, so a curated multi-word name whose last word
    # happens to be a counterion still wins on its own terms.
    base = _salt_base([w for w in re.split(r"\s+", term or "") if w])
    if base:
        found = idx.get(base)
        if found:
            return set(found.forms) | {key}, True
        return {key, base}, False
    return {key}, False


# ------------------------------------------------------------------ the query side


@dataclass
class AgentTerm:
    """One agent the query requires, with every form it may appear under."""
    typed: str                  # exactly what the user wrote, for the message
    forms: tuple[str, ...]      # normalised alternatives, ORed together
    curated: bool               # False => no config/agents.yaml entry

    def matches(self, tokens: set[str]) -> bool:
        return any(f in tokens for f in self.forms)


@dataclass
class AssetQuery:
    """A typed asset phrase, parsed into the agents it requires.

    `terms` are ANDed: "botensilimab and balstilimab" wants a trial carrying
    both. An empty `terms` (the user typed nothing, or only noise) means "do not
    filter on intervention at all" — never "match nothing", because a query that
    silently matches nothing is the failure this module was written to fix.
    """
    raw: str
    terms: list[AgentTerm] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.terms)

    @property
    def is_combination(self) -> bool:
        return len(self.terms) > 1

    @property
    def uncurated_terms(self) -> list[str]:
        return [t.typed for t in self.terms if not t.curated]

    def matches(self, tokens: set[str]) -> bool:
        return bool(self.terms) and all(t.matches(tokens) for t in self.terms)


def parse_descriptive_name(text: str, agents: dict[str, AgentDef] | None = None) -> AssetQuery:
    """Parse a DESCRIPTIVE name — every significant word its own ANDed term.

    `parse_asset` joins a multi-word phrase into a single token, which is right
    for a drug: "trastuzumab deruxtecan" is one molecule and matching it word by
    word would return every trastuzumab record. Device names are not molecule
    names. They are descriptions, and the registry writes the same description
    in a different word order:

        query:  "implantable cardioverter defibrillator"
        record: "Defibrillator, automatic implantable cardioverter, with
                 cardiac resynchronization"

    Joined, those share no token and the match silently returns nothing — which
    is exactly what happened, and it produced a measurement of zero PMA records
    for a device with 2,895 of them. Word-wise and ANDed, order stops mattering
    and every query word still has to be present, so "cardioverter defibrillator"
    does not match a plain defibrillator.

    This is an EXTENSION of the one matcher, not a second one: it reuses the same
    splitter, normaliser, stopword list and alias table, and differs only in
    whether a phrase is one term or several. Which reading a caller wants is a
    property of the SOURCE — molecule names versus descriptions — so the caller
    chooses, and both readings live here.
    """
    seen, terms = set(), []
    for phrase in _phrases(text or ""):
        for word in re.split(r"\s+", phrase):
            for part in [word, *_HYPHEN.split(word)]:
                key = normalize_name(part)
                if len(key) < 3 or key in _STOPWORDS or key in seen:
                    continue
                seen.add(key)
                forms, curated = expand_aliases(part, agents)
                terms.append(AgentTerm(typed=part.strip(), forms=tuple(sorted(forms)),
                                       curated=curated))
    return AssetQuery(raw=text or "", terms=terms)


def collapsed_combination_notes(term_reports: list[dict], asset: str) -> list[str]:
    """Plain-language notes naming WHICH agent of a combination matched nothing.

    Lives here, not in the two callers, because `diligence._trials_for` and
    `claims._retrieve` have the identical fallback and would otherwise carry
    identical messages that drift apart — the failure `markers.py` was created
    to end. `term_reports` is `TrialStore.intervention_terms()`'s shape, so
    nothing in this function knows what a trial is and the drugsFDA caller can
    reuse it unchanged.

    Returns an empty list when the combination is a single agent, or when every
    agent exists — in the latter case the empty result is a real finding ("no
    trial runs these together"), not a matching failure, and saying otherwise
    would be the same not-found-vs-not-searched confusion this codebase already
    guards everywhere else.
    """
    if len(term_reports) < 2:
        return []
    notes = []
    for t in (r for r in term_reports if not r.get("n_trials")):
        note = (f"no trial in this population lists “{t['typed']}” under any name known to "
                f"config/agents.yaml, so the combination “{asset}” matched nothing and this "
                "fell back to free-text search.")
        if not t.get("curated"):
            note += (f" “{t['typed']}” has no entry in config/agents.yaml, so only that exact "
                     "spelling was tried — a brand name or development code for the same "
                     "molecule would not be recognised.")
        notes.append(note)
    return notes


def parse_asset(text: str, agents: dict[str, AgentDef] | None = None) -> AssetQuery:
    """Parse a human-typed asset phrase into the agents it names.

    Deliberately the same splitter the record side uses: an analyst writes
    "botensilimab + balstilimab" and a sponsor writes "Botensilimab, Balstilimab",
    and there is no reason for the two sides to disagree about what a separator
    is. Duplicate terms collapse, so "FOLFOX and FOLFOX" does not AND twice.
    """
    seen, terms = set(), []
    for phrase in _phrases(text or ""):
        key = normalize_name(phrase)
        # Stopwords are checked per WORD, not against the joined form: "of the"
        # normalises to "ofthe", which is in no stopword list and would sail
        # through as an agent name that then matches nothing.
        words = [normalize_name(w) for w in re.split(r"\s+", phrase) if w.strip()]
        if len(key) < 3 or key in seen or all(w in _STOPWORDS or not w for w in words):
            continue
        seen.add(key)
        forms, curated = expand_aliases(phrase, agents)
        terms.append(AgentTerm(typed=phrase.strip(), forms=tuple(sorted(forms)),
                               curated=curated))
    return AssetQuery(raw=text or "", terms=terms)
