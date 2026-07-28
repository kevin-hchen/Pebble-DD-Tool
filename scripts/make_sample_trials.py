"""Seed the trial store from the API v2 fixtures so the structured path is
runnable with no network access.

These are the same fixtures the tests use: a completed Phase 3, a TERMINATED
trial with whyStopped, a WITHDRAWN trial without one, a Phase 2/3, and a sparse
record. Real ingestion is:

    medrag trials --condition "heart failure" --intervention empagliflozin
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fixtures.ctgov import PAGE_ONE, PAGE_TWO  # noqa: E402

from medrag.config import load_config  # noqa: E402
from medrag.trials.client import parse_study  # noqa: E402
from medrag.trials.store import TrialStore  # noqa: E402

TRIALS_DB = "trials.db"


def main() -> None:
    cfg = load_config()
    cfg.ensure_dirs()

    records = []
    for page in (PAGE_ONE, PAGE_TWO):
        for study in page["studies"]:
            rec = parse_study(study)
            if rec:
                records.append(rec)

    with TrialStore(cfg.raw_dir / TRIALS_DB) as store:
        n = store.upsert(records)
        stats = store.stats()

    print(f"seeded {n} trial records into {cfg.raw_dir / TRIALS_DB}")
    print(f"  stopped early: {stats['stopped']}")
    print(f"  with a stated reason: {stats['stopped_with_reason']} "
          f"(fill rate {stats['why_stopped_fill_rate']})")


if __name__ == "__main__":
    main()
