"""Render a trial landscape to Markdown and PDF.

The same table machinery as the claim-verification report (see table_render.py),
so the two artefacts read as one product. This one is a wide, scannable table —
one row per trial a patient could enter — rendered landscape in the PDF to give
the columns room. Every row carries the exact eligibility sentence that placed it
there, because a filtered list with no shown evidence cannot be checked.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .biomarker import ELIGIBLE, UNCLEAR
from .landscape import LandscapeTrial, TrialLandscape, _format_location
from .memo import _fmt_date, _inline_to_rl
from .table_render import markdown_table, pdf_table

LANDSCAPE_DISCLAIMER = (
    "Compiled from ClinicalTrials.gov registry records. Eligibility is summarised "
    "from each trial's own text and may be incomplete or out of date; the matched "
    "criterion is shown so it can be checked. This is a research aid, not medical "
    "advice — confirm eligibility and enrolment with the trial's contact before "
    "acting on it."
)

_MATCH_COLOUR = {ELIGIBLE: "#0a7d0a", UNCLEAR: "#b8860b"}

_HEADERS = ["NCT ID", "Title", "Phase", "Status", "Intervention", "Sponsor",
            "Locations", "PI", "Contact", "Eligibility match"]


# ------------------------------------------------------------------ cell formatting


def _interventions(t: LandscapeTrial) -> str:
    names = [i for i in t.record.interventions if i]
    if not names:
        return ""
    head = "; ".join(names[:2])
    return head + (f" (+{len(names) - 2})" if len(names) > 2 else "")


def _locations(t: LandscapeTrial) -> str:
    locs = t.record.locations or []
    if not locs:
        return ""
    nearest = t.nearest_location or locs[0]
    text = _format_location(nearest)
    if t.proximity_label:
        text += f" — {t.proximity_label}"
    if len(locs) > 1:
        text += f" (+{len(locs) - 1} more)"
    return text


def _pi(t: LandscapeTrial) -> str:
    pi = t.record.principal_investigator
    if not pi:
        return ""
    name = pi.get("name", "")
    aff = pi.get("affiliation", "")
    return f"{name} — {aff}" if aff else name


def _contact(t: LandscapeTrial) -> str:
    c = t.contact
    if not c:
        return ""
    bits = [c.get("name", "")]
    reach = c.get("email") or c.get("phone")
    if reach:
        bits.append(reach)
    return " · ".join(b for b in bits if b)


def _evidence(t: LandscapeTrial) -> str:
    """The match status and the exact criterion sentence behind it."""
    sentence = t.match.evidence or "(no matching criterion text)"
    return f"{t.match.status} — {sentence}"


def _row_values(t: LandscapeTrial) -> list[str]:
    r = t.record
    return [
        r.nct_id,
        r.brief_title or "(untitled)",
        r.phase or "—",
        r.overall_status or "—",
        _interventions(t),
        r.lead_sponsor or "—",
        _locations(t),
        _pi(t),
        _contact(t),
        _evidence(t),
    ]


# ------------------------------------------------------------------ Markdown


def _summary_lines(ls: TrialLandscape) -> list[str]:
    return [
        "## Summary",
        "",
        f"- Condition trials screened: {ls.n_condition}",
        f"- Eligible on the biomarker: {ls.n_eligible}",
        f"- Unclear — shown and flagged: {ls.n_unclear}",
        f"- Require the opposite biomarker (not shown): {ls.n_excluded}",
        f"- Biomarker not mentioned (not shown): {ls.n_not_mentioned}",
        "",
    ]


def render_markdown(ls: TrialLandscape, generated: datetime | None = None) -> str:
    lines = [
        f"# Trial landscape — {ls.condition or 'condition not specified'}",
        "",
        f"**Biomarker:** {ls.biomarker or 'not specified'}  ",
        f"**Location:** {ls.location or 'any'}  ",
        f"**Generated:** {_fmt_date(generated)}",
        "",
        "---",
        "",
    ]
    lines += _summary_lines(ls)

    if ls.warnings:
        lines.append("**Warnings**")
        lines.append("")
        for w in ls.warnings:
            lines.append(f"- {w}")
        lines.append("")

    lines.append(
        "> Only trials whose eligibility references the biomarker are listed. A trial "
        "that does not mention it may still accept the patient — broaden to a plain "
        "condition search to see those."
    )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Trials a patient could enter")
    lines.append("")

    if not ls.trials:
        lines.append("_No trials referencing this biomarker were found for this condition._")
        lines.append("")
    else:
        rows = []
        for t in ls.trials:
            vals = _row_values(t)
            # Make the NCT a link in Markdown; keep everything else plain text.
            vals[0] = f"[{t.record.nct_id}]({t.record.url})"
            vals[9] = f"**{t.match.status}** — {t.match.evidence or '(no matching criterion text)'}"
            rows.append(vals)
        lines.extend(markdown_table(_HEADERS, rows))
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"*{LANDSCAPE_DISCLAIMER}*")
    lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------------ PDF


def render_pdf(ls: TrialLandscape, path: str | Path, generated: datetime | None = None) -> Path:
    from reportlab.lib.pagesizes import LETTER, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    base = getSampleStyleSheet()
    styles = {
        "title": ParagraphStyle("t", parent=base["Title"], fontSize=18, spaceAfter=6),
        "sub": ParagraphStyle("s", parent=base["Normal"], fontSize=9,
                              textColor="#555555", spaceAfter=2),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontSize=12,
                             spaceBefore=12, spaceAfter=6),
        "body": ParagraphStyle("b", parent=base["Normal"], fontSize=9, leading=12, spaceAfter=3),
        "cell": ParagraphStyle("c", parent=base["Normal"], fontSize=6.8, leading=8.4),
        "small": ParagraphStyle("sm", parent=base["Normal"], fontSize=7.5, leading=10,
                                textColor="#666666", spaceAfter=4),
    }

    story = [Paragraph(f"Trial landscape — {_inline_to_rl(ls.condition or 'condition not specified')}",
                       styles["title"])]
    for line in (
        f"Biomarker: {ls.biomarker or 'not specified'}",
        f"Location: {ls.location or 'any'}",
        f"Generated: {_fmt_date(generated)}",
    ):
        story.append(Paragraph(_inline_to_rl(line), styles["sub"]))
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", color="#cccccc"))

    story.append(Paragraph("Summary", styles["h2"]))
    story.append(Paragraph(
        _inline_to_rl(
            f"Condition trials screened: {ls.n_condition} · eligible: {ls.n_eligible} · "
            f"unclear (shown): {ls.n_unclear} · require opposite biomarker: {ls.n_excluded} · "
            f"biomarker not mentioned: {ls.n_not_mentioned}"),
        styles["body"]))
    for w in ls.warnings:
        story.append(Paragraph(_inline_to_rl(w), styles["small"]))

    story.append(Paragraph("Trials a patient could enter", styles["h2"]))
    if not ls.trials:
        story.append(Paragraph("No trials referencing this biomarker were found for this "
                               "condition.", styles["body"]))
    else:
        rows, cell_colours = [], []
        for i, t in enumerate(ls.trials, 1):
            cell_colours.append((9, i, _MATCH_COLOUR.get(t.match.status, "#000000")))
            vals = _row_values(t)
            vals[9] = f"<b>{_inline_to_rl(t.match.status)}</b> — " \
                      f"{_inline_to_rl(t.match.evidence or '(no matching criterion text)')}"
            rows.append([v if i2 == 9 else _inline_to_rl(v) for i2, v in enumerate(vals)])
        widths = [0.8, 1.5, 0.42, 0.72, 0.95, 1.02, 1.05, 0.82, 1.02, 1.55]
        story.append(pdf_table(_HEADERS, rows, [w * inch for w in widths], styles["cell"],
                               cell_colours=cell_colours))

    story.append(Spacer(1, 12))
    story.append(Paragraph(_inline_to_rl(LANDSCAPE_DISCLAIMER), styles["small"]))

    SimpleDocTemplate(
        str(path),
        pagesize=landscape(LETTER),
        leftMargin=0.5 * inch, rightMargin=0.5 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        title=f"Trial landscape — {ls.condition}",
        author="MedRAG",
    ).build(story)
    return path


def export(ls: TrialLandscape, out_dir: str | Path, stem: str | None = None) -> dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"{ls.condition}-{ls.biomarker}" if ls.condition else "landscape"
    stem = stem or re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-") or "landscape"

    md_path = out_dir / f"{stem}-landscape.md"
    md_path.write_text(render_markdown(ls), encoding="utf-8")
    pdf_path = render_pdf(ls, out_dir / f"{stem}-landscape.pdf")
    return {"markdown": md_path, "pdf": pdf_path}
