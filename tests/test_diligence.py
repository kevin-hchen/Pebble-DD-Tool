"""Tests for evidence grading, the negative-evidence pass, the question set,
the diligence runner and memo export.

No network, no API key. The model halves are driven through mocked clients so
prompt construction and response handling are verified rather than assumed.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Direct runs do not load conftest.py, so the no-network guard is installed
# here too. See tests/netguard.py.
from tests import netguard  # noqa: E402

netguard.install()
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures.ctgov import PAGE_ONE, PAGE_TWO  # noqa: E402

from medrag.chunking import chunk_document  # noqa: E402
from medrag.config import Config  # noqa: E402
from medrag.context import build_evidence, provenance_summary, render_context  # noqa: E402
from medrag.diligence import (  # noqa: E402
    DEFAULT_QUESTION_SET,
    DiligenceQuestion,
    DiligenceRunner,
    MemoResult,
    load_question_set,
)
from medrag.documents import Chunk, Document, Retrieved  # noqa: E402
from medrag.evidence_grade import (  # noqa: E402
    distribution,
    grade_document,
    grade_publication,
    rerank_by_evidence,
)
from medrag.memo import export, render_markdown, render_pdf  # noqa: E402
from medrag.negative_evidence import (  # noqa: E402
    ContradictionHunter,
    find_stopped_trials,
    run_negative_pass,
)
from medrag.trials.client import parse_study  # noqa: E402
from medrag.trials.store import TrialStore  # noqa: E402


def _trial_store() -> TrialStore:
    store = TrialStore(Path(tempfile.mkdtemp()) / "t.db")
    records = []
    for page in (PAGE_ONE, PAGE_TWO):
        for study in page["studies"]:
            rec = parse_study(study)
            if rec:
                records.append(rec)
    store.upsert(records)
    return store


def _chunk(doc_id: str, key: str, tag: str, rank: int, score: float) -> Retrieved:
    return Retrieved(
        chunk=Chunk(
            chunk_id=f"{doc_id}::0",
            doc_id=doc_id,
            text=f"Finding from {doc_id}.",
            title=f"Paper {doc_id}",
            citation="Author et al., J Test, 2024",
            evidence_key=key,
            evidence_tag=tag,
            evidence_rank=rank,
        ),
        score=score,
    )


# --------------------------------------------------------------- grading


def test_publication_types_map_to_tiers():
    assert grade_publication(["Meta-Analysis"]).key == "meta-analysis"
    assert grade_publication(["Randomized Controlled Trial"]).key == "rct"
    assert grade_publication(["Observational Study"]).key == "cohort"
    assert grade_publication(["Case Reports"]).key == "case-series"
    assert grade_publication(["Editorial"]).key == "narrative"


def test_strongest_type_wins_when_several_present():
    """PubMed often types one record as both Meta-Analysis and Review."""
    g = grade_publication(["Review", "Meta-Analysis", "Journal Article"])
    assert g.key == "meta-analysis" and g.source == "metadata"


def test_title_fallback_when_no_publication_types():
    g = grade_publication([], title="A systematic review of SGLT2 inhibitors")
    assert g.key == "systematic-review" and g.source == "title"


def test_metadata_beats_title_cue():
    g = grade_publication(["Case Reports"], title="A randomized controlled trial of X")
    assert g.key == "case-series", "a real publication type must outrank a title guess"


def test_unclassified_when_nothing_matches():
    g = grade_publication([], title="Some paper")
    assert g.key == "unclassified" and g.tag == "UNGRADED" and g.source == "default"


def test_weak_evidence_flag():
    assert grade_publication(["Case Reports"]).is_weak
    assert grade_publication(["Editorial"]).is_weak
    assert not grade_publication(["Randomized Controlled Trial"]).is_weak


def test_grading_is_applied_at_ingest():
    doc = Document(
        doc_id="1", title="Trial of X", text="Background: A. Results: B.",
        meta={"publication_types": ["Randomized Controlled Trial"]},
    )
    chunks = chunk_document(doc, Config())
    assert chunks and all(c.evidence_key == "rct" and c.evidence_tag == "RCT" for c in chunks)


def test_grade_document_reads_meta():
    doc = Document(doc_id="1", title="X", text="Y", meta={"publication_types": ["Meta-Analysis"]})
    assert grade_document(doc).key == "meta-analysis"


def test_rerank_promotes_stronger_design_on_near_ties():
    retrieved = [
        _chunk("case", "case-series", "CASE REPORT", 6, 0.80),
        _chunk("meta", "meta-analysis", "META-ANALYSIS", 1, 0.75),
    ]
    assert [r.chunk.doc_id for r in rerank_by_evidence(retrieved)] == ["meta", "case"]


def test_rerank_does_not_override_relevance():
    """An off-topic meta-analysis must still lose to an on-topic case report."""
    retrieved = [
        _chunk("case", "case-series", "CASE REPORT", 6, 0.90),
        _chunk("meta", "meta-analysis", "META-ANALYSIS", 1, 0.40),
    ]
    assert [r.chunk.doc_id for r in rerank_by_evidence(retrieved)] == ["case", "meta"]


def test_rerank_weight_zero_is_a_no_op():
    retrieved = [
        _chunk("case", "case-series", "CASE REPORT", 6, 0.80),
        _chunk("meta", "meta-analysis", "META-ANALYSIS", 1, 0.75),
    ]
    assert [r.chunk.doc_id for r in rerank_by_evidence(retrieved, weight=0.0)] == ["case", "meta"]


def test_grade_tag_is_visible_in_context_and_bibliography():
    evidence = build_evidence(passages=[_chunk("m", "meta-analysis", "META-ANALYSIS", 1, 0.9)])
    assert "META-ANALYSIS" in render_context(evidence)
    assert "META-ANALYSIS" in evidence[0].bib_line()


def test_provenance_counts_weak_evidence():
    summary = provenance_summary(
        build_evidence(passages=[
            _chunk("a", "rct", "RCT", 3, 0.9),
            _chunk("b", "case-series", "CASE REPORT", 6, 0.8),
        ])
    )
    assert summary["n_weak_evidence"] == 1
    assert summary["evidence_tiers"] == {"RCT": 1, "CASE REPORT": 1}


def test_distribution_orders_strongest_first():
    grades = [grade_publication(["Case Reports"]), grade_publication(["Meta-Analysis"])]
    assert list(distribution(grades)) == ["Meta-analysis", "Case series or report"]


def test_old_chunks_without_grades_still_load():
    """An index built before grading existed must degrade, not fail."""
    chunk = Chunk.from_dict({"chunk_id": "a::0", "doc_id": "a", "text": "t", "legacy_field": 1})
    assert chunk.evidence_tag == "UNGRADED" and chunk.evidence_rank == 8


# ------------------------------------------------- negative evidence


def test_deterministic_half_finds_stopped_trials():
    stopped = find_stopped_trials(_trial_store(), intervention="Compound X").trials
    assert {s.record.nct_id for s in stopped} == {"NCT01234567", "NCT07654321"}


def test_stopped_trial_in_other_indication_is_not_hidden():
    """A trial of the same compound terminated in a DIFFERENT indication is
    among the most valuable things diligence can surface. Regression: these
    filters were ANDed, which silently hid it."""
    stopped = find_stopped_trials(
        _trial_store(), intervention="Compound X", condition="solid tumor"
    ).trials
    ids = {s.record.nct_id for s in stopped}
    assert "NCT07654321" in ids, "renal-impairment trial must survive an oncology query"
    assert "NCT01234567" in ids


def test_trials_with_a_stated_reason_come_first():
    stopped = find_stopped_trials(_trial_store(), intervention="Compound X").trials
    assert stopped[0].reason_is_stated


def test_missing_reason_is_reported_not_omitted():
    stopped = find_stopped_trials(_trial_store(), intervention="Compound X").trials
    silent = next(s for s in stopped if not s.reason_is_stated)
    assert silent.reason == "not stated by sponsor"


def test_no_trial_store_returns_empty_not_error():
    sweep = find_stopped_trials(None, intervention="X")
    assert sweep.trials == [] and sweep.n_total == 0


def test_contradiction_hunter_parses_findings():
    client = MagicMock()
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(content='{"findings": ['
        '{"point": "Effect not replicated in the larger cohort.", "citation": 1, '
        '"kind": "non-replication"}]}'))]
    client.chat.completions.create.return_value = completion

    with patch("openai.OpenAI", return_value=client):
        evidence = build_evidence(passages=[_chunk("a", "rct", "RCT", 3, 0.9)])
        findings, model, searched = ContradictionHunter(Config(openai_api_key="sk-t")).hunt(
            "Drug X works", evidence
        )

    assert searched and len(findings) == 1
    assert findings[0].kind == "non-replication" and findings[0].grade_tag == "RCT"

    prompt = client.chat.completions.create.call_args[1]["messages"][0]["content"]
    assert "WEAKENS" in prompt
    assert "empty findings list" in prompt, "the model must be allowed to find nothing"


def test_empty_findings_are_respected_not_backfilled():
    client = MagicMock()
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(content='{"findings": []}'))]
    client.chat.completions.create.return_value = completion

    with patch("openai.OpenAI", return_value=client):
        evidence = build_evidence(passages=[_chunk("a", "rct", "RCT", 3, 0.9)])
        result = run_negative_pass(
            "Drug X works", Config(openai_api_key="sk-t"),
            evidence=evidence,
        )
    assert result.findings == []
    assert "not the same as" in result.summary(), "must not imply nothing exists"


def test_findings_citing_unretrieved_sources_are_dropped():
    client = MagicMock()
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(content='{"findings": ['
        '{"point": "Real point.", "citation": 1},'
        '{"point": "Cites a source that was never retrieved.", "citation": 99}]}'))]
    client.chat.completions.create.return_value = completion

    with patch("openai.OpenAI", return_value=client):
        evidence = build_evidence(passages=[_chunk("a", "rct", "RCT", 3, 0.9)])
        findings, _, _ = ContradictionHunter(Config(openai_api_key="sk-t")).hunt(
            "claim", evidence
        )
    assert len(findings) == 1 and findings[0].citation == 1


def test_unassessed_is_distinct_from_nothing_found():
    """No model means the section was not checked. Reporting that as a clean
    result would be a false negative dressed as a pass."""
    evidence = build_evidence(passages=[_chunk("a", "rct", "RCT", 3, 0.9)])
    result = run_negative_pass("claim", Config(openai_api_key=None), evidence=evidence)
    assert result.searched is False and "Not assessed" in result.summary()


def test_note_when_all_stops_are_unexplained():
    store = _trial_store()
    result = run_negative_pass("claim", Config(openai_api_key=None), evidence=[],
                               trial_store=store, condition="renal insufficiency")
    assert result.stopped_trials and result.note
    assert "not evidence that the reason was benign" in result.note


def test_contradiction_findings_use_the_memo_numbering():
    """Regression: hunt() used to rebuild evidence from passages alone and number
    literature from 1, while the memo numbered trials first. In a route=both
    section a finding citing [4] displayed against source [4] — a trial —
    instead of the fourth literature passage. Fix: caller passes the assembled
    Evidence list so both sides share one numbering."""
    trial = parse_study(PAGE_ONE["studies"][1])   # NCT01234567, TERMINATED
    passages = [_chunk(str(30000000 + i), "rct", "RCT", 3, 0.9 - 0.01 * i) for i in range(5)]
    evidence = build_evidence(trials=[trial], passages=passages)
    # Sanity: with one trial, literature starts at index 2.
    assert [e.index for e in evidence] == [1, 2, 3, 4, 5, 6]
    assert evidence[3].identifier == "PMID 30000002", "index 4 must be the 3rd literature passage"

    # Model returns a finding citing [4]. Under the old code that would resolve
    # against the literature-only rebuild (also numbered from 1) and select
    # PMID 30000003; under the fix it must resolve against `evidence[3]`.
    client = MagicMock()
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(content='{"findings": ['
        '{"point": "Effect size not replicated.", "citation": 4, "kind": "non-replication"}]}'))]
    client.chat.completions.create.return_value = completion

    with patch("openai.OpenAI", return_value=client):
        findings, _, _ = ContradictionHunter(Config(openai_api_key="sk-t")).hunt(
            "Compound X works", evidence
        )
    assert len(findings) == 1
    assert findings[0].citation == 4
    assert findings[0].identifier == "PMID 30000002", \
        "the finding's identifier must match the section's [4], not a parallel numbering"

    # And the prompt actually renders trials ahead of literature: the model sees
    # exactly the numbering the memo will render.
    prompt = client.chat.completions.create.call_args[1]["messages"][0]["content"]
    assert prompt.index("[1]") < prompt.index("[2]") < prompt.index("[6]")
    assert "NCT01234567" in prompt and "PMID 30000002" in prompt


def test_malformed_model_output_does_not_crash():
    client = MagicMock()
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(content="not json"))]
    client.chat.completions.create.return_value = completion
    with patch("openai.OpenAI", return_value=client):
        evidence = build_evidence(passages=[_chunk("a", "rct", "RCT", 3, 0.9)])
        findings, _, searched = ContradictionHunter(Config(openai_api_key="sk-t")).hunt(
            "claim", evidence
        )
    assert findings == [] and searched is False


# ------------------------------------------------- question set


def test_default_question_set_loads():
    qs = load_question_set()
    assert len(qs) >= 8, "the plan calls for 8-10 questions"
    assert len({q.id for q in qs.questions}) == len(qs), "ids must be unique"
    assert all(q.section and q.question for q in qs.questions)


def test_question_set_is_config_not_hardcoded():
    assert DEFAULT_QUESTION_SET.exists() and DEFAULT_QUESTION_SET.suffix == ".yaml"


def test_question_placeholders_substitute():
    qs = load_question_set()
    q = next(q for q in qs.questions if "{asset}" in q.question)
    assert "{" not in q.question.format(asset="Compound X", indication="HFpEF")


def test_duplicate_ids_are_rejected():
    path = Path(tempfile.mkdtemp()) / "dupe.yaml"
    path.write_text(
        "version: 1\nname: t\nquestions:\n"
        "  - id: a\n    question: Q1\n  - id: a\n    question: Q2\n"
    )
    try:
        load_question_set(path)
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate ids must be rejected")


def test_missing_required_field_is_rejected():
    path = Path(tempfile.mkdtemp()) / "bad.yaml"
    path.write_text("version: 1\nname: t\nquestions:\n  - id: a\n")
    try:
        load_question_set(path)
    except ValueError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("a question with no text must be rejected")


# ------------------------------------------------- runner and memo


def _runner() -> DiligenceRunner:
    cfg = Config(openai_api_key=None, data_dir=Path(tempfile.mkdtemp()))
    cfg.ensure_dirs()
    return DiligenceRunner(cfg, rag=None, trial_store=_trial_store())


def test_runner_without_index_still_produces_trial_sections():
    """A registry-only run is useful; a missing index must not fail the memo."""
    runner = _runner()
    q = DiligenceQuestion(id="t", section="Trials", question="Which trials were terminated?",
                          route="structured", k=5)
    result = runner.run_question(q, asset="Compound X", indication="solid tumor")
    assert result.evidence and result.provenance["n_trials"] > 0


def test_config_route_overrides_router():
    runner = _runner()
    q = DiligenceQuestion(id="t", section="S", question="What is the mechanism?",
                          route="structured", k=3)
    assert runner.run_question(q, "Compound X", "").route_method == "config"


def test_full_memo_run_and_markdown_export():
    runner = _runner()
    memo = runner.run(asset="Compound X", indication="solid tumor", progress=False)

    assert len(memo.sections) >= 8
    md = render_markdown(memo)
    assert "# Diligence memo — Compound X" in md
    assert "## Coverage" in md
    assert "Contradicting or unsupportive evidence" in md
    assert "NCT01234567" in md, "every claim must be traceable to an NCT or PMID"
    assert "not investment advice" in md


def test_memo_sections_follow_question_set_order():
    """Fixed order is what makes two memos comparable."""
    qs = load_question_set()
    memo = _runner().run(asset="Compound X", indication="solid tumor", progress=False)
    assert [s.question.id for s in memo.sections] == [q.id for q in qs.questions]


def test_memo_surfaces_warnings_at_the_top():
    memo = MemoResult(asset="X", indication="Y", question_set="qs",
                      warnings=["trial store not found"])
    md = render_markdown(memo)
    assert md.index("Warnings") < md.index("Full source list")


def test_pdf_export_produces_a_real_pdf():
    memo = _runner().run(asset="Compound X", indication="solid tumor", progress=False)
    out = Path(tempfile.mkdtemp()) / "memo.pdf"
    render_pdf(memo, out)
    assert out.exists() and out.stat().st_size > 2000
    assert out.read_bytes().startswith(b"%PDF")


def test_export_writes_both_formats():
    memo = _runner().run(asset="Compound X", indication="", progress=False)
    paths = export(memo, Path(tempfile.mkdtemp()))
    assert paths["markdown"].exists() and paths["pdf"].exists()
    assert paths["markdown"].stem == "compound-x-diligence"


def test_pdf_survives_xml_hostile_titles():
    """An unescaped ampersand in a paper title would otherwise abort the build."""
    memo = _runner().run(asset="Smith & Co <Compound> \"X\"", indication="", progress=False)
    out = Path(tempfile.mkdtemp()) / "memo.pdf"
    render_pdf(memo, out)
    assert out.read_bytes().startswith(b"%PDF")


def test_coverage_counts_are_consistent():
    memo = _runner().run(asset="Compound X", indication="solid tumor", progress=False)
    cov = memo.coverage()
    assert cov["sections"] == len(memo.sections)
    assert cov["sections_with_evidence"] <= cov["sections"]
    assert cov["stopped_trials_found"] >= 1


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as exc:
                failures += 1
                print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print("\nall diligence tests passed" if not failures else f"\n{failures} test(s) failed")
    raise SystemExit(1 if failures else 0)
