"""ClinicalTrials.gov API v2 client.

    GET https://clinicaltrials.gov/api/v2/studies

No API key. Pagination is by opaque `nextPageToken`, not offset, so pages are
followed until the token disappears. The response nests everything under
protocolSection, and field names differ from the v1 API - anything written
against v1 will silently return nothing here.
"""

from __future__ import annotations

import random
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator

import requests

API_URL = "https://clinicaltrials.gov/api/v2/studies"


class IncompleteFetch(RuntimeError):
    """Pagination ended before the registry's own reported total was reached.

    Carries both numbers because "we have 500" is indistinguishable from "we have
    all of them" unless the denominator is stated. Silently keeping a truncated
    result set is the failure this whole module exists to prevent.
    """

    def __init__(self, query_label: str, fetched: int, reported_total: int):
        self.query_label, self.fetched, self.reported_total = (
            query_label, fetched, reported_total)
        super().__init__(
            f"the registry reported {reported_total} studies for {query_label} but "
            f"pagination yielded {fetched}. The store would silently hold a subset. "
            "Re-run the ingest; if it repeats, the registry changed under us mid-fetch."
        )

# Statuses that mean the trial stopped early. These drive the deterministic half
# of the negative-evidence pass: no model judgement, just a database query.
STOPPED_STATUSES = {"TERMINATED", "WITHDRAWN", "SUSPENDED"}

# Requesting only the modules we persist keeps responses small and stable. The
# eligibility/contacts/description trio is what the patient-facing trial landscape
# needs — who can enrol, where, and whom to contact — and is dead weight for the
# asset-diligence flow, but one field list keeps ingestion uniform.
DEFAULT_FIELDS = [
    "protocolSection.identificationModule",
    "protocolSection.statusModule",
    "protocolSection.designModule",
    "protocolSection.sponsorCollaboratorsModule",
    "protocolSection.conditionsModule",
    "protocolSection.armsInterventionsModule",
    "protocolSection.eligibilityModule",
    "protocolSection.contactsLocationsModule",
    "protocolSection.descriptionModule",
]

_LAST_CALL = {"t": 0.0}
_MIN_GAP = 0.25  # be a polite API citizen; there is no published hard limit

# --------------------------------------------------------------------- retry
#
# Measured, not anticipated: a 74-family ingest hit 41 HTTP 500s and 12 dropped
# connections across three passes, and every one of them downgraded an entire
# query set. A family is only as complete as its least lucky query — one blip
# anywhere in `breast`'s nine queries blocks the whole family — so a transient
# server error was costing a 14-minute re-fetch of thousands of studies that
# had already arrived intact.
#
# WHAT IS RETRYED IS DELIBERATELY NARROW. A 500, a 429, a timeout and a reset
# connection are the server declining to answer right now. A 400 or a 404 IS an
# answer — the query is malformed, or there is nothing there — and repeating it
# cannot change it, only make this tool look like something hammering an
# endpoint it does not understand.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

# Attempts, not retries: 4 means one try and three further goes. Capped because
# an unbounded retry converts "the registry is down" into "the ingest hangs
# forever", and an ingest nobody can wait out is its own silent failure.
_MAX_ATTEMPTS = 4

# Deliberately slow. openFDA's Purple Book taught this lesson expensively: three
# consecutive HTTP 404s that were really Akamai bot-detection, triggered by
# request rate, and nearly recorded as "this source does not exist". Backing off
# in fractions of a second is what a scraper does; these are chosen so a run of
# failures reads as a client waiting its turn.
#
# 2s, 8s, 32s (x4 growth), each with up to 50% jitter ADDED — never subtracted,
# so a backoff can only ever be longer than the floor, and concurrent clients
# spread out rather than retrying in lockstep.
_BACKOFF_BASE = 2.0
_BACKOFF_FACTOR = 4.0
_BACKOFF_JITTER = 0.5

# A server that says how long to wait outranks any local schedule. Capped so a
# stray Retry-After header cannot park an ingest for an hour; past the cap this
# gives up and lets the family record PARTIAL, which is a state the tool can
# report rather than one an operator has to sit and watch.
_MAX_RETRY_AFTER = 120.0


class RetryBudget:
    """Counts what retrying cost, so a slow ingest can say why it was slow.

    A source degrading quietly is the thing this codebase keeps guarding
    against, and retry is precisely a mechanism for converting a visible failure
    into an invisible delay. Without a count, "this took four minutes" and "this
    took four minutes because we retried forty times" are the same observation,
    and the second one is a source in trouble.

    Deliberately a plain counter with no success/failure verdict on it: whether
    the fetch as a whole was complete is `run_query`'s and `verify_ingest`'s
    call, and a retry budget that started grading outcomes would be a second
    opinion on completeness competing with the one place that owns it.
    """

    def __init__(self) -> None:
        self.attempts = 0        # total HTTP requests issued, including first tries
        self.retries = 0         # requests that were a repeat of a failed one
        self.slept = 0.0         # seconds spent waiting on backoff
        self.by_reason: dict[str, int] = {}
        self.exhausted = 0       # requests that used every attempt and still failed

    def record_retry(self, reason: str, slept: float) -> None:
        self.retries += 1
        self.slept += slept
        self.by_reason[reason] = self.by_reason.get(reason, 0) + 1

    def merge(self, other: "RetryBudget") -> None:
        self.attempts += other.attempts
        self.retries += other.retries
        self.slept += other.slept
        self.exhausted += other.exhausted
        for k, v in other.by_reason.items():
            self.by_reason[k] = self.by_reason.get(k, 0) + v

    @property
    def clean(self) -> bool:
        return self.retries == 0 and self.exhausted == 0

    def summary(self) -> str:
        """One line, printed only when there is something to report."""
        if self.clean:
            return ""
        reasons = ", ".join(f"{k} x{v}" for k, v in sorted(self.by_reason.items()))
        line = (f"{self.retries} retry/retries over {self.attempts} request(s), "
                f"{self.slept:.0f}s waiting ({reasons})")
        if self.exhausted:
            line += f"; {self.exhausted} request(s) exhausted every attempt and failed"
        return line


def _retry_after_seconds(resp) -> float | None:
    """The server's own instruction, when it sends one. Only the delta-seconds
    form is honoured — the HTTP-date form is legal but needs clock-skew handling
    to be safe, and guessing at a date the server meant is worse than falling
    back to the local schedule."""
    raw = (resp.headers.get("Retry-After") or "").strip() if resp is not None else ""
    if not raw:
        return None
    try:
        secs = float(raw)
    except ValueError:
        return None
    if secs < 0:
        return None
    return min(secs, _MAX_RETRY_AFTER)


def _backoff_seconds(attempt: int) -> float:
    """Jitter is ADDED to the floor, never subtracted: the point of the floor is
    to be polite, and a jitter that can shorten it undoes that on exactly the
    retries that matter most."""
    base = _BACKOFF_BASE * (_BACKOFF_FACTOR ** (attempt - 1))
    return base + random.uniform(0, base * _BACKOFF_JITTER)


def _get_with_retry(url: str, params: dict, timeout: int,
                    budget: "RetryBudget | None" = None):
    """One GET, retried on transient failure only. Returns the response.

    Raises the ORIGINAL exception when attempts run out, rather than a wrapper:
    the caller's error handling, the coverage report and the operator all
    already read those, and burying a 500 inside a RetryExhausted would mean
    every consumer needed teaching about a new type to say the same thing.

    A failure that exhausts the budget is still a failure — it propagates, the
    query records an error, and the family records PARTIAL. Retry shortens the
    odds; it never launders a failure into a success.
    """
    budget = budget if budget is not None else RetryBudget()
    last_exc: Exception | None = None

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        _throttle()
        budget.attempts += 1
        reason = ""
        wait: float | None = None
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            if resp.status_code in _RETRY_STATUSES:
                reason = f"HTTP {resp.status_code}"
                wait = _retry_after_seconds(resp)
                resp.raise_for_status()
            # Any other status — including 400 and 404 — is the server's answer.
            # raise_for_status turns a 4xx into an exception the caller handles,
            # and we do not retry it.
            resp.raise_for_status()
            return resp
        except (requests.Timeout, requests.ConnectionError) as exc:
            reason, last_exc = type(exc).__name__, exc
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status not in _RETRY_STATUSES:
                exc.retry_budget = budget
                raise
            reason, last_exc = reason or f"HTTP {status}", exc

        if attempt == _MAX_ATTEMPTS:
            budget.exhausted += 1
            # The count travels WITH the failure. A query that died after three
            # retries and one that died immediately both record an error, and
            # only this distinguishes "the registry is struggling" from "the
            # registry said no".
            last_exc.retry_budget = budget
            raise last_exc

        delay = wait if wait is not None else _backoff_seconds(attempt)
        budget.record_retry(reason, delay)
        time.sleep(delay)

    raise last_exc  # pragma: no cover - loop always returns or raises above


@dataclass
class TrialRecord:
    nct_id: str
    brief_title: str = ""
    phase: str = ""
    overall_status: str = ""
    why_stopped: str = ""
    enrollment_count: int | None = None
    enrollment_type: str = ""          # ACTUAL vs ESTIMATED - an unmet estimate is a signal
    # designModule.designInfo.allocation. Same class of gap as
    # detailed_description/keywords: designModule is already fetched whole
    # (part of DEFAULT_FIELDS) but this field was never parsed out. Used by
    # ranking.py as an evidence-quality signal, the same principle
    # evidence_grade.py applies to literature.
    allocation: str = ""               # RANDOMIZED | NON_RANDOMIZED | "" (not stated)
    lead_sponsor: str = ""
    sponsor_class: str = ""            # INDUSTRY, NIH, OTHER
    start_date: str = ""
    primary_completion_date: str = ""
    completion_date: str = ""
    study_type: str = ""
    conditions: list[str] = field(default_factory=list)
    # conditionsModule's other field. Already fetched (whole module requested)
    # and never parsed. Registry-chosen tags, not prose, so treated as the
    # least reliable of the biomarker-matching text sources — but real: C-800-25
    # carries "MSS" and "Microsatellite stable" here verbatim.
    keywords: list[str] = field(default_factory=list)
    interventions: list[str] = field(default_factory=list)
    collaborators: list[str] = field(default_factory=list)

    # --- patient-perspective fields (trial landscape) ---
    brief_summary: str = ""
    # descriptionModule's other field. Fetched all along (it is inside
    # DEFAULT_FIELDS' descriptionModule) but never parsed out — ADG126-P001
    # states its MSS focus only here ("...with a focus on MSS CRC"), nowhere in
    # eligibility_criteria or brief_summary. Consulted by the biomarker matchers
    # only when eligibility_criteria itself carries no signal for a marker; see
    # markers.collect_signals.
    detailed_description: str = ""
    eligibility_criteria: str = ""     # the full inclusion/exclusion text
    minimum_age: str = ""              # "18 Years" as the registry states it
    maximum_age: str = ""
    sex: str = ""                      # ALL | FEMALE | MALE
    healthy_volunteers: bool | None = None
    overall_officials: list[dict] = field(default_factory=list)   # name, role, affiliation
    central_contacts: list[dict] = field(default_factory=list)    # name, email, phone
    locations: list[dict] = field(default_factory=list)           # facility, city, state, country, status

    @property
    def url(self) -> str:
        return f"https://clinicaltrials.gov/study/{self.nct_id}"

    @property
    def principal_investigator(self) -> dict | None:
        """The lead contact a patient would ask for. Prefer a named PI, then a
        study director, then whoever is listed — an official with no useful role
        is still better than a blank."""
        officials = [o for o in self.overall_officials if o.get("name")]
        if not officials:
            return None
        for want in ("PRINCIPAL_INVESTIGATOR", "STUDY_DIRECTOR", "STUDY_CHAIR"):
            for o in officials:
                if (o.get("role") or "").upper() == want:
                    return o
        return officials[0]

    @property
    def primary_contact(self) -> dict | None:
        """A contact a patient could actually reach. Central contacts are the
        recruiting desk; fall back to a location contact if the sponsor filed
        one there instead."""
        for c in self.central_contacts:
            if c.get("name") or c.get("email") or c.get("phone"):
                return c
        return None

    @property
    def stopped_early(self) -> bool:
        return self.overall_status.upper() in STOPPED_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TrialRecord":
        return cls(**d)

    def summary(self) -> str:
        """One-line rendering for prompt context and memo tables."""
        bits = [self.nct_id]
        if self.phase:
            bits.append(self.phase)
        bits.append(self.overall_status or "UNKNOWN")
        if self.enrollment_count is not None:
            bits.append(f"n={self.enrollment_count}")
        if self.lead_sponsor:
            bits.append(self.lead_sponsor)
        line = " | ".join(bits)
        if self.why_stopped:
            line += f" | STOPPED: {self.why_stopped}"
        return line


def _throttle() -> None:
    elapsed = time.time() - _LAST_CALL["t"]
    if elapsed < _MIN_GAP:
        time.sleep(_MIN_GAP - elapsed)
    _LAST_CALL["t"] = time.time()


def _first(d: dict, *keys, default=""):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def parse_study(study: dict[str, Any]) -> TrialRecord | None:
    """Map one API v2 study object to a TrialRecord.

    Every module is optional in practice - registry records are frequently
    incomplete - so each lookup defaults rather than raising. A record with no
    NCT ID is unusable and returns None.
    """
    proto = study.get("protocolSection") or {}
    ident = proto.get("identificationModule") or {}
    status = proto.get("statusModule") or {}
    design = proto.get("designModule") or {}
    sponsor = proto.get("sponsorCollaboratorsModule") or {}
    conds = proto.get("conditionsModule") or {}
    arms = proto.get("armsInterventionsModule") or {}
    elig = proto.get("eligibilityModule") or {}
    contacts = proto.get("contactsLocationsModule") or {}
    desc = proto.get("descriptionModule") or {}

    nct_id = ident.get("nctId", "")
    if not nct_id:
        return None

    phases = design.get("phases") or []
    enrollment = design.get("enrollmentInfo") or {}
    lead = sponsor.get("leadSponsor") or {}

    officials = [
        {"name": o.get("name", ""), "role": o.get("role", ""),
         "affiliation": o.get("affiliation", "")}
        for o in (contacts.get("overallOfficials") or []) if o.get("name")
    ]
    central = [
        {"name": c.get("name", ""), "email": c.get("email", ""), "phone": c.get("phone", "")}
        for c in (contacts.get("centralContacts") or [])
        if c.get("name") or c.get("email") or c.get("phone")
    ]
    locations = [
        {"facility": loc.get("facility", ""), "city": loc.get("city", ""),
         "state": loc.get("state", ""), "country": loc.get("country", ""),
         "status": loc.get("status", ""),
         # Recruiting trials nest a per-site contacts[] here — the coordinator at
         # the patient's own site, which is more actionable than the overall
         # study chair. Kept as name/role/email/phone.
         "contacts": [
             {"name": c.get("name", ""), "role": c.get("role", ""),
              "email": c.get("email", ""), "phone": c.get("phone", "")}
             for c in (loc.get("contacts") or [])
             if c.get("name") or c.get("email") or c.get("phone")
         ]}
        for loc in (contacts.get("locations") or [])
    ]

    return TrialRecord(
        nct_id=nct_id,
        brief_title=ident.get("briefTitle", ""),
        # Multi-phase trials list both, e.g. ["PHASE2", "PHASE3"].
        phase="/".join(p.replace("PHASE", "Phase ") for p in phases) if phases else "",
        overall_status=status.get("overallStatus", ""),
        why_stopped=status.get("whyStopped", "").strip(),
        enrollment_count=enrollment.get("count"),
        enrollment_type=enrollment.get("type", ""),
        allocation=(design.get("designInfo") or {}).get("allocation", ""),
        lead_sponsor=lead.get("name", ""),
        sponsor_class=lead.get("class", ""),
        start_date=_first(status.get("startDateStruct") or {}, "date"),
        primary_completion_date=_first(status.get("primaryCompletionDateStruct") or {}, "date"),
        completion_date=_first(status.get("completionDateStruct") or {}, "date"),
        study_type=design.get("studyType", ""),
        conditions=list(conds.get("conditions") or []),
        keywords=list(conds.get("keywords") or []),
        interventions=[
            i.get("name", "") for i in (arms.get("interventions") or []) if i.get("name")
        ],
        collaborators=[c.get("name", "") for c in (sponsor.get("collaborators") or []) if c.get("name")],
        brief_summary=(desc.get("briefSummary") or "").strip(),
        detailed_description=(desc.get("detailedDescription") or "").strip(),
        eligibility_criteria=(elig.get("eligibilityCriteria") or "").strip(),
        minimum_age=elig.get("minimumAge", ""),
        maximum_age=elig.get("maximumAge", ""),
        sex=elig.get("sex", ""),
        # The API sends a real boolean here; keep None distinct from False so
        # "not stated" never reads as "no healthy volunteers".
        healthy_volunteers=elig.get("healthyVolunteers"),
        overall_officials=officials,
        central_contacts=central,
        locations=locations,
    )


@dataclass
class QueryResult:
    """One registry query's full result, with the denominator it was measured
    against. `reported_total` is the registry's own countTotal for the query, so
    a caller can assert it got everything instead of assuming it did."""
    records: list[TrialRecord] = field(default_factory=list)
    reported_total: int | None = None
    pages: int = 0
    truncated: bool = False       # a max_records override stopped us early
    skipped_no_id: int = 0        # studies the API returned with no NCT ID
    # What retrying cost. Carried on the result rather than logged and forgotten
    # so the ingest can report it: a query that took a minute because it was
    # retried eight times and a query that took a minute because it is large are
    # different facts about the registry's health.
    retries: RetryBudget = field(default_factory=RetryBudget)

    @property
    def complete(self) -> bool:
        """Did we get every study the registry said it had?"""
        if self.truncated or self.reported_total is None:
            return False
        return len(self.records) + self.skipped_no_id >= self.reported_total


def iter_studies(
    condition: str | None = None,
    intervention: str | None = None,
    sponsor: str | None = None,
    status: list[str] | None = None,
    term: str | None = None,
    max_records: int | None = None,
    page_size: int = 1000,
    timeout: int = 90,
    offline: bool = False,
    _result: QueryResult | None = None,
) -> Iterator[TrialRecord]:
    """Yield TrialRecords for a registry query, following pageToken pagination.

    `max_records=None` — the default — means fetch everything the query matches.
    A cap is an explicit testing override, never the default: the store's job is
    to hold the population, and a default cap silently redefined the population
    as "whatever the API happened to return first". A full colorectal fetch is
    10k studies in ~20s, so exhaustion is affordable.

    `_result` is an out-parameter carrying the registry's reported total back to
    the caller; a generator cannot return one. Use `run_query` rather than
    threading it by hand.
    """
    if offline:
        raise RuntimeError(
            "offline mode is enabled: refusing to contact clinicaltrials.gov"
        )

    out = _result if _result is not None else QueryResult()

    params: dict[str, Any] = {
        "pageSize": min(page_size, max_records or page_size, 1000),
        "fields": "|".join(DEFAULT_FIELDS),
        "countTotal": "true",
    }
    if condition:
        params["query.cond"] = condition
    if intervention:
        params["query.intr"] = intervention
    if sponsor:
        params["query.spons"] = sponsor
    if term:
        params["query.term"] = term
    if status:
        params["filter.overallStatus"] = "|".join(s.upper() for s in status)

    seen = 0
    page_token: str | None = None

    while max_records is None or seen < max_records:
        if page_token:
            params["pageToken"] = page_token
        # Retried per PAGE, which is the useful granularity: a 10,000-study
        # query is eleven requests, and losing the eleventh to a transient 500
        # used to discard the ten that had already succeeded.
        resp = _get_with_retry(API_URL, params, timeout, budget=out.retries)
        payload = resp.json()

        if out.reported_total is None:
            out.reported_total = payload.get("totalCount")

        studies = payload.get("studies") or []
        if not studies:
            return
        out.pages += 1

        for study in studies:
            record = parse_study(study)
            if record is None:
                out.skipped_no_id += 1
                continue
            yield record
            seen += 1
            if max_records is not None and seen >= max_records:
                out.truncated = True
                return

        page_token = payload.get("nextPageToken")
        if not page_token:
            return


def run_query(check_complete: bool = True, **kwargs) -> QueryResult:
    """Run one registry query to exhaustion and verify nothing was lost.

    Raises IncompleteFetch when pagination yielded fewer studies than the
    registry's own countTotal — the loud failure that stops a truncated store
    from being mistaken for a complete one. An explicit max_records override
    suppresses the check, since truncation is then the caller's intent.
    """
    result = QueryResult()
    result.records = list(iter_studies(_result=result, **kwargs))
    if check_complete and not result.truncated and result.reported_total is not None:
        if len(result.records) + result.skipped_no_id < result.reported_total:
            raise IncompleteFetch(
                _query_label(kwargs), len(result.records), result.reported_total)
    return result


def _query_label(kwargs: dict) -> str:
    bits = [f"{k}={v!r}" for k, v in kwargs.items()
            if k in ("condition", "intervention", "sponsor", "term") and v]
    return ", ".join(bits) or "an unfiltered query"


def search_trials(**kwargs) -> list[TrialRecord]:
    """Eager wrapper around iter_studies."""
    return list(iter_studies(**kwargs))


def fetch_trials(nct_ids: list[str], timeout: int = 45, offline: bool = False) -> list[TrialRecord]:
    """Fetch specific trials by NCT ID."""
    if offline:
        raise RuntimeError("offline mode is enabled: refusing to contact clinicaltrials.gov")
    if not nct_ids:
        return []

    records: list[TrialRecord] = []
    batch = 50
    for i in range(0, len(nct_ids), batch):
        ids = nct_ids[i : i + batch]
        resp = _get_with_retry(
            API_URL,
            {
                "filter.ids": "|".join(ids),
                "pageSize": len(ids),
                "fields": "|".join(DEFAULT_FIELDS),
            },
            timeout,
        )
        for study in resp.json().get("studies") or []:
            record = parse_study(study)
            if record:
                records.append(record)
    return records
