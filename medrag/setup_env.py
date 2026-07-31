"""Read and write .env from the app, so nobody has to edit a dotfile.

Written with restrictive permissions and never echoed back to the browser: the
setup screen reports whether a key is present, never what it is.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_PATH = Path(".env")


def read_env(path: Path = ENV_PATH) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def write_env(updates: dict[str, str], path: Path = ENV_PATH) -> Path:
    """Merge updates into .env, preserving anything already there."""
    existing = read_env(path)
    existing.update({k: v for k, v in updates.items() if v is not None})

    lines = [
        "# MedRAG configuration. Written by the setup screen.",
        "# This file holds a secret - do not commit it or email it.",
        "",
    ]
    for key, value in existing.items():
        lines.append(f"{key}={value}")

    # Create with 0600 from the start rather than chmod-ing after, so the key is
    # never briefly readable by other users on a shared machine.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)

    # Make it live in this process too, so the app picks it up without a restart.
    for key, value in updates.items():
        if value is not None:
            os.environ[key] = value

    return path


def key_looks_valid(provider_key: str, value: str) -> tuple[bool, str]:
    """Cheap shape check so an obvious paste error is caught before a run.

    Deliberately loose - prefixes change, and rejecting a valid key is worse
    than accepting a bad one that fails clearly on first use.
    """
    value = (value or "").strip()
    if not value:
        return False, "No key entered."
    if " " in value or "\n" in value:
        return False, "That looks like it has a space in it — check the paste."
    if len(value) < 20:
        return False, "That looks too short to be an API key."

    expected = {"openai": "sk-", "groq": "gsk_", "openrouter": "sk-or-"}
    prefix = expected.get(provider_key)
    if prefix and not value.startswith(prefix):
        return True, (
            f"Warning: {provider_key} keys usually start with '{prefix}'. "
            "Saved anyway — if runs fail, check you copied the right key."
        )
    return True, ""
