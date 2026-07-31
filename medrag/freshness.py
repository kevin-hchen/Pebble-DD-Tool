"""When was each store last refreshed, and is that a problem yet.

A registry snapshot ageing silently is this system's characteristic failure. The
memo looks identical whether the trial store was refreshed this morning or in
March: same layout, same confident citations, same counts — except the counts are
answers to a question about a world that has moved. Nothing in the product said
so, which made "how old is this?" a question only someone who thought to check
the file modification time could answer.

So freshness is computed in one place and reported everywhere the stores are:
`medrag stats` and the Streamlit settings panel.

Two design notes.

**Read-only, and tolerant of a stale schema.** These queries open the SQLite
files with `mode=ro` rather than through `TrialStore`/`FDAStore`, because those
constructors run `executescript(SCHEMA)` and write `PRAGMA user_version` on open
— a status display must not mutate what it is reporting on. Reading MAX(ingested_at)
directly also means a database too old for its own store class still reports its
age, which is exactly when you most want to know.

**Thresholds differ by source, because the sources move at different speeds.**
The trial registry changes daily; published literature does not. A single
"30 days" for everything would either nag about the corpus or stay quiet about
the registry.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# How long before a store is worth a caution. Not a hard expiry — nothing stops
# working — but the point at which a memo should be read with the date in mind.
STALE_AFTER = {
    "Clinical trials": timedelta(days=30),
    "FDA devices": timedelta(days=90),
    "Literature corpus": timedelta(days=90),
    "Search index": timedelta(days=90),
}


@dataclass
class Freshness:
    """One store's age, and whether that age is worth mentioning."""

    label: str
    last_refreshed: datetime | None = None
    present: bool = True
    detail: str = ""

    @property
    def age(self) -> timedelta | None:
        if self.last_refreshed is None:
            return None
        return datetime.now(timezone.utc) - self.last_refreshed

    @property
    def age_days(self) -> int | None:
        age = self.age
        return None if age is None else max(0, age.days)

    @property
    def stale(self) -> bool:
        age = self.age
        if age is None:
            return False
        return age > STALE_AFTER.get(self.label, timedelta(days=30))

    def describe(self) -> str:
        """Plain language, for someone who will not open a terminal."""
        if not self.present:
            return "not loaded"
        if self.last_refreshed is None:
            return "loaded, but the date was not recorded"
        days = self.age_days
        when = self.last_refreshed.strftime("%d %b %Y")
        if days == 0:
            return f"today ({when})"
        if days == 1:
            return f"yesterday ({when})"
        return f"{days} days ago ({when})"

    def caution(self) -> str | None:
        """The line a user should see when this store is old enough to matter."""
        if not self.stale:
            return None
        limit = STALE_AFTER.get(self.label, timedelta(days=30)).days
        return (
            f"{self.label} was last refreshed {self.age_days} days ago, past the "
            f"{limit}-day mark. Findings will reflect that snapshot, not today. "
            "Re-run the ingest for this source before relying on counts."
        )


def _utc(stamp: str | None) -> datetime | None:
    """SQLite CURRENT_TIMESTAMP is UTC and unmarked; make that explicit."""
    if not stamp:
        return None
    text = str(stamp).strip().replace("T", " ").split(".")[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _max_ingested(path: Path, tables: tuple[str, ...]) -> datetime | None:
    """MAX(ingested_at) across tables, read-only and never raising."""
    if not path.exists():
        return None
    newest = None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        for table in tables:
            try:
                row = conn.execute(f"SELECT MAX(ingested_at) FROM {table}").fetchone()
            except sqlite3.Error:
                continue  # table absent on an older schema; the others still count
            stamp = _utc(row[0] if row else None)
            if stamp and (newest is None or stamp > newest):
                newest = stamp
    finally:
        conn.close()
    return newest


def _mtime(path: Path) -> datetime | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def store_freshness(cfg) -> list[Freshness]:
    """Age of every store, in the order a reader should scan them."""
    from .pipeline import CORPUS_FILE, FDA_DB, TRIALS_DB

    raw, index = Path(cfg.raw_dir), Path(cfg.index_dir)

    trials_path = raw / TRIALS_DB
    fda_path = raw / FDA_DB
    corpus_path = raw / CORPUS_FILE
    manifest = index / "manifest.json"

    out = [
        Freshness(
            "Clinical trials",
            _max_ingested(trials_path, ("trials",)),
            trials_path.exists(),
        ),
        Freshness(
            "FDA devices",
            _max_ingested(fda_path, ("clearances", "recalls", "events")),
            fda_path.exists(),
        ),
        Freshness("Literature corpus", _mtime(corpus_path), corpus_path.exists()),
        Freshness("Search index", _mtime(manifest), manifest.exists()),
    ]

    # An index older than the corpus is a different and more actionable problem
    # than an index that is merely old: documents have been ingested that no
    # search can reach. Worth saying plainly, because the symptom is a memo that
    # is thin for no visible reason.
    corpus_at, index_at = out[2].last_refreshed, out[3].last_refreshed
    if corpus_at and index_at and corpus_at > index_at:
        out[3].detail = (
            "The corpus has been updated since the index was built, so recently "
            "ingested documents are not searchable yet. Run `python -m medrag index`."
        )
    return out


def cautions(cfg) -> list[str]:
    """Every freshness line worth showing a user, or an empty list."""
    notes = []
    for f in store_freshness(cfg):
        note = f.caution()
        if note:
            notes.append(note)
        if f.detail:
            notes.append(f"{f.label}: {f.detail}")
    return notes
