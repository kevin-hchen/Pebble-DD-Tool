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

from .client import AdverseEvent, Clearance510k, Recall

STORE_VERSION = 1

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
    ingested_at          TEXT DEFAULT CURRENT_TIMESTAMP
);
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
        return self._upsert(
            "clearances", "k_number", records, (),
            "clearances_fts", ("k_number", "device_name", "applicant", "statement_or_summary"),
            lambda r: (r.k_number, r.device_name, r.applicant, r.statement_or_summary),
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

    @staticmethod
    def _clearance(row) -> Clearance510k:
        return Clearance510k.from_dict({k: row[k] for k in row.keys() if k != "ingested_at"})

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

    def stats(self) -> dict:
        def count(t):
            return self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        return {
            "clearances": count("clearances"),
            "recalls": count("recalls"),
            "events": count("events"),
            "product_codes": self.conn.execute(
                "SELECT COUNT(DISTINCT product_code) FROM clearances").fetchone()[0],
        }

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "FDAStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
