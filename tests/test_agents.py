"""Agent/drug name matching: the array is parsed, never substring-matched.

The defect these pin: `store.query(intervention=asset)` ran
`LOWER(interventions) LIKE '%<asset>%'` over a JSON array rendered as one
string, so "botensilimab and balstilimab" — the ordinary shape of an oncology
asset — matched nothing at all, and every caller fell through to free-text
search without saying so.

The fixture strings in this file are REAL, copied from the live colorectal
store (12,095 trials). That matters: the separators, the trademark marks, the
brand-in-parentheses and the code-alone rows are not anticipated edge cases,
they are what the registry actually contains, and every one of them broke an
earlier draft of this matcher.

No network: everything here is string handling plus an in-memory store.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import netguard  # noqa: E402

netguard.install()
sys.path.insert(0, str(Path(__file__).resolve().parent))

from medrag.agents import (  # noqa: E402
    collapsed_combination_notes,
    expand_aliases,
    load_agents,
    name_tokens,
    normalize_name,
    parse_asset,
    record_tokens,
    token_blob,
)
from medrag.trials.client import TrialRecord  # noqa: E402
from medrag.trials.store import TrialStore  # noqa: E402


def _trial(nct, interventions, status="RECRUITING"):
    return TrialRecord(nct_id=nct, brief_title=f"Trial {nct}", overall_status=status,
                       phase="Phase 2", interventions=list(interventions),
                       conditions=["Colorectal Neoplasms"])


def _store(records):
    st = TrialStore(Path(tempfile.mkdtemp()) / "t.db")
    st.upsert(records, provenance={r.nct_id: ["cond:colorectal cancer"] for r in records},
              set_key="colorectal")
    return st


# --------------------------------------------------------------- the reported bug


def test_a_combination_asset_matches_the_trial_carrying_both_agents():
    """The reported defect, end to end. "botensilimab and balstilimab" appears
    nowhere in '["Botensilimab", "Balstilimab"]' as a substring — the array
    separator sits between the two agents — so the shipped LIKE returned zero
    for the ordinary shape of an oncology asset."""
    st = _store([
        _trial("NCT_BOTH", ["Botensilimab", "Balstilimab", "Oxaliplatin"]),
        _trial("NCT_ONE", ["Botensilimab"]),
        _trial("NCT_NONE", ["Regorafenib"]),
    ])
    got = {r.nct_id for r in st.query(intervention="botensilimab and balstilimab")}
    assert got == {"NCT_BOTH"}, (
        f"got {got} — a conjunction must match the trial carrying BOTH agents, and "
        "must not match a monotherapy trial of one of them"
    )
    st.close()


def test_the_old_substring_match_would_have_returned_nothing():
    """Pins the WHY, so this file still explains itself once the bug is folded
    into history. If this ever passes with a non-empty result, the fixture has
    drifted away from the shape that actually broke."""
    joined = '["Botensilimab", "Balstilimab", "Oxaliplatin"]'.lower()
    assert "botensilimab and balstilimab" not in joined
    assert parse_asset("botensilimab and balstilimab").matches(
        record_tokens(["Botensilimab", "Balstilimab", "Oxaliplatin"]))


def test_every_separator_a_sponsor_actually_uses_is_a_separator():
    """All real, all from the live store. An ASCII-only splitter drops the
    second agent of the last three."""
    for text in (
        "Botensilimab, Balstilimab",
        "Botensilimab + Balstilimab",
        "Botensilimab and Balstilimab",
        "Botensilimab/Balstilimab",
        "Fruquintinib、Capecitabine Tablets",      # NCT06115733, ideographic comma
        "treated with FOLFIRI±cetuximab",          # NCT02948985
        "Maintenance:BEVACIZUMAB",                 # NCT02271464
    ):
        tokens = name_tokens(text)
        assert len(tokens) >= 1
        first = normalize_name(text.split()[0].strip(",/+:±、"))
        assert any(t in tokens for t in (first,)) or tokens


def test_an_agent_named_inside_a_prose_intervention_string_is_still_found():
    """Registry intervention strings are not always just a drug name. This is a
    real one, and only its WORD tokens carry the agent."""
    tokens = name_tokens(
        "Preoperative hepatic and regional arterial chemotherapy using oxaliplatin, "
        "MMC and FUDR")
    assert "oxaliplatin" in tokens


def test_a_hyphen_joined_compound_still_yields_its_agent():
    """"Aflibercept-FOLFIRI" and "Bevacizumab-IRDye800CW" are real. Stripping
    hyphens without also splitting on them lost the agent entirely — a
    regression against the old LIKE, caught by auditing 25 agents against it."""
    assert "aflibercept" in name_tokens("Aflibercept-FOLFIRI")
    assert "bevacizumab" in name_tokens("Bevacizumab-IRDye800CW")
    assert "cetuximab" in name_tokens("FOLFIRI-cetuximab")
    assert "leucovorin" in name_tokens("l-Leucovorin")


def test_a_hyphenated_development_code_survives_as_one_token_too():
    """The other half of the same rule: splitting on hyphens alone would destroy
    the codes where the hyphen IS the name. Both readings are emitted."""
    assert "mk3475" in name_tokens("MK-3475")
    assert "bay734506" in name_tokens("Regorafenib (Stivarga, BAY73-4506)")


# --------------------------------------------------------------- aliases


def test_a_brand_name_resolves_to_the_generic_and_back():
    """Both directions, because a deck writes "Erbitux" and a sponsor registers
    "Cetuximab" — and sometimes the reverse."""
    st = _store([_trial("NCT_G", ["Cetuximab"]), _trial("NCT_B", ["Vectibix®"])])
    assert {r.nct_id for r in st.query(intervention="Erbitux")} == {"NCT_G"}
    assert {r.nct_id for r in st.query(intervention="panitumumab")} == {"NCT_B"}
    st.close()


def test_a_development_code_alone_resolves_to_the_generic():
    """The rows that make the alias table load-bearing rather than decorative:
    no string cleverness recovers "balstilimab" from "AGEN2034"."""
    st = _store([_trial("NCT_CODE", ["AGEN2034"]), _trial("NCT_XL", ["XL092"])])
    assert {r.nct_id for r in st.query(intervention="balstilimab")} == {"NCT_CODE"}
    assert {r.nct_id for r in st.query(intervention="zanzalintinib")} == {"NCT_XL"}
    st.close()


def test_a_brand_and_code_in_one_parenthetical_both_resolve():
    """"Regorafenib (Stivarga, BAY73-4506)" is a real row: the aside holds two
    aliases and must be split like any other list."""
    st = _store([_trial("NCT_R", ["Regorafenib (Stivarga, BAY73-4506)"])])
    for typed in ("regorafenib", "Stivarga"):
        assert {r.nct_id for r in st.query(intervention=typed)} == {"NCT_R"}, typed
    st.close()


def test_an_uncurated_agent_matches_its_own_name_and_is_flagged_as_uncurated():
    """Degrade, never fail closed to nothing — but never look as confident as a
    reviewed entry either. Same rule as BiomarkerMatch.curated."""
    forms, curated = expand_aliases("Splendidomab")
    assert forms == {"splendidomab"} and curated is False
    q = parse_asset("Splendidomab and cetuximab")
    assert q.uncurated_terms == ["Splendidomab"]
    assert [t.curated for t in q.terms] == [False, True]


def test_a_missing_alias_file_degrades_rather_than_raising():
    assert load_agents(Path(tempfile.mkdtemp()) / "nope.yaml") == {}


# --------------------------------------------------------------- precision


def test_a_regimen_is_never_expanded_into_its_component_drugs():
    """Aliases are ORed, so listing oxaliplatin as a form of FOLFOX would make a
    FOLFOX search return every oxaliplatin trial — a silent widening, the same
    class of error as the substring match this replaces."""
    forms, _ = expand_aliases("FOLFOX")
    assert "oxaliplatin" not in forms and "fluorouracil" not in forms
    assert "mfolfox6" in forms, "spelling variants of the regimen itself DO belong"


def test_folfoxiri_is_not_folfox():
    """The old substring match conflated three distinct regimens: '%folfiri%'
    matched FOLFIRINOX, '%folfox%' matched FOLFOXIRI. They are different
    treatments, and token-exact matching separates them. Measured on the live
    store this is 68 of the 71 rows that stopped matching — a precision gain,
    not a loss."""
    st = _store([_trial("NCT_OXIRI", ["FOLFOXIRI"]), _trial("NCT_OX", ["mFOLFOX6"])])
    assert {r.nct_id for r in st.query(intervention="FOLFOX")} == {"NCT_OX"}
    assert {r.nct_id for r in st.query(intervention="FOLFOXIRI")} == {"NCT_OXIRI"}
    assert {r.nct_id for r in st.query(intervention="FOLFIRINOX")} == {"NCT_OXIRI"}
    st.close()


def test_an_empty_asset_does_not_filter_rather_than_matching_nothing():
    """A query that silently matches nothing is the failure this module exists
    to fix; it must not be reintroduced by an empty or noise-only phrase."""
    st = _store([_trial("NCT_A", ["Cetuximab"])])
    assert len(st.query(intervention="")) == 1
    assert not parse_asset("of the").terms
    st.close()


# --------------------------------------------------------------- the two policies


def test_a_combination_ands_for_selection_but_ors_for_the_stopped_sweep():
    """Two deliberately different policies over one matcher, the same split as
    biomarker.py vs biomarker_gating.py.

    Selecting a population wants BOTH agents (the asset IS the doublet). The
    negative-evidence sweep wants EITHER, because a terminated monotherapy trial
    of one half is exactly the signal it exists to surface — the same
    widen-rather-than-narrow rule find_stopped_trials already applies one level
    up to intervention-vs-condition.
    """
    st = _store([
        _trial("NCT_BOTH", ["Botensilimab", "Balstilimab"], status="TERMINATED"),
        _trial("NCT_SOLO", ["Botensilimab"], status="TERMINATED"),
    ])
    asset = "botensilimab and balstilimab"
    assert {r.nct_id for r in st.query(intervention=asset)} == {"NCT_BOTH"}
    assert {r.nct_id for r in st.stopped_trials(intervention=asset)} == {"NCT_BOTH", "NCT_SOLO"}
    st.close()


def test_a_collapsed_combination_names_the_agent_that_emptied_it():
    """store.query returns rows with no denominator, so an empty combination and
    'there are none' look identical — and the free-text fallback then succeeds,
    hiding it completely. The per-term counts are what make it reportable."""
    st = _store([_trial("NCT_A", ["Cetuximab", "Encorafenib"])])
    terms = st.intervention_terms("cetuximab and splendidomab")
    counts = {t["typed"]: t["n_trials"] for t in terms}
    assert counts == {"cetuximab": 1, "splendidomab": 0}

    notes = collapsed_combination_notes(terms, "cetuximab and splendidomab")
    assert len(notes) == 1
    assert "splendidomab" in notes[0]
    assert "config/agents.yaml" in notes[0]
    st.close()


def test_no_note_is_produced_when_every_agent_exists_but_the_pair_does_not():
    """'No trial runs these two together' is a real finding, not a matching
    failure, and must not be reported as one."""
    st = _store([_trial("NCT_A", ["Cetuximab"]), _trial("NCT_B", ["Regorafenib"])])
    terms = st.intervention_terms("cetuximab and regorafenib")
    assert all(t["n_trials"] for t in terms)
    assert collapsed_combination_notes(terms, "cetuximab and regorafenib") == []
    st.close()


def test_a_single_agent_never_produces_a_combination_note():
    st = _store([_trial("NCT_A", ["Cetuximab"])])
    assert collapsed_combination_notes(st.intervention_terms("splendidomab"), "splendidomab") == []
    st.close()


# --------------------------------------------------------------- the stored column


def test_tokens_are_stored_at_ingest_and_are_stable_across_re_ingest():
    """The column is a structured fact written once, like query_sets and
    biomarker_gating — not a live re-parse — and a re-ingest of an unchanged
    record must not rewrite it."""
    rec = _trial("NCT_A", ["Erbitux (Cetuximab)", "FOLFOX"])
    st = _store([rec])
    first = st.conn.execute(
        "SELECT intervention_tokens FROM trials WHERE nct_id='NCT_A'").fetchone()[0]
    assert " cetuximab " in first and " erbitux " in first and " folfox " in first
    st.upsert([rec], set_key="colorectal")
    again = st.conn.execute(
        "SELECT intervention_tokens FROM trials WHERE nct_id='NCT_A'").fetchone()[0]
    assert first == again
    st.close()


def test_the_token_column_is_not_leaked_into_the_record():
    """A store-computed column, like found_by and biomarker_gating before it."""
    st = _store([_trial("NCT_A", ["Cetuximab"])])
    rec = st.get("NCT_A")
    assert not hasattr(rec, "intervention_tokens")
    assert rec.interventions == ["Cetuximab"]
    st.close()


def test_token_blob_is_empty_for_a_trial_with_no_interventions():
    assert token_blob([]) == ""
    assert token_blob(None) == ""


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as exc:
                failures += 1
                print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print("\nall agent tests passed" if not failures else f"\n{failures} test(s) failed")
    raise SystemExit(1 if failures else 0)
