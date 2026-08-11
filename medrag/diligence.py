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

from . import agents
from .config import Config, load_config
from .context import Evidence, build_evidence, provenance_summary, render_context
from .documents import Retrieved
from .fda.drug_store import ApprovalAnswer
from .generator import SYSTEM_PROMPT, Answer
from .negative_evidence import NegativeEvidence, run_negative_pass
from .router import Route, Router
from .trials.anchors import anchor_for
from .trials.client import TrialRecord
from .trials.queries import resolve_query_set
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

#: Appended for any section carrying a deterministic approval block.
#:
#: The approval sentence is rendered from `ApprovalAnswer` in code and inserted
#: as a fixed string. Every guard around that answer — is_approved requiring
#: positive evidence, the four meanings of absence, tentative-approval-is-not-
#: approval — is a guard in CODE, and a model paraphrasing "no application
#: matched" as "not approved in the US" walks straight past all of them. So the
#: model is told, in the prompt, that the status is already stated and is not
#: its to write. `_flag_approval_overreach` then checks whether it complied,
#: because a prompt instruction is a request, not a guarantee.
APPROVAL_PROMPT_GUARD = """

REGULATORY STATUS IS ALREADY STATED, DETERMINISTICALLY, ELSEWHERE IN THIS \
SECTION. Do NOT state, summarise, infer or imply whether {asset} is approved, \
unapproved, cleared or authorised by the FDA or any other regulator, and do NOT \
describe the absence of a record as evidence of non-approval. Write about what \
the excerpts say — indications, sponsors, dates, trial and label content — and \
leave approval status alone."""

#: Phrasings that assert or imply non-approval. Checked against MODEL prose only
#: (never against the deterministic block, which is allowed to use the words in
#: order to deny them). Matching is deliberately literal and narrow: this exists
#: to catch the model overstepping, not to police an analyst's vocabulary.
_NON_APPROVAL_PHRASES = (
    "not approved", "unapproved", "not been approved", "never approved",
    "no fda approval", "lacks fda approval", "lacks approval", "without fda approval",
    "not fda-approved", "not fda approved", "is not authorised", "is not authorized",
    "not licensed", "no marketing authorisation", "no marketing authorization",
    "failed to gain approval", "denied approval", "rejected by the fda",
)


@dataclass
class DiligenceQuestion:
    id: str
    section: str
    question: str
    route: str = "auto"
    k: int = 6
    negative: bool = False
    status: list[str] | None = None      # per-question overall_status filter, e.g. RECRUITING
    aggregate: bool = False              # a landscape census: SQL counts over the full set
    biomarker: list[str] | None = None   # gating filters as "MARKER:STATUS" (e.g. "MSS:REQUIRED")


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
    aggregate: dict | None = None        # store.landscape() counts, for a census section
    #: The deterministic regulatory answer, rendered by the memo as a FIXED
    #: string from `ApprovalAnswer.render_lines()`. Never summarised by a model.
    approval: "ApprovalAnswer | None" = None


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
        status = q.get("status")
        if isinstance(status, str):
            status = [status]
        biomarker = q.get("biomarker")
        if isinstance(biomarker, str):
            biomarker = [biomarker]
        questions.append(
            DiligenceQuestion(
                id=q["id"],
                section=q.get("section", q["id"].replace("-", " ").title()),
                question=q["question"],
                route=str(q.get("route", "auto")).lower(),
                k=int(q.get("k", 6)),
                negative=bool(q.get("negative", False)),
                status=[str(s).upper() for s in status] if status else None,
                aggregate=bool(q.get("aggregate", False)),
                biomarker=[str(b) for b in biomarker] if biomarker else None,
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

    def __init__(self, cfg: Config | None = None, rag=None, trial_store: TrialStore | None = None,
                 fda_store=None, drug_store=None):
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

        # A memo built while part of the corpus is unreadable says so on its face.
        # The literature answers here were drawn from a smaller body of evidence
        # than the analyst thinks, and that is exactly the kind of silent
        # shortfall the warnings block exists to prevent.
        self.warnings.extend(self._corpus_warnings())

        self.trial_store = trial_store
        if self.trial_store is None:
            from .pipeline import TRIALS_DB
            from .trials.store import TrialStoreSchemaError

            db = self.cfg.raw_dir / TRIALS_DB
            if db.exists():
                # A stale trials.db must not crash the memo: degrade to a
                # literature-only run with the rebuild instruction surfaced.
                try:
                    self.trial_store = TrialStore(db, read_only=self.cfg.read_only)
                except TrialStoreSchemaError as exc:
                    self.warnings.append(str(exc).splitlines()[0])
            else:
                self.warnings.append("trial store not found — run `medrag trials` first")

        # The FDA store is optional too: a device asset benefits from it, a drug
        # asset simply has no clearances and the section stays empty.
        self.fda_store = fda_store
        if self.fda_store is None:
            from .fda.store import FDAStore, FDAStoreSchemaError
            from .pipeline import FDA_DB

            db = self.cfg.raw_dir / FDA_DB
            if db.exists():
                try:
                    self.fda_store = FDAStore(db, read_only=self.cfg.read_only)
                except FDAStoreSchemaError as exc:
                    self.warnings.append(str(exc).splitlines()[0])

        # The DRUG store, likewise optional. Its absence is never a finding
        # about the asset — `ApprovalAnswer.searched` carries that distinction
        # into the memo rather than letting an empty section imply anything.
        self.drug_store = drug_store
        if self.drug_store is None:
            from .fda.drug_store import DrugStore, DrugStoreSchemaError
            from .pipeline import DRUGS_DB

            db = self.cfg.raw_dir / DRUGS_DB
            if db.exists():
                try:
                    self.drug_store = DrugStore(db, read_only=self.cfg.read_only)
                except DrugStoreSchemaError as exc:
                    self.warnings.append(str(exc).splitlines()[0])

    # ------------------------------------------------------------ retrieval

    def _trials_for(self, question: str, asset: str, indication: str, filters: dict,
                    limit: int, statuses: list[str] | None = None) -> list[TrialRecord]:
        if self.trial_store is None:
            return []

        if filters.get("nct_ids"):
            found = [self.trial_store.get(n) for n in filters["nct_ids"]]
            return [f for f in found if f]

        # THE GATE. A structured query is only a query about something when
        # something anchors it; with neither an agent name this store can match
        # nor a query set it has actually ingested, `store.query` degrades to
        # `SELECT * FROM trials LIMIT k` and hands back whatever the store holds
        # most of. See trials/anchors.py for the three paths that produced eight
        # colorectal trials as evidence for a hidradenitis asset.
        anchor = anchor_for(asset, indication, self.trial_store)
        for note in anchor.notes():
            if note not in self.warnings:
                self.warnings.append(note)
        if not anchor:
            return []

        records = self.trial_store.query(
            intervention=asset or None,
            # Only an INGESTED set scopes anything. An ad-hoc key from an
            # unrecognised indication selects nothing, which reads identically
            # to a family searched and found empty — so it is dropped here and
            # reported as not-searched instead.
            query_set=anchor.query_set,
            phase=filters.get("phase"),
            statuses=statuses,
            stopped_only=bool(filters.get("stopped_only")),
            limit=limit,
        )
        # Structured filters can legitimately return nothing (no Phase 3 exists).
        # Fall back to free text so the section is not silently empty — over the
        # ANCHORS, never the question, and with every row re-checked against the
        # asset it claims to be evidence for.
        if not records:
            self._warn_collapsed_combination(asset, anchor.query_set)
            records = [
                r for r in self.trial_store.search(
                    anchor.search_text(), limit=limit, query_set=anchor.query_set,
                )
                if anchor.is_about(r)
            ]
        return records

    def _warn_collapsed_combination(self, asset: str, query_set: str | None) -> None:
        """Say WHICH agent of a combination emptied the result.

        A combination ANDs its agents, so one agent the registry never lists
        under any known name zeroes the whole query — and the caller then falls
        back to free text, which succeeds, which makes the collapse invisible.
        Naming the responsible agent turns "the structured query found nothing"
        into something an analyst can act on: either the asset is misspelt, or
        `config/agents.yaml` lacks the name this registry uses for it. The
        message itself lives in agents.py, shared with `claims._retrieve`.
        """
        if not asset or self.trial_store is None:
            return
        for note in agents.collapsed_combination_notes(
            self.trial_store.intervention_terms(asset, query_set=query_set), asset
        ):
            if note not in self.warnings:
                self.warnings.append(note)

    def _passages(self, question: str, k: int) -> list[Retrieved]:
        if self.rag is None:
            return []
        return self.rag.retriever.retrieve(question, k=k)

    def _fda_for(self, asset: str, filters: dict):
        """510(k) clearances for the asset, capped at cfg.fda_max_clearances.
        Matches on product code and device name — never the applicant, which
        fragments on live data. Returns the records and a provenance dict stating
        the sample against both the local-store total and the openFDA category
        total, so the memo never implies the sample is the whole category."""
        if self.fda_store is None:
            return [], {}
        code = filters.get("product_code")
        records = self.fda_store.clearances(
            product_code=code, device_name=asset or None,
            limit=self.cfg.fda_max_clearances,
        )
        if not code and records:
            code = records[0].product_code
        meta = {
            "fda_product_code": code or "",
            "n_fda_store_total": self.fda_store.clearances_total(product_code=code) if code
            else self.fda_store.clearances_total(device_name=asset or None),
            "n_fda_category_total": self.fda_store.category_total(code),
        }
        return records, meta

    def _drugs_for(self, asset: str, limit: int):
        """The drug applications for an asset, and the deterministic approval
        answer beside them.

        Both are returned because they do different jobs and must not be
        conflated: the applications become numbered EVIDENCE the model can cite
        ("[3] FDA DRUG APPROVAL — NDA 021923"), while the ApprovalAnswer is
        rendered as a fixed string the model never sees as something to
        summarise. When no store exists the answer still comes back, with
        `searched=False`, so the memo can say "not checked" rather than printing
        nothing and letting the silence read as a finding.
        """
        if self.drug_store is None or not asset:
            return [], ApprovalAnswer(asset=asset or "the asset")
        answer = self.drug_store.approval_answer(asset)
        return answer.applications[:limit], answer

    @staticmethod
    def _flag_approval_overreach(answer: ApprovalAnswer, text: str) -> str | None:
        """Did the model write the sentence it was told not to write?

        A prompt instruction is a request, not a guarantee — this codebase's own
        convention is that a guard the caller can bypass is decoration. So the
        model's prose is checked for non-approval phrasing whenever the
        deterministic answer does NOT support it, and a hit becomes a loud memo
        warning rather than a silent contradiction between two paragraphs of the
        same section.
        """
        if answer.is_approved:
            return None      # the claim would be about something else entirely
        lowered = (text or "").lower()
        hit = next((p for p in _NON_APPROVAL_PHRASES if p in lowered), None)
        if not hit:
            return None
        return (
            f"the generated prose for “{answer.asset}” contains the phrase “{hit}”, which "
            "states or implies non-approval. openFDA drugsFDA holding no matching "
            "application is NOT evidence of non-approval — see the regulatory status "
            "block, which is generated deterministically. Treat that sentence as "
            "unsupported."
        )

    # ------------------------------------------------------------ one section

    @staticmethod
    def _biomarker_filters(tokens: list[str] | None) -> list[tuple[str, list[str]]]:
        """Parse ['MSS:REQUIRED', 'MSS:ELIGIBLE_BY_EXCLUSION'] into
        [('MSS', ['REQUIRED', 'ELIGIBLE_BY_EXCLUSION'])] — multiple tokens
        naming the same marker are ORed together (a trial matching any one of
        them counts), so a question can ask for a marker stated directly OR
        stated by excluding its opposite without excluding either from the
        count. Different markers stay separate filters, ANDed. Ignore
        malformed tokens."""
        grouped: dict[str, list[str]] = {}
        for tok in tokens or []:
            if ":" not in tok:
                continue
            marker, status = tok.split(":", 1)
            marker = marker.strip().upper().replace(" ", "_")
            grouped.setdefault(marker, []).append(status.strip().upper())
        return list(grouped.items())

    def _landscape_section(self, q: DiligenceQuestion, rendered: str,
                           indication: str) -> SectionResult:
        """An aggregate census section: counts come from SQL over the full match
        set (store.landscape), and the listed trials are labelled a sample of the
        stated denominator. No model prose — the answer is the table."""
        agg = None
        if self.trial_store is not None:
            # Select the population the FETCH defined, by its recorded query set —
            # not a substring re-match over the free-text condition array. That
            # match ran different logic from the ingest and discarded 6,891 of
            # 12,092 colorectal trials (57%), including every trial registered as
            # "Colorectal Neoplasms". Same rule as build_landscape; see CLAUDE.md.
            agg = self.trial_store.landscape(
                query_set=resolve_query_set(indication).key if indication else None,
                biomarker_filters=self._biomarker_filters(q.biomarker),
                statuses=q.status,
                sample_limit=q.k,
            )
        sample = agg["sample"] if agg else []
        evidence = build_evidence(trials=sample, max_chars=self.cfg.max_context_chars)

        answer = Answer(text="", model="aggregate")
        report = ValidationReport(
            assessed=False,
            reason="aggregate section — counts are SQL over the full set, not model prose",
        )
        negative = None
        if q.negative:
            negative = run_negative_pass(
                claim=rendered, cfg=self.cfg, evidence=evidence,
                trial_store=self.trial_store, condition=indication or None,
                query_set=resolve_query_set(indication).key if indication else None,
                fda_store=None,
            )
        return SectionResult(
            question=q, rendered_question=rendered, answer=answer, evidence=evidence,
            validation=report, route=Route.STRUCTURED, route_method="aggregate",
            provenance=provenance_summary(evidence), negative=negative, aggregate=agg,
        )

    def run_question(self, q: DiligenceQuestion, asset: str, indication: str) -> SectionResult:
        rendered = q.question.format(asset=asset or "the asset",
                                     indication=indication or "the indication")

        # A landscape census is deterministic SQL, not a routed model answer.
        if q.aggregate:
            return self._landscape_section(q, rendered, indication)

        if q.route == "auto":
            decision = self.router.route(rendered)
            route, method = decision.route, decision.method
            filters = decision.filters
            needs_regulatory = decision.needs_regulatory
            needs_drug = decision.needs_drug_regulatory
        else:
            route, method = Route(q.route), "config"
            from .router import classify_by_rules, extract_filters

            filters = extract_filters(rendered)
            rules = classify_by_rules(rendered)
            needs_regulatory = rules.needs_regulatory
            needs_drug = rules.needs_drug_regulatory

        trials = (
            self._trials_for(rendered, asset, indication, filters, q.k, statuses=q.status)
            if route in (Route.STRUCTURED, Route.BOTH)
            else []
        )
        passages = self._passages(rendered, q.k) if route in (Route.SEMANTIC, Route.BOTH) else []
        fda, fda_meta = self._fda_for(asset, filters) if needs_regulatory else ([], {})
        drugs, approval = self._drugs_for(asset, q.k) if needs_drug else ([], None)

        evidence = build_evidence(trials=trials, passages=passages, fda=fda, drugs=drugs,
                                  max_chars=self.cfg.max_context_chars)
        # The model writes AROUND the regulatory status, never writes it.
        answer = self._answer(rendered, evidence,
                              approval_guard=asset if approval is not None else "")
        if approval is not None:
            note = self._flag_approval_overreach(approval, answer.text)
            if note and note not in self.warnings:
                self.warnings.append(note)
        # Validate against the assembled evidence, not the literature subset:
        # the markers the model saw are numbered across both stores.
        report = validate_answer(answer, evidence=evidence)

        negative = None
        if q.negative:
            # The FDA deterministic half always runs when a store is present, like
            # the stopped-trial half — a recall does not need the question to ask
            # for it. The device name is the asset string.
            negative = run_negative_pass(
                claim=rendered,
                cfg=self.cfg,
                evidence=evidence,
                trial_store=self.trial_store,
                intervention=asset or None,
                condition=indication or None,
                # The indication arm selects the population the FETCH defined,
                # like every other consumer. A substring over the condition
                # array missed 58% of the stopped trials it should have seen.
                query_set=resolve_query_set(indication).key if indication else None,
                fda_store=self.fda_store,
                product_code=filters.get("product_code"),
                device_name=asset or None,
            )

        provenance = provenance_summary(evidence)
        provenance.update(fda_meta)   # sample-vs-total counts for the FDA caveat

        return SectionResult(
            question=q,
            rendered_question=rendered,
            answer=answer,
            evidence=evidence,
            validation=report,
            route=route,
            route_method=method,
            provenance=provenance,
            negative=negative,
            approval=approval,
        )

    def _answer(self, question: str, evidence: list[Evidence],
                approval_guard: str = "") -> Answer:
        """Generate a grounded answer over provenance-labelled evidence."""
        if not evidence:
            # NOTHING FOUND, said in the shape the regulatory block uses — which
            # was the one part of an empty-asset memo that behaved correctly.
            #
            # An empty section with no explanation is its own version of the
            # defect the relevance floor fixed: before the floor, sections filled
            # with unrelated evidence; a bare blank would just move the same
            # misreading somewhere else. Absence has to be stated, and stated as
            # absence rather than as a finding.
            return Answer(
                text=(
                    "**Nothing found.** No stored trial record, regulatory record or "
                    "published passage was a close enough match to this question to be "
                    "used as evidence.\n\n"
                    "> This is NOT a finding that no such evidence exists. It means this "
                    "tool's stored snapshot contains nothing above the relevance "
                    "threshold for it. Four things it can mean: nothing has been "
                    "published or registered; it exists under a name this search did not "
                    "match; it has not been ingested into this snapshot; or it is in a "
                    "source this tool does not cover (see the coverage note)."
                ),
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
                    ) + (APPROVAL_PROMPT_GUARD.format(asset=approval_guard)
                         if approval_guard else ""),
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

        # Re-read the list AFTER the sections, not only before them. It was
        # snapshotted at construction, so every warning a section raised —
        # `_warn_collapsed_combination`, `_flag_approval_overreach`, and the
        # anchor notes — was appended to a list the memo had already copied and
        # reached no reader at all. The convention this codebase states for
        # guards applies to notices too: one that production cannot surface is
        # decoration.
        memo.warnings = list(self.warnings)

        return memo

    def _corpus_warnings(self) -> list[str]:
        """Plain-language note when part of the stored corpus could not be read.

        Checked here rather than at ingest so it reaches every memo, from the CLI
        and the app alike, however long ago the damage happened.
        """
        try:
            from .ingest.store import corpus_health
            from .pipeline import CORPUS_FILE

            health = corpus_health(self.cfg.raw_dir / CORPUS_FILE, self.cfg.passphrase)
        except Exception:
            return []  # a status note must never be the thing that fails a memo
        message = health.message()
        return [f"Stored research incomplete: {message}"] if message else []

    def close(self) -> None:
        if self.trial_store is not None:
            self.trial_store.close()
        if self.fda_store is not None:
            self.fda_store.close()
