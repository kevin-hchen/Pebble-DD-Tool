"""Request logging that cannot log a request's contents.

The terms say submitted text is never logged. The ordinary way to break that
promise is not malice, it is a default: every web framework's access log writes
the full request line, and a full request line contains the query string, which
on a search form contains the search terms. Turning that off is not enough
either — an exception traceback carries locals, and a debug log added later
carries whatever the author reached for.

So the log line is BUILT rather than filtered. `RequestLogLine` holds four
values — method, route TEMPLATE, status, milliseconds — and there is nowhere to
put a fifth. The route template is `/landscape`, never `/landscape?condition=…`:
the path a router matched, not the URL a visitor typed.

`tests/test_public_app.py` proves this the only way worth proving it: submit a
document containing a unique sentinel string, capture everything the logger
emitted, and grep it for the sentinel. A test that inspected the logger's
configuration would pass while a stray `logger.info(form)` two files away
defeated it.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass

LOGGER_NAME = "medrag.public.access"

#: Query-string keys whose VALUES are user content. Only the presence of a key
#: is ever recorded, never its value — and only for these, so a new form field
#: is invisible to the log rather than accidentally included.
_RECORDABLE_KEYS = frozenset({"page"})


@dataclass(frozen=True)
class RequestLogLine:
    """One access-log entry. Four fields, and no field can hold user content.

    `route` is the matched template. A raw path would leak a search term the
    moment anything is put in a URL segment, and the whole point of this type is
    that there is no way to write one down.
    """

    method: str
    route: str
    status: int
    duration_ms: float

    def render(self) -> str:
        return f"{self.method} {self.route} {self.status} {self.duration_ms:.1f}ms"


def get_logger() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    # Never hand these lines to the root logger, which a deployment may have
    # pointed at something that stores them with more context than we control.
    logger.propagate = False
    return logger


def log_request(method: str, route: str, status: int, duration_ms: float) -> RequestLogLine:
    line = RequestLogLine(method=method, route=route, status=status,
                          duration_ms=duration_ms)
    get_logger().info(line.render())
    return line


#: Loggers belonging to the SERVER rather than to this application, every one of
#: which writes a full request line — including the query string — by default.
#: Silenced at import, not by a deployment flag.
_SERVER_ACCESS_LOGGERS = ("uvicorn.access", "gunicorn.access", "hypercorn.access",
                          "granian.access", "werkzeug")


def silence_server_access_logs() -> list[str]:
    """Disable the ASGI server's own access log. Returns what was silenced.

    THIS EXISTS BECAUSE THE LIVE VERIFICATION CAUGHT A REAL LEAK. The
    application log built above is careful — four fields, route template, no
    query — and it was clean. But uvicorn writes its own line:

        INFO: 127.0.0.1:59284 - "GET /landscape?condition=<what the visitor
        typed>&biomarker=<...> HTTP/1.1" 200 OK

    which put submitted text straight into the log the terms promise never
    receives it. The unit tests could not see this: `TestClient` drives the ASGI
    app directly and never starts uvicorn's logger. It took running a real
    server and grepping the real log for a sentinel.

    Silenced HERE, at import of the app, rather than with `--no-access-log` on
    the command line: a promise that depends on a deployer remembering a flag is
    a promise that fails the first time someone writes a new systemd unit. The
    information is not lost — `log_request` records method, route, status and
    timing, which is everything an access log is actually used for, minus the
    part that cannot be written down.
    """
    silenced = []
    for name in _SERVER_ACCESS_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.disabled = True
        logger.propagate = False
        # A disabled logger can be re-enabled by a later `logging.config` call;
        # setting the level above CRITICAL means even that leaves nothing that
        # emits at the levels an access line uses.
        logger.setLevel(logging.CRITICAL + 1)
        silenced.append(name)
    return silenced


def safe_route(scope_route: str | None, path: str) -> str:
    """The route template if the router matched one, else a coarse fallback.

    The fallback is deliberately NOT the raw path. An unmatched request is
    usually a 404, and a 404's path is attacker- or visitor-supplied text that
    would land in the log verbatim. Recording the first path segment keeps the
    log useful for spotting scans without writing down what was probed.
    """
    if scope_route:
        return scope_route
    head = (path or "/").split("?", 1)[0].strip("/").split("/", 1)[0]
    return f"/{head}" if head.isalnum() else "/<unmatched>"
