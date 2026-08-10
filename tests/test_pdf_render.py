"""Every PDF renderer, built for real, against content that has broken one.

WHY THIS FILE EXISTS

The suite already built a PDF in three places (test_landscape, test_claims,
test_diligence each render one and check the `%PDF` magic). All three passed
while the trial-landscape PDF was crashing on the first real run, because all
three render short fixture rows: a table only fails this way when a single row
is taller than the page it has to print on, and no fixture row was.

Real registry text is. On the live colorectal store an eligibility criterion
runs to 2,627 characters, which at 6.8pt in a 1.35-inch column is a 772-point
row in a 513-point frame, and reportlab answers that with `LayoutError` — it
aborts the whole document, not the row. So the tests here deliberately feed
each renderer content of the shape that breaks it:

  * a cell far taller than one page,
  * a single unbreakable 500-character token wider than its column,
  * XML-hostile characters,
  * empty cells.

And one property is checked across all three renderers at once rather than
per-file: no renderer may hand `pdf_table` a column budget wider than the frame
it declares. That failure is the silent one — reportlab does not raise on an
over-wide table, it draws the last column off the edge of the paper — so a
future eleventh column has to be caught here, not in a printed PDF.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import netguard  # noqa: E402

netguard.install()
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures.ctgov import LANDSCAPE_PAGE  # noqa: E402

from medrag import claims_memo, landscape_memo  # noqa: E402
from medrag import memo as memo_mod
from medrag.landscape import build_landscape  # noqa: E402
from medrag.table_render import fit_widths  # noqa: E402
from medrag.trials.client import parse_study  # noqa: E402
from medrag.trials.store import TrialStore  # noqa: E402

# A criterion sentence longer than a page can hold, of the shape real registry
# text takes. The live maximum measured on the colorectal store is 2,627
# characters; this is deliberately well past it.
PAGE_BUSTING_CRITERION = (
    "Exclusion Criteria: Known microsatellite instability-high (MSI-H) or mismatch "
    "repair deficient (dMMR) colorectal carcinoma as determined by immunohistochemistry "
    "or polymerase chain reaction based testing at a local laboratory. " * 40
)
UNBREAKABLE_TOKEN = "N" * 500


def _store() -> TrialStore:
    store = TrialStore(Path(tempfile.mkdtemp()) / "trials.db")
    recs = [parse_study(s) for s in LANDSCAPE_PAGE["studies"] if parse_study(s)]
    store.upsert(recs, provenance={r.nct_id: ["cond:colorectal cancer"] for r in recs},
                 set_key="colorectal")
    return store


def _hostile_landscape():
    """A real landscape with one row made pathological in every way at once."""
    ls = build_landscape(_store(), condition="colorectal cancer", biomarker="MSS",
                         query_set="colorectal")
    victim = ls.trials[0]
    victim.match.evidence = PAGE_BUSTING_CRITERION
    victim.record.brief_title = f"A study of {UNBREAKABLE_TOKEN} in Smith & Co <cancer>"
    victim.record.lead_sponsor = ""
    return ls


def _tmp(name: str) -> Path:
    return Path(tempfile.mkdtemp()) / name


# ------------------------------------------------- the reported crash, directly


def test_a_criterion_taller_than_the_page_does_not_abort_the_landscape_pdf():
    """The first-real-run crash. A single row taller than the frame must split
    across pages, never take the whole document down with it."""
    out = _tmp("landscape.pdf")
    landscape_memo.render_pdf(_hostile_landscape(), out)
    assert out.read_bytes().startswith(b"%PDF")


def test_an_uncapped_landscape_of_many_tall_rows_still_renders():
    """The cap is not what makes the PDF safe. Uncapped, with every row carrying
    page-busting text, the document must still build — otherwise raising the cap
    silently re-breaks the export."""
    ls = build_landscape(_store(), condition="colorectal cancer", biomarker="MSS",
                         query_set="colorectal", show_limit=None)
    for t in ls.trials:
        t.match.evidence = PAGE_BUSTING_CRITERION
    out = _tmp("landscape-uncapped.pdf")
    landscape_memo.render_pdf(ls, out)
    assert out.read_bytes().startswith(b"%PDF")


def test_an_unbreakable_token_wider_than_its_column_does_not_abort_the_pdf():
    ls = build_landscape(_store(), condition="colorectal cancer", biomarker="MSS",
                         query_set="colorectal")
    ls.trials[0].record.brief_title = UNBREAKABLE_TOKEN
    out = _tmp("landscape-token.pdf")
    landscape_memo.render_pdf(ls, out)
    assert out.read_bytes().startswith(b"%PDF")


# ------------------------------------------------- the width invariant, swept


def _recording_pdf_table(module, calls):
    """Wrap a renderer module's `pdf_table` name so every call it makes is
    recorded, then delegate. Patching table_render itself would not work: each
    renderer binds the name at import time."""
    real = module.pdf_table

    def spy(headers, rows, col_widths, cell_style, **kw):
        calls.append({
            "module": module.__name__,
            "headers": list(headers),
            "widths": [float(w) for w in col_widths],
            "available_width": kw.get("available_width"),
        })
        return real(headers, rows, col_widths, cell_style, **kw)

    module.pdf_table = spy
    return real


def _census_memo():
    """A diligence memo carrying an AGGREGATE section.

    An ordinary memo run builds no table at all — the diligence PDF's two
    tables live in `_aggregate_pdf`, which only fires for a census section — so
    a sweep driven by a plain memo would cover the diligence renderer
    vacuously. That is what `test_every_renderer_was_actually_exercised_by_the_sweep`
    is guarding, and it caught exactly this when this file was first written.
    """
    from medrag.diligence import DiligenceQuestion, MemoResult  # noqa: PLC0415
    from tests.test_landscape_census import _mixed_store, _runner  # noqa: PLC0415

    runner = _runner(_mixed_store(n_extra=20))
    q = DiligenceQuestion(id="l", section="Landscape", question="What runs in {indication}?",
                          aggregate=True, k=5)
    section = runner.run_question(q, asset="", indication="colorectal cancer")
    return MemoResult(asset="", indication="colorectal cancer",
                      question_set="landscape", sections=[section])


def _render_everything(calls) -> None:
    """Drive all three renderers once, recording every table they build."""
    from tests.test_claims import _report as claims_report  # noqa: PLC0415

    originals = {m: _recording_pdf_table(m, calls)
                 for m in (landscape_memo, claims_memo, memo_mod)}
    try:
        landscape_memo.render_pdf(_hostile_landscape(), _tmp("l.pdf"))
        claims_memo.render_pdf(claims_report(), _tmp("c.pdf"))
        memo_mod.render_pdf(_census_memo(), _tmp("m.pdf"))
    finally:
        for module, real in originals.items():
            module.pdf_table = real


def test_no_renderer_builds_a_table_wider_than_its_own_page():
    """The silent failure: reportlab does not raise on an over-wide table, it
    draws the last column off the edge of the paper. Every renderer must declare
    the frame width it has and stay inside it — checked across all three at once,
    so an eleventh column added to any of them is caught here."""
    calls = []
    _render_everything(calls)
    assert calls, "no PDF table was built — this sweep would pass vacuously"

    for c in calls:
        assert c["available_width"], (
            f"{c['module']} built a table with columns {c['headers']} without "
            "declaring the frame width it has to fit inside"
        )
        # A hair of tolerance for float arithmetic on inch conversions only.
        assert sum(c["widths"]) <= c["available_width"] + 0.01, (
            f"{c['module']}'s table {c['headers']} budgets "
            f"{sum(c['widths']) / 72:.2f}in of columns into a "
            f"{c['available_width'] / 72:.2f}in frame — reportlab will not raise, "
            "it will print the right-hand column off the page"
        )


def test_every_renderer_was_actually_exercised_by_the_sweep():
    """A sweep that silently stops covering a renderer is worse than no sweep."""
    calls = []
    _render_everything(calls)
    assert {c["module"] for c in calls} == {
        "medrag.landscape_memo", "medrag.claims_memo", "medrag.memo",
    }


def test_an_overwide_budget_is_scaled_to_fit_rather_than_printed_off_the_page():
    fitted = fit_widths([5.0, 5.0, 5.0], available_width=9.0)
    assert sum(fitted) == 9.0
    assert fitted[0] == fitted[1] == fitted[2], "scaling must stay proportional"


def test_a_budget_that_already_fits_is_left_exactly_alone():
    widths = [1.0, 2.0, 3.0]
    assert fit_widths(widths, available_width=10.0) == widths
    assert fit_widths(widths, available_width=None) == widths


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
    print("\nall PDF render tests passed" if not failures else f"\n{failures} test(s) failed")
    raise SystemExit(1 if failures else 0)
