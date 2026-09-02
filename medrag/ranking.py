"""Deterministic, explainable ranking for the trials shown in an aggregate
landscape section's sample.

The problem this replaces: `store.landscape()`'s sample used to be sorted
recruiting-first then by completion date, and nothing more — a coarse tie
between "is it still open" and "when does it finish", indifferent to phase,
size, design quality, or how confidently a trial belongs in a filtered
section at all. Correct totals, arbitrary rows: a partner reading a memo
should never have to ask "why is this specific trial in front of me and not
that one" without a one-line answer sitting right there.

Two rules this module exists to keep:

  1. No model call, ever. Every score is a sum of small, named, YAML-defined
     point values read straight off structured columns already in the store.
     `Ranking.explain()` lists exactly which ones fired and for how many
     points — a partner can read one line and know precisely why a row
     outranks the one below it, and argue with a specific number if they
     disagree.

  2. The weights live in `config/ranking.yaml`, not here. This module only
     knows HOW to apply a tier/bin table to a value; it does not know what a
     Phase 3 trial is worth relative to a randomised one. That is a clinical
     and commercial judgment for whoever reviews the config, the same
     division of labour as `config/trial_queries.yaml` and
     `config/markers.yaml`.

Scope: BOTH capped trial tables this tool prints — the aggregate
diligence-memo section (`store.landscape()`) and the patient-facing trial
landscape (`landscape.py`'s `build_landscape`). The patient page used to sort
on biomarker state first and rank not at all, which meant every trial that
named the marker explicitly outranked every trial that named it by excluding
the opposite, no matter how much larger or later-phase the second one was —
the grouping cancelled the ranking. State is a labelled column now; ordering
is this score, on both surfaces.

The one signal that fires on only one of them is `proximity`: a patient types
a location and a diligence section does not, so `score_record` evaluates it
only when a caller passes `proximity_tier`. Every other signal is scored
identically on both, which is the point — two capped tables drawn from the
same store should not disagree about which trials matter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "ranking.yaml"


@dataclass
class ScoredSignal:
    label: str
    points: int


@dataclass
class Ranking:
    score: int
    signals: list[ScoredSignal] = field(default_factory=list)

    def explain(self) -> str:
        """One line: every signal that actually scored, in the order it was
        evaluated, plus the total. A row with nothing on file for any signal
        says so rather than printing an unexplained zero."""
        fired = [s for s in self.signals if s.points]
        if not fired:
            return f"no ranking signal on file — score {self.score}"
        return " · ".join(f"{s.label} (+{s.points})" for s in fired) + f" — score {self.score}"


def load_ranking_config(path: str | Path | None = None) -> dict:
    """Read the clinician/analyst-editable weight table. A missing or
    malformed file yields an empty config — every signal then scores 0 and
    ranking degrades to a flat tie, not a crash."""
    p = Path(path or DEFAULT_CONFIG)
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _tier(value: str, spec: dict) -> ScoredSignal:
    v = (value or "").strip().upper()
    for tier in spec.get("tiers", []):
        if v in {str(m).upper() for m in tier.get("match", [])}:
            return ScoredSignal(tier["label"], tier["points"])
    d = spec.get("default", {"points": 0, "label": ""})
    return ScoredSignal(d["label"], d["points"])


def _bin(count: int | None, spec: dict) -> ScoredSignal:
    d = spec.get("default", {"points": 0, "label": ""})
    if count is None:
        return ScoredSignal(d["label"], d["points"])
    # Bins are matched largest-`min`-first, so list them descending in YAML;
    # sorting here defensively means a misordered file still behaves.
    for b in sorted(spec.get("bins", []), key=lambda b: -b["min"]):
        if count >= b["min"]:
            label = b["label"].format(count=count) if "{count}" in b["label"] else b["label"]
            return ScoredSignal(label, b["points"])
    return ScoredSignal(d["label"], d["points"])


def _recency(start_date: str, spec: dict, today: date) -> ScoredSignal:
    d = spec.get("default", {"points": 0, "label": ""})
    if not start_date:
        return ScoredSignal(d["label"], d["points"])
    try:
        y, m, day = (int(x) for x in (start_date.split("-") + ["1", "1"])[:3])
        started = date(y, m, day if day else 1)
    except (ValueError, TypeError):
        return ScoredSignal(d["label"], d["points"])
    age_days = (today - started).days
    if age_days < 0:
        age_days = 0  # a future-dated start (NOT_YET_RECRUITING) is not "old"
    for b in sorted(spec.get("bins", []), key=lambda b: b["within_days"]):
        if age_days <= b["within_days"]:
            return ScoredSignal(b["label"], b["points"])
    return ScoredSignal(d["label"], d["points"])


def _sites(locations) -> int:
    return len(locations or [])


def _proximity(tier: int, spec: dict) -> ScoredSignal:
    """Score `LandscapeTrial.proximity_tier` (3 city / 2 state / 1 country /
    0 no match). Tier 0 scores nothing and prints nothing, exactly like an
    unmatched provenance bonus."""
    for t in spec.get("tiers", []):
        if int(t.get("tier", -1)) == tier:
            return ScoredSignal(t["label"], t["points"])
    d = spec.get("default", {"points": 0, "label": ""})
    return ScoredSignal(d["label"], d["points"])


def _provenance(found_by, spec: dict) -> ScoredSignal:
    if any(str(label).startswith("term:") for label in (found_by or [])):
        return ScoredSignal(spec.get("term_query_label", "topic-specific search match"),
                            spec.get("term_query_bonus", 0))
    return ScoredSignal("", 0)


def score_record(record, found_by: list[str] | None, cfg: dict | None = None,
                 today: date | None = None, proximity_tier: int | None = None) -> Ranking:
    """Score one TrialRecord against the weight table. `found_by` is passed
    separately rather than read off the record because it is a store-computed
    column (fetch provenance), not part of TrialRecord itself — same split
    `TrialStore._NON_RECORD_COLS` already draws.

    `proximity_tier` is the patient landscape's distance match (see
    `_proximity`). Left as None — every caller that has no patient location,
    which is every diligence section — the signal is not evaluated at all
    rather than scored as a zero, so it cannot quietly appear in an
    explain() line on a surface where distance means nothing.
    """
    cfg = load_ranking_config() if cfg is None else cfg
    today = today or date.today()
    signals_cfg = cfg.get("signals", {})
    signals = []
    if "phase" in signals_cfg:
        signals.append(_tier(record.phase, signals_cfg["phase"]))
    if "status" in signals_cfg:
        signals.append(_tier(record.overall_status, signals_cfg["status"]))
    if "enrollment" in signals_cfg:
        signals.append(_bin(record.enrollment_count, signals_cfg["enrollment"]))
    if "allocation" in signals_cfg:
        signals.append(_tier(getattr(record, "allocation", ""), signals_cfg["allocation"]))
    if "sites" in signals_cfg:
        signals.append(_bin(_sites(record.locations), signals_cfg["sites"]))
    if "provenance" in signals_cfg:
        signals.append(_provenance(found_by, signals_cfg["provenance"]))
    if "recency" in signals_cfg:
        signals.append(_recency(record.start_date, signals_cfg["recency"], today))
    if proximity_tier is not None and "proximity" in signals_cfg:
        signals.append(_proximity(proximity_tier, signals_cfg["proximity"]))

    return Ranking(score=sum(s.points for s in signals), signals=signals)
