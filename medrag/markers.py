"""Shared marker vocabulary and negation grammar for `biomarker.py` (patient-side
eligibility: "does an MSS patient qualify for this trial?") and
`biomarker_gating.py` (trial-side census: "what does this trial gate on?").

Both modules used to carry their own copy of the marker regex table and their
own (different, and in one case entirely absent) negation handling. That let
them disagree on the same trial — a bug in its own right — and it meant a
negation fix made in one module never reached the other. This module is the
single place that owns:

  * the marker vocabulary (loaded from `config/markers.yaml`, a clinician-edited
    file — see that file's header for why it is not model-generated)
  * sentence/section splitting (`iter_criteria`, moved here unchanged from
    `biomarker.py`)
  * the negation grammar (`_negated`, used by both modules)
  * the "this sentence requires a test, not a result" filter (`_is_test_requirement`)
  * signal collection (`collect_signals`) — scanning eligibility text (and,
    only when eligibility is silent, supplementary text such as the trial's
    detailed description) for mentions of a marker or its paired opposite, and
    classifying each into one of four *signal categories*

Each module then REDUCES that shared signal set into its OWN final vocabulary
with its OWN, deliberately different, conflict-resolution policy — see
"THE TWO POLICIES" below. Unifying the vocabulary was the fix; keeping the
policies apart was a design decision, not an oversight, and CLAUDE.md records
the reasoning as well as this docstring does.

NEGATION, GENERALISED

The bug that motivated this module: `biomarker_gating.py`'s old negation check
was anchored immediately before the marker (matching only "not " directly
touching it), so "documented
NOT to have MSI-high" — where "to have" sits between "NOT" and the marker — was
never recognised as a negation at all, and `biomarker.py` had no negation
handling whatsoever. Registry eligibility text negates a marker two ways, and
both are ordinary, common grammar, not edge cases:

  * BEFORE the marker, often with a short gap: "documented NOT to have MSI-high",
    "without evidence of dMMR", "no history of BRAF mutation".
  * AFTER the marker, as a suffix qualifier: "HER2-negative", "RAS wild-type",
    "RAS WT" — this is the standard way oncology eligibility text states that a
    mutation must be ABSENT, and it is at least as common as prefix negation in
    real CRC trials (MOUNTAINEER-03's real eligibility text requires "RAS WT").

`_negated` checks both directions. Neither is unbounded: prefix negation
tolerates at most three intervening words (covers "NOT to have", not a negation
from an unrelated clause three sentences earlier), and suffix negation must
immediately follow the marker (at most a space or hyphen between them).

TESTING FOR A MARKER IS NOT STATING ITS RESULT

"The tumor must have been assessed for MSI-H or dMMR status" and "documented
RAS status (mutant or wild-type)" both name a marker without saying which way
it must resolve — they mandate that a test be run, not that a result be a
particular value. Reading these as REQUIRED (or EXCLUDED) mis-stated C-800-25 as
requiring MSI-H, when its own exclusion criterion says the opposite two lines
later. `_is_test_requirement` recognises a determination-verb ("documented",
"assessed", "tested", "evaluated", "determined") near the word "status" with no
direction-word between them, and such a sentence contributes NO signal for any
marker. This deliberately trades a handful of borderline sentences that DO carry
a direction after "status" (rare, and absent from every fixture measured against
real registry text) for eliminating a demonstrated, silent inversion — consistent
with this codebase's standing rule that a missed signal is safer than a wrong one.

SIGNAL CATEGORIES

For a marker `M` with an optional paired opposite `M'` (MSS/MSI-H is the only
established pair here — see markers.yaml for why the others do not need one),
scanning text yields signals in one of four categories:

  own_required    text names M and wants it present      ("MSS", "microsatellite stable")
  own_excluded    text names M and wants it absent        ("HER2-negative", "RAS wild-type")
  opp_required    text names M' and wants it present      ("Tumors must be MSI-H")
  opp_excluded    text names M' and wants it absent       ("Exclusion: MSI-H or dMMR",
                                                            "documented NOT to have MSI-high")

`opp_excluded` is the category that was completely invisible before this
module: an MSS patient's eligibility for a trial that only ever talks about
MSI-H, by excluding it, used to have no representation at all.

THE TWO POLICIES

Given the same signal set, `biomarker.py` and `biomarker_gating.py` reduce it
differently, and must:

  `biomarker_gating.py` (trial-side census) — REQUIRED wins on conflict. The
  census exists to answer "which trials might be relevant", feeding a COUNT a
  human will still narrow by reading the sample. Undercounting hides a possibly
  relevant trial from that review entirely; overcounting costs a human one extra
  glance. So `own_required` beats every other signal, and `opp_excluded` beats
  `own_excluded`/`opp_required`, even when the two conflict.

  `biomarker.py` (patient-side match) — a genuine conflict (both an
  eligible-leaning and an excluded-leaning signal present) resolves to UNCLEAR,
  never to a pick. This module is telling one person whether one trial might
  admit them; asserting ELIGIBLE against self-contradictory source text is a
  false reassurance a patient could act on, which is a worse failure than
  showing them the contradictory sentence and saying "unclear, judge for
  yourself" — the same reasoning `_aggregate`'s original ELIGIBLE-vs-EXCLUDED
  conflict handling already used, generalised correctly to the case that
  produced C-800-25's original bug: a fixed-priority scan over per-sentence
  outcomes let an EXCLUDED verdict from a MISPARSED sentence (the "must be
  assessed for" one) beat a correctly-derived UNCLEAR verdict, because the old
  code compared individual sentence outcomes in a fixed order instead of asking
  "do the signals, as a set, actually agree with each other".

Both policies are legitimate answers to different questions. Neither is
"more correct" than the other in the abstract; what would be a bug is the two
modules reaching opposite conclusions about the same trial's same marker on
ordinary (non-contradictory) text, which `tests/test_markers.py` checks for.

CURATION

A marker resolved from `config/markers.yaml` is `curated=True` and can reach any
of the confident states. A biomarker string a user typed that resolves to no
config entry falls back to `_literal_match` in `biomarker.py` — a plain
substring search with no negation handling and no reviewed synonyms — and is
`curated=False`. An uncurated match can only ever be UNCLEAR or NOT_MENTIONED:
it has no basis for a confident ELIGIBLE or EXCLUDED, and must not be rendered
with the same weight as a reviewed marker's verdict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "markers.yaml"

# The four states shared verbatim by biomarker_gating.py (trial-centric
# vocabulary — see module docstring). biomarker.py has its own, patient-centric
# vocabulary (ELIGIBLE / ELIGIBLE BY EXCLUSION / EXCLUDED / UNCLEAR / NOT
# MENTIONED) that intentionally uses different literal strings — the two
# modules address different readers, and the space-vs-underscore difference is
# a display convention, not a semantic one. See tests/test_markers.py for how
# the two are checked against each other despite that difference.
REQUIRED = "REQUIRED"
EXCLUDED = "EXCLUDED"
ELIGIBLE_BY_EXCLUSION = "ELIGIBLE_BY_EXCLUSION"
NOT_MENTIONED = "NOT_MENTIONED"

SOURCE_LABELS = {
    "eligibility_criteria": "eligibility criteria",
    "detailed_description": "detailed description",
    "brief_summary": "brief summary",
    "keywords": "registry keywords",
}


def record_texts(
    eligibility_criteria: str,
    detailed_description: str = "",
    brief_summary: str = "",
    keywords=(),
) -> dict[str, str]:
    """The one ordering both modules scan: formal eligibility criteria first,
    then prose that might state a marker informally, then registry-chosen
    keyword tags last — the least reliable of the four, since a keyword list
    carries no section heading and no sentence grammar for negation to work
    against. `collect_signals` only consults a later field when every field
    before it produced nothing at all."""
    return {
        "eligibility_criteria": eligibility_criteria or "",
        "detailed_description": detailed_description or "",
        "brief_summary": brief_summary or "",
        "keywords": " ".join(keywords) if keywords else "",
    }


@dataclass(frozen=True)
class MarkerDef:
    key: str
    label: str
    positive: tuple[str, ...]      # regexes naming the marker itself
    # The subset of `positive` that is the marker's own literal name/abbreviation
    # ("MSS", "MSI-H") rather than a synonym ("pMMR", "non-MSI-H"). Used only to
    # classify a REQUIRED match as EXPLICIT vs SYNONYM for the coverage
    # statement (coverage.py) — never affects matching or any status decision,
    # which still uses the full `positive` alternation. Defaults to `positive`
    # itself (every match reads as explicit) for a marker with no real synonym
    # variation, rather than requiring every config entry to repeat the list.
    canonical: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()  # patient-facing query synonyms
    opposite: str = ""             # key of the paired marker, or ""
    curated_for: tuple[str, ...] = ()
    curated: bool = True

    def __post_init__(self):
        if not self.canonical:
            object.__setattr__(self, "canonical", self.positive)


@dataclass
class MarkerSignal:
    category: str   # own_required | own_excluded | opp_required | opp_excluded
    span: str       # the criterion sentence that produced it
    source: str     # which text field it came from


def load_markers(path: str | Path | None = None) -> dict[str, MarkerDef]:
    """Read the clinician-editable marker table. A missing or malformed file
    yields an empty registry rather than raising — every marker then falls back
    to the uncurated literal-match path, which is degraded but not broken."""
    p = Path(path or DEFAULT_CONFIG)
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    out: dict[str, MarkerDef] = {}
    for key, spec in (data.get("markers") or {}).items():
        spec = spec or {}
        positive = tuple(str(p) for p in (spec.get("positive") or []) if str(p).strip())
        if not positive:
            continue
        out[key] = MarkerDef(
            key=key,
            label=spec.get("label") or key,
            positive=positive,
            canonical=tuple(str(c) for c in (spec.get("canonical") or []) if str(c).strip()),
            aliases=tuple(str(a).strip().lower() for a in (spec.get("aliases") or [])),
            opposite=(spec.get("opposite") or "").strip(),
            curated_for=tuple(spec.get("curated_for") or ()),
            curated=True,
        )
    return out


# Loaded once at import time, same pattern as the old MARKERS tuple. Callers
# that need a different config (tests) pass `markers=` explicitly rather than
# mutating this.
MARKERS: dict[str, MarkerDef] = load_markers()
MARKER_KEYS: tuple[str, ...] = tuple(MARKERS)
MARKER_LABELS: dict[str, str] = {k: m.label for k, m in MARKERS.items()}


def resolve_marker(query: str, markers: dict[str, MarkerDef] | None = None) -> MarkerDef | None:
    """Map a user's biomarker string to a curated definition, or None (the
    caller falls back to an uncurated match)."""
    registry = MARKERS if markers is None else markers
    norm = re.sub(r"\s+", " ", (query or "").strip().lower())
    if not norm:
        return None
    for mdef in registry.values():
        if norm == mdef.key.lower() or norm in mdef.aliases:
            return mdef
        if any(a in norm or norm in a for a in mdef.aliases):
            return mdef
    return None


# ------------------------------------------------------------- section splitting


def iter_criteria(text: str, default_section: str = "unknown"):
    """Yield (section, criterion_sentence) for each line of `text`.

    Tracks the Inclusion/Exclusion headings the registry almost always emits in
    `eligibility_criteria`. `default_section` lets a caller tag lines from a
    field that never carries such headings (a trial's free-text detailed
    description) as "description" rather than "unknown", so evidence shown to a
    reader can say where it came from — see `SOURCE_LABELS`.
    """
    section = default_section
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if "inclusion criteria" in low:
            section = "inclusion"
            rest = line.split(":", 1)[1].strip() if ":" in line else ""
            if rest:
                yield section, rest
            continue
        if "exclusion criteria" in low:
            section = "exclusion"
            rest = line.split(":", 1)[1].strip() if ":" in line else ""
            if rest:
                yield section, rest
            continue
        cleaned = re.sub(r"^[\-\*•–—\d\.\)\(\s]+", "", line)
        if cleaned:
            yield section, cleaned


# Cues that flip an unlabelled criterion into an inclusion or exclusion reading
# when there is no "Inclusion/Exclusion Criteria" heading to lean on.
_EXCL_CUES = re.compile(
    r"\b(exclu|must not|cannot|can not|ineligible|not eligible|without|"
    r"absence of|no evidence of|negative for|no known)\b",
    re.IGNORECASE,
)
_INCL_CUES = re.compile(
    r"\b(required|require|must have|must be|positive for|documented|confirmed|"
    r"eligible if|only if|known to be)\b",
    re.IGNORECASE,
)


def _context(section: str, sentence: str) -> str:
    if section in ("inclusion", "exclusion"):
        return section
    if _EXCL_CUES.search(sentence):
        return "exclusion"
    if _INCL_CUES.search(sentence):
        return "inclusion"
    return "unknown"


# ------------------------------------------------------------- negation grammar

# Negation immediately or near-immediately before the marker. Each branch
# tolerates at most three intervening words ("NOT to have", "not shown to
# express") so a negation from an unrelated clause is not misread as applying
# to this marker.
_NEGATION_BEFORE = re.compile(
    r"(?:\bnon[\s\-]?|\bnot\b(?:\s+\w+){0,3}\s+|\bno\b(?:\s+\w+){0,3}\s+|"
    r"\bwithout\b(?:\s+\w+){0,3}\s+|\babsence of\s+)$",
    re.IGNORECASE,
)

# Negation as a suffix immediately after the marker: "RAS wild-type", "RAS WT".
# Must be tight (a space or hyphen at most) — "RAS mutation, wild-type BRAF
# required separately" should not count.
_NEGATION_AFTER = re.compile(
    r"^[\s\-]?(?:negative\b|neg\b|wild[\s\-]?type|\bwt\b)",
    re.IGNORECASE,
)

# Negation baked INTO the matched span itself: HER2_AMP's own pattern requires
# a qualifier suffix (amplif/positive/pos/negative/neg/+) to avoid matching a
# bare "HER2" mention that states no status at all ("prior anti-HER2 therapy").
# That means "HER2-negative" is consumed as ONE match rather than "HER2"
# followed by a separate "-negative" suffix _NEGATION_AFTER could see — so this
# checks whether the match's OWN captured text ends in a negating qualifier.
_NEGATION_WITHIN = re.compile(r"(?:negative|\bneg)\s*$", re.IGNORECASE)

# A determination verb near "status" with no direction-word between them: the
# sentence mandates a TEST, not a RESULT, and contributes no signal. See the
# module docstring for the accepted false-neutral tradeoff.
_TEST_REQUIREMENT = re.compile(
    r"\b(?:documented|assessed|tested|evaluated|determined)\b"
    r"(?:(?!\b(?:positive|negative|wild[\s\-]?type|mutant|amplif\w*|\bwt\b|non[\s\-]?)\b).){0,150}"
    r"\bstatus\b",
    re.IGNORECASE,
)


def _is_test_requirement(sentence: str) -> bool:
    return bool(_TEST_REQUIREMENT.search(sentence))


def _negated(text: str, start: int, end: int) -> bool:
    return (bool(_NEGATION_BEFORE.search(text[:start]))
            or bool(_NEGATION_AFTER.match(text[end:]))
            or bool(_NEGATION_WITHIN.search(text[start:end])))


def _classify(text: str, section: str, matches: list[re.Match]) -> str:
    """REQUIRED or EXCLUDED for one marker within a sentence already known not
    to be a test-requirement sentence. `presence_wanted` is the single rule
    both prefix and suffix negation, and both inclusion and exclusion
    sections, reduce to: does this sentence want the marker PRESENT or ABSENT?

    A marker is often named more than once in the same sentence — spelled out
    and then abbreviated in parentheses ("microsatellite instability-high
    (MSI-high)"), or given as two synonyms ("MSI-H/dMMR"). These are one
    clinical statement, not several, so negation is decided ONCE for the
    sentence: negated if ANY occurrence carries a detected negation. A prefix
    negation like "documented NOT to have X (Y)" is grammatically attached to
    the first, spelled-out occurrence; the bounded negation-before window
    would miss the parenthetical restatement on its own; requiring EVERY
    occurrence to independently show the negation would then read a single
    negated statement as a REQUIRED/EXCLUDED contradiction with itself.
    """
    negated = any(_negated(text, m.start(), m.end()) for m in matches)
    presence_wanted = negated if section == "exclusion" else not negated
    return REQUIRED if presence_wanted else EXCLUDED


# ------------------------------------------------------------- signal collection

_COMPILED_CACHE: dict[tuple[str, ...], re.Pattern] = {}


def _compiled(mdef: MarkerDef) -> re.Pattern:
    key = mdef.positive
    rx = _COMPILED_CACHE.get(key)
    if rx is None:
        rx = re.compile("|".join(key), re.IGNORECASE)
        _COMPILED_CACHE[key] = rx
    return rx


def _compiled_canonical(mdef: MarkerDef) -> re.Pattern:
    key = mdef.canonical
    rx = _COMPILED_CACHE.get(key)
    if rx is None:
        rx = re.compile("|".join(key), re.IGNORECASE)
        _COMPILED_CACHE[key] = rx
    return rx


def is_explicit_match(mdef: MarkerDef, span: str) -> bool:
    """Did `span` — the sentence a REQUIRED verdict was decided from — name the
    marker by its own literal name/abbreviation, or only by a synonym? Used
    exclusively by the coverage statement (coverage.py) to report "N explicit,
    M by synonym" rather than one undifferentiated REQUIRED count; it plays no
    part in deciding REQUIRED vs EXCLUDED vs NOT_MENTIONED itself."""
    return bool(_compiled_canonical(mdef).search(span or ""))


def split_signals(signals: list[MarkerSignal]):
    """Bucket a signal list into the four categories. Each module applies its
    own precedence over these buckets — see the module docstring."""
    return (
        [s for s in signals if s.category == "own_required"],
        [s for s in signals if s.category == "own_excluded"],
        [s for s in signals if s.category == "opp_required"],
        [s for s in signals if s.category == "opp_excluded"],
    )


def collect_signals(
    mdef: MarkerDef,
    texts: dict[str, str],
    markers: dict[str, MarkerDef] | None = None,
) -> list[MarkerSignal]:
    """Scan `texts` (ordered field_name -> text, e.g. eligibility_criteria then
    detailed_description then brief_summary) for mentions of `mdef` and, if it
    has a paired opposite, of that opposite marker too.

    A later field is consulted ONLY if every field before it produced no signal
    at all — supplementary text fills a genuine gap, it never overrides a clear
    eligibility-criteria statement. This is how ADG126-P001's MSS mention
    (present only in its detailed description, absent from formal eligibility
    text) reaches the reader without letting prose ever outrank a real
    eligibility criterion for a trial that has one.
    """
    registry = MARKERS if markers is None else markers
    opp = registry.get(mdef.opposite) if mdef.opposite else None
    own_re = _compiled(mdef)
    opp_re = _compiled(opp) if opp else None

    for source, text in texts.items():
        if not text:
            continue
        default_section = "unknown" if source == "eligibility_criteria" else "description"
        found: list[MarkerSignal] = []

        for section, sentence in iter_criteria(text, default_section=default_section):
            if _is_test_requirement(sentence):
                continue
            ctx = _context(section, sentence)

            own_matches = list(own_re.finditer(sentence))
            if own_matches:
                status = _classify(sentence, ctx, own_matches)
                found.append(MarkerSignal(
                    "own_required" if status == REQUIRED else "own_excluded",
                    sentence.strip(), source,
                ))

            if opp_re is not None:
                # Mask own-marker spans first, length-preserving so match
                # positions stay valid: "non-MSI-H" is MSS's own established
                # synonym and must not also be read as a bare mention of MSI-H.
                stripped = own_re.sub(lambda mo: " " * len(mo.group(0)), sentence)
                opp_matches = list(opp_re.finditer(stripped))
                if opp_matches:
                    status = _classify(stripped, ctx, opp_matches)
                    found.append(MarkerSignal(
                        "opp_required" if status == REQUIRED else "opp_excluded",
                        sentence.strip(), source,
                    ))

        if found:
            return found
    return []
