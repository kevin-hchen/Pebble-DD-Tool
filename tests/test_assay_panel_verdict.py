"""`_ASSAY_PANEL`'s motivating case, asserted as a VERDICT.

The existing protection for this case lives in tests/test_census_live_parity.py,
which drives the same text through the ingest-time census and the query-time
live screen and asserts they AGREE. That is the right check for the prefilter,
and it is the wrong check for this bug: `_ASSAY_PANEL` sits inside
`markers.collect_signals`, which both paths call. A regression there moves both
answers together, they still agree, and parity passes while the verdict is
wrong. A guard that cannot fail on the defect it was written for is the same
shape as an import-closure fingerprint check — it looks like a guard.

So this file states the answer rather than the agreement.

THE CASE. NCT05619172's inclusion criterion reads:

    "RAS wild type as confirmed by: locally performed ctDNA assessment
     including at least mutations in exon 2 (G12D, G12V, G12C, G12S, G12A,
     G12R, G13D) and exon 3"

`\\bG12C\\b` matched inside that panel listing, so the census recorded
KRAS_G12C: REQUIRED — for a trial whose actual requirement is RAS WILD TYPE, the
opposite. A patient searching KRAS G12C was shown a trial that excludes them,
with the panel sentence printed as the evidence.

Two things are asserted, because either alone is satisfiable by a broken
matcher: the panel must not produce REQUIRED, AND a single-variant requirement
must still produce one. A filter narrowed until it stops firing passes the first
and fails the second; a filter widened until it eats everything passes the
second and fails the first.

Run: python -m pytest tests/test_assay_panel_verdict.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Direct runs do not load conftest.py, so the no-network guard is installed
# here too. See tests/netguard.py.
from tests import netguard  # noqa: E402

netguard.install()

from medrag.biomarker_gating import gate_markers  # noqa: E402

#: NCT05619172's real inclusion criterion, verbatim.
PANEL = (
    "Inclusion Criteria:\n"
    "* RAS wild type as confirmed by: locally performed ctDNA assessment "
    "including at least mutations in exon 2 (G12D, G12V, G12C, G12S, G12A, "
    "G12R, G13D) and exon 3"
)

#: Real single-variant requirements that must SURVIVE the panel rule —
#: NCT06599502 and NCT04585035. They name one variant and assert it.
SINGLE_VARIANT = (
    ("Inclusion Criteria:\n* Documented KRASG12D mutation in tissue or liquid biopsy.",
     "KRAS_G12D"),
    ("Inclusion Criteria:\n* Subject has KRasG12C mutation in tumor tissue.",
     "KRAS_G12C"),
)


def test_an_assay_panel_listing_never_reads_as_requiring_the_variant():
    """The defect, stated as the verdict it produced.

    Not "the two paths agree" — this asserts KRAS_G12C is not REQUIRED for a
    trial that requires RAS wild type, which is the claim a patient acts on.
    """
    flags = gate_markers(PANEL)
    for variant in ("KRAS_G12C", "KRAS_G12D"):
        assert flags[variant].status != "REQUIRED", (
            f"{variant} reads REQUIRED for NCT05619172, whose criterion is RAS "
            f"WILD TYPE — the opposite. A patient searching {variant} would be "
            "shown a trial that excludes them, with the assay-panel sentence "
            "printed as the evidence. This is the bug _ASSAY_PANEL was added "
            "for; see medrag/markers.py."
        )


def test_the_panel_rule_also_consumes_the_direction_the_sentence_states():
    """A KNOWN RESIDUAL, pinned as the current behaviour rather than the desired
    one — so it is visible, and so a later fix has to change this test on purpose.

    "RAS wild type" is a genuine direction: RAS must be ABSENT. The panel rule
    consumes the whole sentence, so on this criterion ALONE, RAS reads
    NOT_ASSESSABLE rather than EXCLUDED. The real trial survives it only because
    its full eligibility text carries other RAS sentences — the stored census
    has RAS: EXCLUDED for NCT05619172 — so nothing is currently wrong for a
    reader. A trial stating its direction ONLY in a panel sentence would go
    silent on the marker it gates on.

    This is the same residual recorded for NCT05619172 in
    tests/fixtures/not_assessable_handread.json: the direction and the panel sit
    in different sentence units, so no narrowing of the filter reaches it.
    Resolving it needs the criteria-segmentation work, not a filter change.
    """
    flags = gate_markers(PANEL)
    assert flags["RAS"].status == "NOT_ASSESSABLE", (
        f"RAS now reads {flags['RAS'].status!r} on the isolated panel criterion. "
        "If this is EXCLUDED, the residual is fixed and this test should be "
        "updated to assert that in the same commit."
    )


def test_a_single_variant_requirement_is_still_required():
    """The negative control. Every assertion above is satisfied by a matcher
    that has stopped recognising variants at all."""
    for text, variant in SINGLE_VARIANT:
        flags = gate_markers(text)
        assert flags[variant].status == "REQUIRED", (
            f"{variant} reads {flags[variant].status!r} for {text.strip()!r}. "
            "This names one variant and asserts it — a real requirement. If the "
            "panel rule has widened far enough to eat it, the filter is now "
            "suppressing the evidence it exists to protect."
        )


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
    print("\nall assay-panel verdict checks passed" if not failures
          else f"\n{failures} failure(s)")
    raise SystemExit(1 if failures else 0)
