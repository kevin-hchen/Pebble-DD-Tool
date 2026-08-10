"""The public service's only route to stored data — read-only, no fetch.

Two properties, both structural rather than remembered:

  * **Every store opens read-only.** `dbopen.connect_read_only` with
    `immutable=1`, so the connection cannot write and needs no lock file, `-wal`
    or `-shm` — which is what lets the service run against a 444 database on a
    read-only mount. A write attempted anywhere raises `ReadOnlyStoreError` by
    name.

  * **A miss is a miss.** There is no fetch-on-miss path in this module and no
    import of anything that fetches. `medrag.autoload`, `trials.queries` and
    `trials.client` are deliberately absent, so a stranger's search cannot make
    this server pull from ClinicalTrials.gov — not because a flag says no, but
    because the code that would do it is not reachable from here.
    `tests/test_public_app.py` asserts that absence by inspecting this module's
    imports, so adding one fails the suite.

`MEDRAG_READ_ONLY=1` is set on the config this module builds rather than left to
the environment. A deployment that forgot the variable would otherwise get a
service that writes; setting it here means the guarantee comes from the code
path, and the environment variable is a belt on top of braces.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from medrag.config import Config
from medrag.coverage import render_lines
from medrag.dbopen import MissingDatabaseError
from medrag.freshness import _mtime  # snapshot date, read-only, never raises
from medrag.landscape import build_landscape
from medrag.storenames import TRIALS_DB
from medrag.trials.store import TrialStore, TrialStoreSchemaError


class SnapshotUnavailable(RuntimeError):
    """No snapshot is present. Rendered as "this tool has not looked", never as
    "no trials exist" — the not-assessed-vs-nothing-found rule, at the service
    boundary."""


@dataclass(frozen=True)
class LandscapeResult:
    landscape: object
    coverage_lines: list[str]
    sample_lines: list[str]
    snapshot_date: str
    shown: int
    total_screened: int
    capped_at: int


def public_config(data_dir: Path) -> Config:
    cfg = Config(data_dir=Path(data_dir))
    # Set here, not inherited from the environment: the guarantee should come
    # from the code path that serves strangers, not from a deployer remembering
    # a variable. read_only implies offline in medrag.config, which drops any
    # API key that happens to be present.
    cfg.read_only = True
    cfg.offline = True
    cfg.openai_api_key = None
    return cfg


def trials_path(data_dir: Path) -> Path:
    """Locate the trial store under either supported layout.

    A built artifact holds the databases at its root (`<artifact>/trials.db`) —
    a deployment artefact should not carry a `raw/` level that means nothing to
    a deployer. A development checkout holds them under `data/raw/`, the layout
    the CLI writes.

    Both are checked, artifact layout first, and the ROOT one wins when both
    exist: a verified artifact must never be shadowed by a stray `raw/` copy
    somebody left in the same directory. Returns the artifact-layout path when
    neither exists, so the error names the file a deployer expects.
    """
    root = Path(data_dir)
    flat, nested = root / TRIALS_DB, root / "raw" / TRIALS_DB
    if flat.exists():
        return flat
    if nested.exists():
        return nested
    return flat


def snapshot_date(data_dir: Path) -> str:
    stamp = _mtime(trials_path(data_dir))
    return stamp.strftime("%d %B %Y") if stamp else "unknown"


def open_trials(data_dir: Path) -> TrialStore:
    """Open the trial store read-only and immutable.

    `immutable=True` is correct HERE specifically: a public deployment serves a
    frozen artefact that no process on the box is writing. It would be wrong on
    a machine where an ingest may run — see `dbopen` for the measurement — which
    is exactly why it is passed explicitly at this one call site rather than
    made the default.
    """
    try:
        return TrialStore(trials_path(data_dir), read_only=True, immutable=True)
    except MissingDatabaseError as exc:
        raise SnapshotUnavailable(
            "No trial snapshot is installed on this server, so nothing has been "
            "searched. This is not a finding that no trials exist."
        ) from exc


def run_landscape(data_dir: Path, condition: str, biomarker: str,
                  location: str = "", max_results: int = 30) -> LandscapeResult:
    """The landscape, from the snapshot, with its coverage statement.

    No consent gate: nothing is submitted. No model. No network. If the query
    set was never ingested, the coverage statement says so and the result is
    empty — a miss, reported as a miss.
    """
    store = open_trials(data_dir)
    try:
        landscape = build_landscape(
            store, condition=condition, biomarker=biomarker,
            location=location, show_limit=max_results,
        )
    except TrialStoreSchemaError as exc:
        raise SnapshotUnavailable(
            "The installed snapshot was built by an older version of this tool and "
            "cannot be read safely."
        ) from exc
    finally:
        store.close()

    cov = getattr(landscape, "coverage_statement", None)
    return LandscapeResult(
        landscape=landscape,
        coverage_lines=render_lines(cov) if cov is not None else [],
        sample_lines=list(landscape.sample_lines()),
        snapshot_date=snapshot_date(data_dir),
        shown=len(landscape.trials),
        total_screened=getattr(landscape, "population_total", 0)
        or getattr(landscape, "n_condition", 0),
        capped_at=max_results,
    )
