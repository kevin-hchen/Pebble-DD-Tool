"""The consent gate, tested at the layer that actually runs it.

Every existing confidentiality test drives the library seam — `extract_claims`
and `ClaimVerifier.verify` raise `ConfirmationRequired` when `confirmed=False`.
Those tests pass and always did. They also never fire in the app, because
`pages/2_Verify_Claims.py` passes `confirmed=True` unconditionally: the real
decision is a Streamlit checkbox, and nothing tested a Streamlit checkbox.

That gap is what let two bypasses live:

  * the verify checkbox had no `key=`, so Streamlit derived identity from the
    label, and the label varied only by claim COUNT — swap three claims for
    three different claims and the tick carried over to text nobody confirmed
  * the extract checkbox had a fixed `key="extract_ok"`, so one tick consented
    to every deck for the rest of the session

So these run the page through `streamlit.testing.v1.AppTest` and assert on
`st.session_state` and the checkbox widgets directly. No network: the provider is
forced remote by config only, and no test here reaches a model.

    python tests/test_consent_gate.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from medrag.claims import TransmissionNotice, consent_key, transmission_notice  # noqa: E402
from medrag.config import Config  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def _notice(items, provider="groq", kind="claims") -> TransmissionNotice:
    return TransmissionNotice(
        local=False,
        provider_key=provider,
        provider_label=f"{provider} — test",
        endpoint="https://example.invalid/v1",
        items=list(items),
        kind=kind,
    )


# ------------------------------------------------- consent_key (library layer)


def test_same_content_and_provider_gives_a_stable_key():
    """Re-rendering an unchanged page must not nag the analyst again."""
    a = consent_key(_notice(["claim one", "claim two"]))
    b = consent_key(_notice(["claim one", "claim two"]))
    assert a == b


def test_changing_claim_text_changes_the_key_at_the_same_count():
    """The exact bypass: 3 claims -> 3 different claims kept the old consent."""
    before = consent_key(_notice(["SECRET-A", "SECRET-B", "SECRET-C"]))
    after = consent_key(_notice(["DIFFERENT-X", "DIFFERENT-Y", "DIFFERENT-Z"]))
    assert before != after, "same count, different text must invalidate consent"


def test_changing_provider_changes_the_key_at_identical_content():
    """'For this run' means this content to THIS destination."""
    ollama = consent_key(_notice(["claim one"], provider="ollama"))
    groq = consent_key(_notice(["claim one"], provider="groq"))
    assert ollama != groq, "switching destination must invalidate consent"


def test_deck_consent_and_claim_consent_never_collide():
    """Identical text as a deck and as a claim are different transmissions."""
    deck = consent_key(_notice(["same text"], kind="deck text"), "extract")
    claim = consent_key(_notice(["same text"], kind="claims"), "verify")
    assert deck != claim


def test_reordering_claims_changes_the_key():
    """Order is content: the model sees a different payload."""
    assert consent_key(_notice(["a", "b"])) != consent_key(_notice(["b", "a"]))


def test_adding_a_claim_changes_the_key():
    assert consent_key(_notice(["a"])) != consent_key(_notice(["a", "b"]))


def test_the_key_is_a_valid_stable_widget_key():
    key = consent_key(_notice(["x"]), "verify")
    assert key.startswith("verify_")
    assert key.replace("verify_", "").isalnum()
    assert len(key) < 40, "keys accumulate in session_state; keep them short"


def test_the_key_does_not_leak_the_claim_text():
    """session_state keys are visible in debug dumps; they must not carry content."""
    key = consent_key(_notice(["ACQUISITION TARGET IS ACME BIO"]))
    assert "ACME" not in key and "ACQUISITION" not in key


# ------------------------------------------------- the registry caveat


def test_the_notice_says_the_asset_name_reaches_the_registry():
    """A local provider stops the claims leaving, not the asset name."""
    rendered = _notice(["a claim"]).render()
    assert "PubMed" in rendered and "ClinicalTrials.gov" in rendered
    assert "regardless of provider" in rendered


def test_a_local_provider_still_warns_about_the_registry():
    cfg = Config(offline=False)
    cfg.provider = "ollama"
    cfg.openai_api_key = None
    notice = transmission_notice(cfg, ["a claim"], kind="claims")
    assert notice.local, "ollama is local"
    assert "ClinicalTrials.gov" in notice.render(), (
        "choosing a local model must not read as a fully private run"
    )


def test_offline_does_not_warn_about_the_registry():
    """Offline blocks the registry too, so the caveat would be false there."""
    cfg = Config(offline=True)
    notice = transmission_notice(cfg, ["a claim"], kind="claims")
    assert notice.local and notice.offline
    assert "ClinicalTrials.gov" not in notice.render()


# ------------------------------------------------- the Streamlit layer
# These are the first tests in this repo that touch st.session_state and
# st.checkbox. They drive the real page file.


def _app(tmp: Path):
    """The claims page, wired to a scratch data dir and a remote provider."""
    from streamlit.testing.v1 import AppTest

    os.environ["MEDRAG_DATA_DIR"] = str(tmp)
    os.environ["MEDRAG_PROVIDER"] = "groq"
    os.environ["GROQ_API_KEY"] = "test-key-not-used-no-call-is-made"
    os.environ.pop("MEDRAG_OFFLINE", None)
    at = AppTest.from_file(str(REPO / "pages" / "2_Verify_Claims.py"), default_timeout=30)
    return at


def _consent_boxes(at):
    """Checkboxes whose label is a confirmation, keyed by their widget key."""
    return {c.key: c for c in at.checkbox if "confirm" in (c.label or "").lower()}


def test_page_renders_a_consent_checkbox_for_pasted_claims():
    tmp = Path(tempfile.mkdtemp())
    at = _app(tmp).run()
    at.text_area(key="claims_text").set_value("claim one\nclaim two").run()

    boxes = _consent_boxes(at)
    assert boxes, "a remote provider must ask for confirmation"
    assert any(k.startswith("verify_") for k in boxes), (
        f"consent must use a content-derived key, got {list(boxes)}"
    )


def test_consent_does_not_survive_a_claim_text_swap_at_the_same_count():
    """The regression for finding 1, at the layer where it happened."""
    tmp = Path(tempfile.mkdtemp())
    at = _app(tmp).run()

    at.text_area(key="claims_text").set_value("SECRET-A\nSECRET-B\nSECRET-C").run()
    first = _consent_boxes(at)
    key_before = next(k for k in first if k.startswith("verify_"))
    first[key_before].check().run()
    assert at.session_state[key_before] is True, "the analyst ticked it"

    # Same count, entirely different confidential text.
    at.text_area(key="claims_text").set_value("DIFFERENT-X\nDIFFERENT-Y\nDIFFERENT-Z").run()
    second = _consent_boxes(at)
    key_after = next(k for k in second if k.startswith("verify_"))

    assert key_after != key_before, "different claims must be a different consent"
    assert second[key_after].value is False, (
        "consent for SECRET-A/B/C must not carry over to DIFFERENT-X/Y/Z"
    )


def test_consent_is_dropped_once_the_analyst_edits_away_from_it():
    """Streamlit GCs widget state for widgets it stops rendering, so consent does
    not even survive a round trip through other content. Pinned because the
    docstring on consent_key claims this, and an unasserted claim about a
    security control is the thing that produced finding 1."""
    tmp = Path(tempfile.mkdtemp())
    at = _app(tmp).run()

    at.text_area(key="claims_text").set_value("claim one\nclaim two").run()
    key = next(k for k in _consent_boxes(at) if k.startswith("verify_"))
    _consent_boxes(at)[key].check().run()
    assert at.session_state[key] is True

    at.text_area(key="claims_text").set_value("something else entirely").run()
    assert key not in at.session_state, "stale consent must not linger in state"

    at.text_area(key="claims_text").set_value("claim one\nclaim two").run()
    assert _consent_boxes(at)[key].value is False, (
        "coming back to previously confirmed claims must ask again"
    )


def test_the_page_never_uses_a_fixed_consent_key():
    """Guards the fix itself: a literal key= is what caused finding 2."""
    # Comments are stripped: the fix's own explanatory comment quotes the old
    # key, and a guard that trips on its own rationale is a guard nobody keeps.
    code = "\n".join(
        line for line in (REPO / "pages" / "2_Verify_Claims.py").read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    for literal in ('key="extract_ok"', "key='extract_ok'"):
        assert literal not in code, (
            "a fixed key makes one tick consent to every later deck this session"
        )
    assert code.count("consent_key(") == 2, "both checkboxes must be content-keyed"


def test_verify_button_is_blocked_until_consent_is_given():
    """The run path re-checks; a stale tick is not the only thing standing here."""
    tmp = Path(tempfile.mkdtemp())
    at = _app(tmp).run()
    at.text_area(key="claims_text").set_value("claim one").run()

    at.button[0].click().run()  # "Verify claims" without ticking

    assert any("Confirm the transmission first" in e.value for e in at.error), (
        f"expected a refusal, got errors={[e.value for e in at.error]}"
    )


def _run_all() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
    print("failures:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
