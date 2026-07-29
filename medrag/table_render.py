"""Shared table rendering for the memo-style outputs.

The claim-verification table and the patient trial-landscape table are the same
artefact in two guises — a titled table with a coloured status column and one
detail block per row — so they render through one code path rather than two that
drift. Each caller supplies its own headers, rows, column widths and per-cell
colours; the pipe-table (Markdown) and the reportlab Table (PDF) live here.

Rows are lists of already-prepared cell strings. For Markdown that means plain
text (pipes and newlines are escaped here); for PDF that means ReportLab inline
markup (run each cell through memo._inline_to_rl before passing it in).
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


def pdf_table(headers, rows, col_widths, cell_style, *,
              cell_colours=None, header_bg="#f2f4f7"):
    """Build a reportlab Table flowable. `cell_colours` is a list of
    (col, row, hex) overrides, row 0 being the header."""
    from reportlab.lib import colors
    from reportlab.platypus import Paragraph, Table, TableStyle

    data = [[Paragraph(f"<b>{h}</b>", cell_style) for h in headers]]
    for row in rows:
        # An empty cell renders as an em dash rather than a blank a reader might
        # mistake for missing rendering.
        data.append([Paragraph(c if c else "—", cell_style) for c in row])

    table = Table(data, colWidths=col_widths, repeatRows=1)
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
