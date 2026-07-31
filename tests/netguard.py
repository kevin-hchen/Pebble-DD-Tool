"""Block outbound sockets for the test suite, in both ways it is run.

CLAUDE.md states the invariant — "Tests never touch the network" — and documents
two ways to run: under pytest, and each file directly. `conftest.py` only covers
the first. That is the wrong half to cover: the direct run is the mode the
project tells people to use, so the guard was missing exactly where a developer
would hit it.

The gap is not hypothetical. While writing the consent tests, a half-finished
stub called `extract_claims` without mocking the client and made a real request
to Groq carrying "ACME BIO CONFIDENTIAL: cures everything". It 401'd, so nothing
landed, but it left the machine.

That is the same shape as the consent-gate finding: an invariant documented in
CLAUDE.md, believed by everyone, enforced by nothing. So this module is imported
and installed by every test file AND by conftest, and the two share one
implementation rather than drifting.

Escape hatch for a genuinely live call (there are none today, and adding one
should be argued for):

    under pytest      @pytest.mark.allow_network
    direct run        MEDRAG_ALLOW_TEST_NETWORK=1 python tests/test_foo.py

Loopback is permitted. Blocking it would break multiprocessing and any local
fixture server, and it does not leave the machine, which is the property being
protected.
"""

from __future__ import annotations

import os
import socket

ALLOW_ENV = "MEDRAG_ALLOW_TEST_NETWORK"

_REAL = {
    "connect": socket.socket.connect,
    "connect_ex": socket.socket.connect_ex,
    "create_connection": socket.create_connection,
}
_installed = False


class NetworkAccessDenied(RuntimeError):
    """A test attempted an outbound connection."""


_LOOPBACK = {"127.0.0.1", "::1", "localhost", ""}


def _is_loopback(address) -> bool:
    """True for AF_UNIX paths and loopback TCP/UDP.

    multiprocessing and any local helper stay working; nothing here reaches a
    network the machine is not already talking to itself on.
    """
    if isinstance(address, str):        # AF_UNIX
        return True
    if isinstance(address, (tuple, list)) and address:
        return str(address[0]) in _LOOPBACK
    return False


def _denier(name, real):
    def guard(*args, **kwargs):
        address = args[1] if name != "create_connection" and len(args) > 1 else (
            args[0] if args else None
        )
        if _is_loopback(address):
            return real(*args, **kwargs)
        raise NetworkAccessDenied(
            f"a test attempted an outbound connection to {address!r}. Tests must "
            "use a mocked transport against a captured fixture (see tests/fixtures/). "
            f"If this is deliberate, set {ALLOW_ENV}=1 or mark it "
            "@pytest.mark.allow_network, and say why."
        )

    return guard


def install() -> bool:
    """Patch the socket entry points. Idempotent; returns False when skipped."""
    global _installed
    if _installed or os.getenv(ALLOW_ENV):
        return False
    socket.socket.connect = _denier("connect", _REAL["connect"])
    socket.socket.connect_ex = _denier("connect_ex", _REAL["connect_ex"])
    socket.create_connection = _denier("create_connection", _REAL["create_connection"])
    _installed = True
    return True


def uninstall() -> None:
    """Restore the real socket entry points (used by the pytest fixture)."""
    global _installed
    socket.socket.connect = _REAL["connect"]
    socket.socket.connect_ex = _REAL["connect_ex"]
    socket.create_connection = _REAL["create_connection"]
    _installed = False
