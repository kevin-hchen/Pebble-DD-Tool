"""The approval statement is RENDERED, not written — end to end through a memo.

WHAT THIS FILE GUARDS

`ApprovalAnswer` carries every guard this codebase built around regulatory
status: `is_approved` needs positive evidence, absence carries its four
meanings, a tentative approval is not an approval. All of those are guards in
CODE. A model handed that object as context and asked to summarise it walks
straight past every one of them — "no application matched" becomes "not approved
in the US" in one paraphrase, and the memo now contains a false statement of
fact about an asset.

So the approval sentence is produced deterministically by
`ApprovalAnswer.render_lines()` and inserted into the memo as a fixed string.
The model may write around it. It may not write it.

The load-bearing test is
`test_a_memo_for_an_investigational_asset_never_implies_non_approval`, which
runs a real memo end to end for botensilimab — a genuinely investigational agent
with no US application, confirmed absent from live drugsFDA — and asserts the
RENDERED memo contains no phrasing implying non-approval. It was verified to
fail by writing such a sentence into the section; see the docstring there.

No network.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import netguard  # noqa: E402

netguard.install()
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures.openfda_drugs import DRUGSFDA_PAGE  # noqa: E402

from medrag.config import Config  # noqa: E402
from medrag.diligence import (  # noqa: E402
    DiligenceQuestion,
    DiligenceRunner,
    MemoResult,
    load_question_set,
)
from medrag.fda.drug_store import DrugStore  # noqa: E402
from medrag.fda.drugs import parse_application  # noqa: E402
from medrag.memo import render_markdown, render_pdf  # noqa: E402

#: Phrasings that assert or imply non-approval. Kept HERE as well as in
#: diligence.py on purpose: this list is the specification the memo must satisfy,
#: and a test that imported the implementation's own list would pass by
#: construction if someone quietly shortened it.
FORBIDDEN = (
    "not approved", "unapproved", "not been approved", "never approved",
    "no fda approval", "lacks fda approval", "lacks approval",
    "without fda approval", "not fda-approved", "not fda approved",
    "is not authorised", "is not authorized", "not licensed",
    "no marketing authorisation", "no marketing authorization",
    "failed to gain approval", "denied approval", "rejected by the fda",
    "has no approval", "is not an approved",
)

REGULATORY_Q = DiligenceQuestion(
    id="regulatory-status", section="US regulatory status",
    question=("What does the FDA record show for {asset} — approved, tentatively "
              "approved, or marketed under any application?"),
    route="structured", k=8,
)


def _drug_store() -> DrugStore:
    """A store that HAS been searched for both assets — so an empty result for
    botensilimab is 'searched and found nothing', the harder case."""
    store = DrugStore(Path(tempfile.mkdtemp()) / "drugs.db")
    store.upsert_applications(
        [a for a in (parse_application(r) for r in DRUGSFDA_PAGE["results"]) if a])
    store.record_search("pembrolizumab", reported_total=2, n_applications=1)
    store.record_search("botensilimab", reported_total=0, n_applications=0)
    return store


def _runner(drug_store=None) -> DiligenceRunner:
    cfg = Config(openai_api_key=None, data_dir=Path(tempfile.mkdtemp()))
    cfg.ensure_dirs()
    return DiligenceRunner(cfg, rag=None, trial_store=None, fda_store=None,
                           drug_store=drug_store if drug_store is not None else _drug_store())


def _memo_for(asset: str) -> tuple[MemoResult, str]:
    runner = _runner()
    section = runner.run_question(REGULATORY_Q, asset=asset, indication="")
    memo = MemoResult(asset=asset, indication="", question_set="test",
                      sections=[section], warnings=list(runner.warnings))
    return memo, render_markdown(memo)


def _approval_block(md: str) -> str:
    start = md.index("### Regulatory status")
    rest = md[start:]
    end = rest.find("\n## ")
    return rest[:end if end > 0 else len(rest)]


# ------------------------------------------------- the load-bearing assertion


def test_a_memo_for_an_investigational_asset_never_implies_non_approval():
    """botensilimab has no US application — confirmed absent from live drugsFDA.
    The rendered memo must contain no phrasing implying non-approval anywhere.

    VERIFIED TO FAIL: inserting `"Botensilimab is not approved in the US."` into
    the section's answer text makes this test fail on the phrase "not approved".
    """
    memo, md = _memo_for("botensilimab")
    lowered = md.lower()
    for phrase in FORBIDDEN:
        assert phrase not in lowered, (
            f"the rendered memo contains “{phrase}”, which states or implies "
            "non-approval from an empty drugsFDA result"
        )


def test_the_same_memo_states_all_four_meanings_of_absence():
    """Silence is not enough — the memo has to say what absence can mean, or a
    reader supplies the missing inference themselves."""
    _, md = _memo_for("botensilimab")
    block = _approval_block(md)
    for meaning in ("never submitted to the FDA",
                    "submitted and still under review, or refused",
                    "approved under a name this search did not match",
                    "approved outside the US"):
        assert meaning in block, f"absence meaning missing from the memo: {meaning}"
    assert "says nothing either way about approval status" in block


def test_the_pdf_carries_the_same_block_and_builds():
    memo, _ = _memo_for("botensilimab")
    out = Path(tempfile.mkdtemp()) / "memo.pdf"
    render_pdf(memo, out)
    assert out.read_bytes().startswith(b"%PDF")


def test_a_memo_with_no_drug_store_says_not_checked_rather_than_nothing():
    """The third outcome. An absent store is not a finding about the asset, and
    an empty section would read as one."""
    runner = DiligenceRunner(
        Config(openai_api_key=None, data_dir=Path(tempfile.mkdtemp())),
        rag=None, trial_store=None, fda_store=None, drug_store=None)
    section = runner.run_question(REGULATORY_Q, asset="botensilimab", indication="")
    md = render_markdown(MemoResult(asset="botensilimab", indication="",
                                    question_set="t", sections=[section]))
    assert "NOT checked" in md
    for phrase in FORBIDDEN:
        assert phrase not in md.lower()


# ------------------------------------------------- the four axes


def test_the_memo_renders_each_axis_rather_than_a_single_approved_yes():
    memo, md = _memo_for("pembrolizumab")
    block = _approval_block(md)
    assert "Submission status:" in block
    assert "Marketing status" in block
    assert "Application type:" in block
    assert "Label history:" in block
    assert "approved: yes" not in block.lower()


def test_approved_and_then_discontinued_is_stated_as_such():
    """One of the more informative things a diligence memo can say, and the one
    a boolean destroys."""
    _, md = _memo_for("norethindrone")
    block = _approval_block(md)
    assert "Discontinued" in block
    assert "approved and then withdrawn from marketing" in block
    assert "different fact from never approved" in block


def test_a_tentative_approval_is_rendered_as_not_an_approval():
    _, md = _memo_for("eluxadoline")
    block = _approval_block(md)
    assert "TENTATIVE approval" in block
    assert "NOT an approval" in block
    for phrase in FORBIDDEN:
        assert phrase not in block.lower()


def test_the_generic_mix_is_reported_because_an_anda_is_a_competitor():
    _, md = _memo_for("lidocaine")
    block = _approval_block(md)
    assert "Application type:" in block
    assert "ANDA" in block


# ------------------------------------------------- coverage


def test_the_approval_block_declares_what_is_not_searched():
    _, md = _memo_for("pembrolizumab")
    block = _approval_block(md)
    assert "Not searched:" in block
    for absent in ("FAERS", "Orange Book", "shortages"):
        assert absent in block, f"undeclared gap: {absent}"
    assert "US applications only" in block


def test_the_block_states_when_it_was_searched():
    _, md = _memo_for("pembrolizumab")
    assert "Searched: openFDA drugsFDA" in _approval_block(md)


# ------------------------------------------------- the model is kept out


def test_the_model_is_told_not_to_write_the_approval_sentence():
    """The prompt guard is only half of it — `_flag_approval_overreach` is the
    other half — but a section carrying an ApprovalAnswer must carry the
    instruction."""
    from medrag.diligence import APPROVAL_PROMPT_GUARD

    guard = APPROVAL_PROMPT_GUARD.format(asset="botensilimab")
    assert "Do NOT state, summarise, infer or imply" in guard
    assert "absence of a record as evidence of non-approval" in guard


def test_model_prose_implying_non_approval_is_flagged_not_silently_kept():
    """A prompt instruction is a request, not a guarantee. If the model writes
    the sentence anyway, the memo must say so rather than carry two paragraphs
    that contradict each other."""
    runner = _runner()
    answer = runner.drug_store.approval_answer("botensilimab")
    note = runner._flag_approval_overreach(
        answer, "Botensilimab is not approved in the United States.")
    assert note and "not approved" in note
    assert "NOT evidence of non-approval" in note

    clean = runner._flag_approval_overreach(
        answer, "Botensilimab is an anti-CTLA-4 antibody in Phase 2 development.")
    assert clean is None


def test_an_approved_asset_is_not_flagged_for_discussing_its_approval():
    """The check must not fire on a drug that IS approved — the phrase would be
    about something else entirely, and a false warning trains readers to ignore
    warnings."""
    runner = _runner()
    answer = runner.drug_store.approval_answer("pembrolizumab")
    assert runner._flag_approval_overreach(
        answer, "The combination is not approved for this indication.") is None


# ------------------------------------------------- the question set


def test_the_shipped_question_set_has_a_question_that_reaches_the_drug_store():
    """A store no question reaches is a store no memo uses."""
    from medrag.router import classify_by_rules

    qs = load_question_set()
    reaching = [q for q in qs.questions
                if classify_by_rules(q.question).needs_drug_regulatory]
    assert reaching, "no question in the shipped set routes to the drug store"
    assert any(q.id == "regulatory-status" for q in qs.questions)


def test_the_regulatory_question_is_not_phrased_as_a_yes_no():
    """Phrasing matters: "is it approved" invites an answer that treats an empty
    database as a no."""
    qs = load_question_set()
    q = next(q for q in qs.questions if q.id == "regulatory-status")
    assert not q.question.lower().startswith("is ")
    assert "what does the fda record show" in q.question.lower()


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
    print("\nall memo-approval tests passed" if not failures else f"\n{failures} test(s) failed")
    raise SystemExit(1 if failures else 0)
