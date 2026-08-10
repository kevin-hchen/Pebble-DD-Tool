"""The one place the app's look is defined.

Every page calls `apply()` first and nothing else styles anything. Read the next
three paragraphs before adding a rule anywhere else.

**Why this file forces colours instead of configuring them.** The theme used to
live in `.streamlit/config.toml`, and each page then patched the rest with its own
copied `<style>` block. That arrangement had one fatal property: the page
*background* was hard-coded to white in CSS while every widget colour was left to
whatever theme Streamlit resolved at runtime. Those two can disagree. When the
config theme is not in effect and the browser reports a dark colour scheme,
Streamlit paints its dark widget set — label text #FAFAFA, input fill #262730,
primary #FF4B4B — onto a CSS-forced white page. Labels land at 1.04:1 against
white (invisible), text inputs become near-black slabs, and the accent turns
coral. Only `app.py` carried a `label { color: ... }` rule, which is the sole
reason the memo page looked right while the other two did not.

So every widget colour here is stated explicitly and defensively, on the widget
element, with `!important`, and `color-scheme: light` pins native controls. There
is no runtime condition under which the surface and its text are chosen by
different authorities. `.streamlit/config.toml` still mirrors these values, but
only so the very first paint is not a flash of the wrong theme; nothing here
depends on it being read.

**Contrast is computed, not eyeballed.** Every ratio below is WCAG 2.1 against the
real background, and the lowest text pair in the system is 4.90:1. Two values
from the original palette were adjusted because they failed:
`#D6D9DE` is 1.42:1 on white — fine as a divider, far below the 3:1 that WCAG
1.4.11 asks of a control boundary, so input borders use `$field_border` instead;
and amber `#B45309` is 4.05:1 on sand, so it was darkened to `#9F4A07`.

**Meaning never rests on hue.** `MARKERS` gives every verdict a geometric shape as
well as its text label, so the support/independence and biomarker columns survive
greyscale printing and colourblind readers. Shapes are Unicode geometry, not
emoji.
"""

from __future__ import annotations

import html
from string import Template

import streamlit as st

# --------------------------------------------------------------- palette
# Ratios are WCAG 2.1, computed against the surface each token actually sits on.
# Text needs 4.5:1 (1.4.3); a control boundary needs 3:1 (1.4.11).

WHITE = "#FFFFFF"          # page background
SAND = "#EDE6DA"           # secondary surfaces, table header rows
INK = "#14181F"            # body text            17.79:1 white / 14.35:1 sand
NAVY = "#163A5F"           # actions, wordmark, section rules  11.64:1 / 9.38:1
NAVY_DEEP = "#0F2942"      # button hover
MUTED = "#4F5763"          # captions, help text   7.30:1 white / 5.89:1 sand
PLACEHOLDER = "#57606E"    # input placeholders    6.36:1 white
FIELD_BORDER = "#767E8C"   # input borders         4.09:1 white / 3.30:1 sand
RULE = "#D6D9DE"           # dividers only — decorative, 1.42:1, never text
AMBER = "#9F4A07"          # caution               6.08:1 white / 4.90:1 sand
RED = "#B42318"            # failure               6.57:1 white / 5.30:1 sand
DISABLED_FILL = "#EFF1F4"  # disabled control fill, with MUTED label at 6.45:1
SAND_EDGE = "#DBD1BF"      # edge of a sand block — decorative

_FONT_SANS = ('-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, '
              '"Helvetica Neue", Arial, sans-serif')
_FONT_MONO = 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace'

# --------------------------------------------------------------- status shapes
# A verdict must be readable with no colour at all, so each one gets a shape as
# well as its label. Filled = affirmative, half = qualified, triangle = adverse,
# hollow = absence of a finding, diamond = outside what can be checked.
FILLED, HALF, ADVERSE, HOLLOW, DIAMOND, NEUTRAL = "●", "◐", "▲", "○", "◇", "–"
PENDING = "◌"   # dotted: retrieved but never judged, distinct from "nothing found"

MARKERS = {
    # claim support
    "SUPPORTED": (FILLED, NAVY),
    "PARTIALLY SUPPORTED": (HALF, AMBER),
    "CONTRADICTED": (ADVERSE, RED),
    "NOT FOUND": (HOLLOW, MUTED),
    "NOT VERIFIABLE": (DIAMOND, MUTED),
    "UNVERIFIED": (PENDING, MUTED),
    # independence
    "INDEPENDENT": (FILLED, NAVY),
    "MIXED": (HALF, AMBER),
    "COMPANY-LINKED": (ADVERSE, RED),
    "COMPANY ONLY": (ADVERSE, RED),
    "NO DISCLOSURE": (HOLLOW, MUTED),
    "N/A": (NEUTRAL, MUTED),
    # biomarker eligibility. ELIGIBLE BY EXCLUSION is a distinct, weaker-
    # confidence state: the trial names only the opposite marker, excluded, not
    # this one directly. Same shape as UNCLEAR (half-filled — not the strongest
    # form) but navy, not amber: the direction is not in doubt, only the
    # directness of the statement.
    "ELIGIBLE BY EXCLUSION": (HALF, NAVY),
    "ELIGIBLE": (FILLED, NAVY),
    "UNCLEAR": (HALF, AMBER),
    "EXCLUDED": (ADVERSE, RED),
    "NOT MENTIONED": (HOLLOW, MUTED),
}

# Badge kinds map onto the same shape vocabulary.
_BADGE_SHAPES = {
    "good": (FILLED, NAVY),
    "warning": (HALF, AMBER),
    "critical": (ADVERSE, RED),
}


def marker(value: str) -> tuple[str, str]:
    """Shape and colour for a verdict string. Matches on the leading token so
    'MIXED 1 company-linked, 1 no disclosure' resolves like 'MIXED'."""
    v = (value or "").strip().upper()
    if v in MARKERS:
        return MARKERS[v]
    for key in sorted(MARKERS, key=len, reverse=True):
        if v.startswith(key):
            return MARKERS[key]
    return (NEUTRAL, MUTED)


# --------------------------------------------------------------- stylesheet

_CSS = Template("""
<style>
/* Pin the light treatment. Without this, native controls and any widget whose
   colour we have not restated follow the OS scheme instead of the page. */
:root { color-scheme: light !important; }

/* --- Streamlit chrome: the app supplies its own header and navigation --- */
[data-testid="stHeader"], [data-testid="stDecoration"], [data-testid="stToolbar"],
[data-testid="stStatusWidget"], [data-testid="stSidebarNav"], #MainMenu, footer {
  display: none !important;
}

/* --- surfaces --- */
html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
  background-color: $white !important;
  color: $ink !important;
  font-family: $sans;
  -webkit-font-smoothing: antialiased;
}
[data-testid="stMainBlockContainer"], .block-container {
  padding-top: 2.5rem !important;
  padding-bottom: 4rem !important;
}

/* --- type: real hierarchy, generous rhythm between sections and tight within --- */
h1, h2, h3, h4, h5, h6 { color: $ink !important; font-family: $sans; }
h1 {
  font-size: 2.25rem !important; font-weight: 700 !important;
  letter-spacing: -0.022em; line-height: 1.15; margin: 0 0 0.35rem !important;
}
/* A section heading sits above a thin navy rule. */
h2 {
  font-size: 1.3125rem !important; font-weight: 700 !important;
  letter-spacing: -0.01em; line-height: 1.3;
  margin: 2.75rem 0 1rem !important; padding-bottom: 0.4rem;
  border-bottom: 1px solid $navy;
}
h3 {
  font-size: 1.0625rem !important; font-weight: 600 !important;
  letter-spacing: -0.005em; margin: 1.75rem 0 0.5rem !important;
}
p, li { color: $ink; font-size: 1rem; line-height: 1.65; }
[data-testid="stMarkdownContainer"] p { margin-bottom: 0.75rem; }
a, a:visited { color: $navy !important; text-decoration: underline; text-underline-offset: 2px; }
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p, small {
  color: $muted !important; font-size: 0.875rem !important; line-height: 1.55;
}
hr, [data-testid="stDivider"] { border-color: $rule !important; margin: 2.5rem 0 !important; }
[data-testid="stVerticalBlock"] { gap: 1rem; }

/* --- widget labels. The field name is the only thing telling the user what a
   field is, so it renders at body ink and never faded. Every branch of the
   label DOM is named because Streamlit nests the text differently per widget. --- */
[data-testid="stWidgetLabel"] { justify-content: flex-start !important; gap: 0.35rem; }
[data-testid="stWidgetLabel"] p,
[data-testid="stWidgetLabel"] label,
[data-testid="stWidgetLabel"] div,
label[data-testid="stWidgetLabel"],
.stTextInput label, .stTextArea label, .stSelectbox label, .stRadio label,
.stCheckbox label, .stMultiSelect label, .stSlider label, .stNumberInput label {
  color: $ink !important; opacity: 1 !important;
  font-size: 0.9375rem !important; font-weight: 600 !important; line-height: 1.4;
}
/* The help-icon wrapper defaults to flex-grow:1, which strands the "?" at the
   far right edge, away from the label it explains. Collapse it. */
[data-testid="stWidgetLabel"] > div:last-child:not(:only-child) { flex: 0 0 auto !important; }
[data-testid="stTooltipIcon"] { margin-left: 0 !important; }
/* The help glyph is a stroked outline with fill="none" — overriding fill paints
   it as a solid blob and swallows the question mark. Only the stroke is set. */
[data-testid="stTooltipIcon"] svg { stroke: $muted !important; fill: none !important; }
[data-testid="stTooltipHoverTarget"] { justify-content: flex-start !important; }

/* --- text inputs, textareas, selects: white fill, visible border, ink text --- */
[data-baseweb="input"], [data-baseweb="base-input"], [data-baseweb="textarea"],
[data-baseweb="select"] > div, [data-testid="stNumberInputContainer"] {
  background-color: $white !important;
  border: 1px solid $field_border !important;
  border-radius: 3px !important;
  box-shadow: none !important;
}
[data-baseweb="input"] input, [data-baseweb="base-input"] input,
input, textarea, [data-baseweb="textarea"] textarea,
[data-baseweb="select"] div, [data-baseweb="select"] span {
  color: $ink !important; -webkit-text-fill-color: $ink !important;
  background-color: transparent !important;
  font-family: $sans !important; font-size: 0.9375rem !important;
}
textarea { line-height: 1.6 !important; }
input::placeholder, textarea::placeholder {
  color: $placeholder !important; -webkit-text-fill-color: $placeholder !important;
  opacity: 1 !important;
}
/* Focus is always visible, and it is navy so it reads as the same system as the
   buttons. 11.64:1 against white. */
[data-baseweb="input"]:focus-within, [data-baseweb="base-input"]:focus-within,
[data-baseweb="textarea"]:focus-within, [data-baseweb="select"] > div:focus-within {
  border-color: $navy !important;
  box-shadow: 0 0 0 2px $navy inset, 0 0 0 3px rgba(22, 58, 95, 0.20) !important;
}
*:focus-visible {
  outline: 2px solid $navy !important; outline-offset: 2px !important; border-radius: 2px;
}
[data-baseweb="popover"] li, [data-baseweb="menu"] li { color: $ink !important; }
[data-baseweb="popover"] ul, [data-baseweb="menu"] ul { background-color: $white !important; }
[data-baseweb="popover"] li:hover, [data-baseweb="menu"] li:hover { background-color: $sand !important; }

/* --- checkbox and radio. The consent checkbox on the claims page is a control
   the analyst must be able to read, so its label gets full ink. --- */
[data-testid="stCheckbox"] label, [data-testid="stRadio"] label,
[data-testid="stCheckbox"] label *, [data-testid="stRadio"] label * {
  color: $ink !important; opacity: 1 !important;
}
[data-testid="stCheckbox"] label, [data-testid="stRadio"] label {
  font-weight: 400 !important; font-size: 0.9375rem !important;
}
[data-testid="stRadio"] [role="radiogroup"] { gap: 0.4rem; }
[data-baseweb="checkbox"] span:first-child, [data-baseweb="radio"] > div:first-child {
  background-color: $white !important; border: 1px solid $field_border !important;
}
[data-baseweb="checkbox"]:has(input:checked) span:first-child,
[data-baseweb="radio"]:has(input:checked) > div:first-child {
  background-color: $navy !important; border-color: $navy !important;
}
[data-baseweb="radio"]:has(input:checked) > div:first-child > div {
  background-color: $white !important;
}
[data-baseweb="checkbox"] svg, [data-baseweb="checkbox"] span:first-child * { fill: $white !important; }

/* --- buttons: one navy primary per screen, everything else quiet --- */
button[kind], [data-testid^="stBaseButton"] {
  border-radius: 3px !important; font-family: $sans !important;
  font-size: 0.9375rem !important; letter-spacing: 0.005em;
  padding: 0.55rem 1.1rem !important; transition: none !important;
}
/* Streamlit puts the label in a nested <p>, which the generic paragraph rule
   above would otherwise repaint ink — dark text on a navy fill. Every button
   rule therefore names its inner nodes too. */
[data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primaryFormSubmit"],
[data-testid="stBaseButton-primary"] *, [data-testid="stBaseButton-primaryFormSubmit"] * {
  color: $white !important; font-weight: 600 !important;
}
[data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primaryFormSubmit"] {
  background-color: $navy !important; border: 1px solid $navy !important;
}
[data-testid="stBaseButton-primary"]:hover, [data-testid="stBaseButton-primaryFormSubmit"]:hover {
  background-color: $navy_deep !important; border-color: $navy_deep !important;
}
[data-testid="stBaseButton-secondary"], [data-testid="stBaseButton-secondaryFormSubmit"],
[data-testid="stBaseButton-secondary"] *, [data-testid="stBaseButton-secondaryFormSubmit"] * {
  color: $ink !important; font-weight: 500 !important;
}
[data-testid="stBaseButton-secondary"], [data-testid="stBaseButton-secondaryFormSubmit"] {
  background-color: $white !important; border: 1px solid $field_border !important;
}
[data-testid="stBaseButton-secondary"]:hover, [data-testid="stBaseButton-secondaryFormSubmit"]:hover {
  border-color: $navy !important; background-color: $white !important;
}
[data-testid="stBaseButton-secondary"]:hover *, [data-testid="stBaseButton-secondaryFormSubmit"]:hover * {
  color: $navy !important;
}
/* Disabled still clears 4.5:1 — a control the user cannot use must still be
   legible enough to explain why the screen is in the state it is in. */
[data-testid^="stBaseButton"]:disabled, [data-testid^="stBaseButton"]:disabled * {
  background-color: $disabled_fill !important; color: $muted !important;
  border-color: $rule !important; opacity: 1 !important; cursor: not-allowed;
}
[data-testid="stDownloadButton"] button { width: 100%; }

/* --- alerts: quiet sand panels. Meaning comes from the wording, not a hue, so
   the built-in icon goes and the text stays ink. --- */
[data-testid="stAlert"] {
  background-color: $sand !important; color: $ink !important;
  border: 1px solid $sand_edge !important; border-left: 3px solid $muted !important;
  border-radius: 2px; padding: 0.7rem 0.9rem !important;
}
[data-testid="stAlert"] svg { display: none !important; }
/* The inner content node carries its own fill, which reads as a second box
   drawn inside the first. Only the outer panel has a surface. */
[data-testid="stAlert"] p, [data-testid="stAlert"] div {
  color: $ink !important; font-size: 0.9375rem;
  background-color: transparent !important; border: 0 !important;
}
[data-testid="stAlertContentWarning"] { border-left-color: $amber !important; }
[data-testid="stAlertContentError"] { border-left-color: $red !important; }
[data-testid="stAlertContentSuccess"] { border-left-color: $navy !important; }
[data-testid="stAlertContentInfo"] { border-left-color: $muted !important; }

/* --- expander --- */
[data-testid="stExpander"] {
  border: 1px solid $rule !important; border-radius: 3px; background: $white !important;
}
[data-testid="stExpander"] summary { color: $ink !important; font-weight: 600; font-size: 0.9375rem; }
[data-testid="stExpander"] summary:hover { color: $navy !important; }
[data-testid="stExpander"] svg { fill: $muted !important; }

/* --- progress and spinner --- */
[data-testid="stProgress"] div[role="progressbar"] > div { background-color: $navy !important; }
[data-testid="stProgress"] p, [data-testid="stSpinner"] p { color: $muted !important; font-size: 0.875rem; }
[data-testid="stSpinner"] svg { stroke: $navy !important; }

/* --- code --- */
code, [data-testid="stMarkdownContainer"] code {
  font-family: $mono !important; font-variant-numeric: tabular-nums;
  background-color: $sand !important; color: $ink !important;
  padding: 0.05rem 0.3rem; border-radius: 2px; font-size: 0.875em;
}
[data-testid="stCode"], pre {
  background-color: $sand !important; border: 1px solid $sand_edge !important; border-radius: 2px;
}
[data-testid="stCode"] code, pre code { background: transparent !important; }

/* --- sidebar: removed. It held nothing but a second copy of the navigation,
   and an empty sand column beside the content reads as a rendering fault. The
   rail below is the single navigation. --- */
[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"],
[data-testid="stExpandSidebarButton"], [data-testid="stSidebarCollapseButton"] {
  display: none !important;
}

/* --- navigation. Three tools, obvious from every page, in one rail under the
   wordmark. The current page is marked by weight and a navy underline as well as
   colour, and by aria-current for a screen reader. --- */
.mr-navrail {
  display: flex; gap: 0; border-bottom: 1px solid $rule; margin-bottom: 2.25rem;
}
.mr-navrail a {
  font-size: 0.9375rem; font-weight: 500; color: $ink !important;
  text-decoration: none !important; padding: 0.5rem 0; margin-right: 1.75rem;
  border-bottom: 2px solid transparent; margin-bottom: -1px;
}
.mr-navrail a:hover { color: $navy !important; border-bottom-color: $rule; }
.mr-navrail a[aria-current="page"] {
  font-weight: 700; color: $navy !important; border-bottom-color: $navy;
}

/* --- wordmark and page header --- */
.mr-wordmark {
  color: $navy; font-size: 0.6875rem; letter-spacing: 0.24em;
  font-weight: 700; text-transform: uppercase; margin-bottom: 0.5rem;
}
.mr-lede {
  color: $muted; font-size: 1rem; line-height: 1.6; max-width: 62ch;
  margin: 0.35rem 0 2rem;
}

/* --- stat row --- */
.mr-stats {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  border-top: 1px solid $navy; border-bottom: 1px solid $rule; margin: 2rem 0;
}
.mr-stat { padding: 0.9rem 1.1rem 1rem; border-right: 1px solid $rule; }
.mr-stat:last-child { border-right: 0; }
.mr-stat-label {
  font-size: 0.6875rem; letter-spacing: 0.13em; text-transform: uppercase;
  color: $muted; font-weight: 700; margin-bottom: 0.4rem;
}
.mr-stat-value {
  font-family: $mono; font-variant-numeric: tabular-nums;
  font-size: 1.625rem; color: $ink; font-weight: 500; line-height: 1.1;
}

/* --- status badge: shape, uppercase label, left rule. Any one of the three is
   enough to tell the states apart, so none of them is load-bearing alone. --- */
.mr-badge {
  background: $sand; border: 1px solid $sand_edge; border-left: 3px solid $muted;
  padding: 0.75rem 0.95rem; margin: 1rem 0; border-radius: 2px;
}
.mr-badge--good { border-left-color: $navy; }
.mr-badge--warning { border-left-color: $amber; }
.mr-badge--critical { border-left-color: $red; }
.mr-badge-label {
  font-size: 0.6875rem; letter-spacing: 0.14em; text-transform: uppercase;
  font-weight: 700; margin-bottom: 0.3rem; display: flex; gap: 0.45rem; align-items: baseline;
}
.mr-badge--good .mr-badge-label { color: $navy; }
.mr-badge--warning .mr-badge-label { color: $amber; }
.mr-badge--critical .mr-badge-label { color: $red; }
.mr-badge-shape { font-size: 0.8rem; line-height: 1; }
.mr-badge-body { color: $ink; font-size: 0.9375rem; line-height: 1.55; }

/* --- coverage statement: what was searched, what was not, what matched. Full
   navy border, not just a left rule — this is the one block on the page that
   should read as load-bearing on sight, distinct from an advisory badge. --- */
.mr-coverage {
  background: $white; border: 1.5px solid $navy; border-radius: 2px;
  padding: 0.9rem 1.05rem; margin: 1rem 0 1.5rem;
}
.mr-coverage-label {
  font-size: 0.6875rem; letter-spacing: 0.14em; text-transform: uppercase;
  font-weight: 700; color: $navy; margin-bottom: 0.5rem;
}
.mr-coverage-line {
  font-family: $mono; font-size: 0.8125rem; line-height: 1.6; color: $ink;
  white-space: pre-wrap; margin: 0 0 0.35rem;
}
.mr-coverage-line:last-child { margin-bottom: 0; }

/* --- result table: sand header, ink body, shape-marked verdicts. Rendered as a
   real <table> rather than the canvas grid so the shapes and weights are actual
   text a screen reader and a greyscale printer can both read. --- */
.mr-tablewrap { overflow-x: auto; margin: 1rem 0 1.25rem; border: 1px solid $rule; border-radius: 2px; }
/* Fixed layout, with widths declared in a colgroup. Under the auto algorithm a
   long verdict label wins the width fight and the claim text beside it collapses
   to one character per line, and a td min-width does not stop it. */
/* !important because Streamlit's own markdown-table CSS sets table-layout and
   wins on specificity otherwise, which silently restores the auto algorithm.
   The width lives here rather than in an inline style because Streamlit strips
   style attributes from <table> — it keeps them on <col>, which is where the
   column widths are declared. */
table.mr-table {
  border-collapse: collapse !important; font-size: 0.875rem;
  table-layout: fixed !important; width: 100% !important; max-width: none !important;
}
/* Many columns: the table takes the width its columns ask for and the wrapper
   scrolls, instead of every row becoming four lines tall. */
table.mr-table--wide { width: max-content !important; min-width: 100% !important; }
table.mr-table caption { text-align: left; color: $muted; font-size: 0.875rem; padding: 0 0 0.5rem; }
/* Headers wrap rather than nowrap: under a fixed layout a nowrap header simply
   overflows its column and the label reads as truncated. */
table.mr-table th {
  background: $sand; color: $ink; text-align: left; font-weight: 700;
  font-size: 0.6875rem; letter-spacing: 0.1em; text-transform: uppercase;
  padding: 0.6rem 0.75rem; border-bottom: 1px solid $navy;
  vertical-align: bottom; line-height: 1.35;
}
table.mr-table td {
  color: $ink; padding: 0.65rem 0.75rem; border-bottom: 1px solid $rule;
  vertical-align: top; line-height: 1.5;
  min-width: 110px; max-width: 380px; overflow-wrap: break-word;
}
table.mr-table tr:last-child td { border-bottom: 0; }
table.mr-table td.mr-num {
  font-family: $mono; font-variant-numeric: tabular-nums; color: $muted;
  width: 2.5rem; min-width: 2.5rem;
}
/* An identifier is a single token: breaking NCT06509126 across two lines makes
   it unsearchable and unquotable, which is the whole point of showing it. */
table.mr-table td.mr-mono {
  font-family: $mono; font-size: 0.8125rem; overflow-wrap: normal; word-break: keep-all;
}
/* Inline, not flex, and free to wrap: a nowrap verdict column starves the claim
   text beside it until it breaks one character per line. The shape stays next to
   the first word because it is part of the same text flow. */
.mr-verdict { font-weight: 700; }
.mr-verdict-shape { font-size: 0.8125rem; }
.mr-verdict--muted { font-weight: 500; }

/* --- verdict legend --- */
/* No top border here: a section heading already carries a navy rule directly
   above the legend, and a second line 8px under it reads as a mistake. */
.mr-legend {
  display: flex; flex-wrap: wrap; gap: 0.35rem 1.25rem; margin: -0.25rem 0 1rem;
}
.mr-legend-item { font-size: 0.8125rem; color: $muted; display: inline-flex; gap: 0.35rem; align-items: baseline; }

/* --- numbered memo sections. The same numbers in the same order for every
   asset is the point of the tool, so the layout states them. --- */
/* No top border: this block always follows a section heading, which already
   carries a navy rule directly above it. */
.mr-sections { margin: 0 0 2rem; }
.mr-section {
  display: grid; grid-template-columns: 2.75rem 1fr auto; gap: 0 0.9rem;
  align-items: baseline; padding: 0.8rem 0.25rem; border-bottom: 1px solid $rule;
}
.mr-section-n {
  font-family: $mono; font-variant-numeric: tabular-nums; font-size: 1.0625rem;
  font-weight: 600; color: $navy;
}
.mr-section-title { color: $ink; font-size: 0.9375rem; font-weight: 600; line-height: 1.45; }
.mr-section-note {
  font-size: 0.75rem; letter-spacing: 0.08em; text-transform: uppercase;
  font-weight: 700; white-space: nowrap;
}
.mr-section-note--has { color: $muted; }
.mr-section-note--none { color: $amber; }
.mr-section-note-shape { font-size: 0.7rem; }

/* Headings inside a rendered memo preview keep the section rule treatment.
   st.container(key="mr_memo") is what puts .st-key-mr_memo on the wrapper. */
.mr-memo h2, .st-key-mr_memo h2 { font-size: 1.1875rem !important; margin-top: 2.25rem !important; }
.mr-memo h3, .st-key-mr_memo h3 { font-size: 1rem !important; }
.mr-memo table, .st-key-mr_memo table {
  border-collapse: collapse; width: 100%; font-size: 0.8125rem; display: block; overflow-x: auto;
}
.mr-memo th, .st-key-mr_memo th {
  background: $sand; text-align: left; padding: 0.5rem 0.6rem; border-bottom: 1px solid $navy;
}
.mr-memo td, .st-key-mr_memo td { padding: 0.5rem 0.6rem; border-bottom: 1px solid $rule; }

/* --- narrow widths: the page must never scroll sideways; wide content scrolls
   inside its own container instead. --- */
@media (max-width: 640px) {
  h1 { font-size: 1.75rem !important; }
  .mr-stats { grid-template-columns: 1fr 1fr; }
  .mr-stat { border-bottom: 1px solid $rule; }
  .mr-section { grid-template-columns: 2rem 1fr; }
  .mr-section-note { grid-column: 2; margin-top: 0.2rem; }
  .mr-navrail { overflow-x: auto; }
  [data-testid="stMainBlockContainer"], .block-container {
    padding-left: 1.1rem !important; padding-right: 1.1rem !important;
  }
}
</style>
""").substitute(
    white=WHITE, sand=SAND, ink=INK, navy=NAVY, navy_deep=NAVY_DEEP, muted=MUTED,
    placeholder=PLACEHOLDER, field_border=FIELD_BORDER, rule=RULE, amber=AMBER,
    red=RED, disabled_fill=DISABLED_FILL, sand_edge=SAND_EDGE,
    sans=_FONT_SANS, mono=_FONT_MONO,
)

_TOOLS = [
    ("app.py", "Diligence memo"),
    ("pages/2_Verify_Claims.py", "Verify claims"),
    ("pages/3_Trial_Landscape.py", "Trial landscape"),
]


# --------------------------------------------------------------- api


def apply() -> None:
    """Inject the whole theme. Call once per page, right after set_page_config,
    before any other Streamlit call."""
    st.markdown(_CSS, unsafe_allow_html=True)


def sidebar_nav(active: str = "") -> None:
    """Retained as a no-op so a page that still calls it does not break.

    Navigation is the rail in `page_header`, and it is the only copy. Running both
    gave three destinations six tab stops and two conflicting reading orders: the
    sidebar sits left of the content but comes after it in the DOM, so tab order
    and visual order disagreed. The rail is also the copy that survives a narrow
    window, where Streamlit pushes the sidebar off-canvas.
    """


def page_header(title: str, lede: str = "", active: str = "") -> None:
    """Wordmark, the tool navigation, the page title, and the navy rule under it.
    `active` is the label of the current tool so it can be marked by weight and
    an underline rather than by colour."""
    current = ' aria-current="page"'
    links = "".join(
        '<a href="%s" target="_self"%s>%s</a>'
        % (_href(path), current if label == active else "", html.escape(label))
        for path, label in _TOOLS
    )
    st.markdown(
        f'<div class="mr-wordmark">MedRAG</div>'
        f'<nav class="mr-navrail" aria-label="Tools">{links}</nav>'
        f'<h1>{html.escape(title)}</h1>',
        unsafe_allow_html=True,
    )
    if lede:
        st.markdown(f'<p class="mr-lede">{html.escape(lede)}</p>', unsafe_allow_html=True)


def _href(path: str) -> str:
    """Streamlit serves the entry script at the app root and each page at
    <root>/<stem minus the numeric prefix>. The links are relative so they still
    resolve if the app is ever served under a base path."""
    if path == "app.py":
        return "./"
    stem = path.rsplit("/", 1)[-1].removesuffix(".py")
    return "./" + (stem.split("_", 1)[1] if "_" in stem else stem)


def coverage_box(lines: list[str], label: str = "Coverage") -> None:
    """The registry-coverage statement: what was searched, what was not, what
    matched. Deliberately its own component, not a badge — this states facts
    with numbers a reader can go verify, not a judgement on whether they are
    good news."""
    body = "".join(f'<p class="mr-coverage-line">{html.escape(line)}</p>' for line in lines)
    st.markdown(
        f'<div class="mr-coverage"><div class="mr-coverage-label">{html.escape(label)}</div>'
        f'{body}</div>',
        unsafe_allow_html=True,
    )


def badge(kind: str, label: str, body: str) -> None:
    """A status block. `kind` is good | warning | critical. The state is carried
    by the shape, the uppercase label and the left rule together, so it survives
    greyscale and colourblind reading."""
    shape, _ = _BADGE_SHAPES.get(kind, (NEUTRAL, MUTED))
    st.markdown(
        f'<div class="mr-badge mr-badge--{html.escape(kind)}">'
        f'<div class="mr-badge-label">'
        f'<span class="mr-badge-shape" aria-hidden="true">{shape}</span>'
        f'<span>{html.escape(label)}</span></div>'
        f'<div class="mr-badge-body">{html.escape(body)}</div></div>',
        unsafe_allow_html=True,
    )


def stat_row(stats: list[tuple[str, str]]) -> None:
    parts = ['<div class="mr-stats">']
    for label, value in stats:
        parts.append(
            '<div class="mr-stat">'
            f'<div class="mr-stat-label">{html.escape(label)}</div>'
            f'<div class="mr-stat-value">{html.escape(value)}</div></div>'
        )
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


def verdict_html(value: str) -> str:
    """A verdict as shape plus text label. Never the shape alone."""
    shape, colour = marker(value)
    weight = "" if colour != MUTED else " mr-verdict--muted"
    return (
        f'<span class="mr-verdict{weight}" style="color:{colour}">'
        f'<span class="mr-verdict-shape" aria-hidden="true">{shape}</span> '
        f"{html.escape(value)}</span>"
    )


def legend(values: list[str]) -> None:
    """Spell out the shape vocabulary in use on this screen."""
    items = "".join(
        f'<span class="mr-legend-item">{verdict_html(v)}</span>' for v in values
    )
    st.markdown(f'<div class="mr-legend">{items}</div>', unsafe_allow_html=True)


def data_table(columns: list[str], rows: list[list], verdict_cols: set[int] | None = None,
               mono_cols: set[int] | None = None, numbered: bool = True) -> None:
    """A semantic result table: sand header, ink body, shape-marked verdicts.

    A real <table> rather than Streamlit's canvas grid, because the canvas cannot
    carry a shape or a font weight into the text layer and reads poorly to a
    screen reader. Wide tables scroll inside the wrapper so the page never does.
    """
    verdict_cols = verdict_cols or set()
    mono_cols = mono_cols or set()

    # Column widths are declared, not negotiated. A narrow table is sized in
    # percentages so it always fits its container; a table with many columns is
    # sized in pixels and scrolls inside the wrapper, because nine columns
    # squeezed into a centred page makes every row four lines tall.
    # Weights, not equal shares: prose columns need room to wrap, a verdict label
    # needs about one line, and an identifier column needs just enough to keep
    # the identifier on one line.
    def _weight(j: int) -> float:
        if j in mono_cols:
            return 1.5
        return 1.3 if j in verdict_cols else 2.0

    tagged = [j for j in range(len(columns)) if j in verdict_cols or j in mono_cols]
    if len(columns) <= 6:
        total = sum(_weight(j) for j in range(len(columns))) or 1
        share = 95.0 / total
        widths = ["5%"] if numbered else []
        widths += [f"{share * _weight(j):.2f}%" for j in range(len(columns))]
        wide = ""
    else:
        px = ([46] if numbered else []) + [
            165 if j in tagged else 260 for j in range(len(columns))
        ]
        widths = [f"{w}px" for w in px]
        wide = " mr-table--wide"

    cols = "".join(f'<col style="width:{w}">' for w in widths)
    head = ('<th scope="col">#</th>' if numbered else "") + "".join(
        f'<th scope="col">{html.escape(c)}</th>' for c in columns
    )
    body = []
    for i, row in enumerate(rows, 1):
        cells = [f'<td class="mr-num">{i}</td>'] if numbered else []
        for j, cell in enumerate(row):
            text = "" if cell is None else str(cell)
            if j in verdict_cols:
                cells.append(f"<td>{verdict_html(text)}</td>")
            else:
                cls = ' class="mr-mono"' if j in mono_cols else ""
                cells.append(f"<td{cls}>{html.escape(text)}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    st.markdown(
        '<div class="mr-tablewrap" tabindex="0" role="region" aria-label="Results">'
        f'<table class="mr-table{wide}"><colgroup>{cols}</colgroup>'
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody>"
        "</table></div>",
        unsafe_allow_html=True,
    )


def section_index(sections: list[tuple[str, bool]]) -> None:
    """The memo's sections as numbered blocks in question-set order.

    Same numbers, same order, every asset — that comparability is the reason the
    question set is fixed, so the layout says it out loud. Each entry is
    (title, has_evidence); a section with nothing behind it is marked, because a
    thin section must not read like a clean one.
    """
    parts = ['<div class="mr-sections">']
    for n, (title, has_evidence) in enumerate(sections, 1):
        shape = FILLED if has_evidence else HOLLOW
        cls = "has" if has_evidence else "none"
        note = "Evidence" if has_evidence else "No evidence"
        parts.append(
            '<div class="mr-section">'
            f'<div class="mr-section-n">{n}</div>'
            f'<div class="mr-section-title">{html.escape(title)}</div>'
            f'<div class="mr-section-note mr-section-note--{cls}">'
            f'<span class="mr-section-note-shape" aria-hidden="true">{shape}</span> '
            f"{note}</div></div>"
        )
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)
