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

# Statuses that mean the trial stopped early. These drive the deterministic half
# of the negative-evidence pass: no model judgement, just a database query.
STOPPED_STATUSES = {"TERMINATED", "WITHDRAWN", "SUSPENDED"}

# Requesting only the modules we persist keeps responses small and stable.
DEFAULT_FIELDS = [
    "protocolSection.identificationModule",
    "protocolSection.statusModule",
    "protocolSection.designModule",
    "protocolSection.sponsorCollaboratorsModule",
    "protocolSection.conditionsModule",
    "protocolSection.armsInterventionsModule",
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

    @property
    def url(self) -> str:
        return f"https://clinicaltrials.gov/study/{self.nct_id}"

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

    nct_id = ident.get("nctId", "")
    if not nct_id:
        return None

    phases = design.get("phases") or []
    enrollment = design.get("enrollmentInfo") or {}
    lead = sponsor.get("leadSponsor") or {}

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
    )


def iter_studies(
    condition: str | None = None,
    intervention: str | None = None,
    sponsor: str | None = None,
    status: list[str] | None = None,
    term: str | None = None,
    max_records: int = 200,
    page_size: int = 100,
    timeout: int = 45,
    offline: bool = False,
) -> Iterator[TrialRecord]:
    """Yield TrialRecords for a registry query, following pageToken pagination."""
    if offline:
        raise RuntimeError(
            "offline mode is enabled: refusing to contact clinicaltrials.gov"
        )

    params: dict[str, Any] = {
        "pageSize": min(page_size, max_records, 1000),
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

    while seen < max_records:
        if page_token:
            params["pageToken"] = page_token
        _throttle()
        resp = requests.get(API_URL, params=params, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()

        studies = payload.get("studies") or []
        if not studies:
            return

        for study in studies:
            record = parse_study(study)
            if record is None:
                continue
            yield record
            seen += 1
            if seen >= max_records:
                return

        page_token = payload.get("nextPageToken")
        if not page_token:
            return


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
