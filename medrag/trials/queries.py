"""Query sets: how a condition is turned into registry queries, and what we can
prove about the coverage that results.

One condition string is one view of the field. The registry lets a sponsor write
its indication however it likes, so a single `query.cond` is a sample of the
population, not the population — and the previous ingest treated it as the
latter, then narrowed it again locally with a substring match that used entirely
different logic from the fetch.

Three things this module fixes, each measurable rather than hoped for:

  * **Union, not one string.** Every query in a set runs and the results union by
    NCT ID. The set lives in `config/trial_queries.yaml` so a clinician can edit
    it without touching code, and is never model-generated at query time.

  * **Provenance.** Every trial records which queries found it. "Did we search
    for colon cancer?" is then a question the database answers.

  * **Marginal yield.** Each query reports how many trials it added that no
    earlier query had. When a newly added synonym adds zero, the set is
    near-complete — a measurement, not a hope.

THE BASKET-TRIAL LIMIT, STATED PLAINLY

`query.cond` is MeSH-expanded server-side, so it already spans "Colorectal
Neoplasms" and "Colon Cancer". What it cannot do is reach a trial that never
registers a colorectal condition at all. NCT05405595 (ADG126-P001) registers
"Advanced/Metastatic Solid Tumors" and mentions "MSS CRC" only in its detailed
description. It is absent from all six colorectal condition queries — no synonym
list will ever find it, because the gap is structural, not lexical.

`query.term` searches full text and does reach it (rank 166 of 391 for "MSS
colorectal"). That is why sets carry a `terms` list. But term queries are keyword
matches over free text, so they trade precision for reach, and they only find a
basket trial that happens to name the indication somewhere in its text. A basket
trial that says only "advanced solid tumours" is unreachable by any query short
of ingesting the entire solid-tumour registry (9,699 studies for
`query.cond="solid tumor"`, to gain one relevant trial).

So basket-trial coverage is PARTIAL and must be reported as such:
`CoverageReport.basket_caveat` is carried into the store and the memo rather than
left to be inferred from an absence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .client import QueryResult, run_query

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "trial_queries.yaml"

CONDITION = "cond"
TERM = "term"

# Said wherever a trial count for an indication is shown. The number is a floor,
# not a total, and the reason is specific enough to act on.
BASKET_CAVEAT = (
    "Basket and solid-tumour trials that never register this indication as a "
    "condition are only partly reachable. Full-text queries find those that name "
    "the indication somewhere in their text; a trial registered solely as "
    "'advanced solid tumours' is not reachable by any condition or synonym query "
    "and may be missing from this count."
)


@dataclass(frozen=True)
class TrialQuery:
    kind: str      # CONDITION | TERM
    value: str

    @property
    def label(self) -> str:
        """Stored verbatim as the provenance token, so an audit reads the axis as
        well as the string — 'cond:colon cancer' vs 'term:MSS colorectal'."""
        return f"{self.kind}:{self.value}"

    def kwargs(self) -> dict:
        return {"condition": self.value} if self.kind == CONDITION else {"term": self.value}


@dataclass(frozen=True)
class QuerySet:
    key: str
    label: str
    queries: tuple[TrialQuery, ...]
    curated: bool = True      # False for an ad-hoc set built from a bare string

    @property
    def condition_queries(self) -> tuple[TrialQuery, ...]:
        return tuple(q for q in self.queries if q.kind == CONDITION)


@dataclass
class QueryYield:
    """What one query contributed. `new` is the marginal yield — the trials no
    earlier query in the set had already found."""
    query: TrialQuery
    reported_total: int | None = None
    fetched: int = 0
    new: int = 0
    error: str = ""
    # How many times this query's requests had to be repeated, and how long was
    # spent waiting. A query that succeeded on the ninth attempt is a different
    # observation from one that succeeded first time, and only this number tells
    # them apart afterwards.
    retries: int = 0
    retry_seconds: float = 0.0


@dataclass
class CoverageReport:
    """The audit trail for one ingest: what was asked, what came back, and what
    is known to be missing. Carried into the store so a memo can state coverage
    instead of implying it."""
    set_key: str
    set_label: str
    curated: bool = True
    yields: list[QueryYield] = field(default_factory=list)
    total_unique: int = 0
    basket_caveat: str = BASKET_CAVEAT
    errors: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """Every query in the set ran and returned its full reported total.

        NOTE: this is the WEAK check — it sees errors only. Whether a query
        actually reached its `reported_total` is decided by
        `store.verify_ingest`, which is what grades a family COMPLETE. Do not
        use this property to decide whether an ingest finished; a capped or
        short fetch passes it.
        """
        return not self.errors and all(not y.error for y in self.yields)

    @property
    def total_retries(self) -> int:
        return sum(y.retries for y in self.yields)

    @property
    def total_retry_seconds(self) -> float:
        return sum(y.retry_seconds for y in self.yields)

    def retry_line(self) -> str:
        """What retrying cost this ingest, or "" when it cost nothing.

        Printed on every ingest that retried at all, including one that
        ultimately succeeded — a source that needs forty retries to answer is
        degrading whether or not this run got its data, and a wall-clock number
        with no explanation hides exactly that.
        """
        if not self.total_retries:
            return ""
        noisy = [(y.query.label, y.retries) for y in self.yields if y.retries]
        noisy.sort(key=lambda t: -t[1])
        worst = ", ".join(f"{lab} x{n}" for lab, n in noisy[:3])
        more = f" and {len(noisy) - 3} other quer{'y' if len(noisy) == 4 else 'ies'}" \
            if len(noisy) > 3 else ""
        return (f"retried {self.total_retries} time(s) across {len(noisy)} quer"
                f"{'y' if len(noisy) == 1 else 'ies'}, {self.total_retry_seconds:.0f}s "
                f"spent waiting — {worst}{more}")

    def marginal_yield_table(self) -> list[tuple[str, int, int, int | None]]:
        return [(y.query.label, y.fetched, y.new, y.reported_total) for y in self.yields]

    def summary(self) -> str:
        n_zero = sum(1 for y in self.yields if y.new == 0 and not y.error)
        line = (f"{self.total_unique} unique trials from {len(self.yields)} queries "
                f"in set '{self.set_key}'")
        if n_zero:
            line += f"; {n_zero} query/queries added nothing new (set is near-complete)"
        return line


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-") or "adhoc"


def load_query_sets(path: str | Path | None = None) -> dict[str, QuerySet]:
    """Read the clinician-editable query config. A missing or malformed file is
    not fatal — an ad-hoc single-query set still works — but it is reported, since
    silently ingesting one query while believing six ran is the failure mode this
    whole module exists to prevent."""
    p = Path(path or DEFAULT_CONFIG)
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    sets: dict[str, QuerySet] = {}
    for key, spec in (data.get("sets") or {}).items():
        spec = spec or {}
        queries = tuple(
            [TrialQuery(CONDITION, c) for c in (spec.get("conditions") or []) if str(c).strip()]
            + [TrialQuery(TERM, t) for t in (spec.get("terms") or []) if str(t).strip()]
        )
        if queries:
            sets[key] = QuerySet(key=key, label=spec.get("label") or key, queries=queries)
    return sets


def resolve_query_set(condition: str, sets: dict[str, QuerySet] | None = None) -> QuerySet:
    """Map what the user typed to a curated set, or build a one-query ad-hoc set.

    An ad-hoc set is marked `curated=False` so the caller can say so out loud: it
    is a single condition string with none of the synonym coverage the curated
    sets have, and reporting it as equivalent would overstate what was searched.
    """
    sets = load_query_sets() if sets is None else sets
    norm = re.sub(r"\s+", " ", (condition or "").strip().lower())
    if not norm:
        raise ValueError("a condition is required to resolve a query set")

    for key, qset in sets.items():
        if norm == key.lower() or norm == qset.label.lower():
            return qset
        if any(norm == q.value.lower() for q in qset.queries):
            return qset
    # A near miss on a curated condition string ("metastatic colorectal cancer"
    # against "colorectal cancer") should use the curated set rather than quietly
    # dropping to a single query.
    for _key, qset in sets.items():
        if any(q.kind == CONDITION and (norm in q.value.lower() or q.value.lower() in norm)
               for q in qset.queries):
            return qset

    return QuerySet(key=_slug(condition), label=condition,
                    queries=(TrialQuery(CONDITION, condition),), curated=False)


def fetch_query_set(
    qset: QuerySet,
    status: list[str] | None = None,
    max_records: int | None = None,
    offline: bool = False,
    progress=None,
) -> tuple[list, dict[str, list[str]], CoverageReport]:
    """Run every query in the set, union by NCT ID, and record who found what.

    Returns (records, provenance, coverage) where provenance maps NCT ID to the
    query labels that found it. A trial found by three queries keeps all three:
    that is the audit trail, and collapsing it to the first would lose the very
    thing it exists to show.

    One query failing does not discard the other five — but it does mark the
    coverage incomplete, so the shortfall is reported rather than absorbed.
    """
    report = CoverageReport(set_key=qset.key, set_label=qset.label, curated=qset.curated)
    by_id: dict[str, object] = {}
    provenance: dict[str, list[str]] = {}

    for i, query in enumerate(qset.queries):
        if progress:
            progress(i / max(len(qset.queries), 1),
                     f"Searching the registry for “{query.value}”…")
        y = QueryYield(query=query)
        try:
            result: QueryResult = run_query(
                status=status, max_records=max_records, offline=offline, **query.kwargs()
            )
        except Exception as exc:
            y.error = f"{type(exc).__name__}: {exc}"
            # A query that failed AFTER exhausting its retries still says so:
            # the retry count belongs to the attempt, not to the success.
            budget = getattr(exc, "retry_budget", None)
            if budget is not None:
                y.retries, y.retry_seconds = budget.retries, budget.slept
            report.errors.append(f"{query.label} failed — {y.error}")
            report.yields.append(y)
            continue

        y.reported_total = result.reported_total
        y.fetched = len(result.records)
        y.retries, y.retry_seconds = result.retries.retries, result.retries.slept
        for rec in result.records:
            if rec.nct_id not in by_id:
                by_id[rec.nct_id] = rec
                y.new += 1
            provenance.setdefault(rec.nct_id, [])
            if query.label not in provenance[rec.nct_id]:
                provenance[rec.nct_id].append(query.label)
        report.yields.append(y)

    report.total_unique = len(by_id)
    return list(by_id.values()), provenance, report
