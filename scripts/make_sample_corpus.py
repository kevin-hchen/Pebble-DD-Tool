"""Generate a small SYNTHETIC corpus so the pipeline runs with no network access.

These records are invented for demonstration and testing. They are deliberately
given SAMPLE-* identifiers rather than real PMIDs so they can never be mistaken
for published findings. For real use, run:

    medrag ingest --query "..." --index
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from medrag.config import load_config  # noqa: E402
from medrag.documents import Document  # noqa: E402
from medrag.ingest.store import save_corpus  # noqa: E402
from medrag.pipeline import CORPUS_FILE, _passphrase_for  # noqa: E402

SAMPLES = [
    Document(
        doc_id="SAMPLE-001",
        title="[SYNTHETIC] Sodium-glucose cotransporter-2 inhibition and heart failure hospitalization in preserved ejection fraction",
        text=(
            "Background: Patients with heart failure and preserved ejection fraction have few "
            "therapies with proven benefit on hospitalization.\n\n"
            "Methods: In this synthetic double-blind trial, 5988 patients with an ejection "
            "fraction above 40 percent were randomly assigned to an SGLT2 inhibitor or placebo "
            "and followed for a median of 26 months.\n\n"
            "Results: The primary composite of cardiovascular death or hospitalization for heart "
            "failure occurred in 13.8 percent of the treatment group and 17.1 percent of the "
            "placebo group (hazard ratio 0.79, 95 percent CI 0.69 to 0.90, p less than 0.001). "
            "The benefit was driven predominantly by a reduction in hospitalization rather than "
            "cardiovascular death. Genital infections were more frequent with active treatment.\n\n"
            "Conclusions: SGLT2 inhibition reduced heart failure hospitalization in preserved "
            "ejection fraction, with a benefit consistent across diabetic and non-diabetic strata."
        ),
        authors=["A Demo", "B Example"],
        journal="J Synthetic Cardiol",
        year="2023",
        url="",
        meta={"synthetic": True, "publication_types": ['Randomized Controlled Trial', 'Multicenter Study']},
    ),
    Document(
        doc_id="SAMPLE-002",
        title="[SYNTHETIC] SGLT2 inhibitors in reduced ejection fraction: a pooled analysis",
        text=(
            "Background: Trials of SGLT2 inhibitors in reduced ejection fraction have reported "
            "consistent benefit, but effect sizes vary by baseline renal function.\n\n"
            "Methods: We pooled four synthetic randomized trials comprising 11,847 participants "
            "with ejection fraction at or below 40 percent.\n\n"
            "Results: Treatment reduced the composite endpoint by a hazard ratio of 0.74 "
            "(95 percent CI 0.67 to 0.83). Absolute risk reduction was 4.2 percentage points "
            "over 24 months, corresponding to a number needed to treat of 24. Estimated GFR "
            "declined transiently in the first four weeks before stabilizing.\n\n"
            "Conclusions: Pooled evidence supports SGLT2 inhibition as standard therapy in "
            "reduced ejection fraction, including in patients with moderate renal impairment."
        ),
        authors=["C Sample"],
        journal="Synth Rev Cardiovasc Med",
        year="2024",
        meta={"synthetic": True, "publication_types": ['Meta-Analysis', 'Systematic Review']},
    ),
    Document(
        doc_id="SAMPLE-003",
        title="[SYNTHETIC] Metformin as initial pharmacotherapy in type 2 diabetes: a cohort study",
        text=(
            "Background: Metformin remains the recommended first-line agent in most guidelines.\n\n"
            "Methods: A synthetic retrospective cohort of 32,410 adults initiating metformin was "
            "followed for a median of 5.2 years.\n\n"
            "Results: Mean HbA1c fell by 1.1 percentage points at 12 months and 0.9 percentage "
            "points at 36 months. Discontinuation for gastrointestinal intolerance occurred in "
            "8.4 percent of patients. No increase in lactic acidosis was observed at eGFR above "
            "30 mL/min/1.73m2.\n\n"
            "Conclusions: Metformin produced durable glycemic control with a favorable safety "
            "profile in this synthetic cohort."
        ),
        authors=["D Placeholder", "E Mock"],
        journal="Synth Diabetes Care",
        year="2022",
        meta={"synthetic": True, "publication_types": ['Observational Study', 'Comparative Study']},
    ),
    Document(
        doc_id="SAMPLE-004",
        title="[SYNTHETIC] Anticoagulation strategy in atrial fibrillation with chronic kidney disease",
        text=(
            "Background: Direct oral anticoagulants are under-studied in advanced chronic kidney "
            "disease.\n\n"
            "Methods: 1,204 synthetic participants with atrial fibrillation and eGFR between 15 "
            "and 30 mL/min/1.73m2 were randomized to a direct oral anticoagulant or warfarin.\n\n"
            "Results: Stroke or systemic embolism occurred at 3.1 percent per year with the "
            "direct oral anticoagulant versus 3.6 percent with warfarin (hazard ratio 0.86, "
            "95 percent CI 0.61 to 1.21), a non-significant difference. Major bleeding was lower "
            "with the direct oral anticoagulant (hazard ratio 0.72, 95 percent CI 0.55 to 0.94).\n\n"
            "Conclusions: In this synthetic trial the direct oral anticoagulant was non-inferior "
            "for thromboembolism and reduced major bleeding. The trial was underpowered for "
            "mortality."
        ),
        authors=["F Fixture"],
        journal="Synth Nephrol Cardiol",
        year="2023",
        meta={"synthetic": True, "publication_types": ['Randomized Controlled Trial']},
    ),
    Document(
        doc_id="SAMPLE-005",
        title="[SYNTHETIC] Early mobilization after cardiac surgery and length of stay",
        text=(
            "Background: Prolonged bed rest after cardiac surgery is associated with "
            "deconditioning.\n\n"
            "Methods: 640 synthetic postoperative patients were assigned to structured "
            "mobilization within 18 hours or usual care.\n\n"
            "Results: Median length of stay was 6.2 days with early mobilization versus 7.8 days "
            "with usual care (p equals 0.004). Readmission at 30 days did not differ "
            "significantly (9.1 percent versus 10.4 percent).\n\n"
            "Conclusions: Early structured mobilization shortened length of stay without "
            "increasing readmission in this synthetic cohort. Follow-up was limited to 30 days."
        ),
        authors=["G Stub", "H Dummy"],
        journal="Synth J Surg Recovery",
        year="2021",
        meta={"synthetic": True, "publication_types": ['Clinical Trial']},
    ),
    Document(
        doc_id="SAMPLE-006",
        title="[SYNTHETIC] Statin intensity and secondary prevention after myocardial infarction",
        text=(
            "Background: The incremental benefit of high-intensity over moderate-intensity statin "
            "therapy after myocardial infarction remains debated in older adults.\n\n"
            "Methods: 8,930 synthetic patients aged 65 and older were randomized within 14 days "
            "of infarction.\n\n"
            "Results: LDL cholesterol at 12 months was 1.6 mmol/L with high-intensity therapy "
            "versus 2.2 mmol/L with moderate intensity. Recurrent major adverse cardiovascular "
            "events occurred in 11.2 percent versus 13.5 percent (hazard ratio 0.82, 95 percent "
            "CI 0.72 to 0.94). Muscle-related discontinuation was more common with high-intensity "
            "therapy (6.8 percent versus 4.1 percent).\n\n"
            "Conclusions: High-intensity statin therapy reduced recurrent events at the cost of "
            "more tolerability-related discontinuation."
        ),
        authors=["I Example", "J Testcase"],
        journal="Synth Circ Outcomes",
        year="2024",
        meta={"synthetic": True, "publication_types": ['Case Reports']},
    ),
]


def main() -> None:
    cfg = load_config()
    cfg.ensure_dirs()
    # Honour MEDRAG_ENCRYPT here too - a seeding script that silently wrote
    # plaintext while the rest of the pipeline encrypted would be a real leak.
    path = save_corpus(SAMPLES, cfg.raw_dir / CORPUS_FILE, passphrase=_passphrase_for(cfg))
    print(f"wrote {len(SAMPLES)} synthetic documents to {path}"
          f"{' (encrypted)' if cfg.encrypt else ''}")
    print("next: python -m medrag index && python -m medrag ask \"your question\"")


if __name__ == "__main__":
    main()
