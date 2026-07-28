"""Privacy and key-safety tests.

These turn claims about safety into assertions. The two questions worth testing
are different and both matter:

  1. Does the API key stay put? Never in a memo, a log, a traceback, an index,
     or a committed file.
  2. What actually leaves the machine? The corpus is public, so the sensitive
     part is the QUERY - it reveals which asset is being looked at, and when.
     These tests pin down exactly what is transmitted and to whom.

Nothing here contacts a real service; every client is a mock whose calls are
inspected.
"""

from __future__ import annotations

import io
import json
import logging
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures.ctgov import PAGE_ONE  # noqa: E402

from medrag.config import Config, load_config  # noqa: E402
from medrag.context import build_evidence  # noqa: E402
from medrag.diligence import DiligenceRunner, DiligenceQuestion  # noqa: E402
from medrag.documents import Chunk, Document, Retrieved  # noqa: E402
from medrag.generator import Generator  # noqa: E402
from medrag.ingest import pubmed  # noqa: E402
from medrag.ingest.store import save_corpus  # noqa: E402
from medrag.memo import render_markdown  # noqa: E402
from medrag.negative_evidence import ContradictionHunter  # noqa: E402
from medrag.providers import make_client  # noqa: E402
from medrag.router import Router  # noqa: E402
from medrag.setup_env import read_env, write_env  # noqa: E402
from medrag.trials import client as ctgov  # noqa: E402
from medrag.trials.client import parse_study  # noqa: E402
from medrag.trials.store import TrialStore  # noqa: E402
from medrag.vectorstore import VectorStore  # noqa: E402
from medrag.embeddings import HashingEmbedder  # noqa: E402
from medrag.chunking import chunk_document  # noqa: E402

SECRET = "gsk_THISISTHESECRETKEYVALUE1234567890"
REPO = Path(__file__).resolve().parents[1]


def _passages(n: int = 2) -> list[Retrieved]:
    return [
        Retrieved(
            chunk=Chunk(
                chunk_id=f"c{i}",
                doc_id=str(30000000 + i),
                text=f"Passage {i}: hazard ratio 0.79 reported.",
                title=f"Paper {i}",
            ),
            score=0.9,
        )
        for i in range(n)
    ]


# ------------------------------------------------------- 1. the key stays put


def test_key_absent_from_config_repr_and_str():
    cfg = Config(provider="groq", openai_api_key=SECRET, passphrase="hunter2")
    assert SECRET not in repr(cfg)
    assert SECRET not in str(cfg)
    assert "hunter2" not in repr(cfg)


def test_key_absent_from_a_traceback():
    """A crash inside a function holding the config must not print the key."""
    cfg = Config(provider="groq", openai_api_key=SECRET)

    def explode(config):
        raise ValueError(f"failed with {config!r}")

    try:
        explode(cfg)
    except ValueError as exc:
        import traceback

        text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        assert SECRET not in text


def test_key_absent_from_stdout_during_a_run():
    """Nothing on the console should carry the key, including progress lines."""
    cfg = Config(provider="groq", openai_api_key=SECRET, data_dir=Path(tempfile.mkdtemp()))
    cfg.ensure_dirs()
    buf_out, buf_err = io.StringIO(), io.StringIO()
    with redirect_stdout(buf_out), redirect_stderr(buf_err):
        with patch("openai.OpenAI", return_value=MagicMock()):
            Generator(cfg)
            Router(cfg)
            ContradictionHunter(cfg)
    assert SECRET not in buf_out.getvalue() + buf_err.getvalue()


def test_key_absent_from_log_records():
    cfg = Config(provider="groq", openai_api_key=SECRET)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("medrag.test")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        logger.debug("config is %r", cfg)
    finally:
        logger.removeHandler(handler)
    assert SECRET not in stream.getvalue()


def test_key_absent_from_the_stored_corpus_and_index():
    d = Path(tempfile.mkdtemp())
    doc = Document(doc_id="1", title="T", text="Background: x. Results: y.")
    save_corpus([doc], d / "corpus.jsonl")

    emb = HashingEmbedder(dim=64)
    chunks = chunk_document(doc, Config())
    store = VectorStore(dim=emb.dim, embedder_name=emb.name)
    store.add(chunks, emb.embed([c.text for c in chunks]))
    store.save(d)

    for f in d.rglob("*"):
        if f.is_file():
            assert SECRET.encode() not in f.read_bytes(), f"{f.name} contains the key"


def test_key_absent_from_the_generated_memo():
    from medrag.diligence import MemoResult

    memo = MemoResult(asset="Compound X", indication="y", question_set="qs",
                      model="llama-3.3-70b-versatile")
    assert SECRET not in render_markdown(memo)


def test_env_file_is_private_and_key_never_echoed_back_in_full():
    d = Path(tempfile.mkdtemp())
    path = d / ".env"
    write_env({"MEDRAG_PROVIDER": "groq", "GROQ_API_KEY": SECRET}, path)
    assert oct(path.stat().st_mode)[-3:] == "600"
    # read_env is used by the app only to check presence, never to display.
    assert read_env(path)["GROQ_API_KEY"] == SECRET  # readable by the owner alone


def test_env_is_gitignored():
    ignored = (REPO / ".gitignore").read_text()
    assert ".env" in ignored


def test_git_would_not_track_env():
    """Belt and braces: ask git itself, not just read .gitignore."""
    try:
        subprocess.run(["git", "init", "-q"], cwd=REPO, capture_output=True, timeout=20)
        result = subprocess.run(
            ["git", "check-ignore", "-q", ".env"], cwd=REPO, capture_output=True, timeout=20
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return  # git unavailable; the .gitignore test above still applies
    assert result.returncode == 0, "git does not consider .env ignored"


# --------------------------------------------- 2. what leaves the machine


def test_transmitted_payload_contains_only_question_and_evidence():
    """Pin down exactly what a provider receives: the question and the cited
    passages. Nothing about the user, the machine, or other assets."""
    client = MagicMock()
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(content="Answer [1]."))]
    completion.usage = MagicMock(prompt_tokens=1, completion_tokens=1)
    client.chat.completions.create.return_value = completion

    cfg = Config(provider="groq", openai_api_key=SECRET)
    with patch("openai.OpenAI", return_value=client):
        Generator(cfg).generate("Does Compound X work in solid tumors?", _passages())

    sent = json.dumps(client.chat.completions.create.call_args[1])
    assert "Compound X" in sent, "the question is transmitted — this is the real exposure"
    assert "hazard ratio 0.79" in sent, "cited passages are transmitted"
    assert SECRET not in sent, "the key travels in the auth header, not the body"


def test_offline_mode_transmits_nothing_anywhere():
    """The strongest privacy guarantee the tool offers, asserted end to end."""
    cfg = Config(provider="groq", openai_api_key=SECRET, offline=True,
                 data_dir=Path(tempfile.mkdtemp()))
    cfg.ensure_dirs()

    with patch("openai.OpenAI") as openai_ctor, \
         patch.object(pubmed.requests, "get") as pubmed_get, \
         patch.object(ctgov.requests, "get") as ctgov_get:
        assert make_client(cfg) is None
        assert Generator(cfg).client is None
        assert Router(cfg).client is None
        assert ContradictionHunter(cfg).client is None

        for fn, args in ((pubmed.search_pubmed, ("q",)), (ctgov.search_trials, ())):
            try:
                fn(*args, cfg=cfg) if fn is pubmed.search_pubmed else fn(offline=True)
            except RuntimeError:
                pass

        openai_ctor.assert_not_called()
        pubmed_get.assert_not_called()
        ctgov_get.assert_not_called()


def test_free_provider_key_is_not_sent_to_openai():
    """Selecting Groq must not leave a client pointed at api.openai.com."""
    with patch("openai.OpenAI") as ctor:
        make_client(Config(provider="groq", openai_api_key=SECRET))
    assert ctor.call_args[1]["base_url"].startswith("https://api.groq.com")


def test_local_provider_sends_to_localhost_only():
    with patch("openai.OpenAI") as ctor:
        make_client(Config(provider="ollama"))
    assert "localhost" in ctor.call_args[1]["base_url"]


def test_ncbi_email_is_omitted_unless_deliberately_set():
    """NCBI_EMAIL identifies the searcher to NIH. It must never be sent by
    default — it exists only to raise a rate limit most users never hit."""
    assert "email" not in pubmed._params(Config(), term="Compound X")
    assert pubmed._params(Config(ncbi_email="me@x.com"), term="q")["email"] == "me@x.com"


def test_asset_name_reaches_the_registry_even_when_fully_local():
    """The honest caveat, asserted: choosing a local model does NOT stop the
    asset name going to NCBI and ClinicalTrials.gov, because that is how the
    research is fetched."""
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"studies": []}
    resp.raise_for_status.return_value = None

    with patch.object(ctgov.requests, "get", MagicMock(return_value=resp)) as get:
        ctgov.search_trials(intervention="SecretCompoundX", max_records=1)
    assert "SecretCompoundX" in json.dumps(get.call_args[1]["params"])


def test_no_telemetry_in_streamlit_config():
    cfg_file = REPO / ".streamlit" / "config.toml"
    assert cfg_file.exists()
    text = cfg_file.read_text()
    assert "gatherUsageStats = false" in text
    assert 'address = "localhost"' in text, "must not bind to a public interface"


def test_diligence_run_with_no_provider_makes_no_outbound_call():
    cfg = Config(provider="none", data_dir=Path(tempfile.mkdtemp()))
    cfg.ensure_dirs()
    store = TrialStore(Path(tempfile.mkdtemp()) / "t.db")
    store.upsert([parse_study(PAGE_ONE["studies"][1])])

    with patch("openai.OpenAI") as ctor:
        runner = DiligenceRunner(cfg, rag=None, trial_store=store)
        q = DiligenceQuestion(id="t", section="S", question="Which trials stopped?",
                              route="structured", k=3)
        runner.run_question(q, "Compound X", "solid tumor")
        runner.close()
    ctor.assert_not_called()


# --------------------------------------------- 3. validation honesty


def test_unassessed_section_is_not_counted_as_passing():
    """Regression: trial-only sections reported a clean pass without anything
    being checked, and the memo's coverage line overstated itself."""
    from medrag.generator import Answer
    from medrag.validation import validate_answer

    report = validate_answer(Answer(text="x", sources=[], model="gpt-4o-mini"), evidence=[])
    assert not report.assessed and not report.passed
    assert "NOT ASSESSED" in report.summary()


def test_validation_uses_the_numbering_the_model_saw():
    """Regression: trials are numbered before literature, so validating against
    literature alone flagged real citations as invalid."""
    from medrag.generator import Answer
    from medrag.validation import validate_answer

    trial = parse_study(PAGE_ONE["studies"][1])
    evidence = build_evidence(trials=[trial], passages=_passages(3))
    answer = Answer(
        text="The trial enrolled 47 patients [1]. A hazard ratio of 0.79 was seen [4].",
        sources=[], model="gpt-4o-mini",
    )
    report = validate_answer(answer, evidence=evidence)
    assert report.invalid_citations == [], "citation [4] is legitimate"
    assert report.ungrounded_numbers == [], "47 comes from the trial record"


def test_validation_still_catches_real_problems():
    from medrag.generator import Answer
    from medrag.validation import validate_answer

    trial = parse_study(PAGE_ONE["studies"][1])
    evidence = build_evidence(trials=[trial], passages=_passages(3))
    answer = Answer(text="It enrolled 999 patients [1]. See [9].", sources=[], model="gpt-4o-mini")
    report = validate_answer(answer, evidence=evidence)
    assert 9 in report.invalid_citations and "999" in report.ungrounded_numbers


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
    print("\nall privacy tests passed" if not failures else f"\n{failures} test(s) failed")
    raise SystemExit(1 if failures else 0)
