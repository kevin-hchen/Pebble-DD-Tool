"""Run a fixed question set against an asset and assemble a diligence memo.

This is what makes the tool an instrument rather than a chat box: the same
questions, in the same order, against every asset, so two memos are comparable.
A free-text box gives a differently shaped answer every time, which is exactly
what you cannot put in front of an investment committee.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import Config, load_config
from .context import Evidence, build_evidence, provenance_summary, render_context
from .documents import Retrieved
from .generator import SYSTEM_PROMPT, Answer
from .negative_evidence import NegativeEvidence, run_negative_pass
from .router import Route, Router
from .trials.client import TrialRecord
from .trials.store import TrialStore
from .validation import ValidationReport, validate_answer

DEFAULT_QUESTION_SET = Path(__file__).resolve().parents[1] / "config" / "diligence_questions.yaml"

DILIGENCE_USER_TEMPLATE = """Question: {question}

Evidence excerpts. Each is labelled by kind — TRIAL RECORD is a clinical trial \
registry entry (a fact about what was run), LITERATURE is a published paper \
(a reported finding), with its study design tier. Do not blur the two: a \
registry record does not report a result, and a narrative review is not a trial.

{context}

Answer using only these excerpts, with inline [n] citations. If the evidence \
does not answer the question, say so plainly and state what is missing."""


@dataclass
class DiligenceQuestion:
    id: str
    section: str
    question: str
    route: str = "auto"
    k: int = 6
    negative: bool = False


@dataclass
class QuestionSet:
    name: str
    version: int
    questions: list[DiligenceQuestion]
    path: Path | None = None

    def __len__(self) -> int:
        return len(self.questions)


@dataclass
class SectionResult:
    question: DiligenceQuestion
    rendered_question: str
    answer: Answer
    evidence: list[Evidence]
    validation: ValidationReport
    route: Route
    route_method: str
    provenance: dict = field(default_factory=dict)
    negative: NegativeEvidence | None = None


@dataclass
class MemoResult:
    asset: str
    indication: str
    question_set: str
    sections: list[SectionResult] = field(default_factory=list)
    embedder: str = ""
    model: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def all_evidence(self) -> list[Evidence]:
        return [e for s in self.sections for e in s.evidence]

    def coverage(self) -> dict:
        """Aggregate signals a reader needs before trusting the memo."""
        answered = sum(1 for s in self.sections if s.evidence)
        # Assessed and passing are reported separately. Counting an unassessed
        # section as passing overstates how much of the memo was checked.
        assessed = sum(1 for s in self.sections if s.validation.assessed)
        validated = sum(1 for s in self.sections if s.validation.passed)
        stopped = sum(
            len(s.negative.stopped_trials) for s in self.sections if s.negative
        )
        findings = sum(len(s.negative.findings) for s in self.sections if s.negative)
        return {
            "sections": len(self.sections),
            "sections_with_evidence": answered,
            "sections_assessed": assessed,
            "sections_passing_validation": validated,
            "stopped_trials_found": stopped,
            "contradicting_findings": findings,
        }


def load_question_set(path: str | Path | None = None) -> QuestionSet:
    path = Path(path or DEFAULT_QUESTION_SET)
    if not path.exists():
        raise FileNotFoundError(f"question set not found: {path}")

    data = yaml.safe_load(path.read_text()) or {}
    raw = data.get("questions") or []
    if not raw:
        raise ValueError(f"question set {path} contains no questions")

    seen: set[str] = set()
    questions: list[DiligenceQuestion] = []
    for i, q in enumerate(raw, 1):
        for required in ("id", "question"):
            if not q.get(required):
                raise ValueError(f"question {i} in {path.name} is missing '{required}'")
        if q["id"] in seen:
            # Section anchors must be stable and unique or the memo silently
            # collapses two sections into one.
            raise ValueError(f"duplicate question id '{q['id']}' in {path.name}")
        seen.add(q["id"])
        questions.append(
            DiligenceQuestion(
                id=q["id"],
                section=q.get("section", q["id"].replace("-", " ").title()),
                question=q["question"],
                route=str(q.get("route", "auto")).lower(),
                k=int(q.get("k", 6)),
                negative=bool(q.get("negative", False)),
            )
        )

    return QuestionSet(
        name=data.get("name", path.stem),
        version=int(data.get("version", 1)),
        questions=questions,
        path=path,
    )


class DiligenceRunner:
    """Orchestrates routing, dual-store retrieval, answering and the negative pass."""

    def __init__(self, cfg: Config | None = None, rag=None, trial_store: TrialStore | None = None):
        self.cfg = cfg or load_config()
        self.router = Router(self.cfg)

        # The literature side is optional: a registry-only run is still useful,
        # and failing the whole memo because no index exists would be wrong.
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
            from .trials.store import TrialStoreSchemaError

            db = self.cfg.raw_dir / TRIALS_DB
            if db.exists():
                # A stale trials.db must not crash the memo: degrade to a
                # literature-only run with the rebuild instruction surfaced.
                try:
                    self.trial_store = TrialStore(db)
                except TrialStoreSchemaError as exc:
                    self.warnings.append(str(exc).splitlines()[0])
            else:
                self.warnings.append("trial store not found — run `medrag trials` first")

    # ------------------------------------------------------------ retrieval

    def _trials_for(self, question: str, asset: str, indication: str, filters: dict,
                    limit: int) -> list[TrialRecord]:
        if self.trial_store is None:
            return []

        if filters.get("nct_ids"):
            found = [self.trial_store.get(n) for n in filters["nct_ids"]]
            return [f for f in found if f]

        records = self.trial_store.query(
            intervention=asset or None,
            condition=indication or None,
            phase=filters.get("phase"),
            stopped_only=bool(filters.get("stopped_only")),
            limit=limit,
        )
        # Structured filters can legitimately return nothing (no Phase 3 exists).
        # Fall back to free text so the section is not silently empty.
        if not records:
            records = self.trial_store.search(f"{asset} {indication} {question}", limit=limit)
        return records

    def _passages(self, question: str, k: int) -> list[Retrieved]:
        if self.rag is None:
            return []
        return self.rag.retriever.retrieve(question, k=k)

    # ------------------------------------------------------------ one section

    def run_question(self, q: DiligenceQuestion, asset: str, indication: str) -> SectionResult:
        rendered = q.question.format(asset=asset or "the asset",
                                     indication=indication or "the indication")

        if q.route == "auto":
            decision = self.router.route(rendered)
            route, method = decision.route, decision.method
            filters = decision.filters
        else:
            route, method = Route(q.route), "config"
            from .router import extract_filters

            filters = extract_filters(rendered)

        trials = (
            self._trials_for(rendered, asset, indication, filters, q.k)
            if route in (Route.STRUCTURED, Route.BOTH)
            else []
        )
        passages = self._passages(rendered, q.k) if route in (Route.SEMANTIC, Route.BOTH) else []

        evidence = build_evidence(trials=trials, passages=passages,
                                  max_chars=self.cfg.max_context_chars)
        answer = self._answer(rendered, evidence)
        # Validate against the assembled evidence, not the literature subset:
        # the markers the model saw are numbered across both stores.
        report = validate_answer(answer, evidence=evidence)

        negative = None
        if q.negative:
            negative = run_negative_pass(
                claim=rendered,
                cfg=self.cfg,
                evidence=evidence,
                trial_store=self.trial_store,
                intervention=asset or None,
                condition=indication or None,
            )

        return SectionResult(
            question=q,
            rendered_question=rendered,
            answer=answer,
            evidence=evidence,
            validation=report,
            route=route,
            route_method=method,
            provenance=provenance_summary(evidence),
            negative=negative,
        )

    def _answer(self, question: str, evidence: list[Evidence]) -> Answer:
        """Generate a grounded answer over provenance-labelled evidence."""
        if not evidence:
            return Answer(
                text="No evidence was retrieved for this question from either store.",
                sources=[],
                model="none",
                grounded=False,
            )

        generator = self.rag.generator if self.rag else None
        client = getattr(generator, "client", None)
        if client is None:
            # Extractive fallback: return the evidence itself rather than an
            # ungrounded synthesis. Clearly labelled so nobody mistakes it for one.
            body = ["*(No model available — showing retrieved evidence verbatim.)*", ""]
            for e in evidence:
                label = f"[{e.index}] ({e.kind} — {e.identifier}"
                label += f" — {e.grade_tag})" if e.grade_tag else ")"
                snippet = e.text.replace("\n", " ")[:400]
                body.append(f"{label} {snippet}")
                body.append("")
            return Answer(text="\n".join(body).strip(), sources=[], model="extractive-fallback")

        resp = client.chat.completions.create(
            model=self.cfg.chat_model,
            temperature=self.cfg.temperature,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": DILIGENCE_USER_TEMPLATE.format(
                        question=question, context=render_context(evidence)
                    ),
                },
            ],
        )
        return Answer(
            text=resp.choices[0].message.content.strip(),
            sources=[],
            model=self.cfg.chat_model,
        )

    # ------------------------------------------------------------ full run

    def run(self, asset: str, indication: str = "",
            question_set: QuestionSet | None = None,
            progress: bool = True) -> MemoResult:
        qs = question_set or load_question_set()
        memo = MemoResult(
            asset=asset,
            indication=indication,
            question_set=qs.name,
            embedder=self.rag.embedder.name if self.rag else "none",
            model=self.cfg.chat_model,
            warnings=list(self.warnings),
        )

        for i, q in enumerate(qs.questions, 1):
            if progress:
                print(f"[medrag] {i}/{len(qs)} {q.section}")
            memo.sections.append(self.run_question(q, asset, indication))

        return memo

    def close(self) -> None:
        if self.trial_store is not None:
            self.trial_store.close()
