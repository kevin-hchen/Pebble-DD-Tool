"""The two device pieces that were built, correct, and reachable from nothing.

`docs/SCOPE.md` says drugs and devices are screened equally and both paths must
be "equally real, equally tested, equally gated". Two finished modules failed
that on wiring alone:

  * `agents.parse_descriptive_name` — the device name matcher — was called at
    exactly one site, `fda/store.py`, so trial retrieval matched device names
    with the drug parser and returned nothing for them.
  * `build_device_answer` / `DeviceRegulatoryAnswer` — 277 lines mirroring
    `ApprovalAnswer`, with three pathways kept deliberately apart — was imported
    by nothing outside tests. Every device memo carried zero FDA records while
    the store held 56,853 PMA records and 482 De Novo authorisations.

These tests pin the wiring, not the modules: both were already covered by
`test_agents.py` and `test_fda_pma.py` and both passed throughout. A test at the
layer that CALLS a guard is the convention this repo already states, and this
file is that layer for the device path.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Direct runs do not load conftest.py, so the no-network guard is installed
# here too. See tests/netguard.py.
from tests import netguard  # noqa: E402

netguard.install()
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

from medrag import phrasing  # noqa: E402
from medrag.config import Config  # noqa: E402
from medrag.diligence import (  # noqa: E402
    _NON_APPROVAL_PHRASES,
    _NON_CLEARANCE_PHRASES,
    DiligenceRunner,
    load_question_set,
)
from medrag.fda.device_answer import DeviceRegulatoryAnswer, build_device_answer  # noqa: E402
from medrag.trials.client import TrialRecord  # noqa: E402
from medrag.trials.store import (  # noqa: E402
    NAME_AS_ASSET,
    NAME_AS_DESCRIPTION,
    TrialStore,
)

CONFIG = Path(__file__).resolve().parents[1] / "config"


def _store() -> TrialStore:
    """A device trial and a drug trial, named the way the registry names them."""
    store = TrialStore(Path(tempfile.mkdtemp()) / "t.db")
    store.upsert([
        TrialRecord(
            nct_id="NCT_DEV", brief_title="Point of care procalcitonin testing in sepsis",
            conditions=["Sepsis"],
            # The registry's word order, not the query's — which is the whole
            # reason a device name needs word-level matching.
            interventions=["Assay, procalcitonin (point of care)"],
            intervention_types=["DIAGNOSTIC_TEST"],
        ),
        TrialRecord(
            nct_id="NCT_ADC", brief_title="Trastuzumab deruxtecan in breast cancer",
            conditions=["Breast Cancer"],
            interventions=["Trastuzumab deruxtecan"],
            intervention_types=["DRUG"],
        ),
        TrialRecord(
            nct_id="NCT_HALF", brief_title="Trastuzumab plus a different payload",
            conditions=["Breast Cancer"],
            interventions=["Trastuzumab", "Deruxtecan-free comparator"],
            intervention_types=["DRUG", "DRUG"],
        ),
    ])
    return store


# ------------------------------------------------- (a) the name-style rule


def test_a_device_name_is_matched_word_by_word_and_a_drug_name_is_not():
    """The defect and its guard in one test.

    `parse_asset` joins a phrase into one token, which is right for a molecule
    and wrong for a description. Measured on the live store: "procalcitonin
    assay" returns 0 trials joined and 2 split, while "trastuzumab deruxtecan"
    returns 123 joined and 132 split — the split version matching the two halves
    of an antibody-drug conjugate separately. Both are two lowercase words.
    """
    store = _store()
    assert store.query(intervention="procalcitonin assay") == [], \
        "the drug parser is expected to miss a device name — that is the defect"
    found = store.query(intervention="procalcitonin assay",
                        name_style=NAME_AS_DESCRIPTION)
    assert [r.nct_id for r in found] == ["NCT_DEV"], \
        "word-level matching must find a device the registry named in another order"

    # The other direction, which is why the rule is declared rather than sniffed.
    joined = store.query(intervention="trastuzumab deruxtecan", name_style=NAME_AS_ASSET)
    split = store.query(intervention="trastuzumab deruxtecan",
                        name_style=NAME_AS_DESCRIPTION)
    assert {r.nct_id for r in joined} == {"NCT_ADC"}
    assert "NCT_HALF" in {r.nct_id for r in split}, (
        "splitting an ADC name matches trials carrying the halves separately — this is "
        "the over-match the declared rule exists to prevent on the drug path"
    )


def test_the_question_set_declares_the_kind_and_nothing_sniffs_it():
    """A guessed modality is not a smaller version of a declared one."""
    assert load_question_set(CONFIG / "screening_devices.yaml").asset_kind == "device"
    assert load_question_set(CONFIG / "diligence_questions.yaml").asset_kind == "drug"
    assert load_question_set(CONFIG / "landscape.yaml").asset_kind == "auto"

    for path in sorted(CONFIG.glob("*.yaml")):
        data = yaml.safe_load(path.read_text()) or {}
        if not data.get("questions"):
            continue
        assert "asset_kind" in data, (
            f"{path.name} ships questions but declares no asset_kind, so its assets "
            "would be parsed with whichever default happens to be current"
        )


def test_an_undeclared_caller_keeps_the_drug_parser():
    """Adding the field must change nothing for anyone who has not opted in."""
    runner = DiligenceRunner(Config(openai_api_key=None,
                                    data_dir=Path(tempfile.mkdtemp())),
                             rag=None, trial_store=_store())
    assert runner.name_style == NAME_AS_ASSET


def test_an_invalid_asset_kind_is_refused_rather_than_defaulted():
    """A typo'd `asset_kind: devise` silently falling back to the drug parser is
    the failure mode this whole change is about, arriving through config."""
    bad = Path(tempfile.mkdtemp()) / "q.yaml"
    bad.write_text("asset_kind: devise\nversion: 1\nname: t\nquestions:\n"
                   "  - id: a\n    question: 'What about {asset}?'\n")
    try:
        load_question_set(bad)
    except ValueError as exc:
        assert "asset_kind" in str(exc)
    else:
        raise AssertionError("an unrecognised asset_kind must be refused")


# ------------------------------------------------- (b) the device answer


def test_the_device_regulatory_answer_reaches_a_section():
    """It existed, was correct, was tested, and was imported by nothing."""
    runner = DiligenceRunner(Config(openai_api_key=None,
                                    data_dir=Path(tempfile.mkdtemp())),
                             rag=None, trial_store=_store())
    answer = runner._device_for("infusion pump", {})
    assert isinstance(answer, DeviceRegulatoryAnswer)
    assert answer.render_lines(), "the block must render even with no store"


def test_absence_is_reported_as_absence_and_never_as_no_authorisation():
    """The fail-closed term the drug path already meets.

    With no device store the answer must not read as "this device has no FDA
    authorisation" — that is the device-side form of the not-found-is-not-
    contradicted rule, and `has_pma_approval` requiring positive evidence is
    what makes it true in the type rather than in the prose.
    """
    answer = build_device_answer(None, "some device")
    text = " ".join(answer.render_lines()).lower()
    assert not answer.found_anything
    assert not answer.has_pma_approval
    for claim in ("not cleared", "no clearance", "not approved", "unapproved"):
        assert claim not in text, f"an empty device store rendered as {claim!r}"


def test_the_two_overreach_phrase_lists_stay_distinct():
    """A device that is PMA-approved but not 510(k)-cleared is a real and common
    shape — 39% of the device types measured have PMA records and no
    clearances — so the drug list and the device list may not blur into one."""
    assert not (set(_NON_CLEARANCE_PHRASES) & set(_NON_APPROVAL_PHRASES))
    assert _NON_CLEARANCE_PHRASES and _NON_APPROVAL_PHRASES


def test_the_rendered_device_block_passes_the_self_contradiction_lint():
    """Same sweep the drug caveats get: a caveat must not contain the claim it
    denies. Four such regressions have shipped in this codebase already."""
    answer = build_device_answer(None, "some device")
    findings = phrasing.audit(answer.render_lines(), "device_answer.render_lines",
                              domains=("clearance", "approval"))
    assert not findings, f"the rendered device block contradicts itself: {findings}"


def test_a_model_claiming_no_authorisation_is_flagged():
    """A prompt instruction is a request. The drug path checks the generated
    prose against the deterministic answer; the device path must too."""
    answer = build_device_answer(None, "some device")
    note = DiligenceRunner._flag_clearance_overreach(
        answer, "This device is not cleared by the FDA.")
    assert note and "not cleared" in note

    ok = DiligenceRunner._flag_clearance_overreach(
        answer, "The record states what was searched.")
    assert ok is None


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL  {name}: {exc}")
    print("\nall device-path tests passed" if not failures else f"\n{failures} failure(s)")
    raise SystemExit(1 if failures else 0)
