---
paths:
  - "medrag/fda/**"
  - "config/fda_decision_codes.yaml"
  - "config/fda_exclusivity_codes.yaml"
  - "config/fda_biologic_exclusivity.yaml"
---

# openFDA source shapes — facts about the bytes, not about the memo

These are here rather than in `CLAUDE.md` because each one constrains only the
code in these files. Nothing outside this glob can violate one, and none of them
tells a renderer, a prompt, or a question set what it may say.

Full reasoning and the measurements: `docs/RATIONALE.md` §15.

## What stays in CLAUDE.md

Everything about what an FDA answer may CLAIM: absence never rendering as
non-approval, the approval statement being rendered rather than written, the
FAERS caveats and the count-is-not-a-rate guard, the Orange Book applicability
answer, "protection lapses" never "generics enter", the no-biosimilars note.
Those constrain renderers, prompts and question sets outside this glob, so a
rule that loads only when someone opens `medrag/fda/` would be silent for the
person who could actually break them.

## Matching keys

**Devices are matched on `product_code`, never on company.** The same firm files
under "Baxter Healthcare Corp" and "Baxter Healthcare Corporation", and
acquisitions scatter a product line across subsidiary names
(Imed → Alaris → CareFusion → BD), so matching on `applicant` or
`recalling_firm` silently misses clearances. A device name resolves to its
product code(s) via the clearances table; recalls and events are looked up from
there.

**The three device endpoints do NOT share a schema.** `device/510k` and
`device/recall` carry `product_code` at the top level; `device/event` nests it
at `device[].device_report_product_code` and a top-level `product_code:` search
404s. `event.date_received` is `YYYYMMDD` while the others are ISO. Multi-term
searches join with `" AND "`, never `"+AND+"` — a literal `+` is URL-encoded to
`%2B` and breaks the query.

**Drugs are matched on `products[].active_ingredients[].name`, NOT
`openfda.generic_name`.** The `openfda` block is a convenience join derived from
SPL linkage and is absent from most of drugsFDA; the ingredient field is present
on 99% of applications against openfda's 43%. Reaching for the field that looks
canonical instead of the one that is populated is the same class of error as
matching a condition by substring.

## Record shapes

**PMA originals are keyed off `supplement_number`, not `supplement_type`.**
Supplements are SEPARATE RECORDS in this source — the opposite of drugsFDA,
where submissions nest — so the key is `(pma_number, supplement_number)`.
`supplement_type` is empty on all originals, which makes it look like the
discriminator, but it is ALSO empty on 1,885 genuine supplements. Using it
overstates the approval base by 128%.

**A PMA is NOT automatically Class III.** Measured across the whole export,
class 2 accounts for 7,177 records against class 3's 48,473, plus several
hundred with no class at all. Collapsing the pathway into the class would be
wrong on 14% of records, so `device_class` is carried verbatim and the rendered
line says it is read from the record rather than inferred.

## Code tables

**Decision-code meanings in `config/fda_decision_codes.yaml` are VERBATIM from
the FDA data dictionary**, never inferred from the letters — `APRL` reads like
"approvable letter" and actually means "Reclassification after approval", and an
earlier draft had `SESK` and `SESU` swapped. The dictionary and the data
disagree in both directions: `OK30` is 49% of the source and is undocumented,
while several documented codes never occur. An undocumented code renders as
undocumented and never as an approval.

**Exclusivity codes in `config/fda_exclusivity_codes.yaml` are CURATED, not
FDA-sourced, and are labelled as such.** openFDA documents the field only as
"Code to designate exclusivity granted by the FDA" and does not enumerate the
values. Meanings are asserted for the two the question turns on (ODE\* orphan,
PED paediatric) and the other observed codes carry no meaning at all.

## The Purple Book CSV

**The file is TWO sections and only the second is the database.** Each monthly
file opens with a changes report, then repeats the identical header and lists
every product. `section=1` selects the full database; taking the first would
silently reduce the Purple Book to one month of changes.

**`PurpleBookLayoutError` fires if a needed column is renamed.** A CSV has no
schema version, and a renamed `License Type` would turn every biosimilar into an
originator, silently.
