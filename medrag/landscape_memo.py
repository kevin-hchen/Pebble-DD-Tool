"""Render a trial landscape to Markdown and PDF.

The same table machinery as the claim-verification report (see table_render.py),
so the two artefacts read as one product. This one is a wide, scannable table —
one row per trial a patient could enter — rendered landscape in the PDF to give
the columns room. Every row carries the exact eligibility sentence that placed it
there, because a filtered list with no shown evidence cannot be checked, and the
score that placed it in the printed sample, because a capped table whose rows
cannot be accounted for is the arbitrary-sample failure `ranking.py` exists to
prevent.

Which rows print, and how many, is decided in `landscape.build_landscape` and
not here — this module renders `ls.trials` and states `ls.sample_lines()`. Both
renderers and the Streamlit page therefore show the same rows by construction
rather than by three surfaces agreeing to apply the same cap.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import coverage
from .biomarker import ELIGIBLE, ELIGIBLE_BY_EXCLUSION, UNCLEAR
from .crypto import harden_outputs, unguessable_stem, write_secure
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

_MATCH_COLOUR = {ELIGIBLE: "#0a7d0a", ELIGIBLE_BY_EXCLUSION: "#0a7d0a", UNCLEAR: "#b8860b"}

_HEADERS = ["NCT ID", "Title", "Phase", "Status", "Intervention", "Sponsor",
            "Locations", "PI", "Contact", "Eligibility match", "Why ranked here"]
_MATCH_COL = 9
_RANK_COL = 10

# Page geometry, stated once — see memo.PAGE_SIZE_IN. Landscape LETTER, so the
# eleven columns have 10 inches to share.
PAGE_SIZE_IN = (11.0, 8.5)          # LETTER, landscape
SIDE_MARGIN_IN = 0.5
AVAILABLE_WIDTH_IN = PAGE_SIZE_IN[0] - 2 * SIDE_MARGIN_IN

# Sums to 9.95in of the 10in budget.
_COL_WIDTHS_IN = [0.75, 1.35, 0.4, 0.7, 0.85, 0.9, 0.95, 0.75, 0.9, 1.35, 1.05]


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


def _why_ranked(t: LandscapeTrial) -> str:
    return t.ranking.explain() if t.ranking else "not ranked"


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
        _why_ranked(t),
    ]


# ------------------------------------------------------------------ Markdown


def _summary_lines(ls: TrialLandscape) -> list[str]:
    return [
        "## Summary",
        "",
        f"- Condition trials screened: {ls.n_condition}",
        f"- Eligible on the biomarker: {ls.n_eligible}",
        f"- Eligible by exclusion of the opposite biomarker: {ls.n_eligible_by_exclusion}",
        f"- Unclear — flagged, ranked alongside the rest: {ls.n_unclear}",
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
    if ls.coverage_statement is not None:
        lines.append("**Coverage**")
        lines.append("")
        for line in coverage.render_lines(ls.coverage_statement):
            lines.append(f"> {line}")
        lines.append("")
        lines.append("---")
        lines.append("")
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
        for line in ls.sample_lines():
            lines.append(f"> {line}")
            lines.append(">")
        lines.append("")
        rows = []
        for t in ls.trials:
            vals = _row_values(t)
            # Make the NCT a link in Markdown; keep everything else plain text.
            vals[0] = f"[{t.record.nct_id}]({t.record.url})"
            vals[_MATCH_COL] = (
                f"**{t.match.status}** — "
                f"{t.match.evidence or '(no matching criterion text)'}")
            rows.append(vals)
        lines.extend(markdown_table(_HEADERS, rows))
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"*{LANDSCAPE_DISCLAIMER}*")
    lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------------ PDF


def render_pdf(ls: TrialLandscape, target, generated: datetime | None = None):
    """Render to a path, or to any binary stream.

    A stream target exists for the public service, which must not write to the
    filesystem on a request path: it hands in a BytesIO and streams the bytes to
    the browser. reportlab accepts a file-like object for its filename argument,
    so this costs one branch — and the branch is the `mkdir`, which is precisely
    the part that would fail on a read-only volume.

    Returns the Path when given one (unchanged for every existing caller), and
    the stream when given a stream.
    """
    from reportlab.lib.pagesizes import LETTER, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

    to_stream = hasattr(target, "write")
    path = target if to_stream else Path(target)
    if not to_stream:
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
        "note": ParagraphStyle("n", parent=base["Normal"], fontSize=8.5, leading=11,
                               leftIndent=10, textColor="#444444", spaceAfter=5),
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

    if ls.coverage_statement is not None:
        story.append(Paragraph("Coverage", styles["h2"]))
        for line in coverage.render_lines(ls.coverage_statement):
            story.append(Paragraph(_inline_to_rl(line), styles["body"]))
        story.append(Spacer(1, 6))
        story.append(HRFlowable(width="100%", color="#cccccc"))

    story.append(Paragraph("Summary", styles["h2"]))
    story.append(Paragraph(
        _inline_to_rl(
            f"Condition trials screened: {ls.n_condition} · eligible: {ls.n_eligible} · "
            f"eligible by exclusion: {ls.n_eligible_by_exclusion} · "
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
        for line in ls.sample_lines():
            story.append(Paragraph(_inline_to_rl(line), styles["note"]))
        rows, cell_colours = [], []
        for i, t in enumerate(ls.trials, 1):
            cell_colours.append((_MATCH_COL, i, _MATCH_COLOUR.get(t.match.status, "#000000")))
            vals = _row_values(t)
            vals[_MATCH_COL] = f"<b>{_inline_to_rl(t.match.status)}</b> — " \
                               f"{_inline_to_rl(t.match.evidence or '(no matching criterion text)')}"
            rows.append([v if i2 == _MATCH_COL else _inline_to_rl(v)
                         for i2, v in enumerate(vals)])
        story.append(pdf_table(_HEADERS, rows, [w * inch for w in _COL_WIDTHS_IN],
                               styles["cell"], cell_colours=cell_colours,
                               available_width=AVAILABLE_WIDTH_IN * inch))

    story.append(Spacer(1, 12))
    story.append(Paragraph(_inline_to_rl(LANDSCAPE_DISCLAIMER), styles["small"]))

    SimpleDocTemplate(
        path if to_stream else str(path),
        pagesize=landscape(LETTER),
        leftMargin=SIDE_MARGIN_IN * inch, rightMargin=SIDE_MARGIN_IN * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        title=f"Trial landscape — {ls.condition}",
        author="MedRAG",
    ).build(story)
    return path


def export(
    ls: TrialLandscape,
    out_dir: str | Path,
    stem: str | None = None,
    passphrase: str | None = None,
) -> dict[str, Path]:
    """Markdown encrypted when a passphrase is configured, PDF plaintext at 0600.

    Landscape output is public registry data, so the encryption buys least here.
    It follows the same rule as the other two anyway: one export path that behaves
    differently per module is how the memo renderers drifted before.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"{ls.condition}-{ls.biomarker}" if ls.condition else "landscape"
    stem = stem or unguessable_stem(base, "landscape")

    md_path = out_dir / f"{stem}-landscape.md"
    write_secure(md_path, render_markdown(ls).encode("utf-8"), passphrase)
    pdf_path = render_pdf(ls, out_dir / f"{stem}-landscape.pdf")
    harden_outputs(out_dir, md_path, pdf_path)
    return {"markdown": md_path, "pdf": pdf_path}
