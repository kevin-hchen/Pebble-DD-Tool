"""Tests for the public service (`public/`).

Zero retention is the product claim, so it is tested as a PROPERTY rather than
as an intention. The centrepiece is `test_a_submitted_sentinel_reaches_neither
_disk_nor_logs`: submit a string that exists nowhere else in the universe, then
search every log record the app emitted and every file under the data root and
the repo for it. A test that inspected the logger's configuration would pass
while a stray `logger.info(form)` two modules away defeated it.

The other properties, each with the mechanism it pins:

  * No filesystem writes on any request path — asserted by snapshotting the tree
    before and after a full round of requests.
  * The consent record cannot hold content — asserted by inspecting the type,
    the same construction as `test_the_answer_object_exposes_no_rate_and_cannot
    _compute_one` in test_faers.py.
  * Feature flags fail closed, including on a typo.
  * The terms cannot silently become untrue when the model provider changes.
  * Nothing in the internal app is importable from the public package.

No network: tests/netguard.py blocks sockets, which also proves the no-fetch
claim — a fetch-on-miss would raise there rather than pass silently.
"""

from __future__ import annotations

import io
import logging
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import netguard  # noqa: E402

netguard.install()

import pytest  # noqa: E402

fastapi = pytest.importorskip("fastapi", reason="the public service needs fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from medrag.trials.client import TrialRecord  # noqa: E402
from medrag.trials.store import TrialStore  # noqa: E402
from public import reqlog  # noqa: E402
from public.config import PublicConfig, _flag  # noqa: E402
from public.consent import ConsentRecord, ConsentRequired, require_consent  # noqa: E402
from public.terms import (  # noqa: E402
    audit_provider_disclosure,
    load_terms,
    provider_statement,
)

REPO = Path(__file__).resolve().parents[1]

#: A string that cannot occur naturally anywhere. If this shows up in a log or a
#: file, retention has been broken.
SENTINEL = "ZZQX-sentinel-7f3a91-never-retain-me-ZZQX"


def _snapshot_dir() -> Path:
    """A data root holding a small trial snapshot, shaped like a deployment."""
    root = Path(tempfile.mkdtemp())
    raw = root / "raw"
    raw.mkdir(parents=True)
    store = TrialStore(raw / "trials.db")
    store.upsert(
        [TrialRecord(nct_id=f"NCT{i:06d}", brief_title=f"Trial {i}",
                     overall_status="RECRUITING", conditions=["Colorectal Cancer"],
                     eligibility_criteria="Inclusion Criteria:\n* MSS (microsatellite stable)")
         for i in range(1, 6)],
        set_key="colorectal")
    store.close()
    return root


def _client(data_dir: Path | None = None, **env) -> TestClient:
    """Build the app against a given snapshot. Imported fresh each time so
    module-level config reflects the environment under test."""
    import importlib

    saved = {k: os.environ.get(k) for k in
             ("MEDRAG_DATA_DIR", "PUBLIC_FEATURE_MEMO", "PUBLIC_FEATURE_CLAIMS",
              "PUBLIC_FEATURE_LANDSCAPE", "MEDRAG_PROVIDER")}
    if data_dir is not None:
        os.environ["MEDRAG_DATA_DIR"] = str(data_dir)
    for k, v in env.items():
        os.environ[k] = v
    try:
        import public.main as main

        importlib.reload(main)
        return TestClient(main.app), main
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _CaptureLogs(logging.Handler):
    """Captures everything any logger emits, formatted, so the sentinel search
    covers stray log calls from anywhere — not only the access logger."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[str] = []

    def emit(self, record):
        try:
            self.records.append(self.format(record))
        except Exception:                       # noqa: BLE001
            self.records.append(str(record.msg))

    def text(self) -> str:
        return "\n".join(self.records)


# --------------------------------------------------------- THE SENTINEL TEST


def test_a_submitted_sentinel_reaches_neither_disk_nor_logs():
    """The product claim, tested the only way worth testing it.

    Submit the sentinel through every route that accepts input, then grep
    everything the process wrote — all log records, and every file under the
    data root, the repo's `out/`, and the system temp directory — for it.
    """
    data_dir = _snapshot_dir()
    client, _main = _client(data_dir)

    capture = _CaptureLogs()
    capture.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    root_logger = logging.getLogger()
    root_logger.addHandler(capture)
    access_logger = reqlog.get_logger()
    access_logger.addHandler(capture)

    # Also capture anything written straight to stdout/stderr, which is where a
    # print() debugging statement would land.
    out_buf, err_buf = io.StringIO(), io.StringIO()
    real_out, real_err = sys.stdout, sys.stderr
    before = _tree_state(data_dir)
    temp_before = _tree_state_shallow(Path(tempfile.gettempdir()))

    try:
        sys.stdout, sys.stderr = out_buf, err_buf
        client.post("/landscape", data={"condition": SENTINEL, "biomarker": SENTINEL})
        client.get(f"/landscape?condition={SENTINEL}")   # a stale bookmarked URL
        client.post("/landscape", data={"condition": "colorectal cancer",
                                        "biomarker": "MSS", "location": SENTINEL})
        client.post("/memo", data={"asset": SENTINEL, "indication": SENTINEL,
                                   "consent": "yes"})
        client.post("/claims", data={"claims_text": SENTINEL, "consent": "yes"})
        client.get("/", params={"q": SENTINEL})
    finally:
        sys.stdout, sys.stderr = real_out, real_err
        root_logger.removeHandler(capture)
        access_logger.removeHandler(capture)

    haystacks = {
        "log records": capture.text(),
        "stdout": out_buf.getvalue(),
        "stderr": err_buf.getvalue(),
    }
    for label, text in haystacks.items():
        assert SENTINEL not in text, f"the submitted string was written to {label}"

    # Nothing new on disk, and the sentinel nowhere in any file that exists.
    assert _tree_state(data_dir) == before, "a request created or changed a file"
    for root in (data_dir, REPO / "out"):
        hit = _grep_tree(root, SENTINEL)
        assert hit is None, f"the submitted string was found on disk at {hit}"
    # The system temp directory is SHARED, so it is scanned for files this
    # request window actually created or changed rather than in full. Grepping
    # all of /tmp made the test fail whenever any unrelated process on the
    # machine had written the sentinel there — including a captured log of this
    # very test's earlier run, which is how it was found. That is a false
    # positive about a different process, and a suite that fails for reasons
    # outside the code under test is a suite people learn to re-run.
    #
    # The property is unweakened: a file the app wrote during the window is by
    # definition new or newer, so it is still scanned.
    hit = _grep_tree_changed(Path(tempfile.gettempdir()), SENTINEL, temp_before)
    assert hit is None, f"the submitted string was found on disk at {hit}"

    # Anti-vacuity: the sentinel really did reach the app, so "not found" is a
    # statement about retention rather than about a request that never happened.
    assert capture.records, "no log records at all — the search proves nothing"


def _tree_state(root: Path) -> dict:
    return {str(p.relative_to(root)): p.stat().st_mtime_ns
            for p in sorted(root.rglob("*")) if p.is_file()}


def _tree_state_shallow(root: Path, max_files: int = 20000) -> dict:
    """mtime by path, tolerating a tree that changes while it is walked — the
    system temp directory belongs to the whole machine, not to this test."""
    out: dict = {}
    if not root.exists():
        return out
    for i, path in enumerate(root.rglob("*")):
        if i > max_files:
            break
        try:
            if path.is_file():
                out[str(path)] = path.stat().st_mtime_ns
        except OSError:
            continue
    return out


def _grep_tree_changed(root: Path, needle: str, before: dict,
                       max_files: int = 20000) -> str | None:
    """Grep only files that are new or modified since `before` was taken."""
    if not root.exists():
        return None
    for i, path in enumerate(root.rglob("*")):
        if i > max_files:
            break
        try:
            if not path.is_file():
                continue
            key = str(path)
            if before.get(key) == path.stat().st_mtime_ns:
                continue
            if path.stat().st_size > 20_000_000:
                continue
            if needle in path.read_text(errors="ignore"):
                return key
        except OSError:
            continue
    return None


def _grep_tree(root: Path, needle: str, max_files: int = 4000) -> str | None:
    if not root.exists():
        return None
    checked = 0
    for path in root.rglob("*"):
        if not path.is_file() or checked > max_files:
            continue
        checked += 1
        try:
            if path.stat().st_size > 20_000_000:
                continue
            if needle.encode() in path.read_bytes():
                return str(path)
        except OSError:
            continue
    return None


def test_the_access_log_records_four_values_and_has_nowhere_to_put_a_fifth():
    line = reqlog.RequestLogLine("GET", "/landscape", 200, 12.3)
    rendered = line.render()
    assert rendered == "GET /landscape 200 12.3ms"
    fields = set(reqlog.RequestLogLine.__dataclass_fields__)
    assert fields == {"method", "route", "status", "duration_ms"}
    for banned in ("body", "query", "params", "form", "content", "text", "payload"):
        assert not any(banned in f for f in fields), \
            f"RequestLogLine gained a field that can hold {banned}"


def test_the_servers_own_access_log_is_silenced_at_import():
    """A REAL leak the unit tests could not see, found by grepping a live
    server's log for a sentinel.

    uvicorn writes its own access line — `GET /landscape?condition=<what the
    visitor typed>` — independently of this application's careful four-field
    logger. `TestClient` drives the ASGI app directly and never starts that
    logger, so every test here passed while a real deployment leaked every
    search term into its log.

    Pinned as a property of importing the app, not of a command-line flag: a
    promise that depends on remembering `--no-access-log` fails the first time
    someone writes a new systemd unit.
    """
    import logging as _logging

    import public.main  # noqa: F401  - importing is what must silence them

    for name in ("uvicorn.access", "gunicorn.access", "hypercorn.access"):
        logger = _logging.getLogger(name)
        assert logger.disabled, f"{name} is still enabled and logs full request lines"
        assert not logger.handlers, f"{name} still has handlers attached"
        assert logger.level > _logging.CRITICAL, f"{name} would still emit"

    # And it really does swallow a line containing a secret.
    captured = _CaptureLogs()
    root = _logging.getLogger()
    root.addHandler(captured)
    try:
        _logging.getLogger("uvicorn.access").info(
            '127.0.0.1 - "GET /landscape?condition=%s HTTP/1.1" 200', SENTINEL)
    finally:
        root.removeHandler(captured)
    assert SENTINEL not in captured.text(), \
        "the server access logger still emitted a request line"


def test_an_unmatched_path_is_not_logged_verbatim():
    """A 404's path is visitor-supplied text. Recording it verbatim would put
    whatever was probed straight into the log."""
    assert reqlog.safe_route(None, f"/{SENTINEL}") == "/<unmatched>"
    assert SENTINEL not in reqlog.safe_route(None, f"/x/{SENTINEL}")
    assert reqlog.safe_route("/landscape", "/landscape?condition=secret") == "/landscape"


# ------------------------------------------------------------------ consent


def test_the_consent_record_cannot_be_given_a_field_that_holds_content():
    """Enforced at the type level, the way FAERSAnswer forbids a rate."""
    record = ConsentRecord(terms_version="2026-08-10-draft-1")
    assert set(ConsentRecord.__slots__) == {"_at", "_terms_version"}

    for attr in ("query", "text", "asset", "document", "claims", "ip", "email", "body"):
        try:
            setattr(record, attr, "content")
            raise AssertionError(f"a consent record accepted a {attr!r} attribute")
        except AttributeError:
            pass

    public_names = [n for n in dir(record) if not n.startswith("__")]
    for banned in ("text", "content", "body", "query", "asset", "document",
                   "claim", "ip", "address", "email", "user", "name"):
        assert not any(banned in n.lower() for n in public_names), \
            f"ConsentRecord exposes {banned!r}; the terms say it stores two values"
    assert SENTINEL not in repr(record)


def test_consent_is_refused_by_default_and_never_carried_over():
    with pytest.raises(ConsentRequired):
        require_consent(False, "v1")
    # There is no store, no session and no cookie in the module — a second call
    # with False must fail exactly as the first did.
    with pytest.raises(ConsentRequired):
        require_consent(False, "v1")
    assert require_consent(True, "v1").terms_version == "v1"


def test_a_consent_record_needs_the_version_it_was_given_under():
    with pytest.raises(ValueError):
        ConsentRecord(terms_version="")


def test_a_flagged_off_feature_refuses_before_it_looks_at_consent():
    """A disabled feature must not be probeable for behaviour by sending it a
    body — the flag check comes first, so the response is the same whether or
    not consent was ticked."""
    client, _ = _client(_snapshot_dir())
    with_consent = client.post("/memo", data={"asset": "x", "consent": "yes"})
    without = client.post("/memo", data={"asset": "x"})
    assert with_consent.status_code == without.status_code == 404
    assert with_consent.text == without.text


def test_the_consent_checkbox_is_unchecked_and_required_in_the_markup():
    """An unticked checkbox is absent from the POST body, so 'not consented' is
    the default by construction rather than by a Python default someone could
    flip."""
    import re as _re

    raw = (REPO / "public" / "templates" / "_consent.html").read_text()
    # Strip the Jinja comment block first: it EXPLAINS that there is no `checked`
    # attribute, so a naive substring search finds the word it is describing.
    markup = _re.sub(r"\{#.*?#\}", "", raw, flags=_re.DOTALL)

    checkbox = _re.search(r"<input[^>]*type=\"checkbox\"[^>]*>", markup)
    assert checkbox, "no checkbox in the consent control"
    tag = checkbox.group(0)
    assert "checked" not in tag, "the consent box is pre-ticked"
    assert "required" in tag
    assert 'name="consent"' in tag
    assert "/terms" in markup, "the consent control must link the full terms"


# ------------------------------------------------------------- feature flags


def test_flags_fail_closed_when_absent_empty_or_mistyped():
    saved = os.environ.get("PUBLIC_FEATURE_MEMO")
    try:
        os.environ.pop("PUBLIC_FEATURE_MEMO", None)
        assert _flag("PUBLIC_FEATURE_MEMO").enabled is False

        for value in ("", "0", "false", "off", "no", "treu", "yes please", "TRUE-ish", "2"):
            os.environ["PUBLIC_FEATURE_MEMO"] = value
            flag = _flag("PUBLIC_FEATURE_MEMO")
            assert flag.enabled is False, f"{value!r} enabled a feature"

        # A mistyped value is REPORTED, not silently treated as off — otherwise
        # it looks identical to a deployer deliberately leaving it off.
        os.environ["PUBLIC_FEATURE_MEMO"] = "treu"
        assert _flag("PUBLIC_FEATURE_MEMO").note

        for value in ("1", "true", "TRUE", "yes", "on"):
            os.environ["PUBLIC_FEATURE_MEMO"] = value
            assert _flag("PUBLIC_FEATURE_MEMO").enabled is True
    finally:
        if saved is None:
            os.environ.pop("PUBLIC_FEATURE_MEMO", None)
        else:
            os.environ["PUBLIC_FEATURE_MEMO"] = saved


def test_the_default_configuration_ships_memo_and_claims_off_and_landscape_on():
    cfg = PublicConfig()
    assert cfg.landscape.enabled is True
    assert cfg.memo.enabled is False
    assert cfg.claims.enabled is False


def test_the_shipped_routes_reflect_the_shipped_flags():
    client, _ = _client(_snapshot_dir())
    assert client.post("/landscape", data={"condition": "colorectal cancer",
                                           "biomarker": "MSS"}).status_code == 200
    assert client.post("/memo", data={"asset": "x", "consent": "yes"}).status_code == 404
    assert client.post("/claims", data={"claims_text": "x", "consent": "yes"}).status_code == 404


def test_a_flagged_on_feature_actually_becomes_reachable():
    """Anti-vacuity for the flag tests: OFF has to be distinguishable from
    'never wired up'."""
    client, _ = _client(_snapshot_dir(), PUBLIC_FEATURE_MEMO="1")
    response = client.post("/memo", data={"asset": "x", "indication": "y", "consent": "yes"})
    assert response.status_code != 404, "turning the flag on changed nothing"


# ------------------------------------------------------------------- terms


def test_configuring_a_hosted_provider_without_updating_the_terms_is_an_error():
    """The lint that stops the terms silently becoming untrue — the same
    principle as phrasing.audit()."""
    terms = load_terms()
    assert audit_provider_disclosure("none", terms) == [], \
        "the shipped terms and the shipped configuration disagree"
    assert audit_provider_disclosure("ollama", terms) == [], \
        "a local model keeps text on the box; the terms already allow for that"

    for hosted in ("openai", "groq", "anthropic"):
        problems = audit_provider_disclosure(hosted, terms)
        assert problems, f"configuring {hosted} did not trip the terms lint"
        assert any("does not name it" in p or "now false" in p for p in problems)


def test_the_provider_statement_names_a_third_party_and_never_implies_local():
    hosted = provider_statement("openai")
    assert hosted.is_local is False
    assert "OpenAI" in hosted.sentence and "third-party" in hosted.sentence

    # The two local cases say different things and both must be honest: `none`
    # uses no model at all, `ollama` runs one on the box.
    nothing = provider_statement("none")
    assert nothing.is_local is True
    assert "stays on this server" in nothing.sentence

    local_model = provider_statement("ollama")
    assert local_model.is_local is True
    assert "stays on this machine" in local_model.sentence

    # Neither local sentence may name a hosted provider.
    for sentence in (nothing.sentence, local_model.sentence):
        assert "OpenAI" not in sentence and "Groq" not in sentence

    # And no sentence may contain the claim it denies — the phrasing lint that
    # exists because this exact trap has now bitten five times.
    from medrag.phrasing import CLAIM_PHRASES

    for sentence in (nothing.sentence, local_model.sentence):
        for phrase in CLAIM_PHRASES["retention"]:
            assert phrase not in sentence.lower(), \
                f"a retention denial contains the claim {phrase!r}: {sentence!r}"


def test_the_terms_carry_a_version_and_the_consent_record_stores_it():
    terms = load_terms()
    assert terms.version
    client, main = _client(_snapshot_dir())
    assert main.TERMS.version == terms.version
    assert terms.version in client.get("/").text


def test_the_shipped_terms_state_each_retention_claim():
    text = load_terms().markdown.lower()
    for claim in ("never written to disk", "never logged", "never used to train",
                  "never read by our staff"):
        assert claim in text, f"the terms no longer state: {claim}"


# ------------------------------------------------- separation from the internal app


def test_the_public_package_cannot_reach_the_internal_streamlit_app():
    """The separation is the security property. Checked on the import graph, so
    'nothing internal is reachable' is a fact about the code rather than about
    which routes happen to be registered."""
    import public.config
    import public.consent
    import public.data
    import public.main
    import public.ratelimit
    import public.reqlog
    import public.terms

    for module in (public.main, public.data, public.config, public.consent,
                   public.terms, public.ratelimit, public.reqlog):
        source = Path(module.__file__).read_text()
        for banned in ("import streamlit", "from streamlit", "import app",
                       "setup_env", "write_env", "pages."):
            assert banned not in source, \
                f"{module.__name__} reaches the internal app via {banned!r}"


def _imported_names(path: Path) -> set[str]:
    """Every module and symbol a file actually imports.

    Parsed with `ast` rather than grepped, because a docstring that NAMES the
    modules it deliberately avoids — which `public/data.py` does, on purpose —
    would fail a substring search. A test that cannot survive its own subject
    documenting itself is testing the prose, not the code.
    """
    import ast

    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            for alias in node.names:
                names.add(alias.name)
    return names


def test_no_module_on_the_public_read_path_can_fetch():
    """A miss must be a miss. Asserted on the import graph rather than on
    behaviour, because the guarantee wanted is that the fetching code is not
    reachable — not that a flag currently says no."""
    import public.data

    names = _imported_names(Path(public.data.__file__))
    for banned in ("autoload", "fetch_query_set", "search_trials", "ingest_pubmed",
                   "requests", "httpx", "urllib", "socket"):
        assert not any(banned in n for n in names), \
            f"public/data.py imports {banned!r}: {sorted(n for n in names if banned in n)}"


def test_the_public_app_advertises_no_api_docs():
    client, main = _client(_snapshot_dir())
    assert main.app.openapi_url is None
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


# ------------------------------------------------------------------- the page


def test_every_result_carries_the_coverage_statement_and_the_snapshot_date():
    client, _ = _client(_snapshot_dir())
    html = client.post("/landscape", data={"condition": "colorectal cancer",
                                           "biomarker": "MSS"}).text
    assert "What was searched" in html
    assert "Data snapshot" in html
    # Not collapsible: no disclosure element wrapping either block.
    assert "<details" not in html


def test_the_page_states_it_is_not_medical_advice_and_that_eligibility_is_indicative():
    client, _ = _client(_snapshot_dir())
    html = client.post("/landscape", data={"condition": "colorectal cancer",
                                           "biomarker": "MSS"}).text
    assert "not medical advice" in html.lower()
    assert "indicative" in html.lower()
    assert "trial team" in html.lower()


def test_each_row_shows_the_criterion_sentence_behind_its_call():
    client, _ = _client(_snapshot_dir())
    html = client.post("/landscape", data={"condition": "colorectal cancer",
                                           "biomarker": "MSS"}).text
    assert "The criterion this is based on" in html
    assert "microsatellite stable" in html.lower(), \
        "the matched criterion text is not on the page"


def test_results_are_hard_capped_regardless_of_what_is_asked_for():
    root = Path(tempfile.mkdtemp())
    raw = root / "raw"
    raw.mkdir(parents=True)
    store = TrialStore(raw / "trials.db")
    store.upsert(
        [TrialRecord(nct_id=f"NCT{i:06d}", brief_title=f"Trial {i}",
                     overall_status="RECRUITING", conditions=["Colorectal Cancer"],
                     eligibility_criteria="Inclusion Criteria:\n* MSS")
         for i in range(1, 121)], set_key="colorectal")
    store.close()

    from public.data import run_landscape

    result = run_landscape(root, condition="colorectal cancer", biomarker="MSS",
                           max_results=30)
    assert len(result.landscape.trials) <= 30

    client, _ = _client(root)
    html = client.post("/landscape", data={"condition": "colorectal cancer",
                                           "biomarker": "MSS"}).text
    assert html.count("clinicaltrials.gov/study/") <= 30


def test_a_missing_snapshot_reads_as_not_searched_never_as_no_trials_exist():
    empty = Path(tempfile.mkdtemp())
    (empty / "raw").mkdir()
    client, _ = _client(empty)
    response = client.post("/landscape", data={"condition": "colorectal cancer",
                                               "biomarker": "MSS"})
    assert response.status_code == 503
    body = response.text.lower()
    assert "nothing has been searched" in body
    assert "not a finding that no trials exist" in body


# ------------------------------------------- the search never enters a URL


def test_the_search_is_a_post_so_the_terms_never_reach_a_url():
    """Silencing our logger and uvicorn's closes two layers. Browser history,
    the Referer header, nginx, Cloudflare and every PaaS request log are layers
    this application cannot silence — so the search must not be in the part of
    the request all of them record by default."""
    client, main = _client(_snapshot_dir())

    methods = {}
    for route in main.app.routes:
        if getattr(route, "path", "") in ("/landscape", "/landscape.pdf"):
            methods.setdefault(route.path, set()).update(route.methods - {"HEAD"})

    assert "POST" in methods["/landscape"], "the search must accept POST"
    assert methods["/landscape.pdf"] == {"POST"}, \
        "the PDF download must not be a GET; a download URL carrying the search " \
        "is if anything more likely to be logged and shared than a page one"

    # The GET form route must not accept the search as parameters, or a stale
    # bookmark would reopen the hole.
    response = client.get(f"/landscape?condition={SENTINEL}&biomarker={SENTINEL}")
    assert response.status_code == 200
    assert SENTINEL not in response.text, \
        "a query-string search was echoed back, so the GET route still honours it"


def test_the_form_markup_posts_rather_than_gets():
    markup = (REPO / "public" / "templates" / "landscape.html").read_text()
    import re as _re

    for form in _re.findall(r"<form[^>]*>", markup):
        assert 'method="post"' in form, f"a form on the search page uses GET: {form}"
    assert "landscape.pdf?" not in markup, "the PDF is still linked with a query string"


def test_the_page_loads_nothing_from_a_third_party_domain():
    """A single external font or analytics tag would send the page URL — and on
    a GET page, the search itself — to someone else, and would tell that third
    party every visitor's IP regardless. Checked on the rendered HTML of a real
    result, not on the templates, so an absolute URL introduced by any layer is
    caught."""
    import re as _re

    client, _ = _client(_snapshot_dir())
    pages = [client.get("/").text,
             client.get("/landscape").text,
             client.post("/landscape", data={"condition": "colorectal cancer",
                                             "biomarker": "MSS"}).text,
             client.get("/terms").text]

    for html in pages:
        # Every src/href/action that loads or submits must be same-origin.
        for attr, url in _re.findall(r'(src|href|action)="([^"]+)"', html):
            if url.startswith(("/", "#", "mailto:")):
                continue
            # The only permitted absolute URLs are the citations themselves, and
            # they are user-followed links, not resources the page fetches.
            assert attr == "href", f"the page loads {url!r} from a third party"
            assert url.startswith("https://clinicaltrials.gov/"), \
                f"unexpected external {attr}: {url}"

        for banned in ("fonts.googleapis", "fonts.gstatic", "cdn.", "unpkg",
                       "jsdelivr", "googletagmanager", "google-analytics",
                       "@import url(http"):
            assert banned not in html, f"the page references {banned}"

        # No script at all on the public pages: nothing to exfiltrate with.
        assert "<script" not in html.lower()


def test_the_stylesheet_fetches_nothing_external():
    css = (REPO / "public" / "static" / "style.css").read_text()
    for banned in ("http://", "https://", "@import", "url("):
        assert banned not in css, f"the stylesheet references {banned!r}"


def test_a_referrer_policy_is_sent_so_outbound_links_leak_no_url():
    client, _ = _client(_snapshot_dir())
    response = client.post("/landscape", data={"condition": "colorectal cancer",
                                               "biomarker": "MSS"})
    assert response.headers.get("referrer-policy") == "no-referrer"
    assert response.headers.get("cache-control") == "no-store"


# ----------------------------------------------- the protective clauses


def _prose(text: str) -> str:
    """Collapse a document to comparable prose.

    Both the terms and the templates are hard-wrapped source, and phrases carry
    markdown emphasis or HTML tags mid-sentence — "does **not** restrict Pebble",
    "<strong>does not create a confidential\n relationship</strong>". Matching
    raw text finds neither, and loosening the assertions to short fragments
    would let a clause be deleted while a test still passed. Normalising is the
    honest fix: strip tags and emphasis, collapse whitespace, lowercase.
    """
    import re as _re

    text = _re.sub(r"<[^>]+>", " ", text)          # HTML tags
    text = _re.sub(r"[*_`]+", "", text)            # markdown emphasis
    text = _re.sub(r"\s+", " ", text)              # hard wrapping
    return text.lower()


def test_the_terms_state_what_a_submission_does_not_create():
    """Section 5 protects Pebble and is the reason unsolicited third-party
    material can be accepted at all. Each clause is pinned by a phrase, so
    deleting one fails the suite rather than passing quietly."""
    text = _prose(load_terms().markdown)
    required = {
        "no confidential relationship": "confidential relationship",
        "no non-disclosure obligation": "non-disclosure obligation",
        "no fiduciary duty": "fiduciary duty",
        "not a pitch / no investment relationship": "not a pitch",
        "no offer or solicitation": "solicitation of an offer",
        "no expectation of review or response": "no expectation",
        "no restriction on Pebble": "does not restrict pebble",
        "competitors explicitly covered": "compete directly with you",
        "others' submissions covered": "submitted through this service by someone",
        "submitter keeps rights": "claims no ownership",
        "no policing of submissions": "does not screen",
        "cannot verify right to submit": "right to submit",
    }
    for label, phrase in required.items():
        assert phrase in text, f"the terms no longer state: {label}"


def test_the_consent_control_names_the_protective_clauses_not_only_retention():
    """A submitter ticking the box must be agreeing to the clauses that matter
    at the moment of submission, not only to the retention promises."""
    markup = _prose((REPO / "public" / "templates" / "_consent.html").read_text())
    assert "does not create a confidential relationship" in markup
    assert "right to submit" in markup
    assert "received in confidence" in markup


def test_the_retention_claims_and_the_protective_clauses_are_kept_apart():
    """"We do not keep it" must not be readable as "so it is confidential" —
    the wrong inference, and the reason 5.1 says so in terms."""
    raw = load_terms().markdown
    assert "## 2. What we do with what you type" in raw
    assert "## 5. What submitting material does NOT create" in raw
    assert "nothing in section 2 limits this section" in _prose(raw)


# ---------------------------------------------------------------- rate limit


def test_requests_are_rate_limited_per_address():
    from public.ratelimit import RateLimiter

    limiter = RateLimiter(limit=3, window_seconds=60)
    assert [limiter.check("1.2.3.4").allowed for _ in range(4)] == [True, True, True, False]
    # A different address is unaffected.
    assert limiter.check("5.6.7.8").allowed is True
    assert limiter.check("1.2.3.4").retry_after_seconds > 0


def test_the_rate_limiter_stores_no_request_content():
    from public.ratelimit import RateLimiter

    limiter = RateLimiter(limit=2, window_seconds=60)
    limiter.check("1.2.3.4")
    assert SENTINEL not in repr(limiter.__dict__)
    for key, value in limiter._hits.items():
        assert isinstance(key, str)
        assert all(isinstance(v, float) for v in value)


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
