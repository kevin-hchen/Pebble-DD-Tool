"""Model providers, including free ones.

Every provider here exposes an OpenAI-compatible chat endpoint, so supporting
all of them costs one thing: a configurable `base_url` on the client. The rest
of the codebase never learns which provider it is talking to.

Free options exist and are good enough for this workload. A ten-question memo is
roughly fifteen chat calls, so a free tier of ~1000 requests/day is about sixty
memos a day - far more than a small fund will run.

Embeddings are a separate question. Most free chat providers do not serve an
embedding endpoint, so the free path embeds locally with sentence-transformers:
no key, no rate limit, no data leaving the machine, and markedly better than the
built-in hashing fallback.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    base_url: str | None            # None means the OpenAI default
    default_model: str
    needs_key: bool
    is_free: bool
    serves_embeddings: bool
    signup_url: str = ""
    notes: str = ""
    env_var: str = "OPENAI_API_KEY"


PROVIDERS: dict[str, Provider] = {
    "groq": Provider(
        key="groq",
        label="Groq — free, fast, no credit card",
        base_url="https://api.groq.com/openai/v1",
        default_model="llama-3.3-70b-versatile",
        needs_key=True,
        is_free=True,
        serves_embeddings=False,
        signup_url="https://console.groq.com/keys",
        notes=(
            "Free tier with no card required. Rate limits are generous enough for "
            "roughly sixty memos a day. Text is sent to Groq."
        ),
        env_var="GROQ_API_KEY",
    ),
    "cerebras": Provider(
        key="cerebras",
        label="Cerebras — free, high throughput",
        base_url="https://api.cerebras.ai/v1",
        default_model="llama-3.3-70b",
        needs_key=True,
        is_free=True,
        serves_embeddings=False,
        signup_url="https://cloud.cerebras.ai",
        notes="Free tier, no card required. Text is sent to Cerebras.",
        env_var="CEREBRAS_API_KEY",
    ),
    "openrouter": Provider(
        key="openrouter",
        label="OpenRouter — free models, many options",
        base_url="https://openrouter.ai/api/v1",
        default_model="meta-llama/llama-3.3-70b-instruct:free",
        needs_key=True,
        is_free=True,
        serves_embeddings=False,
        signup_url="https://openrouter.ai/keys",
        notes=(
            "Free models available. The free daily request cap is low - enough for "
            "a couple of memos a day - and rises with a small top-up."
        ),
        env_var="OPENROUTER_API_KEY",
    ),
    "ollama": Provider(
        key="ollama",
        label="Ollama — fully local, nothing leaves this computer",
        base_url="http://localhost:11434/v1",
        default_model="llama3.1:8b",
        needs_key=False,
        is_free=True,
        serves_embeddings=True,
        signup_url="https://ollama.com/download",
        notes=(
            "Runs on this machine. No key, no limits, no data sent anywhere. "
            "Requires installing Ollama and pulling a model first, and answers are "
            "slower and weaker than a hosted model unless the machine has a GPU."
        ),
    ),
    "openai": Provider(
        key="openai",
        label="OpenAI — paid, best quality",
        base_url=None,
        default_model="gpt-4o-mini",
        needs_key=True,
        is_free=False,
        serves_embeddings=True,
        signup_url="https://platform.openai.com/api-keys",
        notes=(
            "Around one or two cents per memo, but requires billing set up with "
            "credits or calls fail with an insufficient_quota error."
        ),
    ),
    "none": Provider(
        key="none",
        label="No AI — evidence lists only, always free",
        base_url=None,
        default_model="",
        needs_key=False,
        is_free=True,
        serves_embeddings=False,
        notes=(
            "No model at all. Memos list the retrieved evidence, fully cited, but "
            "nothing is summarised and the contradicting-evidence hunt does not run."
        ),
    ),
}

FREE_PROVIDERS = [p for p in PROVIDERS.values() if p.is_free and p.key != "none"]
DEFAULT_PROVIDER = "none"


def get_provider(key: str | None) -> Provider:
    return PROVIDERS.get((key or DEFAULT_PROVIDER).lower(), PROVIDERS[DEFAULT_PROVIDER])


def effective_provider(cfg) -> Provider:
    """The provider actually in force.

    Backwards compatibility matters here: setting OPENAI_API_KEY and nothing
    else used to be the whole configuration, and plenty of scripts and habits
    depend on it. A key present with no provider named means OpenAI. Absent a
    key, the default stays "none" so a fresh install cannot spend money.
    """
    named = (getattr(cfg, "provider", None) or DEFAULT_PROVIDER).lower()
    if named != DEFAULT_PROVIDER and named in PROVIDERS:
        return PROVIDERS[named]
    if getattr(cfg, "openai_api_key", None):
        return PROVIDERS["openai"]
    return PROVIDERS[DEFAULT_PROVIDER]


def make_client(cfg):
    """Build an OpenAI-compatible client for the configured provider.

    Returns None when no model is configured, which callers treat as "fall back
    to extractive output" rather than as an error.
    """
    provider = effective_provider(cfg)
    if provider.key == "none" or getattr(cfg, "offline", False):
        return None

    api_key = cfg.openai_api_key
    if provider.needs_key and not api_key:
        return None
    if not provider.needs_key and not api_key:
        # The OpenAI SDK requires a non-empty key even where the server ignores
        # it, as Ollama does.
        api_key = "local"

    try:
        from openai import OpenAI

        kwargs = {"api_key": api_key}
        if provider.base_url:
            kwargs["base_url"] = provider.base_url
        return OpenAI(**kwargs)
    except Exception:
        return None


def resolve_model(cfg) -> str:
    """Model name for the active provider, honouring an explicit override.

    With no provider configured this returns a name that says so. Leaving the
    stale default in place would put "Model: gpt-4o-mini" in the header of a
    memo that no model ever touched.
    """
    provider = effective_provider(cfg)
    if provider.key == "none":
        return "none (evidence lists only)"
    explicit = getattr(cfg, "chat_model_explicit", False)
    if explicit and cfg.chat_model:
        return cfg.chat_model
    return provider.default_model or cfg.chat_model
