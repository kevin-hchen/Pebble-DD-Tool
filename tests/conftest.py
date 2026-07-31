"""No test in this suite may open a socket.

CLAUDE.md states it as a convention: "Tests never touch the network. External
services are driven through mocked transports against captured fixtures. Every
suite runs with no API key and no internet." Nothing enforced it, and during the
security review a half-finished test slipped through that called `extract_claims`
without mocking the client. It made a real request to Groq carrying deck text.
It failed on a bad key, so nothing landed — but the request left the machine, and
the convention had been true only because everyone had been careful.

This makes it true by construction. Anything that tries to connect raises
`NetworkAccessDenied` naming the test, which is a better failure than a hang, a
401, or a bill.

Escape hatch, for a test that genuinely needs a live call (there are none today,
and adding one should be argued for):

    @pytest.mark.allow_network
    def test_something_that_really_must_dial_out(): ...

Direct runs (`python tests/test_claims.py`, which every file supports) do not load
this file. That is a real gap: the guard binds under pytest only. The suite is run
both ways, so treat a direct run as unguarded.
"""

from __future__ import annotations

import socket

import pytest


class NetworkAccessDenied(RuntimeError):
    """A test attempted an outbound connection."""


_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex
_REAL_CREATE_CONNECTION = socket.create_connection


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    if request.node.get_closest_marker("allow_network"):
        return

    name = request.node.nodeid

    def deny(*args, **kwargs):
        raise NetworkAccessDenied(
            f"{name} attempted an outbound connection. Tests must use a mocked "
            "transport against a captured fixture (see tests/fixtures/). If this "
            "is deliberate, mark it @pytest.mark.allow_network and say why."
        )

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "allow_network: permit outbound connections in this test"
    )
