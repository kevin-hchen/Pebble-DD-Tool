"""The self-contradicting-caveat lint, and every shipped caveat run through it.

Three caveats in this codebase have contained the claim they were denying. Each
was caught by hand when a forbidden-phrase test fired on the tool's own
disclaimer. This file turns that into a check that runs.

The three historical regressions are pinned as cases the lint MUST catch — if a
future edit to CLAIM_PHRASES stops catching them, that is a silent weakening of
the check and these tests fail.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import netguard  # noqa: E402

netguard.install()

from medrag.phrasing import audit, audit_text, report  # noqa: E402

# The three real regressions, verbatim as they shipped before being fixed.
HISTORICAL = (
    ("drug_store.ApprovalAnswer.statement",
     "This is NOT a finding that the asset is unapproved: it is consistent with "
     "4 different situations.",
     "approval"),
    ("drugs.ABSENCE_MEANINGS[1]",
     "submitted but not approved (or still under review)",
     "approval"),
    ("faers.WHAT_THIS_IS_NOT[0]",
     "so these counts are not rates, not incidences, not frequencies and not risks",
     "epidemiology"),
)


def test_the_lint_catches_all_three_historical_regressions():
    """If it stops catching these, the check has been weakened."""
    for where, text, domain in HISTORICAL:
        findings = audit_text(text, where)
        assert findings, f"the lint no longer catches the {where} regression"
        assert any(f.domain == domain for f in findings), (
            f"{where} should trip the {domain} group, got "
            f"{[f.domain for f in findings]}"
        )


def test_a_finding_names_the_place_the_phrase_and_the_fix():
    findings = audit_text(HISTORICAL[0][1], "somewhere.CAVEAT")
    message = str(findings[0])
    assert "somewhere.CAVEAT" in message
    assert "is unapproved" in message
    assert "Reword" in message


def test_the_fixed_wording_of_each_regression_passes():
    """The replacements actually shipped, which is what makes the lint useful
    rather than merely strict."""
    for fixed in (
        "Absence from this database says nothing either way about approval status",
        "submitted and still under review, or refused",
        "these counts cannot say how often an event happens, how likely it is, or "
        "what share of patients experienced it",
    ):
        assert not audit_text(fixed, "fixed"), f"a shipped fix now trips the lint: {fixed}"


def test_audit_indexes_into_a_tuple_so_a_finding_points_at_one_element():
    findings = audit(("clean text", "the asset is unapproved"), "MOD.CAVEATS")
    assert findings and findings[0].where == "MOD.CAVEATS[1]"


def test_domains_can_be_narrowed():
    text = "the incidence of harm"
    assert audit_text(text, "x", domains=("approval",)) == []
    assert audit_text(text, "x", domains=("epidemiology",))


# ------------------------------------------------- every shipped caveat


def _shipped_caveats():
    """Every fixed caveat string this tool asserts in its own voice."""
    from medrag.fda import (
        device_answer,
        drug_store,
        drugs,
        faers,
        orangebook,
        pma,
        purplebook,
    )

    return [
        (purplebook.LIMITS, "purplebook.LIMITS", ("protection",)),
        (purplebook.NOT_SEARCHED, "purplebook.NOT_SEARCHED", ("protection",)),
        (purplebook.NO_BIOSIMILARS_NOTE, "purplebook.NO_BIOSIMILARS_NOTE",
         ("protection",)),
        (purplebook.BIOSIMILAR_IS_NOT_GENERIC, "purplebook.BIOSIMILAR_IS_NOT_GENERIC",
         ("protection",)),
        (purplebook.NOT_APPLICABLE_SMALL_MOLECULE,
         "purplebook.NOT_APPLICABLE_SMALL_MOLECULE", ("protection", "approval")),
        (purplebook.NOT_APPLICABLE_NO_LICENCE,
         "purplebook.NOT_APPLICABLE_NO_LICENCE", ("protection", "approval")),
        (orangebook.LIMITS, "orangebook.LIMITS", ("protection",)),
        (orangebook.NOT_SEARCHED, "orangebook.NOT_SEARCHED", ("protection",)),
        (orangebook.NOT_APPLICABLE_NO_APPROVAL,
         "orangebook.NOT_APPLICABLE_NO_APPROVAL", ("protection", "approval")),
        (orangebook.NOT_APPLICABLE_BIOLOGIC,
         "orangebook.NOT_APPLICABLE_BIOLOGIC", ("protection", "approval")),
        (drugs.ABSENCE_MEANINGS, "drugs.ABSENCE_MEANINGS", ("approval", "protection")),
        (drug_store.NOT_SEARCHED, "drug_store.NOT_SEARCHED", None),
        (drug_store.NOT_SEARCHED_CAVEAT, "drug_store.NOT_SEARCHED_CAVEAT", None),
        (pma.PMA_ABSENCE_MEANINGS, "pma.PMA_ABSENCE_MEANINGS",
         ("approval", "clearance", "protection")),
        (device_answer.NOT_SEARCHED, "device_answer.NOT_SEARCHED", None),
        (device_answer.NOT_SEARCHED_CAVEAT, "device_answer.NOT_SEARCHED_CAVEAT", None),
        (faers.WHAT_THIS_IS, "faers.WHAT_THIS_IS", ("epidemiology", "safety")),
        (faers.WHAT_THIS_IS_NOT, "faers.WHAT_THIS_IS_NOT", ("epidemiology", "safety")),
        (faers.FAERS_ABSENCE_MEANINGS, "faers.FAERS_ABSENCE_MEANINGS",
         ("epidemiology", "safety")),
        (faers.NOT_SEARCHED, "faers.NOT_SEARCHED", None),
    ]


def test_no_shipped_caveat_contains_the_claim_it_denies():
    """The lint, run over the tool's own voice. This is the check that replaces
    catching the fourth one by hand."""
    findings = []
    for caveats, where, domains in _shipped_caveats():
        findings.extend(audit(caveats, where, domains))
    assert not findings, "\n" + report(findings)


def test_the_sweep_covers_every_guard_module_that_ships_caveats():
    """A lint that silently stops covering a module is worse than no lint — the
    same companion check tests/test_pdf_render.py carries.

    Discovered rather than listed: any module under medrag/fda that defines a
    caveat-shaped constant must appear in the sweep. Orange Book was added with
    its own `protection` claim group and was NOT wired in, so an injected
    "no patents" caveat passed the lint — caught by deliberately breaking it.
    """
    import pkgutil

    from medrag import fda

    covered = {where.split(".")[0] for _, where, _ in _shipped_caveats()}
    caveat_names = ("LIMITS", "NOT_SEARCHED", "ABSENCE_MEANINGS", "WHAT_THIS_IS_NOT")
    for mod in pkgutil.iter_modules(fda.__path__):
        module = __import__(f"medrag.fda.{mod.name}", fromlist=["x"])
        ships = [n for n in dir(module)
                 if any(n.endswith(c) for c in caveat_names) and isinstance(
                     getattr(module, n), (str, tuple, list))]
        if ships:
            assert mod.name in covered, (
                f"medrag/fda/{mod.name}.py ships caveat constants {ships} but is not "
                "in the phrasing sweep — wire it in, or the lint passes vacuously "
                "for that module"
            )


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
    print("\nall phrasing tests passed" if not failures else f"\n{failures} test(s) failed")
    raise SystemExit(1 if failures else 0)
