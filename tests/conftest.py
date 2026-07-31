"""pytest wiring for the network guard.

The guard itself lives in `tests/netguard.py` so that a direct run
(`python tests/test_claims.py`, a documented convention in CLAUDE.md) gets the
same protection. conftest.py is only loaded by pytest, so putting the
implementation here would have left the direct run unguarded — the mode the
project actually tells people to use.

See netguard.py for why this exists at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import netguard  # noqa: E402


@pytest.fixture(autouse=True)
def _no_network(request):
    """Deny outbound connections unless the test opts out explicitly."""
    if request.node.get_closest_marker("allow_network"):
        yield
        return
    netguard.install()
    try:
        yield
    finally:
        netguard.uninstall()


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "allow_network: permit outbound connections in this test"
    )
