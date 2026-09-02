"""Bulk-export ingestion for openFDA distributions — download, unzip, parse, date.

WHY THIS EXISTS AND WHY IT IS NOT PART OF client.py

Every other source in this tool paginates an API. `device/pma` cannot: openFDA
caps `skip` at exactly 25,000 (measured — skip=25000 returns 200, skip=25001
returns HTTP 400) against 56,853 PMA records, so the API can reach at most 44%
of the source and can never state a complete denominator. The bulk export can:
one partition, 20.9 MB, and its `total_records` matches the API's reported total
exactly.

That makes bulk a second ingestion MECHANISM, not a PMA quirk, and it is built
here as shared infrastructure because the Orange Book and Purple Book are bulk
distributions too and will need the same download / unzip / parse / freshness
handling. A per-source copy of this is how three matchers drifted apart before.

FRESHNESS IS `export_date`, AND REFRESH MEANS RE-DOWNLOAD

A paginated source can be refreshed incrementally — ask for what changed. A bulk
export cannot: FDA publishes a whole new file, so the only refresh is
re-downloading it. `BulkFreshness` therefore carries the export's OWN
`export_date` (what FDA says the data is), separately from `downloaded_at` (when
we took a copy), and `render_lines()` states both plus the fact that refresh is
a re-download. Implying incremental freshness for a source that has none would
be the same class of error as implying a capped sample is a census.

COMPLETENESS IS ASSERTED, NOT ASSUMED

`load_export` raises `IncompleteBulkExport` when the parsed record count does not
match the `total_records` the catalogue declared — the same fail-loudly choice
`trials.client.run_query` makes against `countTotal`. A bulk file that silently
truncates would redefine the population exactly the way the old 500-record trial
cap did.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

CATALOGUE_URL = "https://api.fda.gov/download.json"


class IncompleteBulkExport(RuntimeError):
    """A bulk export yielded fewer records than its catalogue declared.

    Carries both numbers so a caller can say how much is missing rather than
    reporting a short population as a whole one.
    """

    def __init__(self, key: str, parsed: int, declared: int):
        self.key, self.parsed, self.declared = key, parsed, declared
        super().__init__(
            f"the openFDA bulk export '{key}' declared {declared} records but "
            f"{parsed} parsed — refusing to treat a partial download as the whole "
            "source. Re-run the download; if it persists the export itself is "
            "inconsistent and the count should be reported as a lower bound."
        )


@dataclass(frozen=True)
class BulkPartition:
    url: str
    size_mb: float = 0.0
    records: int = 0


@dataclass(frozen=True)
class BulkExport:
    """One catalogue entry — what FDA says it publishes for a source."""
    key: str                      # "device/pma"
    export_date: str = ""         # FDA's own date for the DATA, not the download
    total_records: int = 0
    partitions: tuple[BulkPartition, ...] = ()

    @property
    def total_mb(self) -> float:
        return round(sum(p.size_mb for p in self.partitions), 1)


@dataclass
class BulkFreshness:
    """When the data is from, and when we took a copy. Two different facts."""
    key: str
    export_date: str = ""          # what FDA published
    downloaded_at: str = ""        # when this machine fetched it
    total_records: int = 0
    partitions: int = 0
    total_mb: float = 0.0
    #: True only when the publisher DECLARED a record count that the parse was
    #: checked against (`load_export`). A CSV published as a monthly file
    #: declares nothing, so completeness is unverifiable and must not be implied.
    completeness_asserted: bool = True

    def render_lines(self) -> list[str]:
        """The ONLY function that turns bulk freshness into text — Markdown and
        PDF both call it, so the surfaces cannot drift."""
        if not self.export_date and not self.downloaded_at:
            return [f"Source: FDA bulk distribution '{self.key}' — never downloaded. "
                    "This is not a finding about any asset."]
        lines = [
            f"Source: FDA bulk distribution '{self.key}', published "
            f"{self.export_date or 'date not stated'}"
            + (f", downloaded {self.downloaded_at[:10]}" if self.downloaded_at else "")
            + f" ({self.total_records:,} records, {self.total_mb} MB in "
              f"{self.partitions} partition(s)).",
            "This source is a bulk distribution, not a paginated API: it cannot be "
            "refreshed incrementally, and bringing it up to date means downloading "
            "the whole file again. Anything published by the FDA after the date "
            "above is not in this copy.",
        ]
        if not self.completeness_asserted:
            lines.append(
                "The publisher declares no record count for this file, so the row "
                "count above is what parsed — it has NOT been checked against a "
                "total the FDA states, unlike the sources that publish one.")
        return lines


def parse_catalogue(payload: dict, key: str) -> BulkExport | None:
    """Read one source out of openFDA's download.json.

    `key` is the slash path used everywhere else in this package — "device/pma",
    "drug/drugsfda" — so a caller names a source the same way whether it reaches
    it by API or by bulk.
    """
    node = payload.get("results") or {}
    for part in key.split("/"):
        node = (node or {}).get(part)
        if node is None:
            return None
    partitions = tuple(
        BulkPartition(
            url=str(p.get("file", "")),
            size_mb=float(p.get("size_mb", 0) or 0),
            records=int(p.get("records", 0) or 0),
        )
        for p in (node.get("partitions") or [])
        if p.get("file")
    )
    return BulkExport(
        key=key,
        export_date=str(node.get("export_date", "") or ""),
        total_records=int(node.get("total_records", 0) or 0),
        partitions=partitions,
    )


def iter_zip_records(blob: bytes) -> Iterator[dict]:
    """Yield the `results` entries from a zipped openFDA export.

    Each partition is a zip holding one JSON document with a `results` array.
    Read from bytes rather than a path so the whole path is testable without a
    temporary file and without a network call.
    """
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for name in zf.namelist():
            if not name.lower().endswith(".json"):
                continue
            with zf.open(name) as fh:
                payload = json.load(fh)
            for record in payload.get("results") or []:
                yield record


@dataclass
class BulkLoad:
    """What a completed bulk load produced."""
    records: list[dict] = field(default_factory=list)
    freshness: BulkFreshness | None = None
    export: BulkExport | None = None


def load_export(
    key: str,
    fetch: Callable[[str], bytes],
    catalogue: dict | None = None,
    fetch_catalogue: Callable[[str], bytes] | None = None,
    offline: bool = False,
    progress: Callable[[str], None] | None = None,
) -> BulkLoad:
    """Download every partition of a bulk export and parse it whole.

    `fetch` is injected rather than imported so the entire path — catalogue,
    download, unzip, parse, completeness assertion — is exercised in tests
    against real captured bytes with no network. Same reason the trial and
    device clients take a mocked transport.
    """
    if offline:
        raise RuntimeError(
            "offline mode is enabled: refusing to download an openFDA bulk export")

    if catalogue is None:
        if fetch_catalogue is None:
            raise ValueError("pass either a catalogue payload or a fetch_catalogue callable")
        catalogue = json.loads(fetch_catalogue(CATALOGUE_URL).decode("utf-8"))

    export = parse_catalogue(catalogue, key)
    if export is None or not export.partitions:
        raise RuntimeError(f"openFDA publishes no bulk export for '{key}'")

    records: list[dict] = []
    for i, part in enumerate(export.partitions, 1):
        if progress:
            progress(f"downloading {key} partition {i}/{len(export.partitions)} "
                     f"({part.size_mb} MB)")
        records.extend(iter_zip_records(fetch(part.url)))

    # Assert against the declared total rather than trusting the download.
    if export.total_records and len(records) != export.total_records:
        raise IncompleteBulkExport(key, len(records), export.total_records)

    return BulkLoad(
        records=records,
        export=export,
        freshness=BulkFreshness(
            key=key,
            export_date=export.export_date,
            downloaded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            total_records=len(records),
            partitions=len(export.partitions),
            total_mb=export.total_mb,
        ),
    )


def http_fetch(url: str, timeout: int = 600) -> bytes:
    """The real transport. Kept trivial and separate so tests never touch it.

    A browser User-Agent is sent because some FDA hosts refuse a default one.
    `purplebooksearch.fda.gov` answered three consecutive requests with an
    Akamai bot-detection page carrying HTTP 404 — indistinguishable from a
    missing resource unless you read the body, which nearly caused this tool to
    record the Purple Book as unavailable. Identifying as a normal browser
    avoids the trap rather than papering over it.
    """
    import requests

    resp = requests.get(url, timeout=timeout, headers={
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,text/csv,*/*",
    })
    resp.raise_for_status()
    return resp.content


# ---------------------------------------------------------------- delimited sources

#: An FDA "apology" page: bot detection served with a 2xx or 4xx status. Reading
#: the status alone reports a missing resource; reading the body catches it.
_ABUSE_MARKERS = (b"abuse-detection-apology", b"excessive-requests-apology",
                  b"FDA Apology")


class BlockedByBotDetection(RuntimeError):
    """The host served a bot-detection page instead of the resource.

    Its own exception because the remedy is different from a 404: wait and retry
    with a browser User-Agent, rather than conclude the source does not exist.
    """


def check_not_blocked(blob: bytes, url: str) -> None:
    head = blob[:2000]
    if any(m in head for m in _ABUSE_MARKERS):
        raise BlockedByBotDetection(
            f"{url} returned an FDA bot-detection page rather than the file. This is "
            "NOT evidence that the source is unavailable — wait, then retry with a "
            "browser User-Agent (medrag.fda.bulk.http_fetch already sends one)."
        )


@dataclass
class DelimitedLoad:
    """A delimited (CSV) source: rows, header, and freshness.

    Separate from `BulkLoad` because the two cannot assert the same things. A
    catalogued JSON export declares `total_records`, so `load_export` can refuse
    a short download. A CSV published as a monthly file declares nothing, so
    completeness cannot be asserted — and this type exists so no caller can
    mistake one guarantee for the other.
    """
    rows: list[dict] = field(default_factory=list)
    header: list[str] = field(default_factory=list)
    freshness: "BulkFreshness | None" = None
    section_note: str = ""


def load_delimited(
    key: str,
    url: str,
    fetch: Callable[[str], bytes],
    export_label: str = "",
    header_marker: str = "",
    section: int = 0,
    offline: bool = False,
    encoding: str = "utf-8-sig",
) -> DelimitedLoad:
    """Load a CSV-distributed FDA source.

    `header_marker` names the value in column 0 of a header row; `section`
    selects WHICH such row starts the data. The Purple Book monthly file carries
    two sections under identical headers — a changes report, then the full
    database — so `section=1` is what selects "all products", and the
    distinction is a parameter rather than a magic slice buried in a caller.
    """
    import csv
    import io

    if offline:
        raise RuntimeError(
            f"offline mode is enabled: refusing to download {key}")

    blob = fetch(url)
    check_not_blocked(blob, url)

    rows = list(csv.reader(io.StringIO(blob.decode(encoding, errors="replace"))))
    starts = [i for i, r in enumerate(rows)
              if r and r[0].strip() == header_marker] if header_marker else [0]
    if not starts:
        raise RuntimeError(
            f"{key}: no header row starting with '{header_marker}' was found in "
            f"{url}. The publisher's layout has changed; refusing to guess at it.")
    if section >= len(starts):
        raise RuntimeError(
            f"{key}: section {section} requested but only {len(starts)} header "
            f"row(s) found in {url}.")

    idx = starts[section]
    header = [c.strip() for c in rows[idx]]
    out = []
    for r in rows[idx + 1:]:
        if not any(c.strip() for c in r):
            continue
        out.append({h: (r[i].strip() if i < len(r) else "")
                    for i, h in enumerate(header)})

    return DelimitedLoad(
        rows=out,
        header=header,
        section_note=(f"section {section + 1} of {len(starts)} in the published file"
                      if len(starts) > 1 else ""),
        freshness=BulkFreshness(
            key=key,
            export_date=export_label,
            downloaded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            total_records=len(out),
            partitions=1,
            total_mb=round(len(blob) / (1024 * 1024), 2),
            completeness_asserted=False,
        ),
    )


def cache_path(raw_dir: str | Path, key: str) -> Path:
    return Path(raw_dir) / f"bulk-{key.replace('/', '-')}.json"
