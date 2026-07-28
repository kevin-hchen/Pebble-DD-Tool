"""SQLite store for trial records.

Structured, queryable, indexed - the opposite of the vector path. Phase and
status are filters, so they live in columns with indexes on them, and a
question like "which Phase 3 trials on this target were terminated" is answered
by SQL rather than by hoping an embedding preserved the distinction.

Note on encryption: SQLite needs random access to its file, so the envelope
encryption used for the corpus and index does not apply here. The database is
created 0600 inside a 0700 directory. If a trial store ever needs encryption at
rest, that is a filesystem-level or SQLCipher question, not something this layer
can fake - see SECURITY.md.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .client import STOPPED_STATUSES, TrialRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS trials (
    nct_id                  TEXT PRIMARY KEY,
    brief_title             TEXT,
    phase                   TEXT,
    overall_status          TEXT,
    why_stopped             TEXT,
    enrollment_count        INTEGER,
    enrollment_type         TEXT,
    lead_sponsor            TEXT,
    sponsor_class           TEXT,
    start_date              TEXT,
    primary_completion_date TEXT,
    completion_date         TEXT,
    study_type              TEXT,
    conditions              TEXT,   -- JSON array
    interventions           TEXT,   -- JSON array
    collaborators           TEXT,   -- JSON array
    ingested_at             TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_status     ON trials(overall_status);
CREATE INDEX IF NOT EXISTS idx_phase      ON trials(phase);
CREATE INDEX IF NOT EXISTS idx_sponsor    ON trials(lead_sponsor);
CREATE INDEX IF NOT EXISTS idx_start_date ON trials(start_date);

-- Free-text search over the fields a diligence question actually names:
-- indication, drug, sponsor, and the stop reason.
--
-- Deliberately NOT a contentless (content='') table. Contentless FTS5 stores no
-- column values, so SELECT nct_id returns NULL and every lookup silently fails
-- to resolve - a matching row with no way to identify what matched.
CREATE VIRTUAL TABLE IF NOT EXISTS trials_fts USING fts5(
    nct_id UNINDEXED, brief_title, conditions, interventions, lead_sponsor, why_stopped
);
"""

_ARRAY_FIELDS = ("conditions", "interventions", "collaborators")


class TrialStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.path.exists()

        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

        if new_file:
            try:
                os.chmod(self.path, 0o600)
            except OSError:  # pragma: no cover
                pass

    # ------------------------------------------------------------ writes

    def upsert(self, records: list[TrialRecord]) -> int:
        """Insert or refresh records. Re-ingesting updates status in place -
        a trial that was RECRUITING last month may be TERMINATED today, and
        that transition is the entire point of tracking it."""
        if not records:
            return 0

        rows = []
        for r in records:
            d = r.to_dict()
            for f in _ARRAY_FIELDS:
                d[f] = json.dumps(d[f])
            rows.append(d)

        cols = list(rows[0].keys())
        placeholders = ", ".join(f":{c}" for c in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "nct_id")

        with self.conn:
            self.conn.executemany(
                f"INSERT INTO trials ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(nct_id) DO UPDATE SET {updates}",
                rows,
            )
            # Clear prior FTS rows for these trials first: re-ingestion would
            # otherwise stack duplicate index entries for the same NCT ID.
            self.conn.executemany(
                "DELETE FROM trials_fts WHERE nct_id = ?", [(r.nct_id,) for r in records]
            )
            self.conn.executemany(
                "INSERT INTO trials_fts (nct_id, brief_title, conditions, interventions, "
                "lead_sponsor, why_stopped) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        r.nct_id,
                        r.brief_title,
                        " ".join(r.conditions),
                        " ".join(r.interventions),
                        r.lead_sponsor,
                        r.why_stopped,
                    )
                    for r in records
                ],
            )
        return len(records)

    # ------------------------------------------------------------ reads

    @staticmethod
    def _to_record(row: sqlite3.Row) -> TrialRecord:
        d = {k: row[k] for k in row.keys() if k != "ingested_at"}
        for f in _ARRAY_FIELDS:
            d[f] = json.loads(d[f] or "[]")
        return TrialRecord.from_dict(d)

    def __len__(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM trials").fetchone()[0]

    def get(self, nct_id: str) -> TrialRecord | None:
        row = self.conn.execute("SELECT * FROM trials WHERE nct_id = ?", (nct_id,)).fetchone()
        return self._to_record(row) if row else None

    def query(
        self,
        intervention: str | None = None,
        condition: str | None = None,
        sponsor: str | None = None,
        phase: str | None = None,
        statuses: list[str] | None = None,
        stopped_only: bool = False,
        limit: int = 50,
    ) -> list[TrialRecord]:
        """Structured filter query. This is the precision the registry exists for."""
        where, params = [], []

        if intervention:
            where.append("LOWER(interventions) LIKE ?")
            params.append(f"%{intervention.lower()}%")
        if condition:
            where.append("LOWER(conditions) LIKE ?")
            params.append(f"%{condition.lower()}%")
        if sponsor:
            where.append("LOWER(lead_sponsor) LIKE ?")
            params.append(f"%{sponsor.lower()}%")
        if phase:
            where.append("LOWER(phase) LIKE ?")
            params.append(f"%{phase.lower()}%")

        if stopped_only:
            where.append(
                f"UPPER(overall_status) IN ({', '.join('?' * len(STOPPED_STATUSES))})"
            )
            params.extend(sorted(STOPPED_STATUSES))
        elif statuses:
            where.append(f"UPPER(overall_status) IN ({', '.join('?' * len(statuses))})")
            params.extend(s.upper() for s in statuses)

        sql = "SELECT * FROM trials"
        if where:
            sql += " WHERE " + " AND ".join(where)
        # Stopped trials first, then most recent: the negative signal leads.
        sql += (
            " ORDER BY CASE WHEN UPPER(overall_status) IN ('TERMINATED','WITHDRAWN','SUSPENDED')"
            " THEN 0 ELSE 1 END, start_date DESC LIMIT ?"
        )
        params.append(limit)

        return [self._to_record(r) for r in self.conn.execute(sql, params).fetchall()]

    def search(self, text: str, limit: int = 20) -> list[TrialRecord]:
        """Free-text search across titles, conditions, interventions, sponsors."""
        safe = " OR ".join(f'"{t}"' for t in text.split() if t.strip())
        if not safe:
            return []
        try:
            rows = self.conn.execute(
                "SELECT nct_id FROM trials_fts WHERE trials_fts MATCH ? LIMIT ?", (safe, limit)
            ).fetchall()
        except sqlite3.OperationalError:  # malformed FTS expression
            return []

        seen, out = set(), []
        for row in rows:
            if row["nct_id"] in seen:
                continue
            seen.add(row["nct_id"])
            rec = self.get(row["nct_id"])
            if rec:
                out.append(rec)
        return out

    def stopped_trials(
        self, intervention: str | None = None, condition: str | None = None, limit: int = 50
    ) -> list[TrialRecord]:
        """Deterministic half of the negative-evidence pass. No model involved."""
        return self.query(
            intervention=intervention, condition=condition, stopped_only=True, limit=limit
        )

    def stats(self) -> dict:
        """Includes why_stopped fill rate: the highest-signal field is also the
        most often left blank, and the memo should not imply coverage it lacks."""
        total = len(self)
        stopped = self.conn.execute(
            "SELECT COUNT(*) FROM trials WHERE UPPER(overall_status) IN "
            "('TERMINATED','WITHDRAWN','SUSPENDED')"
        ).fetchone()[0]
        with_reason = self.conn.execute(
            "SELECT COUNT(*) FROM trials WHERE UPPER(overall_status) IN "
            "('TERMINATED','WITHDRAWN','SUSPENDED') AND why_stopped != ''"
        ).fetchone()[0]
        by_status = {
            r["overall_status"]: r["n"]
            for r in self.conn.execute(
                "SELECT overall_status, COUNT(*) AS n FROM trials GROUP BY overall_status "
                "ORDER BY n DESC"
            )
        }
        return {
            "total": total,
            "stopped": stopped,
            "stopped_with_reason": with_reason,
            "why_stopped_fill_rate": round(with_reason / stopped, 3) if stopped else None,
            "by_status": by_status,
        }

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "TrialStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
