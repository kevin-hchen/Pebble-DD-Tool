"""Tests for claim verification.

No network, no API key. The model is driven through a mocked client so the two
axes (support and independence), the deterministic overlays, NOT VERIFIABLE at
extraction, and the confidentiality gate are verified rather than assumed.

Support and independence are orthogonal, and independence obeys the same honesty
rule as support: absence of a disclosure is NO DISCLOSURE, never a flattering
INDEPENDENT. A claim can be SUPPORTED and COMPANY-LINKED, or PARTIALLY SUPPORTED
and NO DISCLOSURE, and the tests prove those pairs do not collapse.
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

from medrag.claims import (  # noqa: E402
    COMPANY_LINKED,
    CONTRADICTED,
    INDEP_NA,
    INDEPENDENT,
    MIXED,
    NO_DISCLOSURE,
    NOT_FOUND,
    NOT_VERIFIABLE,
    PARTIAL,
    SUPPORTED,
    UNVERIFIED,
    ClaimReport,
    ClaimVerdict,
    ClaimVerifier,
    ConfirmationRequired,
    ExtractedClaim,
    classify_claim,
    extract_claims,
    is_company_source,
    not_verifiable_verdict,
    parse_claims_text,
    requires_confirmation,
    source_linkage,
    transmission_notice,
    triage_claims,
)
from medrag.claims_memo import export, render_markdown, render_pdf  # noqa: E402
from medrag.config import Config  # noqa: E402
from medrag.context import build_evidence  # noqa: E402
from medrag.documents import Chunk, Retrieved  # noqa: E402
from medrag.trials.client import parse_study  # noqa: E402
from medrag.trials.store import TrialStore  # noqa: E402

# --------------------------------------------------------------- helpers


def _client(content: str) -> MagicMock:
    client = MagicMock()
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(content=content))]
    client.chat.completions.create.return_value = completion
    return client


def _client_seq(contents: list[str]) -> MagicMock:
    client = MagicMock()

    def _make(content):
        c = MagicMock()
        c.choices = [MagicMock(message=MagicMock(content=content))]
        return c

    client.chat.completions.create.side_effect = [_make(c) for c in contents]
    return client


def _lit(doc_id: str, text: str, title: str = "Paper", citation: str = "Author et al., J Test, 2024",
         tag: str = "RCT", key: str = "rct", rank: int = 3, score: float = 0.9,
         disclosure: str = "", disclosure_independent: bool = False) -> Retrieved:
    return Retrieved(
        chunk=Chunk(
            chunk_id=f"{doc_id}::0", doc_id=doc_id, text=text, title=title,
            citation=citation, evidence_key=key, evidence_tag=tag, evidence_rank=rank,
            disclosure=disclosure, disclosure_independent=disclosure_independent,
        ),
        score=score,
    )


def _example_trial():
    # NCT01234567: Compound X, TERMINATED, sponsor "Example Therapeutics" (INDUSTRY).
    return parse_study(PAGE_ONE["studies"][1])


def _rival_trial():
    # NCT05555555: Compound Y, sponsor "Rival Biosciences" — a named sponsor that
    # is not the manufacturer, so independent of it.
    return parse_study(PAGE_TWO["studies"][1])


def _trial_store() -> TrialStore:
    store = TrialStore(Path(tempfile.mkdtemp()) / "t.db")
    records = [parse_study(s) for s in PAGE_ONE["studies"]]
    store.upsert([r for r in records if r])
    return store


def _cfg_remote() -> Config:
    return Config(openai_api_key="sk-test")


# --------------------------------------------------------------- the support axis


def test_supported_support():
    evidence = build_evidence(passages=[_lit("111", "Drug X reduced cardiovascular events.")])
    client = _client('{"verdict": "supported", "citations": [1], "rationale": "Independent [1]."}')
    v = classify_claim("Drug X reduces cardiovascular events.", evidence, _cfg_remote(),
                       company="Unrelated Co", client=client)
    assert v.support == SUPPORTED and v.assessed


def test_partially_supported_support():
    evidence = build_evidence(passages=[_lit("111", "Benefit seen in a small subgroup only.")])
    client = _client('{"verdict": "partially_supported", "citations": [1], "rationale": "r"}')
    v = classify_claim("Drug X benefits all patients.", evidence, _cfg_remote(), client=client)
    assert v.support == PARTIAL


def test_contradicted_support():
    evidence = build_evidence(passages=[_lit("111", "The trial found no benefit over placebo.")])
    client = _client('{"verdict": "contradicted", "citations": [1], "rationale": "r"}')
    v = classify_claim("Drug X is highly effective.", evidence, _cfg_remote(), client=client)
    assert v.support == CONTRADICTED
    assert v.independence == INDEP_NA, "independence is about support, not contradiction"


def test_not_found_support_from_model():
    evidence = build_evidence(passages=[_lit("111", "Unrelated pharmacokinetics discussion.")])
    client = _client('{"verdict": "not_found", "citations": [], "rationale": "r"}')
    v = classify_claim("Drug X cures the disease.", evidence, _cfg_remote(), client=client)
    assert v.support == NOT_FOUND and v.independence == INDEP_NA


# --------------------------------------------------------------- NOT FOUND vs CONTRADICTED


def test_empty_retrieval_is_not_found_without_calling_the_model():
    client = _client('{"verdict": "contradicted", "citations": [1]}')
    v = classify_claim("Drug X works.", [], _cfg_remote(), client=client)
    assert v.support == NOT_FOUND
    client.chat.completions.create.assert_not_called()


def test_not_found_never_becomes_contradicted():
    evidence = build_evidence(passages=[_lit("111", "Off-topic content.")])
    client = _client('{"verdict": "not_found", "citations": []}')
    v = classify_claim("Drug X halves mortality.", evidence, _cfg_remote(), client=client)
    assert v.support == NOT_FOUND and v.support != CONTRADICTED


# --------------------------------------------------------------- the independence axis


def test_company_linked_from_trial_sponsor():
    """One manufacturer trial as sole support is SUPPORTED (support) and
    COMPANY-LINKED (independence) — two facts, not one."""
    evidence = build_evidence(trials=[_example_trial()])
    client = _client('{"verdict": "supported", "citations": [1], "rationale": "Sponsor trial [1]."}')
    v = classify_claim("Compound X shows anti-tumor activity.", evidence, _cfg_remote(),
                       company="Example Therapeutics", client=client)
    assert v.support == SUPPORTED
    assert v.independence == COMPANY_LINKED
    assert v.n_company == 1 and v.source_count == 1
    assert v.independence_display() == "COMPANY-LINKED (1 of 1)"


def test_no_disclosure_is_the_honest_default():
    """Literature with no funding/COI disclosure is NO DISCLOSURE — NOT
    INDEPENDENT. Absence of a disclosure is not evidence of independence."""
    evidence = build_evidence(passages=[_lit("111", "Independent-sounding cohort with no disclosure.")])
    client = _client('{"verdict": "supported", "citations": [1], "rationale": "r"}')
    v = classify_claim("Drug X works.", evidence, _cfg_remote(),
                       company="Example Therapeutics", client=client)
    assert v.independence == NO_DISCLOSURE, "no disclosure must never read as INDEPENDENT"
    assert v.independence_display() == "NO DISCLOSURE (1 of 1)"


def test_independent_requires_positive_evidence():
    """INDEPENDENT is emitted only on positive evidence — here, a disclosure flag
    set at ingest from a named non-industry funder or a no-conflict statement."""
    evidence = build_evidence(passages=[
        _lit("111", "Result text.", disclosure="Supported by the National Cancer Institute.",
             disclosure_independent=True)
    ])
    client = _client('{"verdict": "supported", "citations": [1], "rationale": "r"}')
    v = classify_claim("Drug X works.", evidence, _cfg_remote(),
                       company="Example Therapeutics", client=client)
    assert v.independence == INDEPENDENT
    assert v.independence_display() == "INDEPENDENT (1 of 1)"


def test_trial_sponsored_by_another_party_is_independent():
    evidence = build_evidence(trials=[_rival_trial()])
    client = _client('{"verdict": "supported", "citations": [1], "rationale": "Independent [1]."}')
    v = classify_claim("The mechanism is active.", evidence, _cfg_remote(),
                       company="Example Therapeutics", client=client)
    assert v.independence == INDEPENDENT


def test_mixed_independence_reports_its_counts():
    """A manufacturer trial plus an independently sponsored trial, both cited,
    is MIXED with the split spelled out."""
    evidence = build_evidence(trials=[_example_trial(), _rival_trial()])
    client = _client('{"verdict": "supported", "citations": [1, 2], "rationale": "Both [1][2]."}')
    v = classify_claim("The compound class is active.", evidence, _cfg_remote(),
                       company="Example Therapeutics", client=client)
    assert v.independence == MIXED
    assert v.n_company == 1 and v.n_independent == 1 and v.source_count == 2
    assert v.independence_display() == "MIXED (1 company-linked, 1 independent)"


def test_disclosure_blob_catches_company_when_cited_chunk_does_not():
    """The bug this change fixes: the cited chunk is a Results sentence with no
    funder in it, but the document-level disclosure blob carries 'Funded by
    Example Therapeutics' from the Conclusions. The source must read
    COMPANY-LINKED, not INDEPENDENT."""
    ev = build_evidence(passages=[
        _lit("111", "Sensitivity was 92.3% for cancer detection.",  # result chunk, no funder
             disclosure="Funded by Example Therapeutics; ClinicalTrials.gov number NCT00000000.")
    ])[0]
    assert is_company_source(ev, "Example Therapeutics")
    assert source_linkage(ev, "Example Therapeutics") == COMPANY_LINKED


def test_support_and_independence_are_orthogonal():
    """PARTIALLY SUPPORTED and a non-N/A independence at once: a numeric mismatch
    against a source that carries no disclosure."""
    evidence = build_evidence(passages=[_lit("111", "Reported sensitivity was 89% in the cohort.")])
    client = _client('{"verdict": "supported", "citations": [1], "rationale": "r"}')
    v = classify_claim("Sensitivity is 95% in the pivotal study.", evidence, _cfg_remote(),
                       company="Example Therapeutics", client=client)
    assert v.support == PARTIAL, "numeric mismatch downgrades support"
    assert v.independence == NO_DISCLOSURE, "independence is assessed independently of support"


# --------------------------------------------------------------- company-source detection


def test_company_source_flag_on_trial_sponsor():
    trial = build_evidence(trials=[_example_trial()])[0]
    assert is_company_source(trial, "Example Therapeutics")
    assert is_company_source(trial, "Example Therapeutics, Inc.")
    assert not is_company_source(trial, "Boehringer Ingelheim")


def test_company_source_flag_on_literature_disclosure():
    funded = build_evidence(passages=[
        _lit("111", "A result with no funder in this chunk.",
             disclosure="This study was funded by Example Therapeutics.")
    ])[0]
    independent = build_evidence(passages=[_lit("222", "An academic cohort.")])[0]
    assert is_company_source(funded, "Example Therapeutics")
    assert not is_company_source(independent, "Example Therapeutics")


# --------------------------------------------------------------- numeric grounding


def test_numeric_mismatch_downgrades_to_partial():
    evidence = build_evidence(passages=[_lit("111", "Reported sensitivity was 89% in the cohort.")])
    client = _client('{"verdict": "supported", "citations": [1], "rationale": "r"}')
    v = classify_claim("Sensitivity is 95% in the pivotal study.", evidence, _cfg_remote(),
                       client=client)
    assert v.support == PARTIAL
    assert "95" in v.claim_figures and "89" in v.source_figures


def test_matching_figure_stays_supported():
    evidence = build_evidence(passages=[_lit("111", "Reported sensitivity was 89% in the cohort.")])
    client = _client('{"verdict": "supported", "citations": [1], "rationale": "r"}')
    v = classify_claim("Sensitivity is 89%.", evidence, _cfg_remote(), client=client)
    assert v.support == SUPPORTED


# --------------------------------------------------------------- NOT VERIFIABLE


def test_extraction_flags_unverifiable_claims():
    payload = ('{"claims": ['
               '{"text": "Sensitivity is 94% in the pivotal study.", "verifiable": true},'
               '{"text": "Best-in-class accuracy.", "verifiable": false, '
               '"reason": "no measurable content"}]}')
    with patch("openai.OpenAI") as OpenAI:
        OpenAI.return_value = _client(payload)
        claims = extract_claims("deck text", _cfg_remote(), confirmed=True)
    assert [c.verifiable for c in claims] == [True, False]
    assert claims[1].reason == "no measurable content"


def test_triage_marks_unverifiable_claims():
    client = _client('{"assessments": [{"verifiable": true}, '
                     '{"verifiable": false, "reason": "marketing language"}]}')
    tagged = triage_claims(["Sensitivity is 94%.", "Clinically proven."], _cfg_remote(), client)
    assert tagged[0].verifiable and not tagged[1].verifiable


def test_triage_fails_open_on_bad_output():
    client = _client("not json")
    tagged = triage_claims(["A specific 94% claim."], _cfg_remote(), client)
    assert tagged[0].verifiable


def test_not_verifiable_verdict_is_recorded_not_dropped():
    v = not_verifiable_verdict("Best-in-class accuracy.", "no measurable content")
    assert v.support == NOT_VERIFIABLE and v.assessed
    assert v.independence == INDEP_NA


def test_verify_records_unverifiable_without_touching_the_model():
    store = _trial_store()
    with patch("openai.OpenAI") as OpenAI:
        mock = OpenAI.return_value
        verifier = ClaimVerifier(_cfg_remote(), rag=None, trial_store=store)
        try:
            report = verifier.verify(
                [ExtractedClaim(text="Best-in-class.", verifiable=False, reason="marketing")],
                asset="Compound X", confirmed=True,
            )
        finally:
            verifier.close()
    assert report.verdicts[0].support == NOT_VERIFIABLE
    mock.chat.completions.create.assert_not_called()


def test_verify_triages_plain_strings_and_flags_the_vague_one():
    store = _trial_store()
    triage = '{"assessments": [{"verifiable": true}, {"verifiable": false, "reason": "vague"}]}'
    classify = '{"verdict": "not_found", "citations": []}'
    with patch("openai.OpenAI") as OpenAI:
        OpenAI.return_value = _client_seq([triage, classify])
        verifier = ClaimVerifier(_cfg_remote(), rag=None, trial_store=store)
        try:
            report = verifier.verify(
                ["Compound X was studied in solid tumors.", "World-class team."],
                asset="Compound X", indication="solid tumor", confirmed=True,
            )
        finally:
            verifier.close()
    supports = [v.support for v in report.verdicts]
    assert supports[1] == NOT_VERIFIABLE


# --------------------------------------------------------------- shared numbering


def test_citations_use_the_assembled_evidence_numbering():
    trial = _example_trial()
    evidence = build_evidence(trials=[trial], passages=[_lit("30000000", "Independent result.")])
    assert [e.index for e in evidence] == [1, 2]

    client = _client('{"verdict": "supported", "citations": [2], "rationale": "Independent [2]."}')
    v = classify_claim("Compound X works.", evidence, _cfg_remote(), client=client)
    assert v.citations == [2]
    assert v.cited_evidence[0].identifier == "PMID 30000000"

    # Joined across roles on purpose: the property is what the model SAW, which
    # must hold however instructions and data are split between system and user.
    sent = "\n".join(
        m["content"] for m in client.chat.completions.create.call_args[1]["messages"]
    )
    assert sent.index("[1]") < sent.index("[2]")
    assert "NCT01234567" in sent and "PMID 30000000" in sent


def test_malformed_model_output_is_unverified_not_a_guess():
    evidence = build_evidence(passages=[_lit("111", "One passage.")])
    v = classify_claim("Drug X works.", evidence, _cfg_remote(), client=_client("not json"))
    assert v.support == UNVERIFIED and v.assessed is False


# --------------------------------------------------------------- confidentiality


def test_local_provider_transmits_nothing_and_skips_confirmation():
    for cfg in (Config(provider="none"), Config(provider="ollama"),
                Config(openai_api_key="sk-test", offline=True)):
        notice = transmission_notice(cfg, ["a secret claim"])
        assert notice.local
        assert not requires_confirmation(cfg)
        assert "Nothing will leave this machine" in notice.render()


def test_remote_provider_requires_confirmation_and_shows_what_is_sent():
    cfg = _cfg_remote()
    assert requires_confirmation(cfg)
    notice = transmission_notice(cfg, ["Sensitivity is 95%.", "No serious adverse events."])
    text = notice.render()
    assert not notice.local
    assert "Sensitivity is 95%." in text and "No serious adverse events." in text


def test_verify_does_not_transmit_before_confirmation():
    store = _trial_store()
    with patch("openai.OpenAI") as OpenAI:
        mock = OpenAI.return_value
        verifier = ClaimVerifier(_cfg_remote(), rag=None, trial_store=store)
        try:
            raised = False
            try:
                verifier.verify(["Compound X works."], asset="Compound X", confirmed=False)
            except ConfirmationRequired:
                raised = True
            assert raised, "a remote run must refuse to proceed unconfirmed"
            mock.chat.completions.create.assert_not_called()
        finally:
            verifier.close()


def test_local_verify_runs_unconfirmed_but_leaves_claims_unjudged():
    store = _trial_store()
    verifier = ClaimVerifier(Config(provider="none"), rag=None, trial_store=store)
    try:
        report = verifier.verify(["Compound X works."], asset="Compound X",
                                 indication="solid tumor", confirmed=False)
    finally:
        verifier.close()
    assert report.verdicts[0].support == UNVERIFIED
    assert report.verdicts[0].assessed is False


def test_extract_claims_is_gated_before_transmission():
    cfg = _cfg_remote()
    with patch("openai.OpenAI") as OpenAI:
        OpenAI.return_value = _client('{"claims": []}')
        raised = False
        try:
            extract_claims("deck text", cfg, confirmed=False)
        except ConfirmationRequired:
            raised = True
        assert raised
        OpenAI.return_value.chat.completions.create.assert_not_called()


def test_parse_claims_text_skips_blanks_and_comments():
    text = "# a comment\nClaim one\n\n  Claim two  \n# another\n"
    assert parse_claims_text(text) == ["Claim one", "Claim two"]


# --------------------------------------------------------------- report & export


def _report() -> ClaimReport:
    ev_company = build_evidence(trials=[_example_trial()])
    ev_lit = build_evidence(passages=[_lit("30000000", "Independent cohort, sensitivity 89%.")])
    company = ClaimVerdict(claim="Compound X shows activity.", support=SUPPORTED,
                           independence=COMPANY_LINKED, evidence=ev_company, citations=[1],
                           company_sources=[1], n_company=1, source_count=1,
                           rationale="Sponsor trial [1].", model="gpt-4o-mini")
    partial = ClaimVerdict(claim="Sensitivity is 95%.", support=PARTIAL, independence=NO_DISCLOSURE,
                           evidence=ev_lit, citations=[1], n_no_disclosure=1, source_count=1,
                           claim_figures=["95"], source_figures=["89"], model="gpt-4o-mini")
    nv = not_verifiable_verdict("Best-in-class accuracy.", "no measurable content")
    return ClaimReport(asset="Compound X", indication="solid tumor",
                       company="Example Therapeutics", model="gpt-4o-mini", embedder="test",
                       verdicts=[company, partial, nv])


def test_support_and_independence_counts():
    report = _report()
    assert report.support_counts()[SUPPORTED] == 1
    assert report.support_counts()[PARTIAL] == 1
    assert report.support_counts()[NOT_VERIFIABLE] == 1
    assert report.independence_counts()[COMPANY_LINKED] == 1
    assert report.independence_counts()[NO_DISCLOSURE] == 1


def test_markdown_export_has_both_axes_as_columns():
    md = render_markdown(_report())
    assert "| # | Claim | Support | Independence | Sources |" in md
    assert "COMPANY-LINKED (1 of 1)" in md
    assert "NO DISCLOSURE (1 of 1)" in md
    assert NOT_VERIFIABLE in md
    assert "NCT01234567" in md, "every verdict must be traceable to an NCT or PMID"
    assert "no specific, checkable assertion" in md
    assert "not investment advice" in md


def test_pdf_export_produces_a_real_pdf():
    out = Path(tempfile.mkdtemp()) / "claims.pdf"
    render_pdf(_report(), out)
    assert out.exists() and out.read_bytes().startswith(b"%PDF")
    assert out.stat().st_size > 1500


def test_export_writes_both_formats():
    paths = export(_report(), Path(tempfile.mkdtemp()))
    assert paths["markdown"].exists() and paths["pdf"].exists()
    assert paths["markdown"].stem == "example-therapeutics-claims"


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
    print("\nall claim tests passed" if not failures else f"\n{failures} test(s) failed")
    raise SystemExit(1 if failures else 0)
