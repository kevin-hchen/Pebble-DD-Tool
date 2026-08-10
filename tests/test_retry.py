"""Tests for the transient-failure retry in trials/client.py.

Built after a measurement, not in anticipation: a 74-family ingest hit 41 HTTP
500s and 12 dropped connections across three passes, and each one downgraded a
whole query set — a family is only as complete as its least lucky query.

The properties, in the order they matter:

  1. A retry never launders a failure into a success. This is the one that
     matters most: retry exists to reduce how often a family fails, and the
     moment it can turn an exhausted failure into a silent pass, the status
     column it feeds is worthless.
  2. Only transient failures are retried. A 404 is an answer, and repeating it
     is both pointless and the behaviour that gets a client blocked.
  3. The client does not behave like a bot. openFDA's Purple Book already cost
     this project three HTTP 404s that were really Akamai rate-limiting, nearly
     recorded as "this source does not exist"; the backoff floors here are
     chosen against repeating that on a source the tool depends on.
  4. Retrying is COUNTED and reported, on success as well as on failure. A
     source degrading quietly is what this codebase keeps guarding against, and
     retry is exactly a mechanism for turning a loud failure into a quiet delay.

No network: tests/netguard.py blocks sockets, and every test here drives a fake
transport. `time.sleep` is monkeypatched throughout — the real backoff floors
are seconds by design, so a suite that actually slept would take minutes.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import netguard  # noqa: E402

netguard.install()

import requests  # noqa: E402

from medrag.trials import client  # noqa: E402
from medrag.trials.client import (  # noqa: E402
    _MAX_ATTEMPTS,
    _RETRY_STATUSES,
    RetryBudget,
    _backoff_seconds,
    _get_with_retry,
    _retry_after_seconds,
)


class _Resp:
    """The thin slice of requests.Response the client actually touches."""

    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else {"studies": []}
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)


class _Transport:
    """Scripted responses. Each entry is a _Resp to return or an exception to
    raise; the last entry repeats once the script runs out."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def __call__(self, url, params=None, timeout=None, **kw):
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


def _patch(monkey_get, slept: list):
    """Install a fake transport and a sleep that records instead of waiting."""
    client.requests.get = monkey_get
    client.time.sleep = lambda s: slept.append(s)
    client._LAST_CALL["t"] = 0.0


def _restore():
    import time as _time

    client.requests.get = requests.get
    client.time.sleep = _time.sleep


def _run(script):
    slept: list = []
    transport = _Transport(script)
    _patch(transport, slept)
    budget = RetryBudget()
    try:
        return transport, budget, slept, _get_with_retry("http://x", {}, 30, budget=budget)
    finally:
        _restore()


def _run_expecting_failure(script):
    slept: list = []
    transport = _Transport(script)
    _patch(transport, slept)
    budget = RetryBudget()
    try:
        _get_with_retry("http://x", {}, 30, budget=budget)
        raise AssertionError("expected the call to raise after exhausting attempts")
    except (requests.HTTPError, requests.ConnectionError, requests.Timeout) as exc:
        return transport, budget, slept, exc
    finally:
        _restore()


# ------------------------------------------- 1. retry never hides a failure


def test_a_retry_that_exhausts_still_raises_rather_than_returning_empty():
    """The property the status column depends on. If an exhausted retry could
    return a valid-looking empty response, `fetch_query_set` would record a
    successful query with zero results, `verify_ingest` would compare zero
    against a reported total and — for a query whose real total is also
    unknown — the family could grade COMPLETE on nothing at all."""
    transport, budget, _slept, exc = _run_expecting_failure([_Resp(500)] * 10)

    assert transport.calls == _MAX_ATTEMPTS, "attempts must be capped"
    assert budget.exhausted == 1
    assert isinstance(exc, requests.HTTPError)


def test_an_exhausted_failure_carries_its_retry_count_to_the_caller():
    """A query that died after three retries and one that died immediately both
    record an error; only the count distinguishes 'the registry is struggling'
    from 'the registry said no'."""
    _t, _b, _s, exc = _run_expecting_failure([_Resp(503)] * 10)
    assert getattr(exc, "retry_budget", None) is not None
    assert exc.retry_budget.retries == _MAX_ATTEMPTS - 1
    assert exc.retry_budget.exhausted == 1


def test_a_transient_failure_followed_by_success_returns_the_real_payload():
    """The anti-vacuity guard for the tests above: retry must actually work, or
    'it raises' would prove nothing."""
    good = _Resp(200, {"studies": [{"x": 1}], "totalCount": 1})
    transport, budget, slept, resp = _run([_Resp(500), _Resp(500), good])

    assert transport.calls == 3
    assert resp.json()["totalCount"] == 1
    assert budget.retries == 2
    # `slept` also holds the 0.25s politeness throttle taken before every
    # request; the budget counts backoff only, which is the number that means
    # "the registry made us wait".
    assert len([s for s in slept if s > 1.0]) == 2, "each retry waits exactly once"


# ------------------------------------------------ 2. only retryable things


def test_a_404_is_an_answer_and_is_never_retried():
    """A 404 cannot become a 200 by asking again. Repeating it is how a client
    starts to look like something hammering an endpoint it does not
    understand."""
    slept: list = []
    transport = _Transport([_Resp(404)])
    _patch(transport, slept)
    try:
        raised = False
        try:
            _get_with_retry("http://x", {}, 30)
        except requests.HTTPError:
            raised = True
        assert raised
        assert transport.calls == 1, "a 404 must cost exactly one request"
        assert slept == [], "nothing should have waited"
    finally:
        _restore()


def test_a_400_is_never_retried():
    """A malformed query is a bug in this tool, not a blip in the registry.
    Retrying it hides the bug behind a delay."""
    slept: list = []
    transport = _Transport([_Resp(400)])
    _patch(transport, slept)
    try:
        try:
            _get_with_retry("http://x", {}, 30)
        except requests.HTTPError:
            pass
        assert transport.calls == 1
    finally:
        _restore()


def test_the_retryable_set_is_transient_failures_only():
    """Pinned as a set rather than tested case by case, so widening it to
    include a 4xx is a deliberate edit to a reviewed constant."""
    assert _RETRY_STATUSES == frozenset({429, 500, 502, 503, 504})
    for answer in (400, 401, 403, 404, 410, 422):
        assert answer not in _RETRY_STATUSES


def test_timeouts_and_dropped_connections_are_retried():
    """The 12 ConnectionErrors in the real run were `RemoteDisconnected` — the
    server closing the socket without answering, which is not an answer."""
    good = _Resp(200, {"studies": []})
    for exc in (requests.ConnectionError("reset"), requests.Timeout("slow")):
        transport, budget, _slept, resp = _run([exc, good])
        assert transport.calls == 2, f"{type(exc).__name__} should be retried"
        assert budget.retries == 1
        assert resp.status_code == 200


# --------------------------------------------------- 3. not a bot


def test_backoff_grows_and_is_never_shorter_than_the_polite_floor():
    """Jitter is ADDED, never subtracted. A jitter that can shorten the wait
    undoes the politeness on exactly the retries that matter most."""
    for attempt in range(1, _MAX_ATTEMPTS):
        floor = client._BACKOFF_BASE * (client._BACKOFF_FACTOR ** (attempt - 1))
        samples = [_backoff_seconds(attempt) for _ in range(200)]
        assert min(samples) >= floor, "a backoff went below its floor"
        assert max(samples) <= floor * (1 + client._BACKOFF_JITTER)


def test_the_first_backoff_is_seconds_not_milliseconds():
    """The Purple Book lesson, pinned as a number. Akamai answered three fast
    requests with a 404 and a bot-detection body, which this project nearly
    recorded as 'this source does not exist'. Sub-second retry is what a
    scraper does."""
    assert client._BACKOFF_BASE >= 1.0
    assert min(_backoff_seconds(1) for _ in range(100)) >= 1.0


def test_jitter_actually_varies_so_clients_do_not_retry_in_lockstep():
    assert len({round(_backoff_seconds(2), 4) for _ in range(50)}) > 1


def test_total_attempts_are_capped_so_a_dead_registry_cannot_hang_an_ingest():
    """Unbounded retry converts 'the registry is down' into 'the ingest never
    returns', which is its own silent failure — an operator cannot report a
    state they are still waiting for."""
    assert 1 < _MAX_ATTEMPTS <= 6
    transport, _b, _s, _e = _run_expecting_failure([_Resp(500)] * 50)
    assert transport.calls == _MAX_ATTEMPTS


def test_retry_after_is_honoured_over_the_local_schedule():
    """A server that says how long to wait outranks any schedule of ours."""
    good = _Resp(200, {"studies": []})
    transport, budget, slept, _resp = _run(
        [_Resp(429, headers={"Retry-After": "7"}), good])
    assert transport.calls == 2
    # Exactly the server's number, not a locally computed backoff (which would
    # be 2s+jitter for the first retry).
    assert budget.slept == 7.0, f"expected the server's 7s, waited {budget.slept}"
    assert 7.0 in slept


def test_a_hostile_retry_after_cannot_park_the_ingest_indefinitely():
    """Capped, so one stray header cannot stall an operator for an hour."""
    assert _retry_after_seconds(_Resp(429, headers={"Retry-After": "99999"})) \
        == client._MAX_RETRY_AFTER
    # Unparseable or absent falls back to the local schedule rather than to zero,
    # which would be a hot loop.
    assert _retry_after_seconds(_Resp(429, headers={"Retry-After": "soon"})) is None
    assert _retry_after_seconds(_Resp(429)) is None
    assert _retry_after_seconds(_Resp(429, headers={"Retry-After": "-5"})) is None


# ------------------------------------------------------ 4. counted, reported


def test_a_successful_but_retried_ingest_still_reports_that_it_retried():
    """The reporting property. A run that needed forty retries and got its data
    is still a source in trouble, and a bare wall-clock number hides that."""
    from medrag.trials.queries import CONDITION, CoverageReport, QueryYield, TrialQuery

    report = CoverageReport(
        set_key="breast", set_label="Breast cancer",
        yields=[
            QueryYield(query=TrialQuery(CONDITION, "breast cancer"), reported_total=10,
                       fetched=10, new=10, retries=9, retry_seconds=41.0),
            QueryYield(query=TrialQuery(CONDITION, "breast neoplasms"), reported_total=5,
                       fetched=5, new=0),
        ],
        total_unique=10,
    )
    assert report.complete, "this run succeeded — the point is that it still reports"
    line = report.retry_line()
    assert "retried 9 time(s)" in line
    assert "41s" in line
    assert "cond:breast cancer x9" in line


def test_an_ingest_that_never_retried_says_nothing_at_all():
    """Silence is the signal that the source was healthy. A line printed every
    run would train a reader to skip it."""
    from medrag.trials.queries import CONDITION, CoverageReport, QueryYield, TrialQuery

    report = CoverageReport(
        set_key="rett", set_label="Rett",
        yields=[QueryYield(query=TrialQuery(CONDITION, "Rett syndrome"),
                           reported_total=3, fetched=3, new=3)],
        total_unique=3)
    assert report.retry_line() == ""
    assert RetryBudget().summary() == ""


def test_retry_counts_reach_the_stored_coverage_row():
    """Stored, not just printed, so 'was the registry healthy when we fetched
    this?' is answerable from the database months later."""
    import json
    import tempfile

    from medrag.trials.client import TrialRecord
    from medrag.trials.queries import CONDITION, CoverageReport, QueryYield, TrialQuery
    from medrag.trials.store import TrialStore

    store = TrialStore(Path(tempfile.mkdtemp()) / "t.db")
    store.upsert([TrialRecord(nct_id="NCT1", brief_title="t")], set_key="breast")
    store.record_coverage(CoverageReport(
        set_key="breast", set_label="Breast cancer",
        yields=[QueryYield(query=TrialQuery(CONDITION, "breast cancer"),
                           reported_total=1, fetched=1, new=1,
                           retries=3, retry_seconds=12.5)],
        total_unique=1))

    row = store.conn.execute(
        "SELECT yields FROM query_coverage WHERE set_key = 'breast'").fetchone()
    stored = json.loads(row["yields"])[0]
    assert stored["retries"] == 3
    assert stored["retry_seconds"] == 12.5
    store.close()


def test_the_budget_counts_reasons_separately():
    """41 x HTTP 500 and 12 x ConnectionError are different diagnoses of the
    same registry, and collapsing them into one number loses the distinction."""
    b = RetryBudget()
    b.attempts = 5
    b.record_retry("HTTP 500", 2.0)
    b.record_retry("HTTP 500", 8.0)
    b.record_retry("ConnectionError", 2.0)
    assert b.by_reason == {"HTTP 500": 2, "ConnectionError": 1}
    assert b.slept == 12.0
    assert not b.clean
    assert "HTTP 500 x2" in b.summary()


def test_the_budget_has_no_opinion_on_whether_the_fetch_was_complete():
    """Completeness is decided in exactly one place (`verify_ingest`). A retry
    budget that also graded outcomes would be a second opinion competing with
    it — the drift that put the marker vocabulary in markers.py after two
    modules disagreed about the same trial."""
    names = [n for n in dir(RetryBudget) if not n.startswith("_")]
    for banned in ("complete", "ok", "success", "verified"):
        assert not any(banned in n.lower() for n in names), \
            f"RetryBudget exposes {banned!r} — completeness is verify_ingest's call"


if __name__ == "__main__":
    import traceback

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except Exception:
                failures += 1
                print(f"FAIL  {name}")
                traceback.print_exc()
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
