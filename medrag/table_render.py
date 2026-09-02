"""Shared table rendering for the memo-style outputs.

The claim-verification table and the patient trial-landscape table are the same
artefact in two guises — a titled table with a coloured status column and one
detail block per row — so they render through one code path rather than two that
drift. Each caller supplies its own headers, rows, column widths and per-cell
colours; the pipe-table (Markdown) and the reportlab Table (PDF) live here.

Rows are lists of already-prepared cell strings. For Markdown that means plain
text (pipes and newlines are escaped here); for PDF that means ReportLab inline
markup (run each cell through memo._inline_to_rl before passing it in).

TWO WAYS A TABLE OVERFLOWS THE PAGE, AND WHY BOTH ARE HANDLED HERE

A reportlab Table fails on real data in two unrelated ways, and both are
properties of the table, not of any one memo, so both are fixed once here
rather than three times in three renderers.

  * TOO TALL. A Table splits between rows by default, so a row taller than the
    frame has nowhere to go and reportlab raises `LayoutError` — aborting the
    whole PDF, not just that row. This is not hypothetical: the trial landscape
    prints each trial's verbatim eligibility criterion, and real registry text
    runs to 2,600 characters, which is a 772-point row in a 513-point frame.
    `splitInRow=1` lets reportlab break such a row across a page boundary. The
    alternative — truncating the cell — was rejected: the criterion sentence is
    the evidence the row exists to show, and a landscape whose evidence is
    silently clipped is the same failure as a landscape that silently drops
    trials.

  * TOO WIDE. This one does NOT raise. Reportlab draws an over-wide table
    straight off the right edge of the page, so the failure is silent and the
    reader sees a table whose last column is simply missing. `available_width`
    scales the column budget down to fit instead. Scaling is the backstop, not
    the plan: `tests/test_pdf_render.py` asserts that no renderer hands over
    widths that need it, so a newly added column is caught in CI rather than
    discovered as a cropped PDF.
"""

from __future__ import annotations


def _md_escape(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def markdown_table(headers: list[str], rows: list[list]) -> list[str]:
    """Return the lines of a GitHub-flavoured pipe table."""
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_md_escape(c) for c in row) + " |")
    return lines


def fit_widths(col_widths, available_width: float | None) -> list[float]:
    """Scale a column budget down to the printable width if it overruns it.

    Proportional, so the relative emphasis a renderer chose survives; a budget
    that already fits is returned untouched, so the common case is a no-op.
    """
    widths = [float(w) for w in col_widths]
    if not available_width or not widths:
        return widths
    total = sum(widths)
    if total <= available_width:
        return widths
    scale = available_width / total
    return [w * scale for w in widths]


def pdf_table(headers, rows, col_widths, cell_style, *,
              cell_colours=None, header_bg="#f2f4f7", available_width=None):
    """Build a reportlab Table flowable. `cell_colours` is a list of
    (col, row, hex) overrides, row 0 being the header. `available_width` is the
    frame width the table has to live inside — see the module docstring for why
    both overflow directions are handled here."""
    from reportlab.lib import colors
    from reportlab.platypus import Paragraph, Table, TableStyle

    data = [[Paragraph(f"<b>{h}</b>", cell_style) for h in headers]]
    for row in rows:
        # An empty cell renders as an em dash rather than a blank a reader might
        # mistake for missing rendering.
        data.append([Paragraph(c if c else "—", cell_style) for c in row])

    table = Table(data, colWidths=fit_widths(col_widths, available_width),
                  repeatRows=1, splitInRow=1)
    style = [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(header_bg)),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for col, row, colour in (cell_colours or []):
        style.append(("TEXTCOLOR", (col, row), (col, row), colors.HexColor(colour)))
    table.setStyle(TableStyle(style))
    return table
