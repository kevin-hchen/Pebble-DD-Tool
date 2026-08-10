"""The public service.

A SEPARATE application from `app.py`. Not the internal app with pages hidden —
a different ASGI app, in a different package, importing a different set of
modules. That distinction is the security property: nothing in the internal app
is reachable from the internet because none of it is mounted, not because a
route check declines to serve it. There is no Streamlit here, no `pages/`
import, no settings screen, no ingest, no `.env` writer.

Streamlit was the wrong shape for this regardless of security: it runs a Python
process per visitor and holds per-session state in memory, which is a poor fit
for campaign traffic and directly at odds with a service whose selling point is
that it keeps nothing.

WHAT EACH ROUTE MAY TOUCH
-------------------------
  GET  /                one template, no data access
  GET  /terms           the terms markdown, read from disk at import
  GET  /landscape       read-only snapshot query; no consent (nothing submitted)
  POST /memo            FLAGGED OFF; consent required; no filesystem write
  POST /claims          FLAGGED OFF; consent required; no filesystem write
  GET  /healthz         liveness; no data access
  GET  /static/*        stylesheet only

No route writes to the filesystem. There is no upload directory, no cache, no
`out/`, no `.env` access, and PDFs are streamed from memory.
"""

from __future__ import annotations

import io
import time
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from medrag.config import Config

from . import data as public_data
from .config import load_public_config
from .consent import ConsentRequired, require_consent
from .ratelimit import RateLimiter, client_address
from .reqlog import log_request, safe_route, silence_server_access_logs
from .terms import audit_provider_disclosure, load_terms, provider_statement

BASE_DIR = Path(__file__).resolve().parent

# FIRST, before anything can serve a request. uvicorn's own access log writes the
# full request line — query string included — which put submitted text into the
# log the terms promise never receives it. Caught by grepping a real server's log
# for a sentinel, not by the unit tests: TestClient never starts that logger.
silence_server_access_logs()

CFG = load_public_config()
TERMS = load_terms()
PROVIDER = provider_statement(Config().provider if CFG.memo.enabled or CFG.claims.enabled
                              else "none")
LIMITER = RateLimiter(CFG.rate_limit_requests, CFG.rate_limit_window_seconds)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(
    title="Trial finder",
    docs_url=None,       # no interactive docs: a public surface should not
    redoc_url=None,      # advertise an API this service does not offer
    openapi_url=None,
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def _nearest_site(trial) -> str:
    """The nearest listed site, formatted by the same helper the PDF uses.

    In the template rather than on the model would mean two renderers formatting
    a location differently; `_format_location` is already the one implementation
    and the public page calls it too.
    """
    from medrag.landscape import _format_location

    location = trial.nearest_location or (
        trial.record.locations[0] if trial.record.locations else None)
    if not location:
        return "—"
    text = _format_location(location)
    return f"{text} — {trial.proximity_label}" if trial.proximity_label else text


def _base_context(request: Request) -> dict:
    return {
        "request": request,
        "nearest_site": _nearest_site,
        "snapshot_date": public_data.snapshot_date(CFG.data_dir),
        "terms_version": TERMS.version,
        "provider": PROVIDER,
        "flags": CFG,
    }


@app.middleware("http")
async def request_middleware(request: Request, call_next):
    """Rate limit, time the request, log four values, and clear per-request state.

    The log call takes the matched ROUTE, never `request.url`, so a query string
    cannot reach the log. State is cleared in a `finally` so an exception path
    cannot leave a submission attached to the request object for whatever
    handles the error.
    """
    started = time.monotonic()
    decision = LIMITER.check(client_address(request))
    if not decision.allowed:
        response = PlainTextResponse(
            "Too many requests from this address. Please wait a moment.",
            status_code=429, headers={"Retry-After": str(decision.retry_after_seconds)})
    else:
        try:
            response = await call_next(request)
        finally:
            # Anything a handler stashed on the request dies here, before the
            # response leaves. Nothing in this app is supposed to put user
            # content on `state`; this makes that true even if something does.
            request.state.__dict__.clear()

    duration_ms = (time.monotonic() - started) * 1000
    route = safe_route(
        getattr(request.scope.get("route"), "path", None), request.url.path)
    log_request(request.method, route, response.status_code, duration_ms)
    # Conservative headers for a page that renders user-supplied strings.
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", _base_context(request))


@app.get("/terms", response_class=HTMLResponse)
async def terms_page(request: Request):
    ctx = _base_context(request)
    ctx["terms_markdown"] = TERMS.markdown
    return templates.TemplateResponse("terms.html", ctx)


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz():
    return "ok"


@app.get("/landscape", response_class=HTMLResponse)
async def landscape_form(request: Request):
    """The empty search form. Takes NO parameters, deliberately.

    See `landscape` below for why the search itself is a POST. Accepting
    `?condition=` here as a convenience would reopen the hole for anyone who
    linked or bookmarked such a URL.
    """
    ctx = _base_context(request)
    ctx.update(condition="", biomarker="", location="")
    if not CFG.landscape.enabled:
        ctx["error"] = "This feature is not enabled on this deployment."
        return templates.TemplateResponse("landscape.html", ctx, status_code=404)
    return templates.TemplateResponse("landscape.html", ctx)


@app.post("/landscape", response_class=HTMLResponse)
async def landscape(request: Request, condition: str = Form(""),
                    biomarker: str = Form(""), location: str = Form("")):
    """Trial landscape. A POST, and that is a privacy decision, not a REST one.

    A GET puts the search terms in the URL, and a URL is the most-copied string
    in the stack: it lands in browser history, in the Referer header of every
    external resource the page loads, in nginx and Cloudflare access logs, in a
    PaaS request log, in bookmarks, and in whatever a visitor pastes into chat.
    Silencing this application's logger and uvicorn's closes two layers of that;
    it cannot close the rest, because most of them are not ours.

    "We do not log searches" is a promise about a system, so the search must not
    be in the part of the request that every layer of that system records by
    default. A form body is not logged by nginx, Cloudflare or any ordinary PaaS
    unless someone deliberately turns body logging on.

    No consent gate: nothing is submitted in the terms' sense — a condition and a
    biomarker are a structured lookup against a public registry snapshot. The
    POST is about where the words travel, not about what they are.

    The cost, stated: results are not linkable or bookmarkable, and a refresh
    re-submits. That is the right trade for a service whose claim is that the
    search is not recorded anywhere.
    """
    ctx = _base_context(request)
    ctx.update(condition=condition, biomarker=biomarker, location=location)

    if not CFG.landscape.enabled:
        ctx["error"] = "This feature is not enabled on this deployment."
        return templates.TemplateResponse("landscape.html", ctx, status_code=404)

    if not condition.strip() or not biomarker.strip():
        return templates.TemplateResponse("landscape.html", ctx)

    try:
        result = public_data.run_landscape(
            CFG.data_dir, condition=condition.strip(), biomarker=biomarker.strip(),
            location=location.strip(),
            # Two caps, the smaller wins: the service's hard ceiling and the
            # landscape's own default. A caller cannot raise either.
            max_results=min(CFG.max_results, 30),
        )
    except public_data.SnapshotUnavailable as exc:
        ctx["error"] = str(exc)
        return templates.TemplateResponse("landscape.html", ctx, status_code=503)

    ctx["result"] = result
    return templates.TemplateResponse("landscape.html", ctx)


@app.post("/memo", response_class=HTMLResponse)
async def memo(request: Request, asset: str = Form(""), indication: str = Form(""),
               consent: str = Form("")):
    """Diligence memo. SHIPPED OFF.

    The flag is checked FIRST — before consent, before any parsing — so a
    disabled feature cannot be probed for behaviour by sending it a body.
    """
    ctx = _base_context(request)
    if not CFG.memo.enabled:
        ctx["error"] = "This feature is not enabled on this deployment."
        return templates.TemplateResponse("disabled.html", ctx, status_code=404)

    try:
        require_consent(bool(consent), TERMS.version)
    except ConsentRequired as exc:
        ctx["error"] = str(exc)
        return templates.TemplateResponse("disabled.html", ctx, status_code=400)

    ctx["error"] = ("Memo generation is built but not enabled on this deployment. "
                    "No text was processed.")
    return templates.TemplateResponse("disabled.html", ctx, status_code=503)


@app.post("/claims", response_class=HTMLResponse)
async def claims(request: Request, claims_text: str = Form(""), consent: str = Form("")):
    """Claim check. SHIPPED OFF. Same ordering as /memo."""
    ctx = _base_context(request)
    if not CFG.claims.enabled:
        ctx["error"] = "This feature is not enabled on this deployment."
        return templates.TemplateResponse("disabled.html", ctx, status_code=404)

    try:
        require_consent(bool(consent), TERMS.version)
    except ConsentRequired as exc:
        ctx["error"] = str(exc)
        return templates.TemplateResponse("disabled.html", ctx, status_code=400)

    ctx["error"] = ("Claim checking is built but not enabled on this deployment. "
                    "No text was processed.")
    return templates.TemplateResponse("disabled.html", ctx, status_code=503)


@app.post("/landscape.pdf")
async def landscape_pdf(condition: str = Form(""), biomarker: str = Form(""),
                        location: str = Form("")):
    """The same result as a PDF, rendered to a BytesIO and streamed.

    A POST for the same reason as the HTML route: a `GET /landscape.pdf?condition=…`
    download link would put the search back in a URL, and a download URL is if
    anything MORE likely to be logged and shared than a page one. The page
    therefore offers the PDF as a form button carrying the same fields, not as an
    anchor.

    Never written to disk: there is no path here that names a file. The internal
    app's exporter writes to `out/` under a user-derived filename, which is
    exactly what a public service must not do.
    """
    if not CFG.landscape.enabled:
        return PlainTextResponse("Not enabled.", status_code=404)
    if not condition.strip() or not biomarker.strip():
        return PlainTextResponse("Enter a condition and a biomarker.", status_code=400)

    result = public_data.run_landscape(
        CFG.data_dir, condition=condition.strip(), biomarker=biomarker.strip(),
        location=location.strip(), max_results=min(CFG.max_results, 30))

    from medrag.landscape_memo import render_pdf

    buffer = io.BytesIO()
    render_pdf(result.landscape, buffer)
    return Response(
        content=buffer.getvalue(), media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="trial-landscape.pdf"',
                 "Cache-Control": "no-store"},
    )


def startup_report() -> list[str]:
    """Problems a deployer must see at boot: a mistyped flag, and any place the
    published terms no longer describe the configuration."""
    notes = list(CFG.startup_notes())
    notes.extend(audit_provider_disclosure(PROVIDER.provider, TERMS))
    return notes
