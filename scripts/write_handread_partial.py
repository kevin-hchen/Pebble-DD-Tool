"""Assemble the PARTIAL criteria-segmentation hand-read into a JSON record.

32 of the 60 drawn units, hand-read against complete records. Written OUTSIDE
`tests/fixtures/` on purpose: `tests/test_handread_provenance.py` discovers
graded fixtures by shape, and this is not a graded fixture — it is the
work-in-progress behind a stop-and-report. It moves into `tests/fixtures/` only
once the polarity definition is settled and the remaining 28 are read.

Unit text is copied verbatim from the draw (`sample.json`), never retyped, so
the span a label was read from is the span recorded.

    python scripts/write_handread_partial.py <sample_dir>
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

SAMPLE_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/segsample")
OUT = Path("docs/segmentation_handread_partial.json")
DB = "data/raw/trials.db"

SOURCE_SPAN = (
    "complete unit as yielded by iter_criteria, verbatim and un-elided, read "
    "beside the complete eligibility_criteria of the complete registry record"
)

# (nct_id, verdict, clause polarities in order, {marker: expected}, reason)
# ADMITTING = satisfying the clause makes you eligible (a requirement)
# EXCLUDING = satisfying the clause makes you ineligible (a disqualifier)
# NEITHER   = states no condition on the participant at all
READS = [
    # ---- MIXED / ENUMERATION -------------------------------------------------
    ("NCT05700669", "MIXED_POLARITY",
     ["ADMITTING", "ADMITTING", "ADMITTING", "EXCLUDING", "ADMITTING", "ADMITTING"],
     {"HER2_AMP": "EXCLUDED"},
     "Roman-numbered i.-vi. block on ONE line in the inclusion section. Clause iv "
     "'Participants with HER2 positive disease are not eligible for enrollment' is "
     "excluding; clause iii is a test mandate naming HER2 with no result. Acceptance case."),
    ("NCT01982448", "MIXED_POLARITY",
     ["ADMITTING", "EXCLUDING"],
     {"HER2_AMP": "EXCLUDED"},
     "The record contains no newlines, so the whole of it — inclusion criteria AND the "
     "exclusion list — is one unit tagged 'inclusion'. 'Tumors must be HER2 negative' "
     "admits; 'Participants with axillary adenopathy only are not eligible' excludes."),
    ("NCT02103062", "MIXED_POLARITY",
     ["ADMITTING", "EXCLUDING"],
     {"RAS": "NOT_ASSESSABLE"},
     "Whole record in one unit again. Criterion 3 is 'Subject has a known KRAS mutation "
     "status (mutated or wild-type)' — the axis is raised and BOTH values are admitted, "
     "so no direction is stated. HEAD reads EXCLUDED off 'if RAS wild-type tumors'."),
    ("NCT03147287", "MIXED_POLARITY",
     ["ADMITTING", "EXCLUDING"],
     {"HER2_AMP": "EXCLUDED"},
     "Whole record in one unit. 'must have histologically confirmed HR+ HER2 negative "
     "... breast cancer' requires HER2-negative; the exclusion list follows in the same unit."),
    ("NCT04991740", "SINGLE_POLARITY",
     ["ADMITTING", "ADMITTING", "ADMITTING", "ADMITTING"],
     {"MSI_H": "NOT_ASSESSABLE"},
     "Proxy false positive: 'unless participant was ineligible to receive them' WIDENS "
     "the criterion, it does not exclude. MSI-H appears only as 'prior treatment with "
     "anti-PD1 is required for MSI-H CRC' — a prior-therapy condition on a subpopulation, "
     "not a gate. Not reachable by splitting; the fragment alone still reads REQUIRED."),
    ("NCT05629949", "SINGLE_POLARITY",
     ["ADMITTING", "ADMITTING", "ADMITTING"],
     {"HER2_AMP": "REQUIRED"},
     "Proxy false positive: 'cannot be treated with radical surgery' describes a required "
     "disease state. Trial is HER2-positive metastatic breast cancer; HER2 genuinely required."),
    ("NCT06993506", "SINGLE_POLARITY",
     ["ADMITTING", "ADMITTING"],
     {"HER2_AMP": "EXCLUDED"},
     "Proxy false positive: 'ineligible for curative resection' describes the disease "
     "state that admits. Both cohorts require HER2 negative."),
    ("NCT07127822", "SINGLE_POLARITY",
     ["ADMITTING", "ADMITTING", "ADMITTING", "ADMITTING", "ADMITTING", "ADMITTING"],
     {"MSI_H": "REQUIRED"},
     "NOT a segmentation failure. Criterion 7 contains the phrase 'provided that they meet "
     "other inclusion and exclusion criteria', which iter_criteria treats as a SECTION "
     "HEADING: the section flips to 'exclusion' and everything before the line's first "
     "colon (criteria 1-4) is discarded. An MSI-H/dMMR gastric trial whose criterion 5 is "
     "'Confirmed by PCR or NGS as MSI-H' therefore reads MSI_H: EXCLUDED."),
    ("NCT07244874", "SINGLE_POLARITY",
     ["ADMITTING", "ADMITTING"],
     {"HER2_AMP": "EXCLUDED"},
     "Proxy false positive: 'cannot be surgically removed' is a required disease state. "
     "HER2 negative (IHC 0/1+/2+ FISH-negative) required."),
    ("NCT07396090", "SINGLE_POLARITY",
     ["ADMITTING", "ADMITTING", "ADMITTING", "ADMITTING"],
     {"RAS": "REQUIRED", "KRAS_G12C": "REQUIRED"},
     "Proxy false positive: 'must not be breastfeeding' is a requirement on the "
     "participant, not a class turned away. Criterion 6 requires a targeted KRAS mutant."),
    ("NCT07411599", "MIXED_POLARITY",
     ["ADMITTING", "NEITHER", "EXCLUDING", "ADMITTING", "ADMITTING"],
     {"MSS": "REQUIRED", "MSI_H": "EXCLUDED"},
     "Genuine bullet-separated mix inside one unit: 'must have histologically confirmed "
     "microsatellite stable (MSS) CRC' admits, 'Participants with MSI-H tumors are "
     "ineligible' excludes. Section is 'unknown', so _context resolves the WHOLE unit "
     "from cues and both markers collapse onto one value."),
    ("NCT07630077", "MIXED_POLARITY",
     ["ADMITTING", "EXCLUDING", "ADMITTING", "NEITHER"],
     {"HER2_AMP": "REQUIRED"},
     "Criterion 5's parenthetical 'if there is a small cell component, the subject does "
     "not meet the inclusion criteria' excludes, inside an otherwise admitting block. "
     "HER2 amplification/overexpression required by both cohorts."),

    # ---- MIXED / SENTENCE ----------------------------------------------------
    ("NCT06257758", "MIXED_POLARITY",
     ["ADMITTING", "ADMITTING", "EXCLUDING"],
     {"HER2_AMP": "EXCLUDED"},
     "Three plain sentences, NO enumeration marker of any kind — the '3.' that leads the "
     "line is stripped correctly. The excluding clause is simply the third sentence. "
     "Acceptance case, and the one that shows enumeration splitting alone is insufficient."),
    ("NCT00265850", "MIXED_POLARITY",
     ["ADMITTING", "EXCLUDING", "NEITHER", "NEITHER"],
     {"RAS": "EXCLUDED"},
     "Mixed, but both marker-bearing clauses point the SAME way ('Only patients with a "
     "wildtype K-ras gene are eligible' / 'Patients with a mutation in the K-ras gene are "
     "ineligible'). A control: polarity mixing does not always change a verdict."),
    ("NCT02046421", "MIXED_POLARITY",
     ["ADMITTING", "EXCLUDING"],
     {"HER2_AMP": "EXCLUDED"},
     "Only the EXCLUDING sentence names HER2, and it sits in an inclusion-tagged unit, so "
     "the marker takes inclusion polarity. Live inversion toward REQUIRED at HEAD."),
    ("NCT02614456", "SINGLE_POLARITY",
     ["ADMITTING", "NEITHER", "ADMITTING"],
     {"MSI_H": "NOT_ASSESSABLE"},
     "Proxy false positive on 'may not be limited to'. MSI-H appears only inside an "
     "explicitly non-exhaustive list of illustrative tumour types; no gate is stated."),
    ("NCT03568448", "MIXED_POLARITY",
     ["ADMITTING", "EXCLUDING", "NEITHER"],
     {"HER2_AMP": "EXCLUDED"},
     "Note the middle sentence is ITSELF a mix with no separator ('patients with HR+ "
     "disease will be included, patients with luminal A features will be excluded'). A "
     "SENTENCE-class unit can contain a NONE-class mix inside one of its sentences."),
    ("NCT04381650", "MIXED_POLARITY",
     ["ADMITTING", "EXCLUDING"],
     {"BRAF_V600E": "NOT_ASSESSABLE"},
     "BRAF V600E is named only as an example in a list of driver mutations, and the two "
     "sentences state OPPOSITE gates for Phase 1 and Phase 2. No single direction is "
     "assessable from the record."),
    ("NCT05076552", "MIXED_POLARITY",
     ["ADMITTING", "EXCLUDING", "ADMITTING", "NEITHER"],
     {"MSI_H": "REQUIRED"},
     "'Participants with potentially curative therapy will not be enrolled' excludes, "
     "between two admitting sentences. MSI-H CRC is named as an admitted entry route."),
    ("NCT05684965", "MIXED_POLARITY",
     ["NEITHER", "ADMITTING", "ADMITTING", "ADMITTING", "EXCLUDING"],
     {"BRAF_V600E": "NOT_ASSESSABLE"},
     "'Note: patients with uveal melanoma are excluded' is the excluding tail. BRAF V600 "
     "appears only as a prior-therapy condition — those patients ARE eligible — so no gate."),
    ("NCT06149481", "SINGLE_POLARITY",
     ["ADMITTING", "ADMITTING"],
     {"MSI_H": "NOT_ASSESSABLE", "RAS": "NOT_ASSESSABLE"},
     "Proxy false positive on 'been ineligible to receive', which is part of an admitting "
     "requirement. Both markers appear only inside prior-therapy conditions."),
    ("NCT07070466", "MIXED_POLARITY",
     ["ADMITTING", "EXCLUDING", "EXCLUDING"],
     {"HER2_AMP": "EXCLUDED"},
     "Trial title is 'Ivonescimab in Comb. With FOLFOX in Advanced HER2 Neg. GEA'. The "
     "unit's third sentence excludes HER2+ tumours, inside an inclusion-tagged unit. "
     "Live inversion toward REQUIRED at HEAD, on a trial whose own title says HER2-negative."),

    # ---- MIXED / SEMICOLON ---------------------------------------------------
    ("NCT00126581", "MIXED_POLARITY",
     ["ADMITTING", "ADMITTING", "EXCLUDING"],
     {"RAS": "NOT_ASSESSABLE"},
     "K-ras is named inside an assay panel ('available for sequencing of EGFR, K-ras, "
     "Erb-2 and B-raf') with no result stated — the documented panel-sentence residual."),
    ("NCT01744171", "SINGLE_POLARITY",
     ["ADMITTING", "ADMITTING"],
     {"BRAF_V600E": "NOT_ASSESSABLE"},
     "Proxy false positive: 'be ineligible for' is inside an admitting requirement. BRAF "
     "V600E carries only an extra prior-therapy condition, not a gate."),
    ("NCT01989585", "MIXED_POLARITY",
     ["EXCLUDING", "ADMITTING", "NEITHER", "EXCLUDING", "NEITHER"],
     {"RAS": "EXCLUDED"},
     "Semicolon-chained exceptions inside an exclusion-section unit: an admitting "
     "carve-out ('patients disease-free for 3 years are eligible') sits between two "
     "excluding clauses. The mirror-image mix, and the marker clause is excluding."),
    ("NCT02224781", "MIXED_POLARITY",
     ["ADMITTING", "EXCLUDING"],
     {"BRAF_V600E": "REQUIRED"},
     "'Any patient with BRAF V600 mutant melanoma ... is eligible' admits; 'patients with "
     "uveal melanoma are not eligible' excludes. Only the admitting clause names BRAF."),
    ("NCT02886585", "MIXED_POLARITY",
     ["ADMITTING", "EXCLUDING"],
     {"HER2_AMP": "EXCLUDED"},
     "'the following diagnoses will be excluded: HER2-positive breast cancer; small cell "
     "lung cancer; ...' is the excluding tail of an inclusion-tagged unit. Live inversion."),
    ("NCT03365882", "MIXED_POLARITY",
     ["ADMITTING", "EXCLUDING"],
     {"RAS": "EXCLUDED", "BRAF_V600E": "EXCLUDED"},
     "S1613. The test mandate admits; 'patients with any known activating mutation in ... "
     "KRAS/NRAS ... and in exon 15 (BRAFV600E) ... are not eligible' excludes. HEAD reads "
     "BOTH markers REQUIRED — two live inversions toward REQUIRED in one unit."),
    ("NCT04607668", "SINGLE_POLARITY",
     ["ADMITTING", "ADMITTING", "NEITHER", "NEITHER", "NEITHER"],
     {"MSS": "REQUIRED", "RAS": "NOT_ASSESSABLE", "BRAF_V600E": "NOT_ASSESSABLE"},
     "Proxy false positive. The record states POSITIVELY that there is no gate: 'Patients "
     "with any BRAF or KRAS mutation status (wild type or mutant) are eligible'. HEAD "
     "reads both REQUIRED off the surrounding test mandate."),
    ("NCT05003037", "MIXED_POLARITY",
     ["ADMITTING", "EXCLUDING", "NEITHER"],
     {"RAS": "EXCLUDED"},
     "The admitting list of driver mutations deliberately omits KRAS; the next sentence is "
     "'Patients with KRAS mutations are not permitted'. Live inversion toward REQUIRED."),
    ("NCT05239546", "MIXED_POLARITY",
     ["ADMITTING", "EXCLUDING"],
     {"MSI_H": "REQUIRED"},
     "'otherwise patient is not eligible' is the excluding tail. The dMMR gate itself is "
     "stated elsewhere in the record — a neoadjuvant dostarlimab trial in dMMR colon "
     "cancer — so the record-level expectation is REQUIRED."),
    ("NCT06663319", "SINGLE_POLARITY",
     ["ADMITTING", "ADMITTING", "ADMITTING", "ADMITTING", "ADMITTING"],
     {"MSS": "REQUIRED"},
     "Proxy false positive: 'Must not have received irinotecan previously' is a "
     "requirement on the participant, not a class turned away. Arms C and D require MSS/pMMR."),
]


def main() -> None:
    sample = {(r["nct_id"], r["unit_index"]): r
              for r in json.loads((SAMPLE_DIR / "sample.json").read_text())}
    by_nct = {}
    for (nct, _idx), r in sample.items():
        by_nct.setdefault(nct, r)

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    rows = []
    for nct, verdict, polarities, expected, reason in READS:
        drawn = by_nct[nct]
        rec = conn.execute(
            "SELECT brief_title FROM trials WHERE nct_id = ?", (nct,)).fetchone()
        rows.append({
            "nct_id": nct,
            "title": rec["brief_title"],
            "set_key": drawn["set_key"],
            "unit_index": drawn["unit_index"],
            "section_as_tagged": drawn["section"],
            "separator_class": drawn["separator_class"],
            "stratum": drawn["stratum"],
            "unit": drawn["unit"],
            "markers_named": drawn["markers_named"],
            "verdict": verdict,
            "clause_polarities": polarities,
            "expected_marker_verdicts": expected,
            "reason": reason,
            "source_span": SOURCE_SPAN,
        })

    OUT.write_text(json.dumps(rows, indent=2) + "\n")
    print(f"wrote {len(rows)} rows -> {OUT}")
    mixed = sum(1 for r in rows if r["verdict"] == "MIXED_POLARITY")
    print(f"  MIXED_POLARITY {mixed}   SINGLE_POLARITY {len(rows) - mixed}")


if __name__ == "__main__":
    main()
