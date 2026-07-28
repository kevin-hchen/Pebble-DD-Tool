"""Tests for provider presets, key handling, .env writing and auto-loading.

Covers the free path specifically: that a fresh install costs nothing, that a
free provider never gets asked for embeddings it cannot serve, and that keys are
written with restrictive permissions and never echoed back.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from medrag.autoload import LoadReport, _friendly, ensure_data, has_data_for  # noqa: E402
from medrag.config import Config, load_config  # noqa: E402
from medrag.embeddings import get_embedder  # noqa: E402
from medrag.providers import (  # noqa: E402
    FREE_PROVIDERS,
    PROVIDERS,
    get_provider,
    make_client,
    resolve_model,
)
from medrag.setup_env import key_looks_valid, read_env, write_env  # noqa: E402


def _cfg(provider: str, key: str | None = None, **kw) -> Config:
    return Config(provider=provider, openai_api_key=key, **kw)


# ------------------------------------------------------------- presets


def test_free_providers_exist_and_are_marked():
    assert FREE_PROVIDERS, "there must be at least one genuinely free provider"
    assert all(p.is_free for p in FREE_PROVIDERS)
    assert {"groq", "ollama"} <= {p.key for p in FREE_PROVIDERS}


def test_default_config_costs_nothing():
    """A fresh install must not be able to spend money by accident."""
    assert Config().provider == "none"
    assert make_client(Config()) is None


def test_every_provider_has_a_usable_definition():
    for key, p in PROVIDERS.items():
        assert p.key == key
        assert p.label and p.notes
        if p.needs_key:
            assert p.signup_url, f"{key} needs a key so it must say where to get one"
        if p.key != "none":
            assert p.default_model


def test_base_url_is_set_for_alternative_providers():
    with patch("openai.OpenAI") as ctor:
        make_client(_cfg("groq", "gsk_" + "x" * 30))
    assert ctor.call_args[1]["base_url"] == "https://api.groq.com/openai/v1"


def test_openai_uses_the_default_base_url():
    with patch("openai.OpenAI") as ctor:
        make_client(_cfg("openai", "sk-" + "x" * 30))
    assert "base_url" not in ctor.call_args[1]


def test_local_provider_needs_no_key():
    with patch("openai.OpenAI") as ctor:
        client = make_client(_cfg("ollama"))
    assert client is not None
    assert ctor.call_args[1]["api_key"], "the SDK requires a non-empty key even locally"


def test_provider_needing_a_key_returns_no_client_without_one():
    assert make_client(_cfg("groq", None)) is None


def test_offline_beats_any_provider():
    assert make_client(_cfg("groq", "gsk_" + "x" * 30, offline=True)) is None


def test_model_follows_provider():
    assert resolve_model(_cfg("groq")) == PROVIDERS["groq"].default_model
    assert resolve_model(_cfg("openai")) == "gpt-4o-mini"


def test_explicit_model_overrides_the_preset():
    cfg = _cfg("groq")
    cfg.chat_model, cfg.chat_model_explicit = "some-other-model", True
    assert resolve_model(cfg) == "some-other-model"


def test_no_provider_reports_an_honest_model_name():
    """A memo header must not claim a model that never ran."""
    assert "none" in resolve_model(_cfg("none")).lower()


def test_switching_provider_does_not_reuse_another_vendors_key():
    env = {"MEDRAG_PROVIDER": "groq", "GROQ_API_KEY": "gsk_" + "a" * 30}
    with patch.dict(os.environ, env, clear=False):
        os.environ.pop("OPENAI_API_KEY", None)
        assert load_config().openai_api_key.startswith("gsk_")


# ------------------------------------------------------------- embeddings


def test_free_chat_provider_does_not_request_openai_embeddings():
    """Groq serves no embedding endpoint; sending its key there fails obscurely."""
    with patch("openai.OpenAI") as ctor:
        embedder = get_embedder(_cfg("groq", "gsk_" + "x" * 30), verbose=False)
    assert not embedder.name.startswith("openai:")
    ctor.assert_not_called()


def test_openai_provider_still_uses_openai_embeddings():
    with patch("openai.OpenAI", return_value=MagicMock()):
        embedder = get_embedder(_cfg("openai", "sk-" + "x" * 30), verbose=False)
    assert embedder.name.startswith("openai:")


def test_embeddings_always_resolve_to_something():
    """Retrieval must never be the thing that fails; worst case is degraded."""
    assert get_embedder(_cfg("none"), verbose=False).dim > 0


# ------------------------------------------------------------- key handling


def test_key_shape_validation():
    assert key_looks_valid("groq", "gsk_" + "a" * 40)[0]
    assert not key_looks_valid("groq", "")[0]
    assert not key_looks_valid("groq", "short")[0]
    assert not key_looks_valid("openai", "sk-abc def" + "x" * 30)[0]


def test_wrong_prefix_warns_but_still_saves():
    ok, message = key_looks_valid("openai", "gsk_" + "a" * 40)
    assert ok and "usually start" in message


def test_env_is_written_privately_and_merges():
    d = Path(tempfile.mkdtemp())
    path = d / ".env"
    write_env({"MEDRAG_PROVIDER": "groq", "GROQ_API_KEY": "gsk_secret_value_here"}, path)
    assert oct(path.stat().st_mode)[-3:] == "600", "a file holding a key must not be world-readable"

    write_env({"NCBI_EMAIL": "a@b.com"}, path)
    values = read_env(path)
    assert values["GROQ_API_KEY"] == "gsk_secret_value_here", "existing keys must survive"
    assert values["NCBI_EMAIL"] == "a@b.com"


def test_reading_env_ignores_comments_and_blanks():
    d = Path(tempfile.mkdtemp())
    path = d / ".env"
    path.write_text("# a comment\n\nKEY=value\nQUOTED=\"quoted value\"\nnot_a_pair\n")
    values = read_env(path)
    assert values == {"KEY": "value", "QUOTED": "quoted value"}


# ------------------------------------------------------------- autoload


def test_has_data_for_is_false_on_an_empty_install():
    cfg = Config(data_dir=Path(tempfile.mkdtemp()))
    cfg.ensure_dirs()
    assert not has_data_for(cfg, "empagliflozin", "heart failure")


def test_has_data_for_needs_a_search_term():
    cfg = Config(data_dir=Path(tempfile.mkdtemp()))
    cfg.ensure_dirs()
    assert not has_data_for(cfg, "", "")


def test_ensure_data_skips_when_already_loaded():
    cfg = Config(data_dir=Path(tempfile.mkdtemp()))
    cfg.ensure_dirs()
    with patch("medrag.autoload.has_data_for", return_value=True):
        report = ensure_data(cfg, "x", "y")
    assert report.skipped and "already loaded" in report.summary()


def test_one_source_failing_does_not_abort_the_other():
    """A memo built on trials alone beats no memo, provided the gap is reported."""
    cfg = Config(data_dir=Path(tempfile.mkdtemp()))
    cfg.ensure_dirs()
    with patch("medrag.autoload.has_data_for", return_value=False), \
         patch("medrag.autoload.ingest_pubmed", side_effect=RuntimeError("pubmed down")), \
         patch("medrag.autoload.search_trials", return_value=[]):
        report = ensure_data(cfg, "x", "y")
    assert report.errors and "published literature" in report.errors[0]
    assert report.trials_added == 0


def test_progress_callback_is_driven():
    cfg = Config(data_dir=Path(tempfile.mkdtemp()))
    cfg.ensure_dirs()
    seen = []
    with patch("medrag.autoload.has_data_for", return_value=True):
        ensure_data(cfg, "x", "y", progress=lambda f, m: seen.append((f, m)))
    assert seen and seen[-1][0] == 1.0


def test_network_errors_are_translated_for_humans():
    class ProxyError(Exception):
        pass

    assert "network is blocking" in _friendly(ProxyError("tunnel failed"))
    assert "rate-limiting" in _friendly(Exception("429 too many requests"))


def test_load_report_summary_reads_plainly():
    assert "3 papers" in LoadReport(literature_added=3).summary()
    assert "Nothing new" in LoadReport().summary()


# ------------------------------------------------------------- doctor


def _run_doctor(cfg: Config, models_list_effect=None):
    """Run cmd_doctor with mocks in place; return (return_code, stdout, ctor)."""
    import io
    from contextlib import redirect_stdout
    from medrag.cli import cmd_doctor

    args = MagicMock(encrypt=False, offline=False)
    resp = MagicMock(status_code=200)
    resp.raise_for_status.return_value = None

    ctor = MagicMock()
    if models_list_effect is not None:
        ctor.return_value.models.list.side_effect = models_list_effect
    else:
        ctor.return_value.models.list.return_value = []

    buf = io.StringIO()
    with patch("medrag.cli.load_config", return_value=cfg), \
         patch("requests.get", return_value=resp), \
         patch("openai.OpenAI", ctor), \
         redirect_stdout(buf):
        rc = cmd_doctor(args)
    return rc, buf.getvalue(), ctor


def test_doctor_uses_configured_provider_not_openai():
    """Regression: cmd_doctor constructed OpenAI() directly with whatever key
    it found, so MEDRAG_PROVIDER=groq sent a gsk_ key to api.openai.com and
    printed a scary 'OpenAI API FAILED' line for a working install."""
    rc, text, ctor = _run_doctor(_cfg("groq", "gsk_" + "x" * 30))
    assert "Groq" in text, "doctor must name the configured provider"
    assert "OpenAI API" not in text, "must not label the check 'OpenAI API' under a non-OpenAI provider"
    assert ctor.call_args[1]["base_url"].startswith("https://api.groq.com"), \
        "the check went to a client without the Groq base_url — the key was sent to OpenAI"
    assert rc == 0


def test_doctor_provider_failure_does_not_break_exit_code():
    """Data sources are the real dependency. A model provider going down
    degrades the tool - memos fall back to extractive lists - but does not
    break it, so doctor stays at rc 0."""
    rc, text, _ = _run_doctor(
        _cfg("groq", "gsk_" + "x" * 30),
        models_list_effect=RuntimeError("503 Service Unavailable"),
    )
    assert rc == 0, "provider failure alone must not set rc=1"
    assert "WARN" in text
    assert "extractive" in text.lower(), "must state the concrete degradation, not just fail"


def test_doctor_skips_provider_check_when_none_and_says_so():
    rc, text, ctor = _run_doctor(_cfg("none"))
    assert rc == 0
    assert "SKIPPED" in text and "none" in text
    ctor.assert_not_called()


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
    print("\nall provider tests passed" if not failures else f"\n{failures} test(s) failed")
    raise SystemExit(1 if failures else 0)
