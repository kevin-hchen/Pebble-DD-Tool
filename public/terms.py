"""The terms, the model disclosure, and the lint that keeps them true.

`docs/TERMS-DRAFT.md` is the source. It is read at import, not duplicated here,
for the same reason `config/markers.yaml` holds the marker table and
`markers.py` holds only the grammar: a claim that exists in two places drifts,
and the copy a reader sees is not necessarily the copy a test checks.

THE PROVIDER LINT
-----------------
The terms make a promise about where submitted text goes. That promise is a
function of deployment configuration, so it can be falsified by an environment
variable — someone sets a hosted provider, the software starts sending text to a
third party, and the published terms still say nothing leaves the box. Nobody
lied; the text simply stopped describing the software.

`audit_provider_disclosure` compares the configured provider against the
disclosure section and returns a problem when they disagree.
`tests/test_public_app.py` fails on any problem, so configuring a provider
without updating the terms breaks the build. This is the same mechanism as
`phrasing.audit()` — a caveat must not contradict the software it describes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

TERMS_PATH = Path(__file__).resolve().parents[1] / "docs" / "TERMS-DRAFT.md"

_VERSION_RE = re.compile(r"^##\s*TERMS_VERSION:\s*(\S+)", re.MULTILINE)
_DISCLOSURE_RE = re.compile(
    r"\*\*PROVIDER_DISCLOSURE_BEGIN\*\*(.*?)\*\*PROVIDER_DISCLOSURE_END\*\*", re.DOTALL)

#: Providers this codebase can be configured with, and the name the terms must
#: use for each. Keys match `medrag.config.Config.provider`.
#:
#: "none" and "ollama" are the two that keep text on the box — `none` uses no
#: model at all, `ollama` runs one locally — and both are honestly describable
#: as "nothing leaves". Everything else is a third party and must be named.
HOSTED_PROVIDERS = {
    "openai": "OpenAI",
    "groq": "Groq",
    "together": "Together AI",
    "anthropic": "Anthropic",
    "openrouter": "OpenRouter",
    "deepseek": "DeepSeek",
    "mistral": "Mistral",
}
LOCAL_PROVIDERS = frozenset({"none", "", "ollama", "llamacpp", "local"})

#: The sentence the disclosure must contain when nothing is configured. Checked
#: literally, so softening it is a deliberate edit rather than a rewording that
#: happens to lose the claim.
NO_PROVIDER_CLAIM = "no external model provider is used"


@dataclass(frozen=True)
class Terms:
    version: str
    markdown: str
    disclosure: str

    def mentions(self, name: str) -> bool:
        return name.lower() in self.disclosure.lower()


def load_terms(path: Path | None = None) -> Terms:
    text = (path or TERMS_PATH).read_text(encoding="utf-8")
    version = _VERSION_RE.search(text)
    disclosure = _DISCLOSURE_RE.search(text)
    if not version:
        raise ValueError(
            f"{path or TERMS_PATH} has no 'TERMS_VERSION:' line. The consent record "
            "stores that string, so consent cannot be recorded without it.")
    if not disclosure:
        raise ValueError(
            f"{path or TERMS_PATH} has no PROVIDER_DISCLOSURE_BEGIN/END block. That "
            "block is what the provider lint checks; without it the terms could "
            "silently stop describing where submitted text goes.")
    return Terms(version=version.group(1).strip(), markdown=text,
                 disclosure=disclosure.group(1))


@dataclass(frozen=True)
class ProviderStatement:
    """What the page must say, above the submit button, about where text goes."""
    provider: str
    is_local: bool
    display_name: str

    @property
    def sentence(self) -> str:
        # Worded so a denial never contains the claim it denies — a first draft
        # read "Nothing you type is sent to any third party", which carries the
        # substring "is sent to any third party". Fifth instance of that trap in
        # this codebase; `phrasing.CLAIM_PHRASES["retention"]` now lints for it.
        if self.provider in ("none", ""):
            return ("No language model is used for this request. Your text stays on "
                    "this server.")
        if self.is_local:
            return (f"The language model runs on this server ({self.display_name}). "
                    "Your text stays on this machine and leaves it at no point.")
        return (f"This deployment transmits the text you submit to {self.display_name}, "
                "a third-party provider, to generate an answer.")


def provider_statement(provider: str | None) -> ProviderStatement:
    key = (provider or "none").strip().lower()
    if key in LOCAL_PROVIDERS:
        return ProviderStatement(key, True, key or "none")
    return ProviderStatement(key, False, HOSTED_PROVIDERS.get(key, key))


def audit_provider_disclosure(provider: str | None, terms: Terms | None = None) -> list[str]:
    """Problems where the configured provider and the published terms disagree.

    Empty list means they agree. Returned rather than raised so a caller can
    decide — the test raises, the startup path reports.
    """
    terms = terms or load_terms()
    statement = provider_statement(provider)
    problems: list[str] = []

    if statement.is_local:
        if statement.provider in ("none", ""):
            if NO_PROVIDER_CLAIM not in terms.disclosure.lower():
                problems.append(
                    f"no model provider is configured, but the terms disclosure no longer "
                    f"contains {NO_PROVIDER_CLAIM!r}. Either restore that claim or say "
                    "what is configured.")
        # A hosted provider named in the terms while none is configured is not a
        # safety problem — the terms would be over-promising disclosure — but it
        # IS a description that no longer matches, so it is reported.
        for key, name in HOSTED_PROVIDERS.items():
            if key != statement.provider and terms.mentions(name):
                problems.append(
                    f"the terms name {name} as receiving submitted text, but the "
                    f"configured provider is {statement.provider!r}. The disclosure "
                    "describes a deployment this is not.")
        return problems

    # A hosted provider IS configured: the terms must name it, and must not
    # still be claiming that nothing is configured.
    if not terms.mentions(statement.display_name):
        problems.append(
            f"provider {statement.provider!r} is configured, so submitted text is sent "
            f"to {statement.display_name}, but the terms disclosure does not name it. "
            f"Add it to the PROVIDER_DISCLOSURE block in {TERMS_PATH.name} — the terms "
            "must not silently become untrue.")
    if NO_PROVIDER_CLAIM in terms.disclosure.lower():
        problems.append(
            f"the terms still claim {NO_PROVIDER_CLAIM!r}, but {statement.display_name} "
            "is configured. That statement is now false.")
    return problems
