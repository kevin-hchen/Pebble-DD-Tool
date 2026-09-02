"""SQLite store for openFDA DRUG data — applications, labels and recalls.

Alongside `store.py` (devices), not inside it. The two are the same idea applied
to different regulatory objects, and the join keys are genuinely different: a
device store hangs everything off `product_code`, while a drug store hangs
everything off `application_number` (NDA/BLA/ANDA) and matches assets on active
ingredient. Merging them would put a nullable half of each schema in every row.

MATCHING IS agents.py, NOT A SECOND MATCHER
===========================================

`ingredient_tokens` is `agents.token_blob()` over every name an application can
be found under, written at ingest and filtered with the same space-padded LIKE
scheme as `query_sets`, `biomarker_gating` and `intervention_tokens`. Matching
an asset to an active ingredient is the same problem as matching it to a trial
intervention, and this codebase has been bitten three times by two
independently-maintained matchers drifting apart (see `markers.py`). Alias
expansion happens at QUERY time, so `config/agents.yaml` gaining a brand name
takes effect against the database already on disk.

WHAT ABSENCE MEANS, AND WHAT IT NEVER MEANS
===========================================

`approval_answer()` is the only way to ask this store "is this approved", and it
CANNOT return an approval claim from an empty result. When nothing matched it
returns `found=False` with `ABSENCE_MEANINGS` attached — never-submitted,
submitted-and-not-approved, approved-under-a-name-we-did-not-match, or approved
outside the US are four different facts and this store can distinguish none of
them. That is the same not-assessed-vs-nothing-found rule as
`ValidationReport.assessed` and `CoverageStatement.ever_ingested`, applied to
the question where getting it wrong is most expensive: a memo that says "not FDA
approved" about an approved drug is a false statement about a competitor, and
one that says it about the asset under diligence is worse.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .. import agents
from ..dbopen import connect_read_only, prepare_writable, refuse_write
from .drugs import (
    ABSENCE_MEANINGS,
    APPROVED,
    TENTATIVE_APPROVAL,
    DrugApplication,
    DrugLabel,
    DrugRecall,
)

# v4 adds the Purple Book (licensed biologics, biosimilars, interchangeability),
# keyed on (bla_number, product_number). Not an openFDA source — a monthly CSV.
# v3 adds the Orange Book (listed patents and exclusivity), keyed on
# (application_number, product_number) and joined to `applications` on the
# application number.
# v2 adds the FAERS aggregate cache. The cache IS the mirror: FAERS is 20.7M
# reports and 113 GB, so nothing is mirrored and nothing is queried live per
# memo — server-side aggregate counts are fetched once and stored with the
# timestamp they were retrieved, which makes a memo reproducible and makes
# --offline work for anything already cached.
STORE_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    application_number     TEXT PRIMARY KEY,
    application_type       TEXT,    -- NDA | BLA | ANDA, read off the identifier
    sponsor_name           TEXT,
    approval_status        TEXT,    -- APPROVED | TENTATIVE APPROVAL | ... | STATUS NOT STATED
    approval_date          TEXT,    -- YYYYMMDD, verbatim
    review_priority        TEXT,
    submission_class       TEXT,
    n_supplements          INTEGER,
    latest_supplement_date TEXT,
    products               TEXT,    -- JSON array of DrugProduct
    brand_names            TEXT,    -- JSON array
    generic_names          TEXT,    -- JSON array
    substance_names        TEXT,    -- JSON array
    routes                 TEXT,    -- JSON array
    spl_set_ids            TEXT,    -- JSON array; joins to labels.set_id
    unii                   TEXT,    -- JSON array
    pharm_class            TEXT,    -- JSON array
    has_openfda            INTEGER,
    marketing_statuses     TEXT,    -- JSON array, denormalised for filtering
    all_discontinued       INTEGER,
    ingredient_tokens      TEXT,    -- space-padded ' token ' blob, agents.py
    ingested_at            TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_app_status ON applications(approval_status);
CREATE INDEX IF NOT EXISTS idx_app_type   ON applications(application_type);
CREATE INDEX IF NOT EXISTS idx_app_date   ON applications(approval_date);

CREATE TABLE IF NOT EXISTS labels (
    set_id              TEXT PRIMARY KEY,
    spl_id              TEXT,
    version             TEXT,
    effective_time      TEXT,
    application_numbers TEXT,   -- JSON array; empty on 71% of labels
    brand_names         TEXT,
    generic_names       TEXT,
    routes              TEXT,
    manufacturer        TEXT,
    sections            TEXT,   -- JSON {section_name: text}
    truncated_sections  TEXT,   -- JSON array; which sections were cut
    ingredient_tokens   TEXT,
    ingested_at         TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_lab_effective ON labels(effective_time);

CREATE TABLE IF NOT EXISTS drug_recalls (
    recall_number          TEXT PRIMARY KEY,
    classification         TEXT,
    status                 TEXT,
    product_description    TEXT,
    reason_for_recall      TEXT,
    recalling_firm         TEXT,
    report_date            TEXT,
    recall_initiation_date TEXT,
    voluntary_mandated     TEXT,
    application_numbers    TEXT,
    generic_names          TEXT,
    brand_names            TEXT,
    ingredient_tokens      TEXT,
    ingested_at            TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_drec_class ON drug_recalls(classification);

-- Orange Book: listed patents and exclusivity, small molecules only. Keyed on
-- (application_number, product_number); joins to `applications` on the
-- application number. Sparse by nature — 5.4% of records carry patents — and
-- that sparsity must never be read as an absence of intellectual property.
CREATE TABLE IF NOT EXISTS orange_book (
    application_number  TEXT NOT NULL,
    product_number      TEXT NOT NULL DEFAULT '',
    brand_name          TEXT,
    active_ingredients  TEXT,   -- JSON array
    application_type    TEXT,   -- N (NDA) | A (ANDA/generic)
    applicant           TEXT,
    dosage_form         TEXT,
    route               TEXT,
    marketing_status    TEXT,
    therapeutic_equivalence_codes TEXT,
    reference_listed_drug INTEGER,
    approval_date       TEXT,
    approved_prior_to_1982 INTEGER,
    patents             TEXT,   -- JSON array
    exclusivity         TEXT,   -- JSON array
    name_tokens         TEXT,   -- agents.token_blob over brand + ingredients
    ingested_at         TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (application_number, product_number)
);
CREATE INDEX IF NOT EXISTS idx_ob_type ON orange_book(application_type);

-- Purple Book: licensed biologics. Keyed on (bla_number, product_number).
-- Interchangeability is stored as its own column, NOT inferred from being a
-- biosimilar: they are separate FDA findings.
CREATE TABLE IF NOT EXISTS purple_book (
    bla_number          TEXT NOT NULL,
    product_number      TEXT NOT NULL DEFAULT '',
    applicant           TEXT,
    proprietary_name    TEXT,
    proper_name         TEXT,
    license_type        TEXT,   -- 351(a) | 351(k) Biosimilar | 351(k) Interchangeable
    strength            TEXT,
    dosage_form         TEXT,
    route               TEXT,
    marketing_status    TEXT,
    licensure           TEXT,
    approval_date       TEXT,
    interchangeable_approval_date TEXT,
    ref_product_proper_name       TEXT,
    ref_product_proprietary_name  TEXT,
    center              TEXT,
    date_of_first_licensure       TEXT,
    exclusivity_expiration_date   TEXT,
    first_interchangeable_exclusivity_date TEXT,
    ref_product_exclusivity_date  TEXT,
    orphan_exclusivity_date       TEXT,
    patent_list_provided          TEXT,
    is_biosimilar       INTEGER DEFAULT 0,
    is_interchangeable  INTEGER DEFAULT 0,
    name_tokens         TEXT,
    ref_tokens          TEXT,   -- tokens of the REFERENCE product, for the reverse lookup
    ingested_at         TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (bla_number, product_number)
);
CREATE INDEX IF NOT EXISTS idx_pb_biosimilar ON purple_book(is_biosimilar);
CREATE INDEX IF NOT EXISTS idx_pb_license    ON purple_book(license_type);

-- Cached FAERS aggregates, one row per (asset, aggregate kind). `retrieved_at`
-- is what makes a memo reproducible and what --offline reads.
CREATE TABLE IF NOT EXISTS faers_cache (
    asset          TEXT NOT NULL,
    kind           TEXT NOT NULL,   -- reactions | seriousness | reporter | drug_role | scalars
    payload        TEXT,            -- JSON: buckets, or the scalar counts
    retrieved_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (asset, kind)
);

-- What was searched, what openFDA said it held, and when. Mirrors the device
-- store's `catalog` and the trial store's `query_coverage`: a local count means
-- nothing without the denominator it was measured against, and a coverage gap
-- nobody recorded reads as no gap at all.
CREATE TABLE IF NOT EXISTS drug_catalog (
    asset             TEXT PRIMARY KEY,
    n_applications    INTEGER,   -- held locally
    reported_total    INTEGER,   -- openFDA's own total for the same search
    n_labels          INTEGER,
    n_recalls         INTEGER,
    searched_at       TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

_JSON_FIELDS = ("products", "brand_names", "generic_names", "substance_names", "routes",
                "spl_set_ids", "unii", "pharm_class", "marketing_statuses")


class DrugStoreSchemaError(RuntimeError):
    """A drugs.db built before the current schema. Carries a rebuild step."""


@dataclass
class ApprovalAnswer:
    """The answer to "is this approved", shaped so absence cannot be read as no.

    `found=False` means this store holds no matching application, and the four
    `absence_meanings` are the reasons that could be true. There is deliberately
    no boolean called `approved` on this object: a caller wanting one has to go
    through `is_approved`, which is False when nothing was found AND False when
    the store was never populated, and `statement()` never lets those render the
    same way.
    """
    asset: str
    searched: bool = False              # False => no drug store at all
    found: bool = False                 # False => no matching application
    applications: list[DrugApplication] = field(default_factory=list)
    reported_total: int | None = None   # openFDA's own count for this search
    searched_at: str = ""
    absence_meanings: tuple[str, ...] = ABSENCE_MEANINGS

    @property
    def approved_applications(self) -> list[DrugApplication]:
        return [a for a in self.applications if a.approval_status == APPROVED]

    @property
    def tentative_applications(self) -> list[DrugApplication]:
        return [a for a in self.applications if a.approval_status == TENTATIVE_APPROVAL]

    @property
    def is_approved(self) -> bool:
        """True only on positive evidence: a matched application whose ORIG
        submission was approved. Never inferred, and never true when the store
        was not searched."""
        return self.searched and bool(self.approved_applications)

    @property
    def earliest_approval(self) -> str:
        dates = [a.approval_date for a in self.approved_applications if a.approval_date]
        return min(dates) if dates else ""

    def statement(self) -> str:
        """The sentence a memo prints. Three outcomes, worded so they can never
        be confused: not searched, searched-and-not-found, and found."""
        if not self.searched:
            return (f"FDA approval status for “{self.asset}” was NOT checked — no openFDA "
                    "drug store is available. This is not a finding about the asset.")
        if not self.found:
            reasons = "; ".join(self.absence_meanings)
            # Worded so that no substring of this sentence reads as a claim about
            # approval status even when quoted out of context. An earlier draft
            # said "this is NOT a finding that the asset is unapproved", which
            # contains the phrase "is unapproved" — and a reader skimming, or a
            # downstream tool matching on text, sees the claim rather than its
            # negation. `test_is_approved_is_false_for_every_shape_of_absence…`
            # pins that no assertion-shaped phrase survives here.
            return (
                f"No FDA application matching “{self.asset}” was found in openFDA drugsFDA"
                + (f" (searched {_human_date(self.searched_at)})" if self.searched_at else "")
                + ". Absence from this database says nothing either way about approval "
                f"status: it is equally consistent with {len(self.absence_meanings)} "
                f"different situations — {reasons}. drugsFDA covers US applications only."
            )
        bits = []
        if self.approved_applications:
            apps = ", ".join(a.application_number for a in self.approved_applications[:4])
            first = self.earliest_approval
            bits.append(f"{len(self.approved_applications)} approved US application(s) "
                        f"({apps}{'…' if len(self.approved_applications) > 4 else ''})"
                        + (f", earliest approval {_human_date(first)}" if first else ""))
        if self.tentative_applications:
            bits.append(f"{len(self.tentative_applications)} TENTATIVE approval(s) — the FDA "
                        "found the application met requirements but could not approve it, "
                        "usually for patent or exclusivity reasons. A tentative approval is "
                        "not an approval")
        other = [a for a in self.applications
                 if a not in self.approved_applications and a not in self.tentative_applications]
        if other:
            bits.append(f"{len(other)} application(s) whose original submission status is not "
                        "stated in openFDA")
        discontinued = [a for a in self.applications if a.all_discontinued]
        if discontinued:
            bits.append(f"{len(discontinued)} application(s) whose products are all marked "
                        "Discontinued — withdrawn from marketing, which is not the same as "
                        "never approved")
        line = f"openFDA drugsFDA holds {len(self.applications)} application(s) matching "
        if self.reported_total is not None and self.reported_total > len(self.applications):
            line += (f"“{self.asset}” of {self.reported_total} openFDA reports for this search: ")
        else:
            line += f"“{self.asset}”: "
        return line + "; ".join(bits) + "."

    # ------------------------------------------------------------ the four axes

    def axis_submission(self) -> str:
        """Was it approved — AP, TA, or not stated. The axis everything else
        gets collapsed into if nobody stops it."""
        if not self.found:
            return "Submission status: no matching application, so no status to report."
        parts = []
        if self.approved_applications:
            parts.append(f"{len(self.approved_applications)} approved (ORIG submission AP)")
        if self.tentative_applications:
            parts.append(f"{len(self.tentative_applications)} TENTATIVE approval (ORIG "
                         "submission TA — the FDA found the application met requirements "
                         "but could not approve it, usually for patent or exclusivity "
                         "reasons; this is NOT an approval)")
        unstated = [a for a in self.applications
                    if a not in self.approved_applications
                    and a not in self.tentative_applications]
        if unstated:
            parts.append(f"{len(unstated)} with no original-submission status on file")
        first = self.earliest_approval
        line = "Submission status: " + "; ".join(parts) + "."
        if first:
            line += f" Earliest US approval {_human_date(first)}."
        return line

    def axis_marketing(self) -> str:
        """Is it on the market TODAY. Orthogonal to approval: 14,762 of the
        products in drugsFDA are Discontinued, and approved-then-withdrawn is one
        of the more informative things a diligence memo can say."""
        if not self.found:
            return "Marketing status: no matching application, so no status to report."
        statuses: dict[str, int] = {}
        for a in self.applications:
            for s in a.marketing_statuses:
                statuses[s] = statuses.get(s, 0) + 1
        if not statuses:
            return "Marketing status: not stated on any matching application."
        summary = ", ".join(f"{n} {s}" for s, n in sorted(statuses.items()))
        line = f"Marketing status (products, current): {summary}."
        withdrawn = [a for a in self.applications if a.all_discontinued]
        if withdrawn:
            nums = ", ".join(a.application_number for a in withdrawn[:4])
            line += (f" {len(withdrawn)} application(s) have every product marked "
                     f"Discontinued ({nums}) — approved and then withdrawn from marketing, "
                     "which is a different fact from never approved and from currently "
                     "marketed.")
        return line

    def axis_application_mix(self) -> str:
        """Originator versus generic. An ANDA is a generic filing, so the mix is
        a read on exclusivity and on how contested the molecule already is."""
        if not self.found:
            return "Application type: no matching application."
        mix: dict[str, int] = {}
        for a in self.applications:
            mix[a.application_type or "not stated"] = mix.get(a.application_type or "not stated", 0) + 1
        summary = ", ".join(f"{n} {t}" for t, n in sorted(mix.items()))
        line = f"Application type: {summary}."
        n_generic = mix.get("ANDA", 0)
        if n_generic:
            line += (f" {n_generic} ANDA filing(s) means generic versions of this molecule "
                     "are on file with the FDA.")
        elif mix.get("NDA") or mix.get("BLA"):
            line += (" No ANDA filings matched, which is consistent with — but not proof "
                     "of — the molecule still being under exclusivity; the Orange Book, "
                     "which is not searched here, is what actually answers that.")
        return line

    def axis_label_history(self) -> str:
        """Approved supplements: how much the label has expanded since approval.
        A drug approved once and never supplemented reads very differently from
        one with 126 approved supplements."""
        if not self.found:
            return "Label history: no matching application."
        total = sum(a.n_supplements for a in self.applications)
        if not total:
            return ("Label history: no approved supplements on file — the label has not "
                    "been expanded since original approval, or supplements are not "
                    "recorded for these applications.")
        latest = max((a.latest_supplement_date for a in self.applications
                      if a.latest_supplement_date), default="")
        line = (f"Label history: {total} approved supplement(s) across the matching "
                "application(s)")
        if latest:
            line += f", most recent {_human_date(latest)}"
        return line + ". Supplements are efficacy and labelling changes; they never " \
                      "convert an unapproved application into an approved one."

    def coverage_lines(self) -> list[str]:
        """What was searched, what was not, and when — the same declaration the
        trial and device stores carry."""
        lines = []
        if self.searched:
            when = f", searched {_human_date(self.searched_at)}" if self.searched_at else ""
            total = (f"; openFDA reported {self.reported_total} for this search"
                     if self.reported_total is not None else "")
            lines.append(f"Searched: openFDA drugsFDA, label and enforcement{when}{total}.")
        else:
            lines.append("Searched: nothing — no openFDA drug store is available for this "
                         "asset. This is not a finding about the asset.")
        lines.append("Not searched: " + "; ".join(NOT_SEARCHED) + ". " + NOT_SEARCHED_CAVEAT)
        return lines

    def render_lines(self) -> list[str]:
        """THE approval block, as plain text lines.

        The ONLY function that turns an ApprovalAnswer into prose, called
        verbatim by the Markdown memo and the PDF — the same construction as
        `coverage.render_lines` and `StoppedTrialSweep.coverage_line`, so the
        surfaces cannot drift.

        This is also the reason no model is asked to summarise an
        ApprovalAnswer. Every guard in this file is a guard in CODE, and a model
        paraphrasing "no application matched" as "not approved in the US" walks
        straight past all of them. The memo inserts these lines as a fixed
        string; the model may write around them and may not write them.
        """
        lines = [self.statement()]
        if self.found:
            lines += [self.axis_submission(), self.axis_marketing(),
                      self.axis_application_mix(), self.axis_label_history()]
        lines += self.coverage_lines()
        return lines

    def cited_applications(self) -> list[str]:
        """Display-formatted application numbers, for a "see [n]" pointer."""
        return [a.display_number for a in self.applications]


#: Drug sources this tool does NOT search, stated on every approval block.
#: Same rule as `config/registries.yaml` for trial registries: "searched and
#: found nothing" and "never looked" must render differently, and a gap nobody
#: declares reads as no gap. Sizes measured 2026-08-05.
NOT_SEARCHED = (
    "FAERS adverse-event reports (openFDA drug/event, 20.7M reports)",
    "the Orange Book (48,502 records — therapeutic equivalence, patents, exclusivity)",
    "drug shortages (1,651 records)",
)
NOT_SEARCHED_CAVEAT = (
    "So this section says nothing about post-marketing adverse events, patent or "
    "exclusivity expiry, or supply. drugsFDA also covers US applications only — a "
    "drug approved in the EU or Japan and nowhere else is absent from it by design."
)


def _human_date(compact: str) -> str:
    """YYYYMMDD -> 4 Sep 2014. Returns the input unchanged if it is not one."""
    try:
        return datetime.strptime(compact[:8], "%Y%m%d").strftime("%-d %b %Y")
    except (ValueError, TypeError):
        return compact or ""


class DrugStore:
    def __init__(self, path: str | Path, read_only: bool = False,
                 immutable: bool = False):
        """Open the store. `read_only=True` performs no schema execution, no
        `PRAGMA user_version` write and no commit, so a reader never takes a
        write lock and never needs a writable filesystem — see `dbopen`."""
        self.path = Path(path)
        self.read_only = read_only

        if read_only:
            self.conn = connect_read_only(self.path, immutable=immutable)
            self._check_version(self.conn.execute(
                "PRAGMA user_version").fetchone()[0])
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.path.exists()

        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        # WAL + a busy timeout, so a reader serving the previous snapshot and an
        # ingest writing the next one do not block each other. See dbopen.
        prepare_writable(self.conn)

        if not new_file:
            self._check_version(self.conn.execute("PRAGMA user_version").fetchone()[0])

        self.conn.executescript(SCHEMA)
        self.conn.execute(f"PRAGMA user_version = {STORE_VERSION}")
        self.conn.commit()

        if new_file:
            try:
                os.chmod(self.path, 0o600)
            except OSError:  # pragma: no cover
                pass

    def _check_version(self, version: int) -> None:
        """Fail closed on a stale schema. Shared by both open paths, so a
        read-only reader gets the same refusal as an ingest rather than quietly
        querying columns that are not there."""
        if version == STORE_VERSION:
            return
        self.conn.close()
        raise DrugStoreSchemaError(
            f"the openFDA drug database at {self.path} was built by an older version "
            f"(schema v{version or 1}, current is v{STORE_VERSION}). Delete it and "
            "re-ingest:\n"
            f"    rm {self.path}\n"
            '    python -m medrag drugs --asset "..."'
        )

    # ------------------------------------------------------------ writes

    def upsert_applications(self, records: list[DrugApplication]) -> int:
        refuse_write(self, "upsert_applications")
        if not records:
            return 0
        rows = []
        for a in records:
            d = a.to_dict()
            d["application_type"] = a.application_type
            d["marketing_statuses"] = a.marketing_statuses
            d["all_discontinued"] = int(a.all_discontinued)
            d["has_openfda"] = int(a.has_openfda)
            # Every name the application can be found under, tokenised by the
            # ONE matcher. Ingredients lead: present on 99% of records against
            # openfda.generic_name's 43%.
            d["ingredient_tokens"] = agents.token_blob(a.match_names)
            for f in _JSON_FIELDS:
                d[f] = json.dumps(d.get(f) or [])
            rows.append(d)
        return self._insert("applications", "application_number", rows)

    def upsert_labels(self, records: list[DrugLabel]) -> int:
        refuse_write(self, "upsert_labels")
        if not records:
            return 0
        rows = []
        for label in records:
            d = label.to_dict()
            d["ingredient_tokens"] = agents.token_blob(
                [*label.generic_names, *label.brand_names])
            for f in ("application_numbers", "brand_names", "generic_names", "routes",
                      "truncated_sections"):
                d[f] = json.dumps(d.get(f) or [])
            d["sections"] = json.dumps(d.get("sections") or {})
            rows.append(d)
        return self._insert("labels", "set_id", rows)

    def upsert_recalls(self, records: list[DrugRecall]) -> int:
        refuse_write(self, "upsert_recalls")
        if not records:
            return 0
        rows = []
        for r in records:
            d = r.to_dict()
            d["ingredient_tokens"] = agents.token_blob(
                [*r.generic_names, *r.brand_names, r.product_description])
            for f in ("application_numbers", "generic_names", "brand_names"):
                d[f] = json.dumps(d.get(f) or [])
            rows.append(d)
        return self._insert("drug_recalls", "recall_number", rows)

    def _insert(self, table: str, pk: str, rows: list[dict]) -> int:
        cols = [c["name"] for c in self.conn.execute(f"PRAGMA table_info({table})")]
        dicts = [{k: r[k] for k in cols if k in r} for r in rows]
        keys = list(dicts[0].keys())
        placeholders = ", ".join(f":{c}" for c in keys)
        updates = ", ".join(f"{c}=excluded.{c}" for c in keys if c != pk)
        with self.conn:
            self.conn.executemany(
                f"INSERT INTO {table} ({', '.join(keys)}) VALUES ({placeholders}) "
                f"ON CONFLICT({pk}) DO UPDATE SET {updates}", dicts)
        return len(dicts)

    def record_search(self, asset: str, reported_total: int | None,
                      n_applications: int, n_labels: int = 0, n_recalls: int = 0) -> None:
        """Record that this asset WAS searched, and what openFDA said it held.

        Without this row, "we looked and found nothing" and "we never looked"
        are indistinguishable — and `approval_answer` would have to guess which
        one an empty result was."""
        refuse_write(self, "record_search")
        with self.conn:
            self.conn.execute(
                "INSERT INTO drug_catalog (asset, n_applications, reported_total, "
                "n_labels, n_recalls) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(asset) DO UPDATE SET n_applications=excluded.n_applications, "
                "reported_total=excluded.reported_total, n_labels=excluded.n_labels, "
                "n_recalls=excluded.n_recalls, searched_at=CURRENT_TIMESTAMP",
                (asset.strip().lower(), n_applications, reported_total, n_labels, n_recalls))

    # ------------------------------------------------------------ reads

    @staticmethod
    def _application(row) -> DrugApplication:
        d = {k: row[k] for k in row.keys()
             if k not in ("ingested_at", "ingredient_tokens", "application_type",
                          "marketing_statuses", "all_discontinued")}
        for f in ("products", "brand_names", "generic_names", "substance_names", "routes",
                  "spl_set_ids", "unii", "pharm_class"):
            d[f] = json.loads(d.get(f) or "[]")
        d["has_openfda"] = bool(d.get("has_openfda"))
        return DrugApplication.from_dict(d)

    @staticmethod
    def _label(row) -> DrugLabel:
        d = {k: row[k] for k in row.keys() if k not in ("ingested_at", "ingredient_tokens")}
        for f in ("application_numbers", "brand_names", "generic_names", "routes",
                  "truncated_sections"):
            d[f] = json.loads(d.get(f) or "[]")
        d["sections"] = json.loads(d.get("sections") or "{}")
        return DrugLabel.from_dict(d)

    @staticmethod
    def _recall(row) -> DrugRecall:
        d = {k: row[k] for k in row.keys() if k not in ("ingested_at", "ingredient_tokens")}
        for f in ("application_numbers", "generic_names", "brand_names"):
            d[f] = json.loads(d.get(f) or "[]")
        return DrugRecall.from_dict(d)

    def _token_clause(self, asset: str, params: list) -> str:
        """`% token %` over the ingredient blob, ORed across an agent's aliases
        and ANDed across the agents of a combination — the same policy split
        `trials.store._intervention_clause` uses, for the same reason: a
        combination product IS both ingredients."""
        query = agents.parse_asset(asset)
        if not query:
            return ""
        per_term = []
        for term in query.terms:
            per_term.append("(" + " OR ".join(
                ["ingredient_tokens LIKE ?"] * len(term.forms)) + ")")
            params.extend(f"% {f} %" for f in term.forms)
        return "(" + " AND ".join(per_term) + ")"

    def applications(self, asset: str, limit: int = 100) -> list[DrugApplication]:
        params: list = []
        clause = self._token_clause(asset, params)
        if not clause:
            return []
        params.append(limit)
        return [self._application(r) for r in self.conn.execute(
            f"SELECT * FROM applications WHERE {clause} "
            "ORDER BY CASE approval_status WHEN 'APPROVED' THEN 0 ELSE 1 END, "
            "approval_date ASC LIMIT ?", params)]

    def labels(self, asset: str, limit: int = 5) -> list[DrugLabel]:
        params: list = []
        clause = self._token_clause(asset, params)
        if not clause:
            return []
        params.append(limit)
        return [self._label(r) for r in self.conn.execute(
            f"SELECT * FROM labels WHERE {clause} ORDER BY effective_time DESC LIMIT ?",
            params)]

    def recalls(self, asset: str, limit: int = 25) -> list[DrugRecall]:
        params: list = []
        clause = self._token_clause(asset, params)
        if not clause:
            return []
        params.append(limit)
        return [self._recall(r) for r in self.conn.execute(
            f"SELECT * FROM drug_recalls WHERE {clause} ORDER BY "
            "CASE classification WHEN 'Class I' THEN 0 WHEN 'Class II' THEN 1 "
            "WHEN 'Class III' THEN 2 ELSE 3 END, report_date DESC LIMIT ?", params)]

    def search_record(self, asset: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM drug_catalog WHERE asset = ?", (asset.strip().lower(),)).fetchone()
        return dict(row) if row else None

    def approval_answer(self, asset: str) -> ApprovalAnswer:
        """The ONLY way to ask this store about approval.

        Returns an object that cannot state an approval from an empty result:
        `found=False` carries the four things absence can mean, and
        `is_approved` requires a matched application whose ORIG submission was
        approved. See the module docstring.
        """
        record = self.search_record(asset)
        apps = self.applications(asset)
        return ApprovalAnswer(
            asset=asset,
            # Searched if this asset was ingested OR anything matched it: a
            # store populated for a different asset has genuinely not looked at
            # this one, and must say so rather than reporting an absence.
            searched=bool(record) or bool(apps),
            found=bool(apps),
            applications=apps,
            reported_total=(record or {}).get("reported_total"),
            searched_at=(record or {}).get("searched_at", "") or "",
        )

    # ------------------------------------------------------------ Orange Book

    def upsert_orange_book(self, entries) -> int:
        refuse_write(self, "upsert_orange_book")
        if not entries:
            return 0
        cols = [c["name"] for c in self.conn.execute("PRAGMA table_info(orange_book)")]
        rows = []
        for e in entries:
            d = e.to_dict()
            for f in ("active_ingredients", "therapeutic_equivalence_codes",
                      "patents", "exclusivity"):
                d[f] = json.dumps(d.get(f) or [])
            d["reference_listed_drug"] = int(e.reference_listed_drug)
            d["approved_prior_to_1982"] = int(e.approved_prior_to_1982)
            d["name_tokens"] = agents.token_blob(e.match_names)
            rows.append({k: d[k] for k in cols if k in d})
        keys = list(rows[0].keys())
        ph = ", ".join(f":{c}" for c in keys)
        upd = ", ".join(f"{c}=excluded.{c}" for c in keys
                        if c not in ("application_number", "product_number"))
        with self.conn:
            self.conn.executemany(
                f"INSERT INTO orange_book ({', '.join(keys)}) VALUES ({ph}) "
                f"ON CONFLICT(application_number, product_number) DO UPDATE SET {upd}",
                rows)
        return len(rows)

    @staticmethod
    def _ob_entry(row):
        from .orangebook import OrangeBookEntry

        d = {k: row[k] for k in row.keys() if k not in ("ingested_at", "name_tokens")}
        for f in ("active_ingredients", "therapeutic_equivalence_codes",
                  "patents", "exclusivity"):
            d[f] = json.loads(d.get(f) or "[]")
        d["reference_listed_drug"] = bool(d.get("reference_listed_drug"))
        d["approved_prior_to_1982"] = bool(d.get("approved_prior_to_1982"))
        return OrangeBookEntry.from_dict(d)

    def orange_book_entries(self, asset: str, generics: bool | None = None,
                            limit: int = 200):
        params: list = []
        clause = self._token_clause(asset, params)
        if not clause:
            return []
        clause = clause.replace("ingredient_tokens", "name_tokens")
        if generics is True:
            clause += " AND application_type = 'A'"
        elif generics is False:
            clause += " AND application_type != 'A'"
        params.append(limit)
        return [self._ob_entry(r) for r in self.conn.execute(
            f"SELECT * FROM orange_book WHERE {clause} LIMIT ?", params)]

    def protection_answer(self, asset: str):
        """The ONLY way to ask this store about listed protection.

        Decides APPLICABILITY before absence. An asset with no approved
        application, or one approved under a BLA, gets `applicable=False` with
        the reason — never an empty patent list, which would read as an absence
        of intellectual property.
        """
        from .orangebook import (
            NOT_APPLICABLE_BIOLOGIC,
            NOT_APPLICABLE_NO_APPROVAL,
            ProtectionAnswer,
        )

        have = self.conn.execute("SELECT COUNT(*) FROM orange_book").fetchone()[0]
        if not have:
            return ProtectionAnswer(asset=asset, searched=False)

        approval = self.approval_answer(asset)
        answer = ProtectionAnswer(asset=asset, searched=True)
        if not approval.approved_applications:
            answer.applicable = False
            answer.not_applicable_reason = NOT_APPLICABLE_NO_APPROVAL
            return answer
        if all(a.application_type == "BLA" for a in approval.approved_applications):
            answer.applicable = False
            answer.not_applicable_reason = NOT_APPLICABLE_BIOLOGIC
            return answer

        answer.entries = self.orange_book_entries(asset, generics=False)
        answer.generic_entries = self.orange_book_entries(asset, generics=True)
        fresh = self.bulk_freshness_ob()
        answer.export_date = fresh or ""
        return answer

    def bulk_freshness_ob(self) -> str:
        row = self.conn.execute(
            "SELECT MIN(ingested_at) FROM orange_book").fetchone()
        return (row[0] or "")[:10] if row else ""

    # ------------------------------------------------------------ Purple Book

    def upsert_purple_book(self, products) -> int:
        refuse_write(self, "upsert_purple_book")
        if not products:
            return 0
        cols = [c["name"] for c in self.conn.execute("PRAGMA table_info(purple_book)")]
        rows = []
        for p in products:
            d = p.to_dict()
            d["is_biosimilar"] = int(p.is_biosimilar)
            d["is_interchangeable"] = int(p.is_interchangeable)
            d["name_tokens"] = agents.token_blob(p.match_names)
            # The reference product gets its OWN token blob, so "does anything
            # reference this molecule" is a query rather than a scan.
            d["ref_tokens"] = agents.token_blob(
                [n for n in (p.ref_product_proper_name, p.ref_product_proprietary_name) if n])
            rows.append({k: d[k] for k in cols if k in d})
        keys = list(rows[0].keys())
        ph = ", ".join(f":{c}" for c in keys)
        upd = ", ".join(f"{c}=excluded.{c}" for c in keys
                        if c not in ("bla_number", "product_number"))
        with self.conn:
            self.conn.executemany(
                f"INSERT INTO purple_book ({', '.join(keys)}) VALUES ({ph}) "
                f"ON CONFLICT(bla_number, product_number) DO UPDATE SET {upd}", rows)
        return len(rows)

    @staticmethod
    def _pb_product(row):
        from .purplebook import BiologicProduct

        skip = ("ingested_at", "name_tokens", "ref_tokens", "is_biosimilar",
                "is_interchangeable")
        return BiologicProduct.from_dict(
            {k: row[k] for k in row.keys() if k not in skip})

    def purple_book_products(self, asset: str, column: str = "name_tokens",
                             limit: int = 400):
        """Products matching an asset by its OWN name, or — with
        `column='ref_tokens'` — products that name it as their REFERENCE."""
        params: list = []
        clause = self._token_clause(asset, params)
        if not clause:
            return []
        clause = clause.replace("ingredient_tokens", column)
        params.append(limit)
        return [self._pb_product(r) for r in self.conn.execute(
            f"SELECT * FROM purple_book WHERE {clause} LIMIT ?", params)]

    def biologic_protection_answer(self, asset: str):
        """The ONLY way to ask this store about biologic licensure.

        Routes on APPLICATION TYPE before anything else: an NDA asset is sent to
        the Orange Book and told so, which is the mirror of what phase 3 does for
        a BLA. Three inapplicable states, none of them an absence of protection
        or of competition.
        """
        from .purplebook import (
            NOT_APPLICABLE_NO_LICENCE,
            NOT_APPLICABLE_SMALL_MOLECULE,
            BiologicProtectionAnswer,
        )

        have = self.conn.execute("SELECT COUNT(*) FROM purple_book").fetchone()[0]
        if not have:
            return BiologicProtectionAnswer(asset=asset, searched=False)

        answer = BiologicProtectionAnswer(asset=asset, searched=True)
        answer.published = self.purple_book_published()

        approval = self.approval_answer(asset)
        approved = approval.approved_applications
        # Wrong-book routing, said out loud rather than left to be inferred.
        if approved and all(a.application_type in ("NDA", "ANDA") for a in approved):
            answer.applicable = False
            answer.not_applicable_reason = NOT_APPLICABLE_SMALL_MOLECULE
            answer.consulted_book = "Orange Book"
            return answer

        products = self.purple_book_products(asset)
        if not products:
            answer.applicable = False
            answer.not_applicable_reason = NOT_APPLICABLE_NO_LICENCE
            return answer

        answer.products = products
        # Biosimilars are products naming THIS asset as their reference — the
        # reverse lookup, which is why ref_tokens exists as its own column.
        answer.biosimilars = [p for p in self.purple_book_products(asset, "ref_tokens")
                              if p.is_biosimilar]
        from .purplebook import load_exclusivity_config

        cfg = load_exclusivity_config()
        answer.exclusivity_fill_note = ((cfg.get("empty_field_note") or {}).get("text") or "")
        return answer

    def purple_book_published(self) -> str:
        row = self.conn.execute(
            "SELECT MIN(ingested_at) FROM purple_book").fetchone()
        return (row[0] or "")[:10] if row else ""

    # ------------------------------------------------------------ FAERS cache

    def cache_faers(self, answer) -> None:
        """Store one asset's aggregates with the timestamp they were retrieved.

        The cache IS the mirror. Nothing about FAERS is downloaded in bulk and
        nothing is queried live during a memo run; this row is what makes a memo
        reproducible and what `--offline` reads.
        """
        refuse_write(self, "cache_faers")
        from dataclasses import asdict

        key = answer.asset.strip().lower()
        rows = [(key, kind,
                 json.dumps([asdict(t) for t in getattr(answer, kind)]),
                 answer.retrieved_at)
                for kind in ("reactions", "seriousness", "reporter", "drug_role")]
        rows.append((key, "scalars", json.dumps({
            "n_reports": answer.n_reports,
            "n_reports_normalised": answer.n_reports_normalised,
            "n_reports_free_text": answer.n_reports_free_text,
            "n_death_reports": answer.n_death_reports,
            "faers_total": answer.faers_total,
            "matched_field": answer.matched_field,
        }), answer.retrieved_at))
        with self.conn:
            self.conn.executemany(
                "INSERT INTO faers_cache (asset, kind, payload, retrieved_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(asset, kind) DO UPDATE SET "
                "payload=excluded.payload, retrieved_at=excluded.retrieved_at", rows)

    def cached_faers(self, asset: str):
        """Rebuild a FAERSAnswer from the cache, or None if nothing is cached.

        None is NOT an empty answer: "we have never asked" and "we asked and
        found nothing" are different facts, and the caller decides which to
        render — the same rule as `CoverageStatement.ever_ingested`.
        """
        from .faers import FAERSAnswer, TermCount

        rows = self.conn.execute(
            "SELECT kind, payload, retrieved_at FROM faers_cache WHERE asset = ?",
            (asset.strip().lower(),)).fetchall()
        if not rows:
            return None
        answer = FAERSAnswer(asset=asset, searched=True)
        for r in rows:
            payload = json.loads(r["payload"] or "null")
            answer.retrieved_at = answer.retrieved_at or (r["retrieved_at"] or "")
            if r["kind"] == "scalars":
                for k, v in (payload or {}).items():
                    setattr(answer, k, v)
            else:
                setattr(answer, r["kind"],
                        [TermCount(**t) for t in (payload or [])])
        return answer

    def faers_answer(self, asset: str, offline: bool = False, fetch=None):
        """The ONLY way to ask this store about adverse-event reports.

        Cache first, always. When nothing is cached and `offline` is set, the
        answer comes back with `offline_miss=True`, which renders as "not
        consulted, and here is the asset that has no cached aggregate" — never
        as a silent zero that a reader would take for a clean safety record.
        """
        cached = self.cached_faers(asset)
        if cached is not None:
            return cached
        from .faers import FAERSAnswer, fetch_aggregates

        if offline:
            return FAERSAnswer(asset=asset, searched=False, offline_miss=True)
        answer = (fetch or fetch_aggregates)(asset)
        self.cache_faers(answer)
        return answer

    def faers_freshness(self) -> dict:
        """Which assets have a cached aggregate, and when it was taken."""
        rows = self.conn.execute(
            "SELECT asset, MIN(retrieved_at) AS retrieved_at FROM faers_cache "
            "GROUP BY asset ORDER BY retrieved_at DESC")
        return {r["asset"]: r["retrieved_at"] for r in rows}

    def freshness(self) -> dict:
        """When each asset was last searched — the same declaration the trial and
        device stores make, so a stale answer is visible as stale."""
        rows = self.conn.execute(
            "SELECT asset, n_applications, reported_total, searched_at FROM drug_catalog "
            "ORDER BY searched_at DESC")
        return {r["asset"]: dict(r) for r in rows}

    def stats(self) -> dict:
        def count(t):
            return self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        return {
            "applications": count("applications"),
            "labels": count("labels"),
            "drug_recalls": count("drug_recalls"),
            "assets_searched": count("drug_catalog"),
            "approved": self.conn.execute(
                "SELECT COUNT(*) FROM applications WHERE approval_status = ?",
                (APPROVED,)).fetchone()[0],
        }

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "DrugStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
