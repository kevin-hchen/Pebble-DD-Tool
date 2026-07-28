"""Retrieval and faithfulness evaluation, with a before/after on evidence grading.

The number this produces is the difference between "I built a RAG pipeline" and
a sentence you can say out loud: recall at 5 went from X to Y once passages were
weighted by study design.

Two honest caveats, stated here because they belong next to the number:

1. The gold labels below are for the SYNTHETIC sample corpus. They are a harness
   test, not a measurement of real performance. Before quoting a figure, build a
   real set of 12-15 questions over a real corpus and label which document holds
   the answer YOURSELF. If the tool labels its own gold set the number is
   circular.

2. Recall improves from grading only when the corpus actually contains a mix of
   study designs. On a corpus of uniformly graded abstracts the delta will be
   zero, and that is a true result about your corpus, not a broken harness.

    python scripts/eval.py                 # built-in sample set
    python scripts/eval.py my_questions.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from medrag.config import load_config  # noqa: E402
from medrag.pipeline import MedRAG  # noqa: E402

DEFAULT_SET = [
    {"question": "Do SGLT2 inhibitors reduce heart failure hospitalization in preserved ejection fraction?",
     "gold": ["SAMPLE-001"]},
    {"question": "What is the pooled hazard ratio for SGLT2 inhibitors in reduced ejection fraction?",
     "gold": ["SAMPLE-002"]},
    {"question": "How much does metformin lower HbA1c at 12 months?", "gold": ["SAMPLE-003"]},
    {"question": "Is a direct oral anticoagulant safer than warfarin in advanced kidney disease?",
     "gold": ["SAMPLE-004"]},
    {"question": "Does early mobilization after cardiac surgery affect readmission?",
     "gold": ["SAMPLE-005"]},
    {"question": "What is the tradeoff of high-intensity statins in older adults?",
     "gold": ["SAMPLE-006"]},
]


def evaluate(rag: MedRAG, questions: list[dict], k: int, evidence_weight: float) -> dict:
    """Recall@k and MRR at a given evidence-grading weight."""
    hits = 0
    reciprocal: list[float] = []

    for item in questions:
        retrieved = rag.retriever.retrieve(
            item["question"], k=k, evidence_weight=evidence_weight
        )
        ids = [r.chunk.doc_id for r in retrieved]
        gold = set(item["gold"])

        hits += bool(gold & set(ids))
        rr = 0.0
        for rank, doc_id in enumerate(ids, 1):
            if doc_id in gold:
                rr = 1.0 / rank
                break
        reciprocal.append(rr)

    n = len(questions)
    return {"recall": hits / n, "mrr": sum(reciprocal) / n, "hits": hits, "n": n}


def main() -> int:
    questions = DEFAULT_SET
    using_defaults = True
    if len(sys.argv) > 1:
        questions = json.loads(Path(sys.argv[1]).read_text())
        using_defaults = False

    k = 5
    rag = MedRAG(load_config())
    print(f"Evaluating {len(questions)} questions at k={k} | embedder: {rag.embedder.name}\n")

    before = evaluate(rag, questions, k, evidence_weight=0.0)
    after = evaluate(rag, questions, k, evidence_weight=1.0)

    print(f"{'':<28}{'recall@' + str(k):>10}{'MRR':>10}")
    print("-" * 48)
    print(f"{'without evidence grading':<28}{before['recall']:>10.1%}{before['mrr']:>10.3f}")
    print(f"{'with evidence grading':<28}{after['recall']:>10.1%}{after['mrr']:>10.3f}")
    print("-" * 48)

    d_recall = after["recall"] - before["recall"]
    d_mrr = after["mrr"] - before["mrr"]
    print(f"{'delta':<28}{d_recall:>+10.1%}{d_mrr:>+10.3f}")

    if abs(d_recall) < 1e-9 and abs(d_mrr) < 1e-9:
        print(
            "\nNo change. Either the corpus has a uniform evidence mix, or the gold\n"
            "documents were already top-ranked. Both are real results about this\n"
            "corpus rather than a fault in the harness."
        )

    # Faithfulness, reported separately: it measures the answer, not retrieval.
    faithful = sum(1 for item in questions if rag.ask(item["question"]).validation.passed)
    print(f"\nfaithfulness: {faithful / len(questions):.1%} ({faithful}/{len(questions)})")

    if using_defaults:
        print(
            "\nNOTE: this ran on the built-in SYNTHETIC sample set. It verifies the\n"
            "harness, not real-world performance. Build and hand-label your own set\n"
            "of 12-15 questions before quoting any of these figures."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
