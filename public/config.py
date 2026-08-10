"""Configuration for the public service — feature flags that fail closed.

Separate from `medrag.config` on purpose. That one configures a tool an analyst
runs on their own machine, where a missing setting sensibly falls back to
something useful. This one configures a service strangers can reach, where a
missing setting must fall back to OFF. The two have opposite defaults and
merging them would mean one of those defaults was wrong.

FAIL CLOSED MEANS UNPARSEABLE COUNTS AS ABSENT
----------------------------------------------
`FEATURE_MEMO=treu` is a typo, not an instruction. A parser that treated
anything non-empty as true would turn that into a live memo endpoint. Only the
exact affirmative spellings enable a feature; everything else — absent, empty,
misspelled, "yes please", "0", "off" — is off, and an unrecognised value is
reported rather than silently ignored, so a deployer who typed it wrong finds
out from the startup report instead of from a stranger using the feature.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

#: The only strings that turn a feature ON. Deliberately short: a flag is set by
#: a deployer editing a config, not by a user typing prose.
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"", "0", "false", "no", "off"})


@dataclass(frozen=True)
class FeatureFlag:
    name: str
    enabled: bool
    raw: str
    recognised: bool

    @property
    def note(self) -> str:
        if self.recognised:
            return ""
        return (f"{self.name}={self.raw!r} is not a value this flag understands, "
                f"so the feature is OFF. Use one of: {', '.join(sorted(_TRUE))}.")


def _flag(env_name: str) -> FeatureFlag:
    raw = os.getenv(env_name)
    if raw is None:
        return FeatureFlag(env_name, False, "", True)
    value = raw.strip().lower()
    if value in _TRUE:
        return FeatureFlag(env_name, True, raw, True)
    if value in _FALSE:
        return FeatureFlag(env_name, False, raw, True)
    return FeatureFlag(env_name, False, raw, False)


@dataclass
class PublicConfig:
    """What the public service is allowed to do.

    Every field defaults to the safe value, so a `PublicConfig()` built with no
    environment at all is a service that serves the landscape from a snapshot
    and does nothing else.
    """

    # --- features ---
    # Landscape is the shipped feature: a structured query over a public
    # registry, no submitted material, no model.
    landscape: FeatureFlag = field(
        default_factory=lambda: FeatureFlag("PUBLIC_FEATURE_LANDSCAPE", True, "", True))
    # Memo and claim check are built and tested but ship OFF. They take
    # submitted material and may involve a model, so they wait on counsel
    # approving the terms and on the model decision being made.
    memo: FeatureFlag = field(default_factory=lambda: _flag("PUBLIC_FEATURE_MEMO"))
    claims: FeatureFlag = field(default_factory=lambda: _flag("PUBLIC_FEATURE_CLAIMS"))

    # --- data ---
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("MEDRAG_DATA_DIR", "data")))

    # --- limits ---
    #: Requests per IP per window. A cap, not a business rule — the service is
    #: read-only over a snapshot, so the only thing to protect is the box.
    rate_limit_requests: int = 30
    rate_limit_window_seconds: int = 60
    #: Hard cap on rows returned to a browser, independent of any caller's
    #: request. The landscape's own default sample is 30; this is the ceiling
    #: nothing may exceed.
    max_results: int = 50
    #: Hard cap on an uploaded document, checked before it is read into memory.
    max_upload_bytes: int = 2 * 1024 * 1024

    def flags(self) -> list[FeatureFlag]:
        return [self.landscape, self.memo, self.claims]

    def startup_notes(self) -> list[str]:
        """Anything a deployer should see at boot — chiefly a flag they typed
        wrong, which would otherwise look exactly like a flag they left off."""
        return [f.note for f in self.flags() if f.note]


def load_public_config() -> PublicConfig:
    cfg = PublicConfig()
    if os.getenv("PUBLIC_FEATURE_LANDSCAPE") is not None:
        object.__setattr__(cfg, "landscape", _flag("PUBLIC_FEATURE_LANDSCAPE"))
    for name, attr in (("PUBLIC_MAX_RESULTS", "max_results"),
                       ("PUBLIC_RATE_LIMIT", "rate_limit_requests")):
        raw = os.getenv(name)
        if raw and raw.strip().isdigit():
            setattr(cfg, attr, max(1, int(raw.strip())))
    return cfg
