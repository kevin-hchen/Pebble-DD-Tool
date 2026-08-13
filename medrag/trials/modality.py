"""Is this a device trial or a drug trial? Answered from what the registry says.

Until `intervention_types` was stored, this tool could not tell — a parity audit
had to identify device trials with a regex over intervention NAMES, which is
guessing at a fact ClinicalTrials.gov states outright on every record.

Five states, and the fifth is the one that matters.

  DEVICE   at least one DEVICE or DIAGNOSTIC_TEST intervention
  DRUG     at least one DRUG or BIOLOGICAL intervention
  BOTH     at least one of each
  OTHER    only procedure / radiation / behavioral / dietary / genetic / other
  UNKNOWN  the registry recorded no type on any intervention

**UNKNOWN is a state, not a bucket inside OTHER.** The registry genuinely omits
this field, and it omits it more often on exactly the trials this distinction
was added to find — the audit's "no gate type detected at all" bands run
20.9% (imaging) to 40.9% (IVD) on the device side against 10.2% for drugs. A
classifier with nowhere to put "the record does not state it" has to put those
somewhere, and the somewhere is always a real category, so silence would be
read as a finding. That is the not-assessed-versus-nothing-found rule, applied
to modality: absent is not OTHER, and it is certainly not DRUG.

It is also why the store REFUSES to open a pre-v12 file rather than reading the
column as empty. An unfetched column and a registry that stated no type look
identical from here, and only one of them means "we asked".

Nothing in this module guesses. There is deliberately no fallback that reads
intervention NAMES when the type is missing: a name-based guess is what the
regex did, it was measurably wrong in both directions (see the stage-1 report),
and a guess that renders as a verdict is worse than a stated UNKNOWN. If a
caller wants to narrow the unknowns it has to do so visibly, with its own
counted, shown heuristic.
"""

from __future__ import annotations

from collections import Counter

DEVICE = "DEVICE"
DRUG = "DRUG"
BOTH = "BOTH"
OTHER = "OTHER"
UNKNOWN = "UNKNOWN"

#: Every state, in report order. UNKNOWN last because it is the residual, not
#: because it is least important — on the device side it is frequently the
#: largest single bucket.
STATES = (DEVICE, DRUG, BOTH, OTHER, UNKNOWN)

#: The registry's own vocabulary, mapped to the two axes this tool cares about.
#: DIAGNOSTIC_TEST sits with DEVICE because an IVD is a device in every sense
#: this tool uses the word — it is regulated as one, it is evaluated against a
#: reference standard, and the question "what does the evidence for this device
#: look like" is the same question. BIOLOGICAL sits with DRUG for the mirror
#: reason: it is an asset with an approval pathway and a mechanism.
#:
#: COMBINATION_PRODUCT is deliberately in NEITHER set. A drug-eluting stent and
#: a prefilled autoinjector are both combination products and they are not the
#: same kind of asset; assigning the label to one axis would be a coin flip
#: rendered as a fact. It lands in OTHER unless the record carries another
#: intervention that resolves it, which is the honest reading of a type that
#: means "both, unspecified".
_DEVICE_TYPES = frozenset({"DEVICE", "DIAGNOSTIC_TEST"})
_DRUG_TYPES = frozenset({"DRUG", "BIOLOGICAL"})

#: Types that are real and are neither. Listed rather than inferred by
#: exclusion, so a type the registry adds later arrives as an unrecognised
#: value that `unrecognised_types()` reports, instead of being silently swept
#: into OTHER.
_OTHER_TYPES = frozenset({
    "PROCEDURE", "RADIATION", "BEHAVIORAL", "DIETARY_SUPPLEMENT",
    "GENETIC", "COMBINATION_PRODUCT", "OTHER",
})

KNOWN_TYPES = _DEVICE_TYPES | _DRUG_TYPES | _OTHER_TYPES


def classify_types(types) -> str:
    """The trial-level state, from an index-aligned list of intervention types.

    Takes the list rather than the record so the census, the store migration and
    the tests all reduce the same input through the same function — the split
    that put `markers.py` between `biomarker.py` and `biomarker_gating.py` after
    those two disagreed about the same trial.
    """
    present = {t.strip().upper() for t in (types or []) if t and t.strip()}
    if not present:
        return UNKNOWN
    has_device = bool(present & _DEVICE_TYPES)
    has_drug = bool(present & _DRUG_TYPES)
    if has_device and has_drug:
        return BOTH
    if has_device:
        return DEVICE
    if has_drug:
        return DRUG
    return OTHER


def classify(record) -> str:
    """The state for a `TrialRecord`.

    A record whose `interventions` list is non-empty while `intervention_types`
    is empty has not been backfilled — it is not a registry silence. In a v12
    store that cannot happen (the store refuses to open a v11 file, and
    `_assert_aligned` holds at parse), so this raises rather than returning
    UNKNOWN: quietly answering UNKNOWN would reintroduce exactly the ambiguity
    the schema refusal exists to prevent.
    """
    names = getattr(record, "interventions", None) or []
    types = getattr(record, "intervention_types", None) or []
    if names and not types:
        raise ValueError(
            f"{getattr(record, 'nct_id', '?')}: {len(names)} interventions but no types. "
            "This record predates the intervention_types column and has never been "
            "asked; it is NOT a registry silence. Run "
            "`python -m medrag trials --backfill-types`."
        )
    return classify_types(types)


def unrecognised_types(types) -> set[str]:
    """Type values the registry sent that this module has no mapping for.

    Reported rather than absorbed. openFDA's undocumented PMA decision codes are
    the precedent: 27,693 records — 49% of that source — carried a code the data
    dictionary did not define, and folding them into a documented meaning would
    have been the single largest misstatement in the file.
    """
    return {t.strip().upper() for t in (types or []) if t and t.strip()} - KNOWN_TYPES


def census(records) -> Counter:
    """State counts over an iterable of records, plus the total. Every state is
    present in the result even at zero, so a reader can tell "none of these"
    from "this state was not evaluated"."""
    counts = Counter({s: 0 for s in STATES})
    for r in records:
        counts[classify(r)] += 1
    counts["_total"] = sum(counts[s] for s in STATES)
    return counts
