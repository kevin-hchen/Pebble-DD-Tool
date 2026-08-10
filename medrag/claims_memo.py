"""Export a claim-verification result to Markdown and PDF.

Same house style as the diligence memo, same rule that every verdict is
traceable to a cited PMID or NCT, so the two artefacts read as one product. The
result is a table an analyst can drop into a memo: one row per claim, with
support and independence as two separate columns — a claim can be well supported
and entirely company-sourced, and both facts have to be visible without
expanding anything.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .claims import (
    COMPANY_LINKED,
    CONTRADICTED,
    INDEP_NA,
    INDEPENDENT,
    MIXED,
    NO_DISCLOSURE,
    NOT_FOUND,
    NOT_VERIFIABLE,
    PARTIAL,
    SUPPORT_VALUES,
    SUPPORTED,
    UNVERIFIED,
    ClaimReport,
    ClaimVerdict,
)
from .context import TRIAL_LABEL
from .crypto import harden_outputs, write_secure
from .memo import DISCLAIMER, _fmt_date, _inline_to_rl
from .table_render import markdown_table, pdf_table

# Page geometry, stated once — see memo.PAGE_SIZE_IN for why the doc template
# and the column budget must read the same numbers.
PAGE_SIZE_IN = (8.5, 11.0)          # LETTER portrait
SIDE_MARGIN_IN = 0.6
AVAILABLE_WIDTH_IN = PAGE_SIZE_IN[0] - 2 * SIDE_MARGIN_IN

# Support value -> colour for its cell. Text always names it too, so colour is
# never the only signal.
_SUPPORT_COLOUR = {
    SUPPORTED: "#0a7d0a",
    PARTIAL: "#b8860b",
    CONTRADICTED: "#b00020",
    NOT_FOUND: "#555555",
    NOT_VERIFIABLE: "#777777",
    UNVERIFIED: "#555555",
}

_INDEP_COLOUR = {
    COMPANY_LINKED: "#b00020",
    MIXED: "#b8860b",
    NO_DISCLOSURE: "#8a6d3b",   # amber-brown: unverified, not a clean pass
    INDEPENDENT: "#0a7d0a",
    INDEP_NA: "#999999",
}


def _citation_refs(v: ClaimVerdict) -> str:
    """The identifiers behind a verdict, marked when they are company sources."""
    if not v.cited_evidence:
        return "—"
    parts = []
    for e in v.cited_evidence:
        mark = " (company)" if e.index in v.company_sources else ""
        parts.append(f"`{e.identifier}` [{e.index}]{mark}")
    return ", ".join(parts)


def _figure_note(v: ClaimVerdict) -> str:
    if not v.claim_figures:
        return ""
    claimed = ", ".join(v.claim_figures)
    found = ", ".join(v.source_figures) or "no matching figure"
    return f"Claimed figure {claimed}; cited evidence shows {found}."


def _support_summary(report: ClaimReport) -> list[tuple[str, int]]:
    c = report.support_counts()
    return [(v, c.get(v, 0)) for v in SUPPORT_VALUES]


def _independence_summary(report: ClaimReport) -> list[tuple[str, int]]:
    c = report.independence_counts()
    # Concerning-first: company-linked, then the mixed and no-disclosure middle,
    # then the rare positive independence, then the not-applicable tail.
    order = [COMPANY_LINKED, MIXED, NO_DISCLOSURE, INDEPENDENT, INDEP_NA]
    return [(v, c.get(v, 0)) for v in order]


# --------------------------------------------------------------------- Markdown


def render_markdown(report: ClaimReport, generated: datetime | None = None) -> str:
    heading = report.company or report.asset or "claims"
    lines = [
        f"# Claim verification — {heading}",
        "",
        f"**Asset:** {report.asset or 'not specified'}  ",
        f"**Indication:** {report.indication or 'not specified'}  ",
        f"**Company:** {report.company or 'not specified'}  ",
        f"**Generated:** {_fmt_date(generated)}  ",
        f"**Model:** {report.model} · **Embeddings:** {report.embedder}",
        "",
        "---",
        "",
        "## Summary",
        "",
        "**Support** — does the evidence back the claim?",
        "",
    ]
    for verdict, n in _support_summary(report):
        lines.append(f"- {verdict}: {n}")
    lines.append("")
    lines.append("**Independence** — whose evidence is it? (of supported claims)")
    lines.append("")
    for label, n in _independence_summary(report):
        lines.append(f"- {label}: {n}")
    lines.append("")

    nv = report.support_counts().get(NOT_VERIFIABLE, 0)
    if nv:
        lines.append(
            f"> {nv} claim(s) make no specific, checkable assertion and were not "
            "verified. That is a finding, not an omission — they are listed below so "
            "they can be rewritten or dropped."
        )
        lines.append("")
    if any(v.support == UNVERIFIED for v in report.verdicts):
        lines.append(
            "> Some claims are marked UNVERIFIED: evidence was retrieved but no model "
            "was available to judge it. That is not the same as NOT FOUND — it means "
            "nothing checked the claim, not that nothing exists."
        )
        lines.append("")

    if report.warnings:
        lines.append("**Warnings**")
        lines.append("")
        for w in report.warnings:
            lines.append(f"- {w}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Claims")
    lines.append("")
    table_rows = [
        [str(i), v.claim, f"**{v.support}**", v.independence_display(), _citation_refs(v)]
        for i, v in enumerate(report.verdicts, 1)
    ]
    lines.extend(markdown_table(["#", "Claim", "Support", "Independence", "Sources"], table_rows))
    lines.append("")

    # Per-claim detail: rationale, figure mismatch, and the cited sources in full
    # so a verdict can be audited without leaving the document.
    for i, v in enumerate(report.verdicts, 1):
        lines.append(f"### {i}. {v.support} · {v.independence_display()}")
        lines.append("")
        lines.append(f"*{v.claim}*")
        lines.append("")
        if v.rationale:
            lines.append(v.rationale)
            lines.append("")
        note = v.note
        fig = _figure_note(v)
        if fig:
            note = f"{note} {fig}".strip()
        if note:
            lines.append(f"> {note}")
            lines.append("")
        if v.cited_evidence:
            lines.append("<details><summary>Cited evidence</summary>")
            lines.append("")
            for e in v.cited_evidence:
                mark = " — **company source**" if e.index in v.company_sources else ""
                lines.append(f"- [{e.index}] {e.bib_line()}{mark}")
            lines.append("")
            lines.append("</details>")
            lines.append("")
        lines.append("---")
        lines.append("")

    lines.append("## Full source list")
    lines.append("")
    seen: set[str] = set()
    for e in report.all_evidence:
        if e.identifier in seen:
            continue
        seen.add(e.identifier)
        kind = "trial" if e.kind == TRIAL_LABEL else "paper"
        tag = f" [{e.grade_tag}]" if e.grade_tag else ""
        title = e.title or "(untitled)"
        url = f" — {e.url}" if e.url else ""
        lines.append(f"- `{e.identifier}` ({kind}){tag} {title}{url}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"*{DISCLAIMER}*")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------- PDF


def render_pdf(report: ClaimReport, path: str | Path, generated: datetime | None = None) -> Path:
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    base = getSampleStyleSheet()
    styles = {
        "title": ParagraphStyle("t", parent=base["Title"], fontSize=20, spaceAfter=6),
        "sub": ParagraphStyle("s", parent=base["Normal"], fontSize=9,
                              textColor="#555555", spaceAfter=2),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontSize=13,
                             spaceBefore=14, spaceAfter=6),
        "body": ParagraphStyle("b", parent=base["Normal"], fontSize=10, leading=14,
                               alignment=TA_LEFT, spaceAfter=6),
        "cell": ParagraphStyle("c", parent=base["Normal"], fontSize=8.5, leading=11),
        "q": ParagraphStyle("q", parent=base["Normal"], fontSize=10, leading=13,
                            textColor="#333333", leftIndent=10, spaceAfter=8,
                            fontName="Helvetica-Oblique"),
        "small": ParagraphStyle("sm", parent=base["Normal"], fontSize=8, leading=11,
                                textColor="#666666", spaceAfter=4),
        "note": ParagraphStyle("n", parent=base["Normal"], fontSize=9, leading=12,
                               leftIndent=12, textColor="#444444", spaceAfter=8),
    }

    story = []
    heading = report.company or report.asset or "claims"
    story.append(Paragraph(f"Claim verification — {_inline_to_rl(heading)}", styles["title"]))
    for line in (
        f"Asset: {report.asset or 'not specified'}",
        f"Indication: {report.indication or 'not specified'}",
        f"Company: {report.company or 'not specified'}",
        f"Generated: {_fmt_date(generated)}",
        f"Model: {report.model} · Embeddings: {report.embedder}",
    ):
        story.append(Paragraph(_inline_to_rl(line), styles["sub"]))
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", color="#cccccc"))

    story.append(Paragraph("Summary", styles["h2"]))
    support_line = " · ".join(f"{v}: {n}" for v, n in _support_summary(report))
    indep_line = " · ".join(f"{v}: {n}" for v, n in _independence_summary(report))
    story.append(Paragraph(f"<b>Support</b> — {_inline_to_rl(support_line)}", styles["body"]))
    story.append(Paragraph(f"<b>Independence</b> — {_inline_to_rl(indep_line)}", styles["body"]))

    nv = report.support_counts().get(NOT_VERIFIABLE, 0)
    if nv:
        story.append(Paragraph(
            _inline_to_rl(f"{nv} claim(s) make no checkable assertion and were not verified — "
                          "a finding, listed below."), styles["note"]))
    if report.warnings:
        story.append(Paragraph("Warnings", styles["h2"]))
        for w in report.warnings:
            story.append(Paragraph(_inline_to_rl(w), styles["body"]))

    # The result table: support and independence are separate columns. Rendered
    # through the shared table helper the trial landscape also uses.
    story.append(Paragraph("Claims", styles["h2"]))
    rows, cell_colours = [], []
    for i, v in enumerate(report.verdicts, 1):
        cell_colours.append((2, i, _SUPPORT_COLOUR.get(v.support, "#000000")))
        cell_colours.append((3, i, _INDEP_COLOUR.get(v.independence, "#000000")))
        rows.append([
            str(i),
            _inline_to_rl(v.claim),
            f"<b>{_inline_to_rl(v.support)}</b>",
            _inline_to_rl(v.independence_display()),
            _inline_to_rl(_citation_refs(v)),
        ])
    story.append(pdf_table(
        ["#", "Claim", "Support", "Independence", "Sources"], rows,
        [0.25 * inch, 2.7 * inch, 1.35 * inch, 1.3 * inch, 1.5 * inch],
        styles["cell"], cell_colours=cell_colours,
        available_width=AVAILABLE_WIDTH_IN * inch,
    ))
    story.append(Spacer(1, 10))

    # Per-claim detail.
    for i, v in enumerate(report.verdicts, 1):
        story.append(Paragraph(f"{i}. {_inline_to_rl(v.support)} · "
                               f"{_inline_to_rl(v.independence_display())}", styles["h2"]))
        story.append(Paragraph(_inline_to_rl(v.claim), styles["q"]))
        if v.rationale:
            story.append(Paragraph(_inline_to_rl(v.rationale), styles["body"]))
        note = v.note
        fig = _figure_note(v)
        if fig:
            note = f"{note} {fig}".strip()
        if note:
            story.append(Paragraph(_inline_to_rl(note), styles["note"]))
        for e in v.cited_evidence:
            mark = " — company source" if e.index in v.company_sources else ""
            story.append(Paragraph(_inline_to_rl(f"[{e.index}] {e.bib_line()}{mark}"), styles["small"]))
        story.append(HRFlowable(width="100%", color="#dddddd"))

    story.append(Spacer(1, 14))
    story.append(Paragraph(_inline_to_rl(DISCLAIMER), styles["small"]))

    SimpleDocTemplate(
        str(path),
        pagesize=LETTER,
        leftMargin=SIDE_MARGIN_IN * inch,
        rightMargin=SIDE_MARGIN_IN * inch,
        topMargin=0.9 * inch,
        bottomMargin=0.9 * inch,
        title=f"Claim verification — {heading}",
        author="MedRAG",
    ).build(story)
    return path


def export(
    report: ClaimReport,
    out_dir: str | Path,
    stem: str | None = None,
    passphrase: str | None = None,
) -> dict[str, Path]:
    """Markdown encrypted when a passphrase is configured, PDF plaintext at 0600.

    This is the export that carries deck-derived claims, so it is the one the
    split matters most for. See `memo.export` for why the PDF is treated
    differently.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = report.company or report.asset or "claims"
    stem = stem or re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-") or "claims"

    md_path = out_dir / f"{stem}-claims.md"
    write_secure(md_path, render_markdown(report).encode("utf-8"), passphrase)
    pdf_path = render_pdf(report, out_dir / f"{stem}-claims.pdf")
    harden_outputs(out_dir, md_path, pdf_path)
    return {"markdown": md_path, "pdf": pdf_path}
