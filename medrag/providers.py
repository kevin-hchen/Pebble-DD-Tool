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


# ------------------------------------------------------- when the model refuses
#
# `make_client` returning None is the "no model configured" path, and every
# caller already handles it by degrading to extractive output. A CONFIGURED
# provider that refuses is a different thing entirely, and until now nothing
# handled it: a revoked or wrong key raised `openai.PermissionDeniedError` out
# of `client.chat.completions.create` and took the whole run down with a
# traceback on question 1 of 11.
#
# That made the documented behaviour false. CLAUDE.md and docs/RUNBOOK.md both
# state that an expired key degrades — routing falls back to rules, answers
# become extractive evidence lists, the contradiction hunt does not run — and
# two of those three were true: `Router.route` and `ContradictionHunter.hunt`
# already caught everything. The answer path did not, so the degradation nobody
# could observe was the one that mattered most.


@dataclass(frozen=True)
class ModelFailure:
    """Why a configured provider did not answer. Carries no key and no prompt.

    The message is BUILT from the status code and the provider name rather than
    from `str(exc)`, for the same reason `public/reqlog.RequestLogLine` has four
    fields and nowhere to put a fifth: an SDK exception renders the response
    body, a future SDK version may render more, and this string is written into
    a memo a human reads and may circulate. A status code and a provider name
    are enough to act on — the RUNBOOK's instruction is "check the key first" —
    and they are structurally incapable of carrying a prompt.
    """

    status: int | None
    kind: str
    #: True when asking again cannot change the answer, so the caller should
    #: stop calling for the rest of the run.
    fatal: bool
    message: str


#: Statuses no retry and no later question can fix. The same rule
#: `trials/client._RETRY_STATUSES` states from the other side: a 401, 403 or 404
#: is an ANSWER. Asking eleven times produces eleven identical refusals, eleven
#: identical warnings, and eleven round trips to a provider that has already
#: said no.
_FATAL_STATUSES = frozenset({400, 401, 403, 404})

_REASONS = {
    400: "the request was rejected as malformed, which is a bug in this tool rather "
         "than a problem with the key",
    401: "the API key was rejected — it is missing, wrong, or has been revoked",
    403: "the provider refused the request — the key is revoked, out of quota, or "
         "not permitted to use this model",
    404: "the configured model name was not found at this provider",
    429: "the provider's rate limit was hit",
}


def describe_failure(exc: BaseException, provider_key: str = "", model: str = "") -> ModelFailure:
    """Classify a provider exception without quoting it.

    Anything unrecognised is treated as NON-fatal, deliberately: a transient
    network blip degrading one section is recoverable, while wrongly latching
    the model off for the whole run turns a hiccup into eleven empty syntheses.
    Failing open here is safe precisely because the fallback is extractive
    evidence, not silence.
    """
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = None
    kind = type(exc).__name__
    fatal = status in _FATAL_STATUSES
    reason = _REASONS.get(status) or "the provider could not be reached or did not respond"
    where = f" ({provider_key})" if provider_key else ""
    detail = f" for model “{model}”" if model and status == 404 else ""
    code = f"HTTP {status}" if status else kind
    return ModelFailure(
        status=status,
        kind=kind,
        fatal=fatal,
        message=(
            f"the configured model provider{where} returned {code}{detail}: {reason}. "
            + ("No further model calls will be made in this run. " if fatal else "")
            + "This section falls back to listing the retrieved evidence verbatim "
              "instead of a written synthesis. The evidence and its citations are "
              "unaffected — what is missing is the prose, not the sources."
        ),
    )


def call_chat(client, provider_key: str = "", **kwargs):
    """One chat call, returning `(response, failure)` instead of raising.

    Every model call in this codebase goes through the OpenAI SDK, whichever
    provider is configured, so `openai.OpenAIError` is the one base that covers
    auth, rate limiting, timeouts, connection loss and bad status. Caught
    narrowly rather than with a bare `except Exception`, so a TypeError in the
    arguments this tool builds still surfaces as the bug it is instead of being
    reported to an analyst as a provider outage.
    """
    try:
        from openai import OpenAIError
    except Exception:      # the SDK is absent; make_client cannot have built a client
        OpenAIError = Exception

    try:
        return client.chat.completions.create(**kwargs), None
    except OpenAIError as exc:
        return None, describe_failure(exc, provider_key, kwargs.get("model", ""))
