"""ClinicalTrials.gov API v2 client.

    GET https://clinicaltrials.gov/api/v2/studies

No API key. Pagination is by opaque `nextPageToken`, not offset, so pages are
followed until the token disappears. The response nests everything under
protocolSection, and field names differ from the v1 API - anything written
against v1 will silently return nothing here.
"""

from __future__ import annotations

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


@dataclass
class TrialRecord:
    nct_id: str
    brief_title: str = ""
    phase: str = ""
    overall_status: str = ""
    why_stopped: str = ""
    enrollment_count: int | None = None
    enrollment_type: str = ""          # ACTUAL vs ESTIMATED - an unmet estimate is a signal
    lead_sponsor: str = ""
    sponsor_class: str = ""            # INDUSTRY, NIH, OTHER
    start_date: str = ""
    primary_completion_date: str = ""
    completion_date: str = ""
    study_type: str = ""
    conditions: list[str] = field(default_factory=list)
    interventions: list[str] = field(default_factory=list)
    collaborators: list[str] = field(default_factory=list)

    # --- patient-perspective fields (trial landscape) ---
    brief_summary: str = ""
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
        lead_sponsor=lead.get("name", ""),
        sponsor_class=lead.get("class", ""),
        start_date=_first(status.get("startDateStruct") or {}, "date"),
        primary_completion_date=_first(status.get("primaryCompletionDateStruct") or {}, "date"),
        completion_date=_first(status.get("completionDateStruct") or {}, "date"),
        study_type=design.get("studyType", ""),
        conditions=list(conds.get("conditions") or []),
        interventions=[
            i.get("name", "") for i in (arms.get("interventions") or []) if i.get("name")
        ],
        collaborators=[c.get("name", "") for c in (sponsor.get("collaborators") or []) if c.get("name")],
        brief_summary=(desc.get("briefSummary") or "").strip(),
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
        _throttle()
        resp = requests.get(API_URL, params=params, timeout=timeout)
        resp.raise_for_status()
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
        _throttle()
        resp = requests.get(
            API_URL,
            params={
                "filter.ids": "|".join(ids),
                "pageSize": len(ids),
                "fields": "|".join(DEFAULT_FIELDS),
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        for study in resp.json().get("studies") or []:
            record = parse_study(study)
            if record:
                records.append(record)
    return records
