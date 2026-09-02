---
paths:
  - "medrag/markers.py"
  - "medrag/biomarker.py"
  - "medrag/biomarker_gating.py"
  - "config/markers.yaml"
---

# The marker negation grammar — how a sentence is read

These four are here rather than in `CLAUDE.md` because they are rules about
parsing a sentence, and `markers.py` is the only place a sentence is parsed.
Nothing outside these files can violate one.

Full reasoning, with the registry text each was derived from:
`docs/RATIONALE.md` §3 and §6.

## What stays in CLAUDE.md

The census being a DERIVED column (a change here forces a `STORE_VERSION` bump
in `trials/store.py`); the prefilter being safe only because the census has no
UNCLEAR (a policy change here silently changes what `store.query` returns);
`resolve_marker` matching exactly (its failure lands on a patient-facing page);
curated-versus-uncurated confidence (carried to every surface); and the
shared-vocabulary-but-separate-policy rule. Each of those warns about a
consequence outside this glob. The four below do not.

**Negation is recognised in three positions, not just immediately before the
marker.** `_NEGATION_BEFORE` allows up to three intervening words, because
STELLAR-303's real inclusion line reads "Documented NOT to have microsatellite
instability-high (MSI-high)" and "to have" sits in the gap. `_NEGATION_AFTER`
catches the tight suffix — "RAS wild-type", "RAS WT" — which is at least as
common in real CRC eligibility text. `_NEGATION_WITHIN` checks the tail of a
match whose own pattern already consumed the qualifier, like HER2_AMP's
"HER2-negative".
→ `test_wild_type_never_requires_the_marker_for_every_curated_marker`

**A marker restated twice in one sentence is one statement, not two.** Negation
is decided once per (sentence, key), not once per regex match. "documented NOT
to have microsatellite instability-high (MSI-high)" names the same marker
spelled out and then abbreviated; classifying each occurrence independently
found the negation on the first and not the second, manufacturing a
REQUIRED-and-EXCLUDED contradiction out of one unambiguous sentence.
`collect_signals` finds all matches of a key within a sentence first, then
classifies the sentence once, negated if ANY occurrence shows a negation.
→ `test_a_marker_named_twice_in_one_sentence_is_not_a_self_contradiction`

**A sentence that mandates a TEST states no RESULT.** `_is_test_requirement`
recognises a determination verb (documented / assessed / tested / evaluated /
determined) near "status" with no direction word between them, and lets that
sentence contribute NO signal for any marker. C-800-25's "The tumor must have
been assessed for MSI-H or dMMR status per a standard local testing method" read
as REQUIRED and overruled the trial's real exclusion criterion two lines later.
The verb-to-"status" gap is deliberately loose (150 chars) because the negative
lookahead already forbids a direction word anywhere inside it.

**A sentence that ENUMERATES an assay panel states no result either.**
`_ASSAY_PANEL` fires only on an assay noun followed by three or more
comma-separated variant-shaped tokens with no direction word between.
NCT05619172's "RAS wild type as confirmed by: ... at least mutations in exon 2
(G12D, G12V, G12C, G12S, G12A, G12R, G13D)" matched `\bG12C\b` inside the list
and recorded KRAS_G12C REQUIRED for a trial requiring the opposite. Narrow
enough that "Subject has KRasG12C mutation" stays a genuine requirement.

**Text sources are consulted in reliability order, and only on silence.**
`record_texts` orders them eligibility criteria → detailed description → brief
summary → keywords, and `collect_signals` consults each later source ONLY when
every earlier one is completely silent for that marker. Prose or a registry
keyword tag must never override a real eligibility-criteria statement.

**An all-caps acronym pluralised with a lowercase "s" is not the marker it
spells.** Marker patterns are compiled `re.IGNORECASE` — they must be, the
registry writes "Kras", "kras" and "Ras" — so `\bK?RAS\b` also matched `RAs`,
the plural of RA. Measured store-wide that put a RAS verdict on 180 trials with
no oncology content: "GLP-1 RAs" and "GIP RAs" (receptor agonists), "TPO-RAs",
and "research assistants (RAs)". `_is_acronym_plural` rejects a match that
fullmatches `[A-Z]{2,}s`; every curated match routes through `_matches` /
`_matched` so a new call site cannot reacquire the defect by calling `finditer`
directly. The rule is narrow: RAS, ras, Ras, KRAS, Kras, kras and NRAS all still
match. Known residual, one record: "RAS test" for a RAST allergy assay is
all-caps and no case rule reaches it (RATIONALE §25).
→ `test_an_acronym_plural_is_not_the_marker_it_spells`,
  `test_the_acronym_plural_rule_does_not_reject_ordinary_marker_spellings`
