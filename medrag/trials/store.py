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
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from .. import agents, coverage, ranking
from ..biomarker_gating import (
    MARKER_KEYS,
    gate_markers,
    gating_basis_token,
    gating_basis_tokens,
    gating_token,
    gating_tokens,
)
from ..dbopen import connect_read_only, prepare_writable, refuse_write
from .client import STOPPED_STATUSES, TrialRecord

# Bumped when the columns change. Written to PRAGMA user_version so a database
# built before the current columns existed is refused on open rather than read
# with columns silently missing — the same fail-closed choice the vector index
# makes on an embedder or schema mismatch. v1 was the original diligence schema;
# v2 added the patient-perspective fields; v3 adds the precomputed biomarker
# gating census, so a landscape count is real SQL over the full match set rather
# than a parse of the retrieved sample; v4 adds fetch provenance (which queries
# found each trial) and the coverage catalog, so the population a local query
# selects is the one the fetch defined rather than one re-derived from a string;
# v5 adds detailed_description and keywords — both already fetched (whole
# modules requested) but never parsed — because the biomarker matchers now
# consult them when eligibility_criteria itself is silent on a marker (see
# markers.py's collect_signals): ADG126-P001 states MSS only in its detailed
# description, and C-800-25 carries "MSS" verbatim as a registry keyword;
# v6 adds allocation (RANDOMIZED/NON_RANDOMIZED) — same class of gap,
# designModule.designInfo already fetched whole and never parsed — for
# ranking.py's deterministic relevance score (config/ranking.yaml);
# v7 adds biomarker_basis — for a REQUIRED marker, whether the winning
# sentence named it EXPLICITLY or by SYNONYM — so the coverage statement
# (coverage.py) can report "16 explicit, 23 by synonym" as a stored SQL COUNT,
# never a live re-scan of eligibility text;
# v8 adds intervention_tokens — the parsed, normalised agent names from the
# interventions ARRAY (agents.py) — so a drug filter matches a structured fact
# instead of running LIKE '%<asset>%' over the JSON array rendered as one
# string, which could never match a combination ("botensilimab and
# balstilimab" is not a substring of '["Botensilimab", "Balstilimab"]').
# v9 adds the ingest lifecycle to query_coverage (status, started_at, held) —
# see INGEST_STATES below. Every guard in this codebase against a truncated
# population fires on a RESPONSE that came back short; none of them can fire
# when the PROCESS dies, because a killed ingest raises nothing. The marker is
# written before the fetch and only cleared by a verified count, so an
# interrupted run leaves a family visibly in progress instead of a plausible
# lie.
# v10 replaces the `query_sets` space-padded token column with a real indexed
# join table (`trial_query_sets`). The token scheme required
# `query_sets LIKE '% key %'`, and a LEADING-wildcard LIKE cannot use an index,
# so every landscape search full-scanned all 241,298 rows — six times, once per
# count/query/provenance/census call. Measured: ~3 seconds of fixed cost on
# EVERY search regardless of how small the family was (a 104-trial family took
# 3.1s). The column is DROPPED rather than kept alongside: two sources of the
# same truth is how `biomarker.py` and `biomarker_gating.py` drifted apart, and
# the migration asserts the join table reproduces the column exactly before
# removing it.
# v11 recomputes the biomarker census columns (biomarker_gating,
# biomarker_basis, biomarker_flags). They are baked at ingest, so a change to
# the MATCHER leaves the stored census describing the old rules — and this
# codebase's rule is that a stale derived column is refused rather than read.
# Two matcher fixes forced it: markers.resolve_marker no longer substring-matches
# a query onto the wrong marker, and _is_test_requirement now also neutralises an
# assay PANEL listing (a sentence enumerating the variants an assay covers is not
# a sentence saying the patient has them). Pure recomputation from stored text —
# no re-fetch.
# v12 adds `intervention_types` — the registry's own DRUG/DEVICE/DIAGNOSTIC_TEST
# enum, fetched on every trial since the first ingest and thrown away. Unlike
# every previous bump this one is NOT derivable from stored text: nothing in the
# file implies it. It is also not worth a full re-ingest, because the only
# missing field lives in a module the API will return on its own. Hence
# `_REFETCHABLE_FROM` below — a third category between "recompute offline" and
# "delete and start over".
STORE_VERSION = 12

#: The backfill's record of what has been ASKED. Separate from the column, which
#: records what has been ANSWERED — an ID the registry does not return leaves the
#: column NULL (writing [] would claim the trial has no typed interventions),
#: so without this there is no way to tell "not yet reached" from "asked and
#: there is nothing there".
BACKFILL_LEDGER = """
CREATE TABLE IF NOT EXISTS intervention_type_backfill (
    nct_id   TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL DEFAULT 0,
    outcome  TEXT
)
"""

#: The three states a query set can be in, and why there are three rather than
#: a boolean. A family with no row at all was never searched — the
#: not-assessed-vs-nothing-found rule that `ValidationReport.assessed` and
#: `NegativeEvidence.searched` already apply, here at the population level.
#:
#:   IN_PROGRESS  a fetch was started and never verified. Either it is running
#:                now, or the process died. Both mean the same thing to a
#:                reader: what the store holds for this family is not known to
#:                be what the registry has.
#:   COMPLETE     the stored count was verified against the fetch, and every
#:                query in the set reached its own registry-reported total.
#:   PARTIAL      the fetch finished but did not verify — a query errored, a
#:                query came back short, --max-records capped it, or the store
#:                holds a different number than the fetch produced.
#:
#: PARTIAL and IN_PROGRESS are kept apart because they need different actions:
#: PARTIAL records a known shortfall with numbers behind it, IN_PROGRESS
#: records that nobody knows. Neither may ever render as a complete census.
INGEST_IN_PROGRESS = "IN_PROGRESS"
INGEST_COMPLETE = "COMPLETE"
INGEST_PARTIAL = "PARTIAL"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trials (
    nct_id                  TEXT PRIMARY KEY,
    brief_title             TEXT,
    phase                   TEXT,
    overall_status          TEXT,
    why_stopped             TEXT,
    enrollment_count        INTEGER,
    enrollment_type         TEXT,
    allocation              TEXT,   -- RANDOMIZED | NON_RANDOMIZED | '' not stated
    lead_sponsor            TEXT,
    sponsor_class           TEXT,
    start_date              TEXT,
    primary_completion_date TEXT,
    completion_date         TEXT,
    study_type              TEXT,
    conditions              TEXT,   -- JSON array
    interventions           TEXT,   -- JSON array
    -- JSON array, INDEX-ALIGNED with interventions. NULL means this record
    -- predates the column and has never been asked; '[]' or a list of values
    -- (including "") means the registry was asked and this is what it said.
    -- The two are kept apart deliberately: "the registry states no type" and
    -- "we never fetched the field" are different facts, and collapsing them
    -- would make the UNKNOWN classification mean two things at once.
    intervention_types      TEXT,
    intervention_tokens     TEXT,   -- space-padded ' agent ' tokens for LIKE filtering
    collaborators           TEXT,   -- JSON array
    brief_summary           TEXT,
    detailed_description    TEXT,
    keywords                TEXT,   -- JSON array
    eligibility_criteria    TEXT,
    minimum_age             TEXT,
    maximum_age             TEXT,
    sex                     TEXT,
    healthy_volunteers      INTEGER,  -- 1/0/NULL; NULL means not stated
    overall_officials       TEXT,   -- JSON array of {name, role, affiliation}
    central_contacts        TEXT,   -- JSON array of {name, email, phone}
    locations               TEXT,   -- JSON array of {facility, city, state, country, status}
    biomarker_gating        TEXT,   -- space-padded ' MARKER:STATUS ' tokens for LIKE filtering
    biomarker_basis         TEXT,   -- space-padded ' MARKER:EXPLICIT|SYNONYM|NONE ' tokens
    biomarker_flags         TEXT,   -- JSON {marker: {status, span}}, computed at ingest
    found_by                TEXT,   -- JSON array of query labels that returned this trial
    ingested_at             TEXT DEFAULT CURRENT_TIMESTAMP
);
-- Query-set membership, indexed. Replaces `query_sets LIKE '% key %'`, which
-- could not use an index and full-scanned the table on every search.
-- (set_key, nct_id) leading with set_key is the order that matters: every query
-- selects one set and wants its members, so this is a range scan into the
-- trials primary key rather than a table scan.
CREATE TABLE IF NOT EXISTS trial_query_sets (
    nct_id  TEXT NOT NULL,
    set_key TEXT NOT NULL,
    PRIMARY KEY (set_key, nct_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_tqs_nct ON trial_query_sets(nct_id);

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
    status         TEXT,      -- IN_PROGRESS | COMPLETE | PARTIAL; see INGEST_STATES
    started_at     TEXT,      -- when the fetch began, written BEFORE any network call
    held           INTEGER,   -- store count verified at completion; NULL until verified
    updated_at     TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

# JSON-encoded on write, decoded on read. The three patient-perspective lists
# join the diligence trio here.
_ARRAY_FIELDS = (
    "conditions", "keywords", "interventions", "intervention_types", "collaborators",
    "overall_officials", "central_contacts", "locations",
)


#: Membership test against the indexed join table. One definition, used by every
#: caller — the old scheme had the same LIKE pattern written out at eight call
#: sites, which is how a token-format change would have missed one.
#: How a typed asset phrase becomes SQL terms. Two parsers already existed and
#: only one was reachable from trial retrieval.
#:
#:   NAME_AS_ASSET       "trifluridine tipiracil" is ONE molecule, one token.
#:                       Correct for drugs, and the reason a combination like
#:                       "botensilimab and balstilimab" ANDs two agents rather
#:                       than five words.
#:   NAME_AS_DESCRIPTION "procalcitonin assay" is a DESCRIPTION whose words the
#:                       registry reorders freely, so each significant word is
#:                       its own ANDed term. `agents.parse_descriptive_name`
#:                       was written for `device/pma`'s trade_name/generic_name
#:                       and called at exactly one site; trial retrieval used
#:                       the drug parser on device names and returned nothing.
#:
#: THE RULE IS DECLARED, NOT SNIFFED. A caller states which it wants — the
#: question set declares `asset_kind`, and `screening_devices.yaml` is the
#: device one. Guessing the modality from the asset string is how "procalcitonin
#: assay" and "trifluridine tipiracil" become indistinguishable: both are two
#: words, and splitting the second is what would shred a combination asset.
NAME_AS_ASSET = "asset"
NAME_AS_DESCRIPTION = "description"

_QUERY_SET_CLAUSE = (
    "nct_id IN (SELECT nct_id FROM trial_query_sets WHERE set_key = ?)")


def _intervention_clause(intervention: str, params: list, join: str = "AND",
                         name_style: str = NAME_AS_ASSET) -> str:
    """SQL for "this trial involves these agents", over the token column.

    `join` is a deliberate per-caller POLICY, the same shape as the split
    between `biomarker.py` and `biomarker_gating.py`:

      * AND (population selection). "botensilimab and balstilimab" means trials
        carrying BOTH. This is what a diligence section or a claim retrieval
        wants — the asset IS the combination, and a monotherapy trial of one
        half is a different asset.

      * OR (the negative-evidence sweep). `find_stopped_trials` exists to
        surface a molecule that failed somewhere, and ANDing would hide a
        terminated botensilimab-monotherapy trial from a reader diligencing the
        doublet — which is precisely the failure the "OR intervention and
        indication, never AND" rule already guards against one level up. Same
        reasoning, one level down: widening risks a few loosely related trials a
        reader can dismiss, narrowing risks a silence they cannot detect.

    `name_style` picks which PARSER turns the typed phrase into terms, and it is
    passed in by a caller that knows, never sniffed from the string. See
    NAME_AS_ASSET / NAME_AS_DESCRIPTION.

    A term the alias table does not know still matches its own name, so this
    never degrades to matching nothing on an uncurated agent.
    """
    parse = (agents.parse_descriptive_name if name_style == NAME_AS_DESCRIPTION
             else agents.parse_asset)
    query = parse(intervention)
    if not query:
        return "1=1"
    per_term = []
    for term in query.terms:
        per_term.append(
            "(" + " OR ".join(["intervention_tokens LIKE ?"] * len(term.forms)) + ")"
        )
        params.extend(f"% {f} %" for f in term.forms)
    return "(" + f" {join} ".join(per_term) + ")"


#: Schema versions whose gap to the current one is entirely DERIVABLE — every
#: added column can be recomputed from fields already stored, with no network.
#: v7 -> v8 added `intervention_tokens`, which is a parse of the
#: `interventions` array v7 already holds. v8 -> v9 added the ingest lifecycle,
#: which is derivable in the only direction that is safe: a pre-v9 row can be
#: called COMPLETE only when its own recorded numbers prove it (see
#: `_derive_status`), and everything else — including every row whose numbers
#: are merely silent — becomes PARTIAL. A version not listed here is refused
#: v9 -> v10 rebuilds query-set membership as an indexed join table, which is a
#: pure re-shaping of the `query_sets` token column already stored — and the
#: migration ASSERTS the two agree for every family before dropping the column,
#: so the reshaping cannot lose a membership silently.
#: A version not listed here is refused
#: outright, because the missing columns hold data only a re-fetch can supply
#: (v4's fetch provenance, v5's detailed_description) and inventing them would
#: be worse than the refusal.
_BACKFILLABLE_FROM = frozenset({7, 8, 9, 10})

#: Schema versions whose gap needs the REGISTRY but not a re-ingest.
#:
#: A third category, and it exists because the two that came before it are both
#: wrong for v12. `intervention_types` cannot be recomputed from stored text —
#: no column implies it — so it is not backfillable. But "delete it and
#: re-ingest" would throw away 241,298 records, every eligibility text, every
#: provenance label and every verified coverage row to recover one field the API
#: will hand over on its own, and an operator told to do that will reasonably
#: decide the upgrade is not worth it.
#:
#: `python -m medrag trials --backfill-types` fetches ONLY the interventions
#: module for the NCT IDs already held. Measured on the live store: ~42 minutes
#: for all 374 queries, against ~14 hours for a full re-ingest.
#:
#: Note what this category does NOT permit: opening the file and reading the
#: column as empty. That would make UNKNOWN mean both "the registry states no
#: type" and "nobody has asked", which is the not-assessed-versus-nothing-found
#: rule broken at the storage layer. The store still refuses until the backfill
#: has run.
_REFETCHABLE_FROM = frozenset({11})


def verify_ingest(held: int, total_unique: int, yields: list[dict],
                  errors: list[str]) -> tuple[str, list[str]]:
    """Decide COMPLETE vs PARTIAL from recorded numbers, and say why.

    The ONE place that answers "did this family finish", so the live ingest and
    the v8 backfill cannot drift apart — the same reasoning that put the marker
    vocabulary in `markers.py` and the coverage wording in `coverage.py`.

    Note what it checks that `CoverageReport.complete` never did: that each
    query's `fetched` reached its own `reported_total`. `complete` only looked
    for errors, so a `--max-records` ingest — truncation by intent, no error
    raised, `IncompleteFetch` deliberately suppressed — recorded as a finished
    census. That is the same silent-subset failure as a killed process, reached
    through a documented flag instead of a crash.
    """
    reasons: list[str] = []
    if errors:
        reasons.extend(errors)
    for y in yields:
        if y.get("error"):
            reasons.append(f"{y.get('query')} failed — {y['error']}")
            continue
        reported, fetched = y.get("reported_total"), y.get("fetched") or 0
        if reported is None:
            reasons.append(f"{y.get('query')} recorded no registry total to check against")
        elif fetched < reported:
            reasons.append(
                f"{y.get('query')} fetched {fetched:,} of {reported:,} reported")
    if held != total_unique:
        reasons.append(
            f"the store holds {held:,} trials for this set but the fetch produced "
            f"{total_unique:,}")
    return (INGEST_COMPLETE if not reasons else INGEST_PARTIAL), reasons


def migrate_derived_columns(path: str | Path) -> dict:
    """Recompute the columns a newer schema derives from data already on disk.

    Deliberately NOT a general migration framework, and deliberately not
    automatic: it refuses any gap it cannot close honestly, and the fail-closed
    refusal in `TrialStore.__init__` stays the default. What it exists to stop
    is a pointless re-fetch — the alternative for a v7 store was deleting 12,095
    records and pulling them again over the network to obtain a column that is
    a pure function of a column already sitting in the file. That is the same
    reasoning as `ingest_pubmed` parking fetched abstracts rather than
    discarding them on a local write failure.
    """
    path = Path(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == STORE_VERSION:
            return {"migrated": False, "from_version": version, "rows": 0,
                    "graded": [], "membership": {}, "reason": "already current"}
        if version in _REFETCHABLE_FROM:
            raise TrialStoreSchemaError(
                f"the trial database at {path} is schema v{version} and v{STORE_VERSION} "
                "adds `intervention_types`, which the registry states and this file has "
                "never been told. It cannot be recomputed from what is stored.\n"
                "  It does NOT need a re-ingest: only the interventions module is "
                "missing, and the API will return it for the records already held.\n"
                f"    python -m medrag trials --backfill-types\n"
                "  Roughly 42 minutes for all 374 queries; nothing else in the file is "
                "touched.\n"
                "  Until it runs, the store is refused rather than opened with an empty "
                "column: an empty column cannot be told apart from a registry that "
                "stated no type, and the classification depends on that difference."
            )
        if version not in _BACKFILLABLE_FROM:
            raise TrialStoreSchemaError(
                f"the trial database at {path} is schema v{version or 1} and the gap to "
                f"v{STORE_VERSION} is not derivable from what it holds — the missing "
                "columns need data only a re-fetch can supply. Delete it and re-ingest:\n"
                f"    rm {path}\n"
                '    python -m medrag trials --condition "..."'
            )

        # Each step is guarded by the version it closes, so migrating a v8 file
        # does not pointlessly recompute 15,000 token blobs that are already
        # there — and so a future v10 step cannot silently re-run v8's.
        updates: list = []
        if version < 8:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(trials)")}
            if "intervention_tokens" not in cols:
                conn.execute("ALTER TABLE trials ADD COLUMN intervention_tokens TEXT")

            rows = conn.execute("SELECT nct_id, interventions FROM trials").fetchall()
            updates = [
                (agents.token_blob(json.loads(r["interventions"] or "[]")), r["nct_id"])
                for r in rows
            ]

        graded: list = []
        if version < 9:
            cov_cols = {r["name"] for r in conn.execute("PRAGMA table_info(query_coverage)")}
            for col, decl in (("status", "TEXT"), ("started_at", "TEXT"), ("held", "INTEGER")):
                if col not in cov_cols:
                    conn.execute(f"ALTER TABLE query_coverage ADD COLUMN {col} {decl}")

            # A pre-v9 row carries no marker, so completeness has to be RE-DERIVED
            # from the numbers it does carry — and only a row whose own numbers
            # prove it may be called COMPLETE. Anything ambiguous grades PARTIAL:
            # a family wrongly told to re-run costs one fetch, a family wrongly
            # called complete is the failure this whole change exists to stop.
            for row in conn.execute(
                    "SELECT set_key, yields, total_unique, errors FROM query_coverage"):
                held = conn.execute(
                    "SELECT COUNT(*) FROM trial_query_sets WHERE set_key = ?",
                    (row["set_key"],)).fetchone()[0]
                status, _why = verify_ingest(
                    held=held,
                    total_unique=row["total_unique"] or 0,
                    yields=json.loads(row["yields"] or "[]"),
                    errors=json.loads(row["errors"] or "[]"),
                )
                graded.append((status, held, row["set_key"]))

        # v9 -> v10: build the indexed join table from the token column, PROVE it
        # reproduces that column exactly for every family, and only then drop the
        # column. Leaving both would be two sources of one truth — the drift that
        # produced the biomarker bug — and dropping it without the check would
        # risk losing a membership silently, which is the failure this whole
        # codebase is organised against.
        membership_check: dict = {}
        if version < 10:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(trials)")}
            conn.executescript(
                "CREATE TABLE IF NOT EXISTS trial_query_sets ("
                "  nct_id TEXT NOT NULL, set_key TEXT NOT NULL,"
                "  PRIMARY KEY (set_key, nct_id)) WITHOUT ROWID;"
                "CREATE INDEX IF NOT EXISTS idx_tqs_nct ON trial_query_sets(nct_id);")

            if "query_sets" in cols:
                pairs = []
                for row in conn.execute("SELECT nct_id, query_sets FROM trials"):
                    for key in (row["query_sets"] or "").split():
                        pairs.append((row["nct_id"], key))
                conn.executemany(
                    "INSERT OR IGNORE INTO trial_query_sets (nct_id, set_key) VALUES (?, ?)",
                    pairs)

                # The assertion. Per family, the old LIKE and the new join must
                # select exactly the same NCT IDs.
                mismatches = []
                for (key,) in conn.execute(
                        "SELECT DISTINCT set_key FROM trial_query_sets ORDER BY set_key"):
                    old_ids = {r[0] for r in conn.execute(
                        "SELECT nct_id FROM trials WHERE query_sets LIKE ?",
                        (f"% {key} %",))}
                    new_ids = {r[0] for r in conn.execute(
                        "SELECT nct_id FROM trial_query_sets WHERE set_key = ?", (key,))}
                    if old_ids != new_ids:
                        mismatches.append(
                            f"{key}: token column {len(old_ids)}, join table {len(new_ids)}, "
                            f"symmetric difference {len(old_ids ^ new_ids)}")
                    membership_check[key] = len(new_ids)
                if mismatches:
                    raise TrialStoreSchemaError(
                        "refusing to migrate: the join table does not reproduce the "
                        "query_sets column.\n  " + "\n  ".join(mismatches) +
                        "\n  Nothing has been changed. This is a bug in the migration, "
                        "not in your data.")

                conn.execute("ALTER TABLE trials DROP COLUMN query_sets")

        # v10 -> v11: recompute the biomarker census from stored text. A pure
        # function of columns already held, so no network — but it MUST run when
        # the matcher changes, or every landscape count still reflects the old
        # rules while the live screen reflects the new ones. That divergence is
        # exactly what the census/live equality gate exists to catch.
        census: list = []
        if version < 11:
            rows = conn.execute(
                "SELECT nct_id, eligibility_criteria, detailed_description, "
                "brief_summary, keywords FROM trials").fetchall()
            for r in rows:
                flags = gate_markers(
                    r["eligibility_criteria"] or "",
                    detailed_description=r["detailed_description"] or "",
                    brief_summary=r["brief_summary"] or "",
                    keywords=json.loads(r["keywords"] or "[]"),
                )
                census.append((
                    gating_tokens(flags), gating_basis_tokens(flags),
                    json.dumps({k: {"status": f.status, "span": f.span}
                                for k, f in flags.items()}),
                    r["nct_id"]))

        with conn:
            if census:
                conn.executemany(
                    "UPDATE trials SET biomarker_gating = ?, biomarker_basis = ?, "
                    "biomarker_flags = ? WHERE nct_id = ?", census)
            if updates:
                conn.executemany(
                    "UPDATE trials SET intervention_tokens = ? WHERE nct_id = ?", updates)
            if graded:
                conn.executemany(
                    "UPDATE query_coverage SET status = ?, held = ? WHERE set_key = ?", graded)
            conn.execute(f"PRAGMA user_version = {STORE_VERSION}")
        return {"migrated": True, "from_version": version, "rows": len(updates),
                "graded": [(k, s) for s, _h, k in graded],
                "membership": membership_check, "census_rows": len(census),
                "reason": ""}
    finally:
        conn.close()


class TrialStoreSchemaError(RuntimeError):
    """A trials.db built before the current schema. Carries a rebuild instruction
    so the CLI can print something a user can act on, not a traceback."""


class TrialStore:
    def __init__(self, path: str | Path, read_only: bool = False,
                 immutable: bool = False):
        """Open the trial store.

        `read_only=True` opens a connection that CANNOT write and performs no
        schema execution, no `PRAGMA user_version` write and no commit — the
        constructor's writes are the reason a reader used to take a write lock
        and die with "database is locked" whenever an ingest was running, and
        the reason the app could not start at all on a read-only filesystem.

        `immutable=True` additionally tells SQLite the file is a frozen
        snapshot, which is what a genuinely read-only mount needs (no lock file,
        no -wal, no -shm to create) and which is WRONG while an ingest may be
        writing. See `dbopen` for the measurement behind that split.
        """
        self.path = Path(path)
        self.read_only = read_only

        if read_only:
            # Nothing here creates, changes or locks the file: no mkdir (the
            # directory may itself be read-only), no schema, no version write,
            # no chmod. A missing file raises rather than being created empty,
            # because an empty store answers every question "nothing found".
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

        # Refuse a stale database before touching it. Running the new SCHEMA over
        # an old file would leave the table missing columns (CREATE TABLE IF NOT
        # EXISTS does not add them), so every landscape query would then fail on a
        # column that isn't there. Fail closed instead, with a rebuild step.
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
        read-only reader gets the same refusal (and the same remedy) as an
        ingest rather than quietly querying columns that are not there."""
        if version == STORE_VERSION:
            return
        self.conn.close()
        # A store NEWER than the code is a different fault with the OPPOSITE
        # remedy, and it is checked FIRST because every branch below assumes the
        # store is behind. Such a store lacks nothing: a later revision wrote it,
        # so it holds more than this code knows how to read, not less. The thing
        # that has to move is the CODE.
        #
        # Until this branch existed the final `else` caught it, so the message
        # said "built by an older version" — false — and handed the operator
        # `rm <store>` plus a re-ingest. Following that destroys a verified
        # 241,298-record store to recover data it already has, on a diagnosis
        # that is backwards. A destructive instruction given on a wrong
        # diagnosis is worse than a crash: a crash stops, this proceeds.
        #
        # The branch states what to KEEP and what to check out, and deliberately
        # does not spell out the destructive command in order to forbid it — a
        # reader skimming, or a text match, sees the instruction and not its
        # negation. Same rule as the caveats phrasing.py lints. See CLAUDE.md.
        if version > STORE_VERSION:
            raise TrialStoreSchemaError(
                f"the trial database at {self.path} was built by a NEWER version "
                f"of this tool (schema v{version}, this code is v{STORE_VERSION}). "
                "The store is intact and needs no re-fetch: it holds more than "
                "this code can read, not less. Keep the store and move the code — "
                f"check out the revision that declares STORE_VERSION = {version}:\n"
                f"    git log -S 'STORE_VERSION = {version}' -- medrag/trials/store.py\n"
                "  If no revision in history declares it, the store was written "
                "by uncommitted work, and the only code that can read it is that "
                "working tree."
            )
        # Two different remedies, and telling them apart matters: some gaps need
        # a re-fetch, and some are a pure recomputation of data already in the
        # file. Sending an operator to re-download 12,000 records for a column
        # that can be derived locally is its own kind of wrong answer.
        if version in _BACKFILLABLE_FROM:
            remedy = ("every column it lacks can be recomputed from what it "
                      "already holds — no re-download:\n"
                      "    python -m medrag trials --migrate")
        elif version in _REFETCHABLE_FROM:
            # The third remedy. Telling a v11 operator to delete a verified
            # 241,298-record store to recover ONE field the API returns on its
            # own is a wrong answer that reads like a correct one, and an
            # operator given it will reasonably skip the upgrade instead.
            remedy = ("the one column it lacks (`intervention_types`) is stated by "
                      "the registry and cannot be derived locally — but it does NOT "
                      "need a re-ingest, only the interventions module for records "
                      "already held:\n"
                      "    python -m medrag trials --backfill-types\n"
                      "  Roughly 42 minutes for a full store; nothing else is touched. "
                      "Until then the store is refused rather than opened with an "
                      "empty column, because an empty column and a registry that "
                      "stated no type cannot be told apart, and the device/drug "
                      "classification depends on that difference.")
        else:
            remedy = ("the columns it lacks need data only a re-fetch can "
                      "supply. Delete it and re-ingest:\n"
                      f"    rm {self.path}\n"
                      '    python -m medrag trials --condition "..."')
        raise TrialStoreSchemaError(
            f"the trial database at {self.path} was built by an older version "
            f"(schema v{version or 1}, current is v{STORE_VERSION}). " + remedy
        )

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
                f"SELECT nct_id, found_by FROM trials WHERE nct_id IN "
                f"({', '.join('?' * len(chunk))})", chunk
            ).fetchall()
            members: dict[str, list[str]] = {}
            for r in self.conn.execute(
                    f"SELECT nct_id, set_key FROM trial_query_sets WHERE nct_id IN "
                    f"({', '.join('?' * len(chunk))})", chunk):
                members.setdefault(r["nct_id"], []).append(r["set_key"])
            for row in rows:
                labels = json.loads(row["found_by"] or "[]")
                out[row["nct_id"]] = (labels, members.get(row["nct_id"], []))
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
        refuse_write(self, "upsert")
        if not records:
            return 0

        prior = self._existing_provenance([r.nct_id for r in records])

        memberships: list[tuple[str, str]] = []
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
            # Membership goes to the indexed join table, not a token column —
            # collected here and written in the same transaction as the rows.
            memberships.extend((r.nct_id, key) for key in sets)
            # The gating census is deterministic (regex only) and computed once
            # here, so a landscape COUNT is real SQL over the stored flags — not a
            # parse of the retrieved sample. Supplementary fields are consulted
            # by gate_markers only when eligibility_criteria itself is silent.
            flags = gate_markers(
                r.eligibility_criteria,
                detailed_description=r.detailed_description,
                brief_summary=r.brief_summary,
                keywords=r.keywords,
            )
            # Agent names are parsed from the ARRAY, never the joined string —
            # see agents.py. Stored as the registry's own surface forms, not
            # canonicalised to a generic: alias expansion happens at query time
            # so config/agents.yaml can gain a brand name without a re-ingest.
            d["intervention_tokens"] = agents.token_blob(r.interventions)
            d["biomarker_gating"] = gating_tokens(flags)
            d["biomarker_basis"] = gating_basis_tokens(flags)
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
            # Membership MERGES, never replaces — dropping a prior set would
            # erase the answer to "did we ever search for colon cancer?", the
            # same rule `found_by` follows. INSERT OR IGNORE on the composite
            # primary key is that merge.
            self.conn.executemany(
                "INSERT OR IGNORE INTO trial_query_sets (nct_id, set_key) VALUES (?, ?)",
                memberships,
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
    _NON_RECORD_COLS = ("ingested_at", "biomarker_gating", "biomarker_basis",
                        "biomarker_flags", "found_by",
                        "intervention_tokens")

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
        intervention_join: str = "AND",
        admits_marker: str | None = None,
        name_style: str = NAME_AS_ASSET,
    ) -> list[TrialRecord]:
        """Structured filter query. This is the precision the registry exists for.

        `query_set` selects the population the FETCH defined — every trial any
        query in that set returned — and is what an indication-first caller should
        use. `condition` re-runs a substring match over the free-text condition
        array with different logic from the fetch, so it DISCARDS trials the
        ingest deliberately went and got: "Colorectal Neoplasms" does not contain
        "colorectal cancer". It has NO production caller: `stopped_trials` was
        the last one, exempted on the reasoning that a substring only ever
        widens a negative-evidence sweep. Measured, it does the opposite — it
        saw 557 of 1,336 stopped colorectal trials, missing 58% — so that arm
        selects by `query_set` too. Do not reach for this to scope anything.

        `intervention` is matched against the parsed agent tokens, not as a
        substring of the joined array — a combination like "botensilimab and
        balstilimab" ANDs its agents and each agent ORs its aliases. See
        agents.py. Use `intervention_terms()` to find out WHICH agent of a
        combination returned nothing before reporting an empty result.
        """
        where, params = [], []

        if intervention:
            where.append(_intervention_clause(intervention, params, join=intervention_join,
                                              name_style=name_style))
        if condition:
            where.append("LOWER(conditions) LIKE ?")
            params.append(f"%{condition.lower()}%")
        if query_set:
            where.append(_QUERY_SET_CLAUSE)
            params.append(query_set)
        if admits_marker:
            # THE CENSUS PREFILTER. Narrow to trials the ingest-time census says
            # could admit this marker, so the caller's live screen runs over
            # hundreds of records instead of tens of thousands. On colorectal
            # that is 826 instead of 12,095.
            #
            # Safe ONLY because it was proven so: for all 74 families and all 7
            # curated markers — 2,150,918 record-comparisons — the set the census
            # admits is exactly the set the live matcher admits. The census has
            # no UNCLEAR (a conflict resolves to REQUIRED, which is admitting),
            # so a live-UNCLEAR trial is always kept. See
            # tests/test_census_live_parity.py, which keeps that equality
            # enforced so a future census change cannot silently reintroduce the
            # gap this prefilter would then hide.
            where.append("(biomarker_gating LIKE ? OR biomarker_gating LIKE ?)")
            params.append(f"%{gating_token(admits_marker, 'REQUIRED')}%")
            params.append(f"%{gating_token(admits_marker, 'ELIGIBLE_BY_EXCLUSION')}%")
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

    def search(self, text: str, limit: int = 20,
               query_set: str | None = None) -> list[TrialRecord]:
        """Free-text search across titles, conditions, interventions, sponsors.

        Every token is ORed, so this is a LOOSE match and always has been — it
        exists to rescue a section a structured filter emptied, not to select a
        population. Two consequences a caller must hold in mind. Give it a
        sentence and it matches on the sentence's furniture: handed the rendered
        diligence question it answered a hidradenitis query with colorectal
        trials, because `trials`, `other` and `run` are in the index and the
        asset name is not. And a row coming back means one token matched
        somewhere, never that the record is about the query — `trials.anchors`
        is the check that belongs behind this one.

        `query_set` confines the match to a family the ingest actually fetched,
        via the same indexed join `query()` uses. It cannot make the match
        relevant; it stops the fallback answering from a population nobody
        asked about.
        """
        safe = " OR ".join(f'"{t}"' for t in text.split() if t.strip())
        if not safe:
            return []
        sql = "SELECT nct_id FROM trials_fts WHERE trials_fts MATCH ?"
        params: list = [safe]
        if query_set:
            sql += " AND " + _QUERY_SET_CLAUSE
            params.append(query_set)
        sql += " LIMIT ?"
        params.append(limit)
        try:
            rows = self.conn.execute(sql, params).fetchall()
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
            "SELECT COUNT(*) FROM trial_query_sets WHERE set_key = ?", (query_set,)
        ).fetchone()[0]

    def biomarker_counts(self, marker: str, query_set: str | None = None) -> dict[str, int]:
        """The REQUIRED/ELIGIBLE_BY_EXCLUSION/EXCLUDED/NOT_MENTIONED census for
        ONE marker over a query set's population — the same computation
        `landscape()`'s `by_biomarker` does for all seven, exposed standalone
        so the coverage statement (coverage.py) does not pay for six markers
        it will not report."""
        where, params = [], []
        if query_set:
            where.append(_QUERY_SET_CLAUSE)
            params.append(query_set)
        clause = (" WHERE " + " AND ".join(where) + " AND " if where else " WHERE ")
        return {
            status: self.conn.execute(
                f"SELECT COUNT(*) FROM trials{clause}biomarker_gating LIKE ?",
                (*params, f"%{gating_token(marker, status)}%"),
            ).fetchone()[0]
            for status in ("REQUIRED", "ELIGIBLE_BY_EXCLUSION", "EXCLUDED", "NOT_MENTIONED")
        }

    def records_by_id(self, nct_ids: list[str]) -> list[TrialRecord]:
        """Fetch specific trials by NCT ID, in one query per batch.

        The precomputed fast path already knows WHICH trials it wants and in
        what order, so it needs exactly these rows and not a filtered scan —
        loading 30 records instead of 826 is most of what the precompute buys.
        """
        if not nct_ids:
            return []
        out: list[TrialRecord] = []
        batch = 500
        for i in range(0, len(nct_ids), batch):
            chunk = nct_ids[i : i + batch]
            rows = self.conn.execute(
                f"SELECT * FROM trials WHERE nct_id IN ({', '.join('?' * len(chunk))})",
                chunk).fetchall()
            out.extend(self._to_record(r) for r in rows)
        return out

    def count_without_eligibility(self, query_set: str | None = None) -> int:
        """Trials in the population with no eligibility text on file at all.

        A SQL count, because the census prefilter means most records are never
        loaded — and this number is printed ("N of which have no eligibility
        text"), so inferring it from the screened subset would report 0 for a
        population that has hundreds. It is a subset of NOT_MENTIONED: nothing
        to screen is not the same as screened and silent, the same distinction
        `ValidationReport.assessed` draws.
        """
        where, params = [], []
        if query_set:
            where.append(_QUERY_SET_CLAUSE)
            params.append(query_set)
        where.append("(eligibility_criteria IS NULL OR TRIM(eligibility_criteria) = '')")
        return self.conn.execute(
            f"SELECT COUNT(*) FROM trials WHERE {' AND '.join(where)}", params).fetchone()[0]

    def biomarker_basis_counts(self, marker: str, query_set: str | None = None) -> dict[str, int]:
        """For a REQUIRED marker, the SQL-COUNTED split of how many admitting
        trials name it EXPLICITLY versus only by SYNONYM, over one query set's
        population. Feeds the coverage statement's 'N explicit, M by synonym'
        line — a stored count, never a live re-scan of eligibility text."""
        where, params = [], []
        if query_set:
            where.append(_QUERY_SET_CLAUSE)
            params.append(query_set)
        clause = (" WHERE " + " AND ".join(where) + " AND " if where else " WHERE ")
        return {
            basis: self.conn.execute(
                f"SELECT COUNT(*) FROM trials{clause}biomarker_basis LIKE ?",
                (*params, f"%{gating_basis_token(marker, basis)}%"),
            ).fetchone()[0]
            for basis in ("EXPLICIT", "SYNONYM")
        }

    def found_by(self, nct_id: str) -> list[str]:
        """The query labels that returned this trial — the audit trail for
        'did we search for colon cancer?'."""
        row = self.conn.execute(
            "SELECT found_by FROM trials WHERE nct_id = ?", (nct_id,)).fetchone()
        return json.loads(row["found_by"] or "[]") if row else []

    def found_by_map(self, query_set: str | None = None) -> dict[str, list[str]]:
        """`found_by` for a whole population in one read.

        Provenance is one of `ranking.score_record`'s inputs, and it is a
        store-computed column rather than part of TrialRecord
        (`_NON_RECORD_COLS`), so a caller that ranks a screened population — the
        patient landscape does — would otherwise issue one point query per
        trial to get it.
        """
        sql = "SELECT nct_id, found_by FROM trials"
        params: list = []
        if query_set:
            sql += f" WHERE {_QUERY_SET_CLAUSE}"
            params.append(query_set)
        return {r["nct_id"]: json.loads(r["found_by"] or "[]")
                for r in self.conn.execute(sql, params)}

    def begin_ingest(self, qset) -> None:
        """Mark a query set IN_PROGRESS **before the first network call**.

        This is the half of the guarantee that `IncompleteFetch` cannot give.
        That exception fires on a short RESPONSE; it has nothing to say about a
        process that is killed, and a killed ingest raises nothing at all — the
        family simply stops growing and then sits in the store looking exactly
        like a finished one. Writing the marker first inverts the default: the
        crash leaves a visible in-progress state, and only a verified count
        clears it.

        A family that was already COMPLETE and is being re-fetched is knocked
        back to IN_PROGRESS too, and that is deliberate rather than tidy: the
        moment a new fetch starts writing, the old recorded total no longer
        describes what the store holds, so continuing to advertise it would be
        the same lie in a smaller window. The previous numbers are kept in the
        row, so the coverage line can still say "N of M" while it says the
        ingest did not finish.
        """
        refuse_write(self, "begin_ingest")
        with self.conn:
            self.conn.execute(
                "INSERT INTO query_coverage (set_key, set_label, curated, status, "
                "started_at, held) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, NULL) "
                "ON CONFLICT(set_key) DO UPDATE SET set_label=excluded.set_label, "
                "curated=excluded.curated, status=excluded.status, "
                "started_at=CURRENT_TIMESTAMP, held=NULL",
                (qset.key, getattr(qset, "label", qset.key),
                 int(getattr(qset, "curated", True)), INGEST_IN_PROGRESS),
            )

    def record_coverage(self, report) -> SimpleNamespace:
        """Persist one ingest's CoverageReport beside the records it produced,
        and grade it — COMPLETE only when the stored count is verified against
        what the fetch produced and every query reached its registry-reported
        total.

        `held` is counted here rather than passed in on purpose. The claim being
        made is about the DATABASE, so it has to be read from the database; a
        caller handing in the number it hoped for would verify nothing. Must
        therefore be called after `upsert`, which is the order `cmd_trials`
        uses.
        """
        refuse_write(self, "record_coverage")
        held = self.count(query_set=report.set_key)
        yields = [
            {"query": y.query.label, "fetched": y.fetched, "new": y.new,
             "reported_total": y.reported_total, "error": y.error,
             # Stored so "was the registry healthy when we fetched this?" is
             # answerable from the database months later, not only from whatever
             # scrolled past in a terminal at the time.
             "retries": getattr(y, "retries", 0),
             "retry_seconds": round(getattr(y, "retry_seconds", 0.0), 1)}
            for y in report.yields
        ]
        status, reasons = verify_ingest(
            held=held, total_unique=report.total_unique,
            yields=yields, errors=list(report.errors),
        )
        prior = self.conn.execute(
            "SELECT started_at FROM query_coverage WHERE set_key = ?",
            (report.set_key,)).fetchone()
        started_at = prior["started_at"] if prior else None

        self.conn.execute("DELETE FROM query_coverage WHERE set_key = ?", (report.set_key,))
        with self.conn:
            self.conn.execute(
                "INSERT INTO query_coverage (set_key, set_label, curated, yields, "
                "total_unique, basket_caveat, errors, status, started_at, held) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    report.set_key, report.set_label, int(report.curated),
                    json.dumps(yields),
                    report.total_unique, report.basket_caveat,
                    json.dumps(report.errors), status, started_at, held,
                ),
            )
        return SimpleNamespace(status=status, reasons=reasons, held=held,
                               total_unique=report.total_unique)

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
        # A row with no status predates the lifecycle columns and cannot be read
        # as a finished census: unknown grades PARTIAL, never COMPLETE.
        d["status"] = d.get("status") or INGEST_PARTIAL
        d["verified_complete"] = d["status"] == INGEST_COMPLETE
        if d["held"] is None:
            d["held"] = self.count(query_set=set_key)
        return d

    def ingest_states(self) -> list[dict]:
        """Every query set the store has a row for, with its lifecycle state and
        the two numbers a re-run decision needs. Ordered incomplete-first,
        because the reason to ask is to find what still needs fetching."""
        out = []
        for row in self.conn.execute(
                "SELECT set_key, set_label, status, total_unique, held, started_at, "
                "updated_at FROM query_coverage"):
            d = dict(row)
            d["status"] = d["status"] or INGEST_PARTIAL
            if d["held"] is None:
                d["held"] = self.count(query_set=d["set_key"])
            out.append(d)
        return sorted(out, key=lambda d: (d["status"] == INGEST_COMPLETE, d["set_key"]))

    def incomplete_sets(self) -> list[dict]:
        """The families a resume must re-run: started and never verified, or
        verified and found short. A family with NO row is absent from this list
        by design — never searched and searched-but-unfinished are different
        states, and only the caller holding the config knows the full list of
        families that ought to exist."""
        return [d for d in self.ingest_states() if d["status"] != INGEST_COMPLETE]

    def stopped_trials(
        self, intervention: str | None = None, condition: str | None = None,
        limit: int = 50, query_set: str | None = None,
    ) -> list[TrialRecord]:
        """Deterministic half of the negative-evidence pass. No model involved.

        The agents of a combination are ORed here, not ANDed — a terminated
        monotherapy trial of one half is exactly the signal this sweep exists to
        surface. See `_intervention_clause` for the full reasoning; it is the
        same widen-rather-than-narrow rule `find_stopped_trials` already applies
        to intervention-vs-condition, applied one level down.

        `query_set` selects the indication arm the way every other consumer
        selects a population. `condition` remains only for a caller that
        genuinely wants a raw substring; it under-selects badly (measured: 557
        of 1,336 stopped colorectal trials) and no production path uses it.
        """
        return self.query(
            intervention=intervention, condition=condition, stopped_only=True, limit=limit,
            query_set=query_set, intervention_join="OR",
        )

    def stopped_trials_total(
        self, intervention: str | None = None, condition: str | None = None,
        query_set: str | None = None,
    ) -> int:
        """How many stopped trials the arm actually HAS, independent of any cap.

        The denominator `stopped_trials` cannot supply. Without it a sweep that
        shows 25 of 1,336 and a sweep that shows 25 of 25 are indistinguishable
        — the same gap `store.landscape` closed with population_total and the
        landscape page closed with n_candidates.
        """
        where, params = [], []
        if intervention:
            where.append(_intervention_clause(intervention, params, join="OR"))
        if condition:
            where.append("LOWER(conditions) LIKE ?")
            params.append(f"%{condition.lower()}%")
        if query_set:
            where.append(_QUERY_SET_CLAUSE)
            params.append(query_set)
        where.append(
            f"UPPER(overall_status) IN ({', '.join('?' * len(STOPPED_STATUSES))})")
        params.extend(sorted(STOPPED_STATUSES))
        return self.conn.execute(
            f"SELECT COUNT(*) FROM trials WHERE {' AND '.join(where)}", params).fetchone()[0]

    def intervention_terms(self, intervention: str, query_set: str | None = None) -> list[dict]:
        """Per-agent match counts for a typed asset, so an empty combination
        result can name WHICH agent collapsed it.

        `store.query` returns rows with no denominator, which is how "0 of 214"
        and "there were none" became indistinguishable (CLAUDE.md records the
        same gap for the free-text fallback). For a combination the useful
        diagnostic is finer than a total: "botensilimab matched 23, balstilimab
        matched 22, both together 18" is an answer; a bare 0 is not.
        """
        query = agents.parse_asset(intervention)
        out = []
        for term in query.terms:
            params: list = []
            clause = "(" + " OR ".join(["intervention_tokens LIKE ?"] * len(term.forms)) + ")"
            params.extend(f"% {f} %" for f in term.forms)
            if query_set:
                clause += f" AND {_QUERY_SET_CLAUSE}"
                params.append(query_set)
            n = self.conn.execute(
                f"SELECT COUNT(*) FROM trials WHERE {clause}", params).fetchone()[0]
            out.append({"typed": term.typed, "forms": list(term.forms),
                        "curated": term.curated, "n_trials": n})
        return out

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

        `biomarker_filters` is a list of (marker_key, status_or_statuses)
        narrowing the set. `status_or_statuses` is a single status string, e.g.
        [("MSS", "REQUIRED")], OR a list of statuses ORed together for that one
        marker, e.g. [("MSS", ["REQUIRED", "ELIGIBLE_BY_EXCLUSION"])] — a trial
        stating MSS directly OR excluding MSI-H counts either way. Different
        marker filters AND together. NOT_MENTIONED trials are only included if
        a caller explicitly asks for ("MARKER", "NOT_MENTIONED") — they are
        never folded into a REQUIRED/ELIGIBLE_BY_EXCLUSION/EXCLUDED count.
        """
        where, params = [], []
        if condition:
            where.append("LOWER(conditions) LIKE ?")
            params.append(f"%{condition.lower()}%")
        if query_set:
            where.append(_QUERY_SET_CLAUSE)
            params.append(query_set)
        if phase:
            where.append("LOWER(phase) LIKE ?")
            params.append(f"%{phase.lower()}%")
        if statuses:
            where.append(f"UPPER(overall_status) IN ({', '.join('?' * len(statuses))})")
            params.extend(s.upper() for s in statuses)
        # The population BEFORE any biomarker narrowing — condition/query_set/
        # phase/status only. The coverage statement's breakdown needs THIS
        # population (so "not mentioned" and "requires the opposite" trials are
        # counted, which the biomarker-filtered `total` below excludes by
        # construction), not the post-biomarker-filter one.
        base_where, base_params = list(where), list(params)
        for marker, status_or_statuses in (biomarker_filters or []):
            statuses_for_marker = (
                [status_or_statuses] if isinstance(status_or_statuses, str)
                else list(status_or_statuses)
            )
            # OR within one marker's allowed statuses (a trial can only ever
            # have ONE status per marker, so ANDing two statuses for the same
            # marker is never satisfiable and would silently zero the count).
            where.append("(" + " OR ".join(["biomarker_gating LIKE ?"] * len(statuses_for_marker)) + ")")
            params.extend(f"%{gating_token(marker, st)}%" for st in statuses_for_marker)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        base_clause = (" WHERE " + " AND ".join(base_where)) if base_where else ""

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
            for st in ("REQUIRED", "ELIGIBLE_BY_EXCLUSION", "EXCLUDED", "NOT_MENTIONED"):
                row[st] = scalar(" AND biomarker_gating LIKE ?", (f"%{gating_token(mkey, st)}%",))
            by_biomarker[mkey] = row

        # The coverage statement's biomarker breakdown, when this call filters
        # on exactly one marker (the common case — "MSS/pMMR trials
        # specifically"). Deliberately scored over `base_clause` (query_set +
        # status/phase, WITHOUT the biomarker filter itself), never `clause`:
        # `clause` already requires this marker to be REQUIRED-or-
        # ELIGIBLE_BY_EXCLUSION, so a NOT_MENTIONED or EXCLUDED count against it
        # would always be zero by construction — the coverage line needs the
        # population BEFORE that narrowing to report what it set aside.
        filtered_markers = [m for m, _ in (biomarker_filters or [])]
        coverage_biomarker_marker = filtered_markers[0] if len(filtered_markers) == 1 else None
        biomarker_coverage_obj = None
        if coverage_biomarker_marker:
            def base_scalar(extra_sql="", extra_params=()):
                return self.conn.execute(
                    f"SELECT COUNT(*) FROM trials{base_clause}{extra_sql}",
                    (*base_params, *extra_params),
                ).fetchone()[0]

            base_total = base_scalar()
            gating_counts = {
                st: base_scalar(" AND biomarker_gating LIKE ?",
                                (f"%{gating_token(coverage_biomarker_marker, st)}%",))
                for st in ("REQUIRED", "ELIGIBLE_BY_EXCLUSION", "EXCLUDED", "NOT_MENTIONED")
            }
            basis_counts = {
                b: base_scalar(" AND biomarker_basis LIKE ?",
                               (f"%{gating_basis_token(coverage_biomarker_marker, b)}%",))
                for b in ("EXPLICIT", "SYNONYM")
            }
            scope_bits = []
            if statuses:
                scope_bits.append(", ".join(statuses))
            if phase:
                scope_bits.append(phase)
            biomarker_coverage_obj = coverage.biomarker_coverage_from_counts(
                coverage_biomarker_marker, gating_counts, basis_counts,
                population_total=base_total, scope_note=" and ".join(scope_bits),
            )

        # The printed sample is picked by a deterministic, explainable relevance
        # score (ranking.py), not by "recruiting first, then completion date" —
        # a partner reading a memo needs a one-line reason a row outranks the
        # one below it, not a coincidence of two SQL tie-breakers. Scoring reads
        # only narrow columns over the FULL filtered set (no eligibility text or
        # other large TEXT blobs) so this stays cheap even when a section has no
        # other filter and the population is the whole query set.
        rank_cfg = ranking.load_ranking_config()
        today = date.today()
        score_sql = (
            f"SELECT nct_id, phase, overall_status, enrollment_count, allocation, "
            f"start_date, locations, found_by FROM trials{clause}"
        )
        rankings: dict[str, ranking.Ranking] = {}
        order_keys: dict[str, tuple] = {}
        for row in self.conn.execute(score_sql, params):
            shim = SimpleNamespace(
                phase=row["phase"], overall_status=row["overall_status"],
                enrollment_count=row["enrollment_count"], allocation=row["allocation"],
                start_date=row["start_date"], locations=json.loads(row["locations"] or "[]"),
            )
            found_by = json.loads(row["found_by"] or "[]")
            r = ranking.score_record(shim, found_by, rank_cfg, today=today)
            rankings[row["nct_id"]] = r
            # A tie on score falls back to NCT ID, not a second unscored
            # signal — anything that broke ties would need to appear in
            # explain() to keep every row's position accountable to something
            # printed, and a genuine tie means the scored signals ran out.
            order_keys[row["nct_id"]] = (-r.score, row["nct_id"])

        top_ids = sorted(order_keys, key=order_keys.get)[:sample_limit]

        if top_ids:
            rows_by_id = {
                r["nct_id"]: r for r in self.conn.execute(
                    f"SELECT * FROM trials WHERE nct_id IN ({', '.join('?' * len(top_ids))})",
                    top_ids,
                )
            }
            sample_rows = [rows_by_id[i] for i in top_ids if i in rows_by_id]
        else:
            sample_rows = []
        sample = [self._to_record(r) for r in sample_rows]
        sample_flags = [json.loads(r["biomarker_flags"] or "{}") for r in sample_rows]
        sample_rankings = [rankings[r["nct_id"]] for r in sample_rows]

        cov_row = self.coverage(query_set) if query_set else None
        qset_total = self.count(query_set=query_set) if query_set else len(self)

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
            "coverage": cov_row,
            "population_total": qset_total,
            # The full coverage statement — searched/not-searched/biomarker
            # breakdown — for THIS section's population. None only when no
            # query_set was given at all (a caller not going through the
            # query-set path this whole system is built around).
            "coverage_statement": (
                coverage.registry_coverage_statement(
                    cov_row, qset_total, biomarker=biomarker_coverage_obj,
                ) if query_set else None
            ),
            "filters": {
                "condition": condition or "",
                "query_set": query_set or "",
                "phase": phase or "",
                "statuses": list(statuses or []),
                "biomarker": list(biomarker_filters or []),
            },
            "sample": sample,
            "sample_flags": sample_flags,
            # Parallel to `sample`: why each printed row ranks where it does.
            # See ranking.py — deterministic, no model call, config-driven.
            "sample_rankings": sample_rankings,
        }

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "TrialStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def backfill_intervention_types(path: str | Path, fetch=None, chunk: int = 400,
                                progress=None, fetch_full=None) -> dict:
    """Fill `intervention_types` for records already held, from the registry.

    The third migration category, and the only one that needs the network. See
    `_REFETCHABLE_FROM`: this column cannot be recomputed from stored text, and
    a full re-ingest to recover one field would discard 241,298 records,
    every eligibility text and every verified coverage row to get it.

    Only `filter.ids` + the interventions module is requested, so the payload is
    a fraction of an ingest's. Measured on the live store: ~42 minutes for the
    whole file against roughly fourteen hours for a re-ingest.

    `fetch` is injected so this is testable against captured fixtures with no
    network, the same shape as `bulk.load_export`.

    `chunk` is 400 because `filter.ids` is a URL parameter and the registry
    returns HTTP 414 above roughly 500 IDs — measured: 400 succeeds (~5,000
    character URL, 259 records/second), 600 fails. Below 400 the per-request
    overhead dominates: 100 IDs per request runs at 78 records/second, which
    turns an 18-minute backfill into a 68-minute one for no benefit.

    Resumable by construction, and verified by killing a run: the work set is
    "column still NULL" and each chunk commits, so an interrupted run loses at
    most one chunk and a re-run picks up exactly what is left. That is separate
    from partial tolerance — the version stamp still goes on only when every
    record has been answered for, so an interrupted store stays refused.

    Two properties worth stating, because both are the difference between a
    backfill and a corruption:

      * An ID the registry does not return is left NULL and counted as
        `not_returned`. Writing `[]` for it would record "the registry says this
        trial has no typed interventions", which is a claim, not an absence.
      * The length check from parse time is re-applied here. A record whose name
        count and type count disagree is counted as `misaligned` and skipped
        rather than written, because a misaligned row states something false
        about which intervention is which.

    The schema version is stamped ONLY when every held record has been answered
    for. A partial backfill leaves the file at its old version, so the store goes
    on refusing rather than opening with a column that is full in places — which
    would put UNKNOWN back to meaning two things.
    """
    path = Path(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(trials)")}
        if "intervention_types" not in cols:
            conn.execute("ALTER TABLE trials ADD COLUMN intervention_types TEXT")
        # The ledger of what has been ASKED, which is not the same as what has
        # been answered. A filled column already records a success; this records
        # the attempts that did not produce one, so a re-run can say "these 12
        # have been asked four times and the registry has never returned them"
        # instead of retrying them forever in silence.
        conn.execute(BACKFILL_LEDGER)
        conn.commit()

        todo = [(r["nct_id"], json.loads(r["interventions"] or "[]"))
                for r in conn.execute(
                    "SELECT nct_id, interventions FROM trials "
                    "WHERE intervention_types IS NULL ORDER BY nct_id")]
        total = len(todo)
        already = conn.execute(
            "SELECT COUNT(*) FROM trials WHERE intervention_types IS NOT NULL").fetchone()[0]
        if fetch is None:
            from .client import fetch_intervention_types as fetch

        done = misaligned = not_returned = 0
        for i in range(0, total, chunk):
            batch = todo[i:i + chunk]
            answered = fetch([n for n, _ in batch])
            writes, ledger = [], []
            for nct, names in batch:
                types = answered.get(nct)
                if types is None:
                    not_returned += 1
                    ledger.append((nct, "NOT_RETURNED"))
                    continue
                if len(types) != len(names):
                    misaligned += 1
                    ledger.append((nct, "MISALIGNED"))
                    continue
                writes.append((json.dumps(types), nct))
            # Both writes committed together, per chunk. That is what makes an
            # interrupted run resumable rather than a 42-minute restart: the
            # resume set is "column still NULL", so work already committed is
            # never redone and nothing needs to be replayed.
            if writes:
                conn.executemany(
                    "UPDATE trials SET intervention_types = ? WHERE nct_id = ?", writes)
            if ledger:
                conn.executemany(
                    "INSERT INTO intervention_type_backfill (nct_id, attempts, outcome) "
                    "VALUES (?, 1, ?) ON CONFLICT(nct_id) DO UPDATE SET "
                    "attempts = attempts + 1, outcome = excluded.outcome", ledger)
            conn.commit()
            done += len(writes)
            if progress:
                progress(min(i + chunk, total), total, done, not_returned, misaligned)

        # Answered for EVERY held record, or the file keeps its old version and
        # goes on being refused. Resumable and partial-tolerant are different
        # things: an interrupted run must not have to start over, and must also
        # not leave a store that opens with a column full in places.
        #
        # REPAIR. A record whose stored name count disagrees with the registry's
        # type count has DRIFTED — the registry changed after the ingest, which
        # is the thing this store exists to track. Measured on the live store:
        # 21 of 241,298, of which 18 had gained interventions and 3 had lost
        # them. Writing new types against stale names would misalign them, so
        # the names are refreshed alongside the types.
        #
        # Names, types and the derived token column move TOGETHER or not at all —
        # `intervention_tokens` is a parse of the names, and leaving it behind
        # would make an agent query silently disagree with the record it is
        # querying. Everything else on a drifted record (status, dates) is as
        # stale as it was before this ran; that is a pre-existing condition, it
        # is reported as `drifted` so a re-ingest can close it, and it is not
        # something the backfill can honestly fix one column at a time.
        drifted = []
        if fetch_full is None:
            try:
                from .client import fetch_studies_by_id as fetch_full
            except Exception:
                fetch_full = None
        stale_ids = [r["nct_id"] for r in conn.execute(
            "SELECT nct_id FROM intervention_type_backfill WHERE outcome = 'MISALIGNED' "
            "AND nct_id IN (SELECT nct_id FROM trials WHERE intervention_types IS NULL)")]
        if stale_ids and fetch_full is not None:
            for i in range(0, len(stale_ids), chunk):
                for rec in fetch_full(stale_ids[i:i + chunk]) or []:
                    if len(rec.interventions) != len(rec.intervention_types):
                        continue          # cannot happen past _assert_aligned; belt and braces
                    conn.execute(
                        "UPDATE trials SET interventions = ?, intervention_types = ?, "
                        "intervention_tokens = ? WHERE nct_id = ?",
                        (json.dumps(rec.interventions), json.dumps(rec.intervention_types),
                         agents.token_blob(rec.interventions), rec.nct_id))
                    drifted.append(rec.nct_id)
            conn.commit()
            done += len(drifted)

        remaining = conn.execute(
            "SELECT COUNT(*) FROM trials WHERE intervention_types IS NULL").fetchone()[0]
        complete = remaining == 0
        if complete:
            conn.execute(f"PRAGMA user_version = {STORE_VERSION}")
            conn.commit()

        misaligned_ids = [r["nct_id"] for r in conn.execute(
            "SELECT nct_id FROM intervention_type_backfill WHERE outcome = 'MISALIGNED' "
            "AND nct_id IN (SELECT nct_id FROM trials WHERE intervention_types IS NULL)")]
        stuck = [dict(r) for r in conn.execute(
            "SELECT nct_id, attempts, outcome FROM intervention_type_backfill "
            "WHERE nct_id IN (SELECT nct_id FROM trials WHERE intervention_types IS NULL) "
            "ORDER BY attempts DESC, nct_id LIMIT 20")]
        return {"total": total, "filled": done, "not_returned": not_returned,
                "misaligned": misaligned, "remaining": remaining, "complete": complete,
                "version_stamped": complete, "already_filled_on_entry": already,
                "stuck": stuck, "misaligned_ids": misaligned_ids,
                "drifted": drifted}
    finally:
        conn.close()
