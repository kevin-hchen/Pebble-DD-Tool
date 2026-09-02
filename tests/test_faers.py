"""FAERS: the interpretive guard first, the retrieval second.

FAERS is the source most likely to produce a confidently misleading memo in this
whole tool. It yields large, specific-looking numbers that invite exactly the
reading they cannot support — a count with no denominator read as a rate, a
report read as a finding, an association read as causation.

So the load-bearing tests here are not about fetching. They assert that the
rendered section says what the number is NOT, every time, in full; that no
surface can turn a count into a rate; and that an empty result never reads as a
clean safety record.

No network: aggregates come from real captured `count` responses.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import netguard  # noqa: E402

netguard.install()
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures.faers import (  # noqa: E402
    REACTIONS_PAGE,
    REPORTER_PAGE,
    ROLE_PAGE,
    SERIOUS_PAGE,
    TOTALS,
)

from medrag.fda.drug_store import DrugStore  # noqa: E402
from medrag.fda.faers import (  # noqa: E402
    DRUG_ROLE,
    FAERS_ABSENCE_MEANINGS,
    REPORTER_QUALIFICATION,
    SERIOUSNESS,
    WHAT_THIS_IS_NOT,
    FAERSAnswer,
    TermCount,
)

#: Words that would turn a report count into an epidemiological claim. This is
#: the specification, kept here rather than imported.
FORBIDDEN_QUANTITATIVE = (
    "incidence", "prevalence", "rate of", "risk of", "frequency of",
    "% of patients", "per 1000", "per 100,000", "caused by", "causes ",
    "side effect rate", "occurs in",
)
#: Phrasings that assert a clean safety record from an empty result.
FORBIDDEN_ABSENCE = (
    "no adverse events", "no safety signal", "no side effects", "is safe",
    "well tolerated", "no reports of harm", "clean safety",
)


def _buckets(page, decode):
    out = []
    for row in page.get("results", []):
        term = str(row["term"])
        label = "" if decode is None else (
            decode.get(term) or f"code {term}, which the FDA data dictionary does not define")
        out.append(TermCount(term=term, count=int(row["count"]), label=label))
    return out


def _answer(**over) -> FAERSAnswer:
    base = dict(
        asset="pembrolizumab", searched=True, retrieved_at="2026-08-06T06:54:39",
        matched_field="normalised OR free text",
        n_reports=TOTALS["free_text"], n_reports_normalised=TOTALS["normalised"],
        n_reports_free_text=TOTALS["free_text"], faers_total=TOTALS["faers_total"],
        n_death_reports=18745,
        reactions=_buckets(REACTIONS_PAGE, None),
        seriousness=_buckets(SERIOUS_PAGE, SERIOUSNESS),
        reporter=_buckets(REPORTER_PAGE, REPORTER_QUALIFICATION),
        drug_role=_buckets(ROLE_PAGE, DRUG_ROLE),
    )
    base.update(over)
    return FAERSAnswer(**base)


# ------------------------------------------------------- THE GUARD


def test_every_rendered_section_says_what_the_number_is_not_in_full():
    """Not a footnote, not conditional on the count being large. All five
    caveats, every time."""
    text = " ".join(_answer().render_lines())
    for caveat in WHAT_THIS_IS_NOT:
        assert caveat in text, f"missing caveat: {caveat[:60]}"


def test_the_no_denominator_point_is_made_explicitly():
    text = " ".join(_answer().render_lines())
    assert "NO DENOMINATOR" in text
    # Stated concretely rather than by naming the epidemiological terms it rules
    # out — the denial must not share a substring with the claim.
    assert "how often an event happens" in text
    assert "what share of patients experienced it" in text
    assert "cannot be compared" in text


def test_no_surface_renders_a_count_as_a_rate_or_a_cause():
    """The whole point of the phase. A count of reports is not an epidemiological
    quantity and no rendered line may imply it is."""
    for answer in (_answer(), _answer(n_reports=1, reactions=[], seriousness=[],
                                      reporter=[], drug_role=[])):
        text = " ".join(answer.render_lines()).lower()
        for phrase in FORBIDDEN_QUANTITATIVE:
            assert phrase not in text, f"rendered a report count as “{phrase}”"


def test_the_answer_object_exposes_no_rate_and_cannot_compute_one():
    """A guard in prose is weaker than a guard in the type. There is no field or
    method here that divides one count by another."""
    answer = _answer()
    for banned in ("rate", "incidence", "frequency", "risk", "per_patient"):
        assert not any(banned in name for name in dir(answer) if not name.startswith("_")), (
            f"FAERSAnswer exposes “{banned}”, which invites a denominator it does not have"
        )


def test_concomitant_reports_are_called_out_not_folded_into_the_total():
    """A third of the reports counted for this drug record it as merely present
    alongside the drug actually suspected."""
    text = " ".join(_answer().render_lines())
    assert "Concomitant" in text
    assert "not itself suspected" in text
    assert "not validated by FDA" in text


def test_the_disease_appears_in_the_reaction_list_and_the_memo_says_so():
    """MALIGNANT NEOPLASM PROGRESSION is the top reported event for this drug —
    the cancer progressing, not a drug effect."""
    answer = _answer()
    assert answer.reactions[0].term == "MALIGNANT NEOPLASM PROGRESSION"
    text = " ".join(answer.render_lines())
    assert "Progression of the treated disease" in text
    assert "is not a drug effect" in text


def test_a_fatal_outcome_is_never_rendered_as_a_drug_caused_death():
    text = " ".join(_answer().render_lines())
    assert "18,745 report(s) record a fatal outcome" in text
    assert "not a finding that the drug caused the death" in text


def test_reporter_mix_is_shown_because_a_lawyer_filed_report_is_not_a_signal():
    text = " ".join(_answer().render_lines())
    assert "Lawyer" in text
    assert "litigation artefact before it is a safety signal" in text


def test_an_undocumented_code_is_labelled_not_printed_as_a_bare_number():
    """ROLE_PAGE contains code 4, which the FDA dictionary does not define."""
    answer = _answer()
    codes = {t.term for t in answer.drug_role}
    assert "4" in codes, "the fixture must reproduce the undocumented code"
    text = " ".join(answer.render_lines())
    assert "does not define" in text


# ------------------------------------------------------- absence


def test_an_asset_with_no_reports_never_reads_as_a_clean_safety_record():
    answer = _answer(n_reports=0, n_reports_normalised=0, n_reports_free_text=0,
                     reactions=[], seriousness=[], reporter=[], drug_role=[],
                     n_death_reports=0)
    assert answer.found is False
    text = " ".join(answer.render_lines()).lower()
    for phrase in FORBIDDEN_ABSENCE:
        assert phrase not in text, f"absence rendered as “{phrase}”"


def test_absence_states_all_four_meanings():
    answer = _answer(n_reports=0, n_reports_normalised=0, n_reports_free_text=0)
    text = " ".join(answer.render_lines())
    for meaning in FAERS_ABSENCE_MEANINGS:
        assert meaning in text, f"missing absence meaning: {meaning[:50]}"
    assert "says nothing either way about the drug's safety" in text


def test_never_consulted_is_distinct_from_consulted_and_empty():
    never = FAERSAnswer(asset="x")
    empty = _answer(n_reports=0, n_reports_normalised=0, n_reports_free_text=0)
    assert "NOT consulted" in never.statement()
    assert "No FAERS report matching" in empty.statement()
    assert never.statement() != empty.statement()


def test_an_offline_miss_names_the_asset_rather_than_returning_a_silent_zero():
    answer = FAERSAnswer(asset="botensilimab", offline_miss=True)
    text = " ".join(answer.render_lines())
    assert "botensilimab" in text
    assert "offline mode is on and no cached aggregate" in text
    assert "not a finding about the drug" in text
    for phrase in FORBIDDEN_ABSENCE:
        assert phrase not in text.lower()


# ------------------------------------------------------- matching / linkage


def test_the_free_text_field_is_ored_in_rather_than_relied_on_alone():
    """Measured: irinotecan returns 12,783 reports on the normalised block and
    47,829 on the free-text name, and two investigational assets return ZERO
    normalised with free-text reports present. Matching the normalised field
    alone would report zero for them — a false clean-safety impression."""
    answer = _answer(n_reports_normalised=0, n_reports_free_text=36, n_reports=36)
    text = " ".join(answer.render_lines())
    assert "found ONLY by the free-text name" in text
    assert "approximate" in text


def test_the_linkage_rate_is_stated_not_assumed():
    text = " ".join(_answer().render_lines())
    assert "88.8%" in text and "11.2%" in text
    assert "LOWER BOUND" in text


def test_matching_goes_through_the_shared_alias_table():
    from medrag.fda.faers import MATCH_FIELD, _search_for

    clause = _search_for("Keytruda", MATCH_FIELD)
    assert "pembrolizumab" in clause.lower(), "a brand name must reach the generic"
    assert _search_for("", MATCH_FIELD) == ""


# ------------------------------------------------------- the cache


def _store() -> DrugStore:
    return DrugStore(Path(tempfile.mkdtemp()) / "drugs.db")


def test_the_cache_round_trips_with_its_retrieval_timestamp():
    """The cache IS the mirror: bounded, reproducible, offline-capable."""
    store = _store()
    store.cache_faers(_answer())
    back = store.cached_faers("pembrolizumab")
    assert back is not None
    assert back.n_reports == TOTALS["free_text"]
    assert back.n_reports_normalised == TOTALS["normalised"]
    assert back.retrieved_at.startswith("2026-08-06")
    assert [t.term for t in back.reactions] == [t.term for t in _answer().reactions]
    assert back.drug_role and back.drug_role[0].label
    store.close()


def test_nothing_cached_is_none_not_an_empty_answer():
    """"Never asked" and "asked and found nothing" are different facts."""
    store = _store()
    assert store.cached_faers("anything") is None
    store.close()


def test_offline_with_a_cached_aggregate_serves_it_without_fetching():
    store = _store()
    store.cache_faers(_answer())

    def explode(_asset):
        raise AssertionError("offline must not fetch")

    answer = store.faers_answer("pembrolizumab", offline=True, fetch=explode)
    assert answer.n_reports == TOTALS["free_text"] and answer.offline_miss is False
    store.close()


def test_offline_without_a_cached_aggregate_refuses_and_flags_the_miss():
    store = _store()

    def explode(_asset):
        raise AssertionError("offline must not fetch")

    answer = store.faers_answer("botensilimab", offline=True, fetch=explode)
    assert answer.offline_miss is True and answer.found is False
    assert "botensilimab" in answer.statement()
    store.close()


def test_a_cache_hit_is_preferred_over_fetching_even_when_online():
    store = _store()
    store.cache_faers(_answer())

    def explode(_asset):
        raise AssertionError("a cached aggregate must not be re-fetched")

    assert store.faers_answer("pembrolizumab", offline=False, fetch=explode).n_reports
    store.close()


def test_a_fetch_is_cached_so_the_next_run_is_reproducible():
    store = _store()
    calls = []

    def fake(asset):
        calls.append(asset)
        return _answer(asset=asset)

    store.faers_answer("pembrolizumab", fetch=fake)
    store.faers_answer("pembrolizumab", fetch=fake)
    assert len(calls) == 1, "the second call must come from the cache"
    assert "pembrolizumab" in store.faers_freshness()
    store.close()


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
    print("\nall FAERS tests passed" if not failures else f"\n{failures} test(s) failed")
    raise SystemExit(1 if failures else 0)
