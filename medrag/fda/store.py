"""SQLite store for openFDA device data — a third structured store beside the
trial registry, built the same way and for the same reason.

Clearance status, product code and device class are FILTERS, not semantics, so
they live in indexed columns and are answered by SQL, not by hoping an embedding
preserved the distinction — exactly the argument that keeps trial records out of
the vector index.

The join key is product_code, indexed, because that is what makes "everything
cleared in this category" a real query and what links a clearance to its recalls
and adverse events. Matching is on product_code and device_name, NEVER on the
company: `applicant` and `recalling_firm` fragment badly on live data — the same
firm files under "Baxter Healthcare Corp" and "Baxter Healthcare Corporation",
and acquisitions and subsidiaries scatter a product line across unrelated names.
Matching on the manufacturer would silently miss clearances; matching on the
product code does not.

Versioned from the start: STORE_VERSION is written to PRAGMA user_version and a
database from an older schema is refused with a rebuild step, the same fail-closed
choice the trial store and the vector index make.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .. import agents
from .client import AdverseEvent, Clearance510k, Recall
from .pma import PMARecord, is_de_novo

# v2 adds premarket APPROVAL (device/pma) beside clearance, joined on
# product_code, plus a De Novo flag on the clearances table. Both are new
# regulatory FACTS rather than new fields on an existing one: a PMA is not a
# 510(k), and a De Novo clearance is not a substantial-equivalence finding.
STORE_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS clearances (
    k_number             TEXT PRIMARY KEY,
    applicant            TEXT,
    device_name          TEXT,
    product_code         TEXT,
    decision_code        TEXT,
    decision_description TEXT,
    decision_date        TEXT,
    date_received        TEXT,
    clearance_type       TEXT,
    advisory_committee   TEXT,
    statement_or_summary TEXT,
    device_class         TEXT,
    regulation_number    TEXT,
    medical_specialty    TEXT,
    -- 1 when decision_code marks a De Novo grant (DENG). A De Novo is granted
    -- BECAUSE no predicate exists, so rendering one as "substantially
    -- equivalent to a predicate" is a false statement about a company's
    -- regulatory history. 482 live records; see config/fda_decision_codes.yaml.
    is_de_novo           INTEGER DEFAULT 0,
    ingested_at          TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_clr_de_novo ON clearances(is_de_novo);
-- product_code is the join key; device_class is the other filter a diligence
-- question actually names ("what Class III devices cleared in this category").
CREATE INDEX IF NOT EXISTS idx_clr_product_code ON clearances(product_code);
CREATE INDEX IF NOT EXISTS idx_clr_device_class ON clearances(device_class);
CREATE INDEX IF NOT EXISTS idx_clr_decision_date ON clearances(decision_date);

CREATE TABLE IF NOT EXISTS recalls (
    recall_number         TEXT PRIMARY KEY,
    cfres_id              TEXT,
    product_code          TEXT,
    device_class          TEXT,
    product_description   TEXT,
    reason_for_recall     TEXT,
    recalling_firm        TEXT,
    recall_status         TEXT,
    root_cause_description TEXT,
    event_date_initiated  TEXT,
    event_date_posted     TEXT,
    k_numbers             TEXT,   -- JSON array
    ingested_at           TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_rec_product_code ON recalls(product_code);

CREATE TABLE IF NOT EXISTS events (
    report_number    TEXT PRIMARY KEY,
    mdr_report_key   TEXT,
    event_type       TEXT,
    date_received    TEXT,
    product_code     TEXT,
    device_class     TEXT,
    brand_name       TEXT,
    generic_name     TEXT,
    manufacturer     TEXT,
    product_problems TEXT,   -- JSON array
    narrative        TEXT,
    ingested_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_evt_product_code ON events(product_code);
CREATE INDEX IF NOT EXISTS idx_evt_event_type ON events(event_type);

CREATE VIRTUAL TABLE IF NOT EXISTS clearances_fts USING fts5(
    k_number UNINDEXED, device_name, applicant, statement_or_summary
);
CREATE VIRTUAL TABLE IF NOT EXISTS recalls_fts USING fts5(
    recall_number UNINDEXED, product_description, reason_for_recall, recalling_firm
);

-- Premarket APPROVAL. A separate table from `clearances`, not extra columns on
-- it, because a PMA and a 510(k) are different regulatory objects: one is
-- approval on clinical evidence, the other is clearance by substantial
-- equivalence to a predicate. Sharing a table would put a nullable half of each
-- schema in every row and invite exactly the "cleared or approved" collapse
-- this whole change exists to prevent.
--
-- Supplements are SEPARATE RECORDS in this source (unlike drugsFDA, where
-- submissions nest), so the key is (pma_number, supplement_number). An original
-- application is the row whose supplement_number is empty — NOT whose
-- supplement_type is empty, which is also blank on 1,885 older supplements.
CREATE TABLE IF NOT EXISTS pma (
    pma_number           TEXT NOT NULL,
    supplement_number    TEXT NOT NULL DEFAULT '',
    supplement_type      TEXT,
    supplement_reason    TEXT,
    product_code         TEXT,   -- the join key to clearances, present on 98.6%
    decision_code        TEXT,
    decision_date        TEXT,
    date_received        TEXT,
    trade_name           TEXT,
    generic_name         TEXT,   -- with trade_name, the device_name equivalent
    applicant            TEXT,
    advisory_committee   TEXT,
    advisory_committee_description TEXT,
    ao_statement         TEXT,
    expedited_review_flag TEXT,
    device_class         TEXT,   -- verbatim; a PMA is NOT automatically Class III
    device_name          TEXT,
    regulation_number    TEXT,
    medical_specialty    TEXT,
    is_original          INTEGER DEFAULT 0,
    name_tokens          TEXT,   -- agents.token_blob over trade/generic/device name
    ingested_at          TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (pma_number, supplement_number)
);
CREATE INDEX IF NOT EXISTS idx_pma_product_code ON pma(product_code);
CREATE INDEX IF NOT EXISTS idx_pma_original     ON pma(is_original);
CREATE INDEX IF NOT EXISTS idx_pma_decision     ON pma(decision_code);

-- Freshness for bulk-distributed sources. Separate from `catalog` because the
-- two answer different questions: catalog holds an API-reported total for a
-- product code, this holds "which published export is this copy, and when did
-- we take it". A bulk source cannot be refreshed incrementally and the memo
-- says so rather than implying otherwise.
CREATE TABLE IF NOT EXISTS bulk_freshness (
    source_key     TEXT PRIMARY KEY,   -- "device/pma"
    export_date    TEXT,               -- FDA's own date for the data
    downloaded_at  TEXT,               -- when this machine took a copy
    total_records  INTEGER,
    partitions     INTEGER,
    total_mb       REAL
);

-- The openFDA-reported total 510(k) count for a product code, recorded at ingest
-- so the memo can say "showing N of M in this category" — the local store almost
-- never holds the whole category, and a memo must not imply it does.
CREATE TABLE IF NOT EXISTS catalog (
    product_code   TEXT PRIMARY KEY,
    category_total INTEGER,
    updated_at     TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

# worst-first, so the negative-evidence pass shows a Death before a Malfunction
# when it can only surface a handful.
_EVENT_ORDER = ("CASE event_type WHEN 'Death' THEN 0 WHEN 'Injury' THEN 1 "
                "WHEN 'Malfunction' THEN 2 ELSE 3 END, date_received DESC")


class FDAStoreSchemaError(RuntimeError):
    """An fda.db built before the current schema. Carries a rebuild instruction."""


class FDAStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.path.exists()

        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row

        if not new_file:
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if version != STORE_VERSION:
                self.conn.close()
                raise FDAStoreSchemaError(
                    f"the openFDA database at {self.path} was built by an older version "
                    f"(schema v{version or 1}, current is v{STORE_VERSION}). Delete it and "
                    "re-ingest:\n"
                    f"    rm {self.path}\n"
                    '    python -m medrag fda --product-code "..."'
                )

        self.conn.executescript(SCHEMA)
        self.conn.execute(f"PRAGMA user_version = {STORE_VERSION}")
        self.conn.commit()

        if new_file:
            try:
                os.chmod(self.path, 0o600)
            except OSError:  # pragma: no cover
                pass

    # ------------------------------------------------------------ writes

    def _upsert(self, table, pk, rows, array_fields, fts_table, fts_cols, fts_row):
        if not rows:
            return 0
        dicts = []
        for r in rows:
            d = r.to_dict()
            for f in array_fields:
                d[f] = json.dumps(d.get(f) or [])
            # Drop dataclass-only fields that are not table columns.
            dicts.append({k: d[k] for k in self._columns(table) if k in d})
        cols = list(dicts[0].keys())
        placeholders = ", ".join(f":{c}" for c in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != pk)
        with self.conn:
            self.conn.executemany(
                f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT({pk}) DO UPDATE SET {updates}",
                dicts,
            )
            self.conn.executemany(
                f"DELETE FROM {fts_table} WHERE {fts_cols[0]} = ?",
                [(getattr(r, pk),) for r in rows],
            )
            self.conn.executemany(
                f"INSERT INTO {fts_table} ({', '.join(fts_cols)}) "
                f"VALUES ({', '.join('?' * len(fts_cols))})",
                [fts_row(r) for r in rows],
            )
        return len(rows)

    def _columns(self, table) -> list[str]:
        return [r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")]

    def upsert_clearances(self, records: list[Clearance510k]) -> int:
        n = self._upsert(
            "clearances", "k_number", records, (),
            "clearances_fts", ("k_number", "device_name", "applicant", "statement_or_summary"),
            lambda r: (r.k_number, r.device_name, r.applicant, r.statement_or_summary),
        )
        # The De Novo overlay, computed from the decision code already stored.
        # Done here rather than on the record so an existing store gains the flag
        # on re-ingest without the client changing shape.
        with self.conn:
            self.conn.executemany(
                "UPDATE clearances SET is_de_novo = ? WHERE k_number = ?",
                [(int(is_de_novo(r.decision_code)), r.k_number) for r in records],
            )
        return n

    def upsert_pma(self, records: list[PMARecord]) -> int:
        """Insert PMA rows keyed on (pma_number, supplement_number)."""
        if not records:
            return 0
        cols = self._columns("pma")
        rows = []
        for r in records:
            d = r.to_dict()
            d["is_original"] = int(r.is_original)
            # Matching goes through agents.py — the same tokeniser the trial
            # interventions and drug ingredients use. Device names are not drug
            # names, but the tokenisation problem is identical and a second
            # matcher is the drift this codebase keeps getting bitten by.
            d["name_tokens"] = agents.token_blob(r.match_names)
            rows.append({k: d[k] for k in cols if k in d})
        keys = list(rows[0].keys())
        placeholders = ", ".join(f":{c}" for c in keys)
        updates = ", ".join(f"{c}=excluded.{c}" for c in keys
                            if c not in ("pma_number", "supplement_number"))
        with self.conn:
            self.conn.executemany(
                f"INSERT INTO pma ({', '.join(keys)}) VALUES ({placeholders}) "
                f"ON CONFLICT(pma_number, supplement_number) DO UPDATE SET {updates}",
                rows,
            )
        return len(rows)

    def record_bulk_freshness(self, freshness) -> None:
        """Persist which published export this copy is, and when it was taken."""
        if freshness is None:
            return
        with self.conn:
            self.conn.execute(
                "INSERT INTO bulk_freshness (source_key, export_date, downloaded_at, "
                "total_records, partitions, total_mb) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(source_key) DO UPDATE SET export_date=excluded.export_date, "
                "downloaded_at=excluded.downloaded_at, total_records=excluded.total_records, "
                "partitions=excluded.partitions, total_mb=excluded.total_mb",
                (freshness.key, freshness.export_date, freshness.downloaded_at,
                 freshness.total_records, freshness.partitions, freshness.total_mb),
            )

    def bulk_freshness(self, source_key: str):
        """The stored freshness for a bulk source, or None if never downloaded —
        which a renderer must show differently from 'downloaded and empty'."""
        from .bulk import BulkFreshness

        row = self.conn.execute(
            "SELECT * FROM bulk_freshness WHERE source_key = ?", (source_key,)).fetchone()
        if not row:
            return None
        return BulkFreshness(
            key=row["source_key"], export_date=row["export_date"] or "",
            downloaded_at=row["downloaded_at"] or "",
            total_records=row["total_records"] or 0, partitions=row["partitions"] or 0,
            total_mb=row["total_mb"] or 0.0,
        )

    def upsert_recalls(self, records: list[Recall]) -> int:
        return self._upsert(
            "recalls", "recall_number", records, ("k_numbers",),
            "recalls_fts", ("recall_number", "product_description", "reason_for_recall", "recalling_firm"),
            lambda r: (r.recall_number, r.product_description, r.reason_for_recall, r.recalling_firm),
        )

    def upsert_events(self, records: list[AdverseEvent]) -> int:
        # Events have no FTS table — they are queried by product_code exact only.
        if not records:
            return 0
        cols = self._columns("events")
        dicts = []
        for r in records:
            d = r.to_dict()
            d["product_problems"] = json.dumps(d.get("product_problems") or [])
            dicts.append({k: d[k] for k in cols if k in d})
        keys = list(dicts[0].keys())
        placeholders = ", ".join(f":{c}" for c in keys)
        updates = ", ".join(f"{c}=excluded.{c}" for c in keys if c != "report_number")
        with self.conn:
            self.conn.executemany(
                f"INSERT INTO events ({', '.join(keys)}) VALUES ({placeholders}) "
                f"ON CONFLICT(report_number) DO UPDATE SET {updates}",
                dicts,
            )
        return len(records)

    # ------------------------------------------------------------ reads

    #: Store-computed columns that are not part of the Clearance510k record.
    _NON_CLEARANCE_COLS = ("ingested_at", "is_de_novo")

    @staticmethod
    def _clearance(row) -> Clearance510k:
        return Clearance510k.from_dict(
            {k: row[k] for k in row.keys() if k not in FDAStore._NON_CLEARANCE_COLS})

    @staticmethod
    def _recall(row) -> Recall:
        d = {k: row[k] for k in row.keys() if k != "ingested_at"}
        d["k_numbers"] = json.loads(d.get("k_numbers") or "[]")
        return Recall.from_dict(d)

    @staticmethod
    def _event(row) -> AdverseEvent:
        d = {k: row[k] for k in row.keys() if k != "ingested_at"}
        d["product_problems"] = json.loads(d.get("product_problems") or "[]")
        return AdverseEvent.from_dict(d)

    @staticmethod
    def _clearance_where(product_code, device_name, device_class):
        where, params = [], []
        if product_code:
            where.append("UPPER(product_code) = ?")
            params.append(product_code.upper())
        if device_name:
            where.append("LOWER(device_name) LIKE ?")
            params.append(f"%{device_name.lower()}%")
        if device_class:
            where.append("device_class = ?")
            params.append(str(device_class))
        return (" WHERE " + " AND ".join(where)) if where else "", params

    def clearances(self, product_code: str | None = None, device_name: str | None = None,
                   device_class: str | None = None, limit: int = 100) -> list[Clearance510k]:
        """The competitive clearance landscape. Filters on product_code and
        device_name — never the applicant."""
        clause, params = self._clearance_where(product_code, device_name, device_class)
        sql = "SELECT * FROM clearances" + clause + " ORDER BY decision_date DESC LIMIT ?"
        params.append(limit)
        return [self._clearance(r) for r in self.conn.execute(sql, params)]

    def clearances_total(self, product_code: str | None = None, device_name: str | None = None,
                         device_class: str | None = None) -> int:
        """How many clearances the local store holds for this filter — the M in
        'showing N of M', distinct from the openFDA category total below."""
        clause, params = self._clearance_where(product_code, device_name, device_class)
        return self.conn.execute("SELECT COUNT(*) FROM clearances" + clause, params).fetchone()[0]

    def set_category_total(self, product_code: str, total: int | None) -> None:
        """Record the openFDA-reported total for a product code, captured at ingest."""
        if not product_code or total is None:
            return
        with self.conn:
            self.conn.execute(
                "INSERT INTO catalog (product_code, category_total) VALUES (?, ?) "
                "ON CONFLICT(product_code) DO UPDATE SET category_total=excluded.category_total, "
                "updated_at=CURRENT_TIMESTAMP",
                (product_code.upper(), int(total)),
            )

    def category_total(self, product_code: str | None) -> int | None:
        """The openFDA-reported 510(k) total for a product code, or None if the
        store was populated before this was recorded."""
        if not product_code:
            return None
        row = self.conn.execute(
            "SELECT category_total FROM catalog WHERE product_code = ?", (product_code.upper(),)
        ).fetchone()
        return row["category_total"] if row else None

    def product_codes_for_device(self, device_name: str, limit: int = 10) -> list[str]:
        """Resolve a device name to its product code(s) — the join step that lets
        a recall/event lookup start from an asset name."""
        rows = self.conn.execute(
            "SELECT product_code, COUNT(*) n FROM clearances WHERE LOWER(device_name) LIKE ? "
            "AND product_code != '' GROUP BY product_code ORDER BY n DESC LIMIT ?",
            (f"%{device_name.lower()}%", limit),
        ).fetchall()
        return [r["product_code"] for r in rows]

    def recalls(self, product_code: str | None = None, device_name: str | None = None,
                limit: int = 50) -> list[Recall]:
        """Recalls for a product code (primary) OR a device-description text match.
        ORed, never ANDed — same reasoning as the stopped-trial lookup: a recall of
        the same product line under a different description is exactly what must not
        be hidden."""
        seen: set[str] = set()
        out: list[Recall] = []
        if product_code:
            for r in self.conn.execute(
                "SELECT * FROM recalls WHERE UPPER(product_code) = ? "
                "ORDER BY event_date_initiated DESC LIMIT ?", (product_code.upper(), limit)
            ):
                rec = self._recall(r)
                if rec.recall_number not in seen:
                    seen.add(rec.recall_number)
                    out.append(rec)
        if device_name and len(out) < limit:
            for rec in self._recalls_by_text(device_name, limit):
                if rec.recall_number not in seen:
                    seen.add(rec.recall_number)
                    out.append(rec)
        return out[:limit]

    def _recalls_by_text(self, text: str, limit: int) -> list[Recall]:
        safe = " OR ".join(f'"{t}"' for t in text.split() if t.strip())
        if not safe:
            return []
        try:
            rows = self.conn.execute(
                "SELECT recall_number FROM recalls_fts WHERE recalls_fts MATCH ? LIMIT ?",
                (safe, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        out = []
        for row in rows:
            r = self.conn.execute(
                "SELECT * FROM recalls WHERE recall_number = ?", (row["recall_number"],)
            ).fetchone()
            if r:
                out.append(self._recall(r))
        return out

    def events(self, product_code: str, limit: int = 25) -> list[AdverseEvent]:
        """Adverse events for a product code, worst-severity first. MAUDE is
        enormous, so this is always bounded — the caller decides the cap and the
        memo says how many of how many are shown."""
        if not product_code:
            return []
        rows = self.conn.execute(
            f"SELECT * FROM events WHERE UPPER(product_code) = ? ORDER BY {_EVENT_ORDER} LIMIT ?",
            (product_code.upper(), limit),
        )
        return [self._event(r) for r in rows]

    def event_counts(self, product_code: str) -> dict[str, int]:
        """How many events of each type are stored for a product code — so the
        memo can report '3 of 812 shown' rather than implying it saw them all."""
        rows = self.conn.execute(
            "SELECT event_type, COUNT(*) n FROM events WHERE UPPER(product_code) = ? "
            "GROUP BY event_type", (product_code.upper(),)
        )
        return {r["event_type"] or "Unspecified": r["n"] for r in rows}

    # ------------------------------------------------------------ PMA reads

    @staticmethod
    def _pma(row) -> PMARecord:
        d = {k: row[k] for k in row.keys()
             if k not in ("ingested_at", "name_tokens", "is_original")}
        return PMARecord.from_dict(d)

    def _name_clause(self, device_name: str, params: list) -> str:
        """`% token %` over the PMA name tokens, ORed across aliases and ANDed
        across terms — the same tokeniser the trial and drug stores use.

        `parse_descriptive_name`, not `parse_asset`: a device name is a
        description whose words the registry reorders freely ("Defibrillator,
        automatic implantable cardioverter" for "implantable cardioverter
        defibrillator"), where a drug name is a molecule. Joining the phrase into
        one token found nothing at all for a device with 2,895 PMA records.
        """
        query = agents.parse_descriptive_name(device_name)
        if not query:
            return ""
        per_term = []
        for term in query.terms:
            per_term.append(
                "(" + " OR ".join(["name_tokens LIKE ?"] * len(term.forms)) + ")")
            params.extend(f"% {f} %" for f in term.forms)
        return "(" + " AND ".join(per_term) + ")"

    def pma_records(self, product_code: str | None = None, device_name: str | None = None,
                    originals_only: bool = False, limit: int = 100) -> list[PMARecord]:
        """PMA rows for a product code and/or a device name.

        Matched on trade_name and generic_name — there is NO device_name on this
        source, and assuming symmetry with the clearance path is what made six of
        eighteen real device types invisible.
        """
        where, params = [], []
        if product_code:
            where.append("UPPER(product_code) = ?")
            params.append(product_code.upper())
        if device_name:
            clause = self._name_clause(device_name, params)
            if clause:
                where.append(clause)
        if originals_only:
            where.append("is_original = 1")
        if not where:
            return []
        params.append(limit)
        return [self._pma(r) for r in self.conn.execute(
            "SELECT * FROM pma WHERE " + " AND ".join(where)
            + " ORDER BY is_original DESC, decision_date DESC LIMIT ?", params)]

    def pma_total(self, product_code: str | None = None, device_name: str | None = None,
                  originals_only: bool = False) -> int:
        """The denominator. A capped sample of PMA rows is meaningless without
        it, and 97% of this source is supplements."""
        where, params = [], []
        if product_code:
            where.append("UPPER(product_code) = ?")
            params.append(product_code.upper())
        if device_name:
            clause = self._name_clause(device_name, params)
            if clause:
                where.append(clause)
        if originals_only:
            where.append("is_original = 1")
        if not where:
            return 0
        return self.conn.execute(
            "SELECT COUNT(*) FROM pma WHERE " + " AND ".join(where), params).fetchone()[0]

    def de_novo_clearances(self, product_code: str | None = None,
                           device_name: str | None = None,
                           limit: int = 50) -> list[Clearance510k]:
        """Clearances granted through De Novo — no predicate existed."""
        clause, params = self._clearance_where(product_code, device_name, None)
        clause = (clause + " AND is_de_novo = 1") if clause else " WHERE is_de_novo = 1"
        params.append(limit)
        return [self._clearance(r) for r in self.conn.execute(
            "SELECT * FROM clearances" + clause + " ORDER BY decision_date DESC LIMIT ?",
            params)]

    def stats(self) -> dict:
        def count(t):
            return self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        return {
            "clearances": count("clearances"),
            "recalls": count("recalls"),
            "events": count("events"),
            "pma": count("pma"),
            "pma_originals": self.conn.execute(
                "SELECT COUNT(*) FROM pma WHERE is_original = 1").fetchone()[0],
            "de_novo": self.conn.execute(
                "SELECT COUNT(*) FROM clearances WHERE is_de_novo = 1").fetchone()[0],
            "product_codes": self.conn.execute(
                "SELECT COUNT(DISTINCT product_code) FROM clearances").fetchone()[0],
        }

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "FDAStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
