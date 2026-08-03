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

from ..biomarker_gating import MARKER_KEYS, gate_markers, gating_token, gating_tokens
from .client import STOPPED_STATUSES, TrialRecord

# Bumped when the columns change. Written to PRAGMA user_version so a database
# built before the current columns existed is refused on open rather than read
# with columns silently missing — the same fail-closed choice the vector index
# makes on an embedder or schema mismatch. v1 was the original diligence schema;
# v2 added the patient-perspective fields; v3 adds the precomputed biomarker
# gating census, so a landscape count is real SQL over the full match set rather
# than a parse of the retrieved sample; v4 adds fetch provenance (which queries
# found each trial) and the coverage catalog, so the population a local query
# selects is the one the fetch defined rather than one re-derived from a string.
STORE_VERSION = 4

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
    brief_summary           TEXT,
    eligibility_criteria    TEXT,
    minimum_age             TEXT,
    maximum_age             TEXT,
    sex                     TEXT,
    healthy_volunteers      INTEGER,  -- 1/0/NULL; NULL means not stated
    overall_officials       TEXT,   -- JSON array of {name, role, affiliation}
    central_contacts        TEXT,   -- JSON array of {name, email, phone}
    locations               TEXT,   -- JSON array of {facility, city, state, country, status}
    biomarker_gating        TEXT,   -- space-padded ' MARKER:STATUS ' tokens for LIKE filtering
    biomarker_flags         TEXT,   -- JSON {marker: {status, span}}, computed at ingest
    found_by                TEXT,   -- JSON array of query labels that returned this trial
    query_sets              TEXT,   -- space-padded ' setkey ' tokens for LIKE filtering
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

-- What was searched, and what is known to be missing from it. Mirrors the FDA
-- store's `catalog`: a count means nothing without the denominator it was
-- measured against, and a coverage gap nobody recorded reads as no gap at all.
CREATE TABLE IF NOT EXISTS query_coverage (
    set_key        TEXT PRIMARY KEY,
    set_label      TEXT,
    curated        INTEGER,   -- 0 when the set was an ad-hoc single string
    yields         TEXT,      -- JSON [{query, fetched, new, reported_total, error}]
    total_unique   INTEGER,
    basket_caveat  TEXT,
    errors         TEXT,      -- JSON array; non-empty means coverage is incomplete
    updated_at     TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

# JSON-encoded on write, decoded on read. The three patient-perspective lists
# join the diligence trio here.
_ARRAY_FIELDS = (
    "conditions", "interventions", "collaborators",
    "overall_officials", "central_contacts", "locations",
)


class TrialStoreSchemaError(RuntimeError):
    """A trials.db built before the current schema. Carries a rebuild instruction
    so the CLI can print something a user can act on, not a traceback."""


class TrialStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.path.exists()

        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row

        # Refuse a stale database before touching it. Running the new SCHEMA over
        # an old file would leave the table missing columns (CREATE TABLE IF NOT
        # EXISTS does not add them), so every landscape query would then fail on a
        # column that isn't there. Fail closed instead, with a rebuild step.
        if not new_file:
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if version != STORE_VERSION:
                self.conn.close()
                raise TrialStoreSchemaError(
                    f"the trial database at {self.path} was built by an older version "
                    f"(schema v{version or 1}, current is v{STORE_VERSION}) and lacks the "
                    "eligibility and location columns. Delete it and re-ingest:\n"
                    f"    rm {self.path}\n"
                    '    python -m medrag trials --condition "..." --intervention "..."'
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

    def _existing_provenance(self, nct_ids: list[str]) -> dict[str, tuple[list, list]]:
        """Read back what earlier ingests recorded, so a re-ingest UNIONs rather
        than overwrites. Dropping a prior query label would erase the answer to
        'did we ever search for colon cancer?' — which is the point of storing it."""
        out: dict[str, tuple[list, list]] = {}
        batch = 500
        for i in range(0, len(nct_ids), batch):
            chunk = nct_ids[i : i + batch]
            rows = self.conn.execute(
                f"SELECT nct_id, found_by, query_sets FROM trials WHERE nct_id IN "
                f"({', '.join('?' * len(chunk))})", chunk
            ).fetchall()
            for row in rows:
                labels = json.loads(row["found_by"] or "[]")
                sets = [s for s in (row["query_sets"] or "").split() if s]
                out[row["nct_id"]] = (labels, sets)
        return out

    def upsert(
        self,
        records: list[TrialRecord],
        provenance: dict[str, list[str]] | None = None,
        set_key: str | None = None,
    ) -> int:
        """Insert or refresh records. Re-ingesting updates status in place -
        a trial that was RECRUITING last month may be TERMINATED today, and
        that transition is the entire point of tracking it.

        `provenance` maps NCT ID to the query labels that found it and `set_key`
        names the query set; both are merged with whatever earlier ingests
        recorded, never replaced.
        """
        if not records:
            return 0

        prior = self._existing_provenance([r.nct_id for r in records])

        rows = []
        for r in records:
            d = r.to_dict()
            for f in _ARRAY_FIELDS:
                d[f] = json.dumps(d[f])

            old_labels, old_sets = prior.get(r.nct_id, ([], []))
            labels = list(old_labels)
            for lab in (provenance or {}).get(r.nct_id, []):
                if lab not in labels:
                    labels.append(lab)
            sets = list(old_sets)
            if set_key and set_key not in sets:
                sets.append(set_key)
            d["found_by"] = json.dumps(labels)
            d["query_sets"] = (" " + " ".join(sets) + " ") if sets else ""
            # The gating census is deterministic (regex only) and computed once
            # here, so a landscape COUNT is real SQL over the stored flags — not a
            # parse of the retrieved sample.
            flags = gate_markers(r.eligibility_criteria)
            d["biomarker_gating"] = gating_tokens(flags)
            d["biomarker_flags"] = json.dumps(
                {k: {"status": f.status, "span": f.span} for k, f in flags.items()}
            )
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

    # Columns computed by the store, not part of TrialRecord.
    _NON_RECORD_COLS = ("ingested_at", "biomarker_gating", "biomarker_flags",
                        "found_by", "query_sets")

    @staticmethod
    def _to_record(row: sqlite3.Row) -> TrialRecord:
        d = {k: row[k] for k in row.keys() if k not in TrialStore._NON_RECORD_COLS}
        for f in _ARRAY_FIELDS:
            d[f] = json.loads(d[f] or "[]")
        # SQLite has no bool; restore it, keeping NULL ("not stated") distinct
        # from False.
        hv = d.get("healthy_volunteers")
        d["healthy_volunteers"] = None if hv is None else bool(hv)
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
        query_set: str | None = None,
        limit: int = 50,
    ) -> list[TrialRecord]:
        """Structured filter query. This is the precision the registry exists for.

        `query_set` selects the population the FETCH defined — every trial any
        query in that set returned — and is what an indication-first caller should
        use. `condition` re-runs a substring match over the free-text condition
        array with different logic from the fetch, so it DISCARDS trials the
        ingest deliberately went and got: "Colorectal Neoplasms" does not contain
        "colorectal cancer". It is kept only for `stopped_trials`, where it is
        ORed with intervention to WIDEN a negative-evidence sweep rather than
        narrow a population. Do not reach for it to scope a landscape.
        """
        where, params = [], []

        if intervention:
            where.append("LOWER(interventions) LIKE ?")
            params.append(f"%{intervention.lower()}%")
        if condition:
            where.append("LOWER(conditions) LIKE ?")
            params.append(f"%{condition.lower()}%")
        if query_set:
            where.append("query_sets LIKE ?")
            params.append(f"% {query_set} %")
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

    def count(self, query_set: str | None = None) -> int:
        """How many trials the store holds, optionally for one query set. This is
        the denominator a landscape must state — a listed count means nothing
        without the population it was drawn from."""
        if not query_set:
            return len(self)
        return self.conn.execute(
            "SELECT COUNT(*) FROM trials WHERE query_sets LIKE ?", (f"% {query_set} %",)
        ).fetchone()[0]

    def found_by(self, nct_id: str) -> list[str]:
        """The query labels that returned this trial — the audit trail for
        'did we search for colon cancer?'."""
        row = self.conn.execute(
            "SELECT found_by FROM trials WHERE nct_id = ?", (nct_id,)).fetchone()
        return json.loads(row["found_by"] or "[]") if row else []

    def record_coverage(self, report) -> None:
        """Persist one ingest's CoverageReport beside the records it produced."""
        self.conn.execute("DELETE FROM query_coverage WHERE set_key = ?", (report.set_key,))
        with self.conn:
            self.conn.execute(
                "INSERT INTO query_coverage (set_key, set_label, curated, yields, "
                "total_unique, basket_caveat, errors) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    report.set_key, report.set_label, int(report.curated),
                    json.dumps([
                        {"query": y.query.label, "fetched": y.fetched, "new": y.new,
                         "reported_total": y.reported_total, "error": y.error}
                        for y in report.yields
                    ]),
                    report.total_unique, report.basket_caveat,
                    json.dumps(report.errors),
                ),
            )

    def coverage(self, set_key: str) -> dict | None:
        """What was searched for this set, or None if it was never ingested —
        which is NOT the same as 'searched and found nothing'."""
        row = self.conn.execute(
            "SELECT * FROM query_coverage WHERE set_key = ?", (set_key,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["yields"] = json.loads(d["yields"] or "[]")
        d["errors"] = json.loads(d["errors"] or "[]")
        d["curated"] = bool(d["curated"])
        return d

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

    def landscape(
        self,
        condition: str | None = None,
        biomarker_filters: list[tuple[str, str]] | None = None,
        statuses: list[str] | None = None,
        phase: str | None = None,
        query_set: str | None = None,
        sample_limit: int = 25,
    ) -> dict:
        """Aggregates over the FULL match set, computed in SQL — never from a
        retrieved sample. This is the fix for the counting problem: a memo section
        can state the denominator and label its listed trials as a sample of it.

        `biomarker_filters` is a list of (marker_key, status) narrowing the set,
        e.g. [("MSS", "REQUIRED")]. NOT_MENTIONED trials are only included if a
        caller explicitly asks for ("MARKER", "NOT_MENTIONED") — they are never
        folded into a REQUIRED or EXCLUDED count.
        """
        where, params = [], []
        if condition:
            where.append("LOWER(conditions) LIKE ?")
            params.append(f"%{condition.lower()}%")
        if query_set:
            where.append("query_sets LIKE ?")
            params.append(f"% {query_set} %")
        if phase:
            where.append("LOWER(phase) LIKE ?")
            params.append(f"%{phase.lower()}%")
        if statuses:
            where.append(f"UPPER(overall_status) IN ({', '.join('?' * len(statuses))})")
            params.extend(s.upper() for s in statuses)
        for marker, status in (biomarker_filters or []):
            where.append("biomarker_gating LIKE ?")
            params.append(f"%{gating_token(marker, status)}%")
        clause = (" WHERE " + " AND ".join(where)) if where else ""

        def scalar(extra_sql="", extra_params=()):
            return self.conn.execute(
                f"SELECT COUNT(*) FROM trials{clause}{extra_sql}", (*params, *extra_params)
            ).fetchone()[0]

        def group(expr):
            rows = self.conn.execute(
                f"SELECT {expr} AS k, COUNT(*) AS n FROM trials{clause} "
                f"GROUP BY {expr} ORDER BY n DESC", params
            )
            return {(r["k"] if r["k"] not in (None, "") else "not stated"): r["n"] for r in rows}

        total = scalar()
        readable = scalar(" AND TRIM(COALESCE(eligibility_criteria,'')) != ''"
                          if clause else " WHERE TRIM(COALESCE(eligibility_criteria,'')) != ''")

        # Per-marker census over the match set: how many trials require / exclude
        # / do not mention each gating marker. Counts of NOT_MENTIONED are reported
        # explicitly so the gap is visible.
        by_biomarker: dict[str, dict[str, int]] = {}
        for mkey in MARKER_KEYS:
            row = {}
            for st in ("REQUIRED", "EXCLUDED", "NOT_MENTIONED"):
                row[st] = scalar(" AND biomarker_gating LIKE ?", (f"%{gating_token(mkey, st)}%",))
            by_biomarker[mkey] = row

        # A capped sample to list, with its stored gating flags for the table.
        sample_sql = (
            f"SELECT * FROM trials{clause} ORDER BY "
            "CASE WHEN UPPER(overall_status) IN ('RECRUITING','NOT_YET_RECRUITING') THEN 0 ELSE 1 END, "
            "primary_completion_date DESC LIMIT ?"
        )
        sample_rows = self.conn.execute(sample_sql, (*params, sample_limit)).fetchall()
        sample = [self._to_record(r) for r in sample_rows]
        sample_flags = [json.loads(r["biomarker_flags"] or "{}") for r in sample_rows]

        return {
            "total": total,
            "eligibility_readable": readable,
            "shown": len(sample),
            "dropped": max(0, total - len(sample)),
            "by_phase": group("phase"),
            "by_status": group("overall_status"),
            "by_sponsor_class": group("sponsor_class"),
            "by_completion_year": group("substr(primary_completion_date,1,4)"),
            "by_biomarker": by_biomarker,
            # What was searched to build this population, or None if this set was
            # never ingested. A census of 0 because nothing was fetched and a
            # census of 0 because nothing matched are different facts, and the
            # memo has to be able to tell the reader which one it is.
            "coverage": self.coverage(query_set) if query_set else None,
            "population_total": self.count(query_set=query_set) if query_set else len(self),
            "filters": {
                "condition": condition or "",
                "query_set": query_set or "",
                "phase": phase or "",
                "statuses": list(statuses or []),
                "biomarker": list(biomarker_filters or []),
            },
            "sample": sample,
            "sample_flags": sample_flags,
        }

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "TrialStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
