"""The retrieval contract, tested at the layer that actually runs it.

`tests/test_trial_queries.py` proves `build_landscape` selects by query set. That
test would stay green if `pages/3_Trial_Landscape.py` went on passing a condition
string — the tested seam and the operative seam would be different objects, which
is exactly how two consent bypasses lived through five green library tests. See
the convention note in CLAUDE.md and tests/test_consent_gate.py.

So this drives the real page through AppTest and asserts a trial registered as
"Colorectal Neoplasms" — which does NOT contain the substring "colorectal
cancer" — reaches the rendered table. That is MOUNTAINEER-03's exact shape, and
the old local re-narrowing dropped it.

No network: offline mode is set and the store is seeded on disk, so the page
takes its already-ingested branch.

    python tests/test_landscape_page.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import netguard  # noqa: E402

netguard.install()

from medrag.trials.client import parse_study  # noqa: E402
from medrag.trials.queries import BASKET_CAVEAT, CONDITION, QuerySet, TrialQuery  # noqa: E402
from medrag.trials.store import TrialStore  # noqa: E402

REPO = Path(__file__).resolve().parents[1]

# A recruiting trial that states MSS indirectly, registered the way
# MOUNTAINEER-03 registers: no literal "colorectal cancer" anywhere in the
# condition array.
NEOPLASMS_TRIAL = {
    "protocolSection": {
        "identificationModule": {"nctId": "NCT05253651",
                                 "briefTitle": "Tucatinib plus trastuzumab"},
        "statusModule": {"overallStatus": "RECRUITING"},
        "conditionsModule": {"conditions": ["Colorectal Neoplasms"]},
        "eligibilityModule": {
            "eligibilityCriteria": "Inclusion Criteria:\n- Microsatellite stable disease"
        },
        "contactsLocationsModule": {
            "locations": [{"facility": "Site", "city": "Boston", "country": "United States"}]
        },
    }
}


@contextmanager
def _seeded_app(set_key: str = "colorectal"):
    """A scratch data dir holding a trials.db stamped with a query set, yielding
    the landscape page. The store has to be created here rather than assumed —
    a test that needs a file the repo does not ship must build it."""
    from streamlit.testing.v1 import AppTest

    tmp = Path(tempfile.mkdtemp())
    (tmp / ".env").write_text("MEDRAG_OFFLINE=1\n", encoding="utf-8")
    raw = tmp / "data" / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    rec = parse_study(NEOPLASMS_TRIAL)
    with TrialStore(raw / "trials.db") as store:
        store.upsert([rec], provenance={rec.nct_id: ["cond:colorectal neoplasms"]},
                     set_key=set_key)
        qset = QuerySet(set_key, set_key, (TrialQuery(CONDITION, "colorectal cancer"),))
        from medrag.trials.queries import CoverageReport, QueryYield
        cov = CoverageReport(set_key=set_key, set_label=set_key, total_unique=1,
                             yields=[QueryYield(query=qset.queries[0], fetched=1, new=1,
                                                reported_total=1)])
        store.record_coverage(cov)

    keys = ("MEDRAG_DATA_DIR", "MEDRAG_OFFLINE", "MEDRAG_PROVIDER")
    saved = {k: os.environ.get(k) for k in keys}
    cwd = os.getcwd()

    os.chdir(tmp)
    os.environ["MEDRAG_DATA_DIR"] = str(tmp / "data")
    os.environ["MEDRAG_OFFLINE"] = "1"
    os.environ.pop("MEDRAG_PROVIDER", None)
    try:
        yield AppTest.from_file(
            str(REPO / "pages" / "3_Trial_Landscape.py"), default_timeout=60)
    finally:
        os.chdir(cwd)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _submit(at, condition="colorectal cancer", biomarker="MSS"):
    at.text_input[0].set_value(condition)
    at.text_input[1].set_value(biomarker)
    at.button[0].click().run()
    return at


def _rendered(at) -> str:
    """Everything the page drew, as one string. The table is rendered through
    theme.data_table, so assert on the page text rather than a widget type."""
    return "\n".join(
        str(getattr(el, "value", "")) for el in at.markdown
    ) + "\n".join(str(getattr(el, "value", "")) for el in at.warning)


def test_page_shows_a_trial_whose_condition_string_lacks_the_typed_words():
    """The operative regression: the page must select the population the fetch
    defined, not re-filter it on the words the user typed."""
    with _seeded_app() as app:
        at = _submit(app.run())
        assert "NCT05253651" in _rendered(at), (
            "a trial registered as 'Colorectal Neoplasms' was dropped — the page is "
            "re-narrowing on the condition string again"
        )


def test_page_names_the_basket_trial_gap_rather_than_leaving_it_to_inference():
    with _seeded_app() as app:
        at = _submit(app.run())
        warnings = " ".join(str(w.value) for w in at.warning)
        assert BASKET_CAVEAT[:40] in warnings, (
            "a known-unreachable class of trial must be stated in the UI"
        )


def test_page_finds_nothing_when_the_query_set_was_never_ingested():
    """Selecting by a set that does not exist must read as 'not ingested', not as
    'searched and found nothing' — offline, the page cannot go and get it."""
    with _seeded_app(set_key="some-other-disease") as app:
        at = _submit(app.run())
        text = _rendered(at) + " ".join(str(w.value) for w in at.warning)
        assert "NCT05253651" not in text
        assert "ingested" in text.lower() or "no biomarker-matched" in text.lower()


if __name__ == "__main__":
    import traceback

    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except Exception:
                failures += 1
                print(f"FAIL  {name}")
                traceback.print_exc()
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
