"""Automatic research loading, so the user never touches a terminal.

Loading used to be a shell script someone had to run before the app was useful.
That put the hardest step first, which is the wrong way round: an analyst who
has to ask a colleague to run step one will not use the tool at all.

This module makes loading a consequence of asking for a memo. It checks whether
anything relevant is already loaded, fetches only what is missing, and reports
progress through a callback so the UI can show real stages rather than a
spinner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .config import Config
from .ingest.store import load_corpus
from .pipeline import CORPUS_FILE, TRIALS_DB, build_index, ingest_pubmed
from .trials.client import search_trials
from .trials.store import TrialStore

Progress = Callable[[float, str], None]


@dataclass
class LoadReport:
    literature_added: int = 0
    trials_added: int = 0
    already_had_literature: bool = False
    already_had_trials: bool = False
    skipped: bool = False
    # True when read-only mode declined to fetch. Kept DISTINCT from `skipped`
    # (which means "already had it") because they are different answers to
    # "why is there no new data": one says the snapshot already covers this,
    # the other says nobody looked and nobody will. Collapsing them would let a
    # miss read as a hit — the not-assessed-vs-nothing-found rule, at the
    # loader.
    read_only: bool = False
    errors: list[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []

    def summary(self) -> str:
        if self.read_only:
            return ("Answering from the stored snapshot — this deployment does not "
                    "fetch. Anything not already stored is absent from this answer, "
                    "which is NOT a finding that it does not exist.")
        if self.skipped:
            return "Research for this asset was already loaded."
        bits = []
        if self.literature_added:
            bits.append(f"{self.literature_added} papers")
        if self.trials_added:
            bits.append(f"{self.trials_added} trial records")
        return f"Loaded {' and '.join(bits)}." if bits else "Nothing new was found to load."


def _terms(asset: str, indication: str) -> list[str]:
    return [t.lower() for t in (asset, indication) if t and t.strip()]


def has_data_for(cfg: Config, asset: str, indication: str, passphrase: str | None = None) -> bool:
    """Is there already loaded research mentioning this asset or indication?

    Deliberately a low bar. The cost of a false negative is a few minutes of
    re-fetching; the cost of a false positive is a memo built on a corpus about
    something else, which looks complete and is worthless.
    """
    terms = _terms(asset, indication)
    if not terms:
        return False

    trials_path = cfg.raw_dir / TRIALS_DB
    if trials_path.exists():
        try:
            with TrialStore(trials_path, read_only=True) as store:
                for term in terms:
                    if store.query(intervention=term, limit=1) or store.search(term, limit=1):
                        return True
        except Exception:
            pass

    corpus_path = cfg.raw_dir / CORPUS_FILE
    if corpus_path.exists():
        try:
            docs = load_corpus(corpus_path, passphrase=passphrase)
            for doc in docs:
                haystack = f"{doc.title} {doc.text[:600]}".lower()
                if any(term in haystack for term in terms):
                    return True
        except Exception:
            pass

    return False


def ensure_data(
    cfg: Config,
    asset: str,
    indication: str,
    progress: Progress | None = None,
    max_papers: int = 100,
    max_trials: int = 200,
    force: bool = False,
) -> LoadReport:
    """Fetch and index whatever is missing for this asset.

    Failures in one source do not abort the other: a memo built on trials alone
    is worth more than no memo, provided the gap is reported rather than hidden.
    """
    def step(fraction: float, message: str) -> None:
        if progress:
            progress(fraction, message)

    report = LoadReport()

    # The first thing checked, ahead of `force`, and before any store is even
    # probed. A read-only deployment serves what it has; a miss returns "not in
    # this snapshot" and never a network call, so a stranger's search cannot
    # make the server fetch from PubMed or the registry on their behalf. Note
    # this deliberately outranks `force=True` — a caller asking to refresh is
    # asking for something this deployment does not do.
    if cfg.read_only:
        report.read_only = True
        report.skipped = False
        step(1.0, report.summary())
        return report

    if not force and has_data_for(cfg, asset, indication):
        report.skipped = True
        step(1.0, "Research already loaded — going straight to the memo.")
        return report

    query = " ".join(t for t in (asset, indication) if t and t.strip())

    step(0.05, f"Searching published literature for “{query}”…")
    try:
        docs = ingest_pubmed(query, retmax=max_papers, cfg=cfg)
        report.literature_added = len(docs)
    except Exception as exc:
        report.errors.append(f"Could not load published literature: {_friendly(exc)}")

    step(0.35, "Searching the clinical trial registry…")
    try:
        records = search_trials(
            condition=indication or None,
            intervention=asset or None,
            max_records=max_trials,
            offline=cfg.offline,
        )
        # Fall back to a broad term search: an exact intervention match often
        # misses trials that name the compound differently.
        if not records and query:
            records = search_trials(term=query, max_records=max_trials, offline=cfg.offline)
        if records:
            with TrialStore(cfg.raw_dir / TRIALS_DB) as store:
                store.upsert(records)
            report.trials_added = len(records)
    except Exception as exc:
        report.errors.append(f"Could not load trial records: {_friendly(exc)}")

    if report.literature_added:
        step(0.65, "Indexing the literature for search…")
        try:
            build_index(cfg)
        except Exception as exc:
            report.errors.append(f"Could not index the literature: {_friendly(exc)}")

    step(1.0, report.summary())
    return report


def _friendly(exc: Exception) -> str:
    """Translate the failures a non-technical user will actually hit."""
    name = type(exc).__name__
    text = str(exc)
    if "Proxy" in name or "proxy" in text.lower():
        return (
            "the network is blocking access to this source. Company and campus "
            "networks often do — try a different network or a phone hotspot."
        )
    if "Connection" in name or "Timeout" in name or "timed out" in text.lower():
        return "could not reach the source. Check the internet connection and try again."
    if "429" in text or "rate limit" in text.lower():
        return "the source is rate-limiting requests. Wait a minute and try again."
    return f"{name}: {text[:120]}"
