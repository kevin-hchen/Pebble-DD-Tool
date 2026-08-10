"""openFDA device API client — clearances, recalls, and adverse events.

    GET https://api.fda.gov/device/510k.json     clearances and predicates
    GET https://api.fda.gov/device/recall.json    recalls
    GET https://api.fda.gov/device/event.json      MAUDE adverse events

No API key is required. Pagination is skip/limit — NOT the opaque pageToken the
ClinicalTrials.gov client uses — and `skip` is capped at 25,000 by the API
(deeper paging needs Elasticsearch search_after, which this tool does not do).

Rate limits (openFDA, per the docs):
  * without an API key: 240 requests/minute and 1,000/day, per IP
  * with a free key (OPENFDA_API_KEY): 240/minute and 120,000/day, per key
A key is passed as the `api_key` query parameter when present.

Field realities confirmed against the live API on the FRN (infusion pump)
category, because the three endpoints do NOT share a schema and the docs paper
over the differences:

  * device/510k carries product_code at the top level; device/recall does too,
    plus a `k_numbers` array linking back to clearances. But device/event does
    NOT — its product code is nested at device[].device_report_product_code, so
    a top-level `product_code:FRN` search returns 404. Searches here use the
    nested field for events.
  * dates are ISO (YYYY-MM-DD) on 510k and recall, but compact (YYYYMMDD) on
    event.date_received. Both are stored verbatim and normalised only for display.
  * device/event is enormous — ~1.8M reports for one product code — so the event
    search is always bounded and severity-sorted by the caller, never dumped.
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator

import requests

API_BASE = "https://api.fda.gov"

# MAUDE event_type, worst first — a Death outranks a Malfunction when the pass
# can only show a handful.
EVENT_SEVERITY = {"Death": 0, "Injury": 1, "Malfunction": 2, "Other": 3, "No answer provided": 4}

_LAST_CALL = {"t": 0.0}
_MIN_GAP = 0.26  # ~230/min, just under the published 240/min ceiling


def _api_key() -> str | None:
    return os.getenv("OPENFDA_API_KEY") or os.getenv("FDA_API_KEY") or None


def _throttle() -> None:
    elapsed = time.time() - _LAST_CALL["t"]
    if elapsed < _MIN_GAP:
        time.sleep(_MIN_GAP - elapsed)
    _LAST_CALL["t"] = time.time()


# --------------------------------------------------------------------- records


@dataclass
class Clearance510k:
    k_number: str
    applicant: str = ""
    device_name: str = ""
    product_code: str = ""
    decision_code: str = ""
    decision_description: str = ""
    decision_date: str = ""
    date_received: str = ""
    clearance_type: str = ""
    advisory_committee: str = ""
    statement_or_summary: str = ""
    device_class: str = ""
    regulation_number: str = ""
    medical_specialty: str = ""

    @property
    def url(self) -> str:
        return (f"https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfPMN/pmn.cfm"
                f"?ID={self.k_number}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Clearance510k":
        return cls(**d)


@dataclass
class Recall:
    recall_number: str                 # product_res_number, e.g. "Z-0001-2011"
    cfres_id: str = ""
    product_code: str = ""
    device_class: str = ""
    product_description: str = ""
    reason_for_recall: str = ""
    recalling_firm: str = ""
    recall_status: str = ""
    root_cause_description: str = ""
    event_date_initiated: str = ""
    event_date_posted: str = ""
    k_numbers: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return (f"https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfRES/res.cfm"
                f"?id={self.cfres_id}") if self.cfres_id else ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Recall":
        return cls(**d)


@dataclass
class AdverseEvent:
    report_number: str
    mdr_report_key: str = ""
    event_type: str = ""               # Death | Injury | Malfunction | Other
    date_received: str = ""            # YYYYMMDD on live data
    product_code: str = ""
    device_class: str = ""
    brand_name: str = ""
    generic_name: str = ""
    manufacturer: str = ""
    product_problems: list[str] = field(default_factory=list)
    narrative: str = ""                # the mdr_text event description

    @property
    def severity_rank(self) -> int:
        return EVENT_SEVERITY.get(self.event_type, 5)

    @property
    def url(self) -> str:
        return (f"https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfmaude/detail.cfm"
                f"?mdrfoi__id={self.mdr_report_key}") if self.mdr_report_key else ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AdverseEvent":
        return cls(**d)


# --------------------------------------------------------------------- parsers


def parse_510k(rec: dict[str, Any]) -> Clearance510k | None:
    k = rec.get("k_number", "")
    if not k:
        return None
    of = rec.get("openfda") or {}
    return Clearance510k(
        k_number=k,
        applicant=rec.get("applicant", ""),
        device_name=rec.get("device_name", ""),
        product_code=(rec.get("product_code", "") or "").upper(),
        decision_code=rec.get("decision_code", ""),
        decision_description=rec.get("decision_description", ""),
        decision_date=rec.get("decision_date", ""),
        date_received=rec.get("date_received", ""),
        clearance_type=rec.get("clearance_type", ""),
        advisory_committee=rec.get("advisory_committee", ""),
        statement_or_summary=rec.get("statement_or_summary", ""),
        device_class=str(of.get("device_class", "") or ""),
        regulation_number=of.get("regulation_number", ""),
        medical_specialty=of.get("medical_specialty_description", ""),
    )


def parse_recall(rec: dict[str, Any]) -> Recall | None:
    rid = rec.get("product_res_number") or rec.get("cfres_id") or ""
    if not rid:
        return None
    of = rec.get("openfda") or {}
    return Recall(
        recall_number=rid,
        cfres_id=str(rec.get("cfres_id", "") or ""),
        product_code=(rec.get("product_code", "") or "").upper(),
        device_class=str(of.get("device_class", "") or ""),
        product_description=(rec.get("product_description", "") or "").strip(),
        reason_for_recall=(rec.get("reason_for_recall", "") or "").strip(),
        recalling_firm=rec.get("recalling_firm", ""),
        recall_status=rec.get("recall_status", ""),
        root_cause_description=rec.get("root_cause_description", ""),
        event_date_initiated=rec.get("event_date_initiated", ""),
        event_date_posted=rec.get("event_date_posted", ""),
        k_numbers=list(rec.get("k_numbers") or []),
    )


# The narrative worth surfacing is the event description; the manufacturer's
# follow-up narratives are noise for a first pass.
_EVENT_TEXT_PRIORITY = ("Description of Event or Problem",)


def parse_event(rec: dict[str, Any]) -> AdverseEvent | None:
    report = rec.get("report_number") or rec.get("mdr_report_key") or ""
    if not report:
        return None
    device = (rec.get("device") or [{}])[0]
    of = device.get("openfda") or {}

    texts = rec.get("mdr_text") or []
    narrative = ""
    for want in _EVENT_TEXT_PRIORITY:
        for t in texts:
            if (t.get("text_type_code") or "") == want and (t.get("text") or "").strip():
                narrative = t["text"].strip()
                break
        if narrative:
            break
    if not narrative:  # fall back to the first non-empty block
        narrative = next((t.get("text", "").strip() for t in texts if (t.get("text") or "").strip()), "")

    return AdverseEvent(
        report_number=str(report),
        mdr_report_key=str(rec.get("mdr_report_key", "") or ""),
        event_type=rec.get("event_type", ""),
        date_received=rec.get("date_received", ""),
        product_code=(device.get("device_report_product_code", "") or "").upper(),
        device_class=str(of.get("device_class", "") or ""),
        brand_name=device.get("brand_name", ""),
        generic_name=device.get("generic_name", ""),
        manufacturer=device.get("manufacturer_d_name", ""),
        product_problems=list(rec.get("product_problems") or []),
        narrative=narrative,
    )


# --------------------------------------------------------------------- fetch


def _fetch(endpoint: str, search: str, max_records: int, page_size: int,
           timeout: int, offline: bool) -> Iterator[dict]:
    """Skip/limit pagination over one openFDA endpoint. Yields raw result dicts.

    A 404 from openFDA means "no matches", not an error — it is swallowed and the
    iterator simply ends, so an asset with no recalls is silence, not a crash."""
    if offline:
        raise RuntimeError("offline mode is enabled: refusing to contact api.fda.gov")

    key = _api_key()
    seen = 0
    while seen < max_records:
        limit = min(page_size, max_records - seen, 1000)
        params = {"search": search, "limit": limit, "skip": seen}
        if key:
            params["api_key"] = key
        _throttle()
        resp = requests.get(f"{API_BASE}/{endpoint}.json", params=params, timeout=timeout)
        if resp.status_code == 404:
            return
        resp.raise_for_status()
        results = resp.json().get("results") or []
        if not results:
            return
        for raw in results:
            yield raw
            seen += 1
            if seen >= max_records:
                return
        if len(results) < limit:
            return
        # openFDA refuses skip beyond 25k; stop cleanly rather than 400.
        if seen >= 25000:
            return


def _fetch_json(endpoint: str, params: dict, timeout: int = 45) -> dict | None:
    """One arbitrary openFDA request through the shared throttle.

    Exists for `count=` aggregations, which return buckets rather than a
    `results` array of records and so do not fit `_fetch`. Kept here rather than
    in faers.py so every openFDA call in this package passes the same 240/min
    per-IP throttle — two independent throttles would each think they had the
    whole budget.
    """
    key = _api_key()
    query = dict(params)
    if key:
        query["api_key"] = key
    _throttle()
    resp = requests.get(f"{API_BASE}/{endpoint}.json", params=query, timeout=timeout)
    if resp.status_code == 404:      # openFDA's "no matches"
        return None
    resp.raise_for_status()
    return resp.json()


def _total(endpoint: str, search: str, timeout: int, offline: bool) -> int | None:
    """The openFDA-reported total for a search — one cheap request that reads
    meta.results.total, so the caller can say 'N of M' without paging everything."""
    if offline:
        raise RuntimeError("offline mode is enabled: refusing to contact api.fda.gov")
    params = {"search": search, "limit": 1}
    key = _api_key()
    if key:
        params["api_key"] = key
    _throttle()
    resp = requests.get(f"{API_BASE}/{endpoint}.json", params=params, timeout=timeout)
    if resp.status_code == 404:
        return 0
    resp.raise_for_status()
    return (resp.json().get("meta") or {}).get("results", {}).get("total")


def count_510k(product_code: str | None = None, device_name: str | None = None,
               timeout: int = 45, offline: bool = False) -> int | None:
    """How many 510(k) clearances openFDA holds for a product code / device name,
    regardless of how many are ingested locally."""
    terms = []
    if product_code:
        terms.append(f"product_code:{product_code.upper()}")
    if device_name:
        terms.append(f'device_name:"{device_name}"')
    if not terms:
        return None
    return _total("device/510k", " AND ".join(terms), timeout, offline)


def search_510k(product_code: str | None = None, device_name: str | None = None,
                applicant: str | None = None, max_records: int = 200,
                timeout: int = 45, offline: bool = False) -> list[Clearance510k]:
    """Clearances for a product code and/or device name. Note applicant is
    supported for completeness but is NOT the matching key — see store.py."""
    terms = []
    if product_code:
        terms.append(f"product_code:{product_code.upper()}")
    if device_name:
        terms.append(f'device_name:"{device_name}"')
    if applicant:
        terms.append(f'applicant:"{applicant}"')
    if not terms:
        return []
    # Space-separated AND, not "+AND+": requests URL-encodes a literal '+' to
    # %2B, which openFDA reads as a plus sign and the query breaks. A space is
    # encoded correctly and Elasticsearch treats "AND" as the operator.
    search = " AND ".join(terms)
    out = [parse_510k(r) for r in _fetch("device/510k", search, max_records, 100, timeout, offline)]
    return [c for c in out if c]


def search_recalls(product_code: str | None = None, device_name: str | None = None,
                   max_records: int = 100, timeout: int = 45,
                   offline: bool = False) -> list[Recall]:
    """Recalls for a product code (primary) or a device-description text match."""
    if product_code:
        search = f"product_code:{product_code.upper()}"
    elif device_name:
        search = f'product_description:"{device_name}"'
    else:
        return []
    out = [parse_recall(r) for r in _fetch("device/recall", search, max_records, 100, timeout, offline)]
    return [r for r in out if r]


def search_events(product_code: str | None = None, max_records: int = 100,
                  timeout: int = 45, offline: bool = False) -> list[AdverseEvent]:
    """MAUDE adverse events. The product code lives under device[], so the search
    field is device.device_report_product_code — a top-level product_code search
    404s. MAUDE is huge, so callers must keep max_records small and sort by
    severity themselves."""
    if not product_code:
        return []
    search = f"device.device_report_product_code:{product_code.upper()}"
    out = [parse_event(r) for r in _fetch("device/event", search, max_records, 100, timeout, offline)]
    return [e for e in out if e]
