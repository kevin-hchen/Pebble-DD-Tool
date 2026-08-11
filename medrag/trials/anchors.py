"""What makes a trial query a query ABOUT something.

The literature half of this tool has a relevance floor: a passage below it is
not evidence. The registry half had no equivalent, and could not have one —
SQL and FTS return rows, not similarities, so no threshold can be applied to
them. That asymmetry was the larger half of a real defect. Asked to diligence
PBX-7749 in hidradenitis suppurativa — an asset with no trial and no
publication anywhere — the memo returned eight colorectal-cancer trials as
evidence, each with a real NCT ID, in a store that has never been asked about
hidradenitis at all.

Three separate paths produced that, and all three are closed here.

**A structured query with nothing to filter on returns the whole store.**
`store.query()` builds a WHERE clause from whatever it is given; given nothing
it is `SELECT * FROM trials ... LIMIT k`, which is a perfectly good answer to a
question nobody asked. Two ways in: an indication that resolves to a query set
the store has never ingested (the clause is dropped or matches nothing), and an
asset that `agents.parse_asset` cannot parse into a single agent term, where
`_intervention_clause` returns `1=1` by design so an uncurated name still
matches itself.

**An un-ingested query set is not an empty one.** `resolve_query_set` always
returns a set — an ad-hoc one, `curated=False`, for a string no curated family
claims. Passing its key to `store.query` yields zero rows, which is
indistinguishable from a family that was searched and found nothing. It is the
`ValidationReport.assessed` rule arriving at the registry: "this store has never
been asked about hidradenitis" and "there are no hidradenitis trials" are
different statements, and only the first is true. `query_coverage` already
records which families were actually fetched, so this is a lookup, not a guess.

**The free-text fallback searched the QUESTION.** `store.search` ORs every token
it is given, and it was handed `f"{asset} {indication} {question}"`. The
question is boilerplate — "Which other sponsors have run clinical trials on this
mechanism or target" — so the OR matched on `trials`, `other`, `run`, and the
registry answered with whatever the store happens to hold most of. The asset and
the indication, the only two tokens that carried any meaning, contributed
nothing to a single returned row.

So: the fallback searches the ANCHORS ONLY, and every row it returns must
independently be about the asset or about the ingested indication —
`TrialAnchor.is_about` re-checks each record against `agents.py`, the same
matcher the structured path uses in SQL. A record that survives FTS but names a
different compound is dropped. This is deliberately the same shape as the
census/live-screen parity the biomarker prefilter has: a loose retrieval is
allowed only when a strict check runs behind it.

The fallback keeps its original purpose — a structured filter can legitimately
empty a genuine result set ("no Phase 3 exists") and a section should not go
silent for that reason — because an asset-matched FTS hit still passes.

Nothing here decides that an asset HAS no trials. It decides whether this store
was ever in a position to answer, which is the distinction the memo must print
rather than resolve.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import agents


@dataclass(frozen=True)
class TrialAnchor:
    """The two things that can tie a trial query to what was asked about.

    Either is sufficient on its own — a compound is worth looking up in a
    disease nobody ingested, and a disease is worth looking up for an asset the
    registry has never heard of. Neither is a claim that anything was found.
    """

    asset: str
    indication: str
    #: The query set key ONLY when the store has actually ingested it. `None`
    #: means the indication was never searched, which is not the same as
    #: searched-and-empty and must never be rendered as it.
    query_set: str | None
    #: What the indication resolved to, ingested or not — for the message.
    resolved_key: str
    #: The parsed asset. Falsy when the typed string yields no agent term, in
    #: which case it anchors nothing: `_intervention_clause` would return `1=1`
    #: and the query would match the whole store.
    asset_query: agents.AssetQuery

    @property
    def by_asset(self) -> bool:
        return bool(self.asset_query)

    @property
    def by_indication(self) -> bool:
        return self.query_set is not None

    def __bool__(self) -> bool:
        """True when this query is about something this store can be asked."""
        return self.by_asset or self.by_indication

    def is_about(self, record) -> bool:
        """Is this record actually about what was asked — checked in Python,
        against the same matcher the SQL path uses.

        The asset side reads the parsed intervention array AND the title.
        Interventions are the structured field and the primary signal; the title
        is included because the registry mis-spells intervention names ("Balstililmab"
        on NCT06751524) while the title spells them correctly, and losing a
        real trial to a sponsor's typo is the failure `agents.py` already
        refuses to fix with fuzzy matching.

        The indication side is membership in the ingested query set, which the
        caller has already enforced in SQL — the fetch defines the population,
        and re-deriving it here from a substring over the free-text condition
        array is the exact defect that discarded 57% of the colorectal set.
        """
        if self.by_asset:
            names = list(record.interventions or [])
            if record.brief_title:
                names.append(record.brief_title)
            return self.asset_query.matches(agents.record_tokens(names))
        return self.by_indication

    def search_text(self) -> str:
        """What the free-text fallback may search: the anchors, never the
        question. The question is boilerplate shared by every asset, and ORing
        its tokens is what returned colorectal trials for a hidradenitis
        asset."""
        return " ".join(p for p in (self.asset, self.indication) if p and p.strip()).strip()

    def notes(self) -> list[str]:
        """Every anchor that is missing, said out loud.

        Emitted whether or not the query was gated, because the two facts are
        independent and the more dangerous one survives a query that ran: an
        asset can anchor a search of a store that has never held a single trial
        in the indication, and every section then prints "nothing found" for a
        registry that was never asked. That is exactly the distinction
        `ValidationReport.assessed` and `NegativeEvidence.searched` exist to
        keep — not-assessed is not nothing-found — arriving one layer down, and
        an empty section with no explanation would move the original misreading
        rather than fix it.
        """
        out = []
        if self.indication and not self.by_indication:
            out.append(
                f"this store has never searched the registry for “{self.indication}” — it "
                f"resolves to the query set “{self.resolved_key}”, for which no ingest has "
                "ever run. Every trial count and every empty trial section in this memo is "
                "therefore a statement about what was searched, NOT a finding that no such "
                "trial exists. Ingest it with `python -m medrag trials -c "
                f"\"{self.indication}\"`, or add the phrasing to config/trial_queries.yaml if "
                "it belongs to a family already ingested."
            )
        if self.asset and not self.by_asset:
            out.append(
                f"no agent name could be parsed from the asset “{self.asset}”, so it cannot "
                "anchor a trial query — an unparseable asset drops the intervention filter "
                "entirely rather than narrowing it. Check the spelling against "
                "config/agents.yaml."
            )
        if not self:
            out.append(
                "no trial record was used as evidence anywhere in this memo: neither the "
                "asset nor the indication anchors a query this store can answer, so the "
                "registry was not consulted at all."
            )
        return out


def _has_been_searched(store, key: str) -> bool:
    """Has this store ever been asked about this family?

    Two independent signals, ORed, because either is positive evidence that a
    fetch happened and neither is guaranteed to be the one present:

      * a `query_coverage` row — written by `begin_ingest` BEFORE the first
        network call and cleared only by a verified `record_coverage`, so it is
        the signal that survives a family fetched and found genuinely empty; and
      * trials stamped with the set key — the signal that survives a store
        populated by `upsert(set_key=...)` without the ingest lifecycle around
        it, which is how several test fixtures and any direct-write path build
        one.

    Requiring the coverage row alone would make an entire ingested family
    invisible to every query on a store assembled the second way, which is a
    silent zeroing — the failure mode this module exists to prevent, reached
    from the other side. What must NOT be inferred is the reverse: neither
    signal present means nobody has looked, and that is never rendered as
    nothing-found.
    """
    if store is None or not key:
        return False
    if store.coverage(key) is not None:
        return True
    return store.count(query_set=key) > 0


def anchor_for(asset: str, indication: str, store) -> TrialAnchor:
    """Build the anchor, consulting the store for what it has actually searched."""
    from .queries import resolve_query_set

    resolved = resolve_query_set(indication).key if indication and indication.strip() else ""
    searched = _has_been_searched(store, resolved)
    return TrialAnchor(
        asset=asset or "",
        indication=indication or "",
        query_set=resolved if searched else None,
        resolved_key=resolved,
        asset_query=agents.parse_asset(asset or ""),
    )
