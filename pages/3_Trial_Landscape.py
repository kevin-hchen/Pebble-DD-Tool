"""Third page: trial landscape from the patient's perspective.

Given a condition and a biomarker, enumerate the trials a patient could actually
enter — filtered to trials whose eligibility references the biomarker, each shown
with the exact criterion that placed it there. No model and no confidentiality
gate: this is a structured query over the public registry, nothing leaves beyond
the ClinicalTrials.gov fetch itself.
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

import streamlit as st

from medrag.biomarker import ELIGIBLE, UNCLEAR
from medrag.config import load_config
from medrag.landscape import build_landscape
from medrag.landscape_memo import export as export_landscape
from medrag.pipeline import TRIALS_DB
from medrag.trials.client import search_trials
from medrag.trials.store import TrialStore, TrialStoreSchemaError

st.set_page_config(page_title="MedRAG — Trial landscape", page_icon=None, layout="wide")
OUT_DIR = Path("out")

_STYLE = """
<style>
:root {
  --ink-900: #14181f; --ink-600: #4a5568; --ink-400: #64748b;
  --rule: #e2e5ea; --surface-2: #f7f8fa; --accent: #1e4f8f;
  --critical: #d03b3b; --warning: #fab219; --good: #0ca30c;
  --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
[data-testid="stHeader"], [data-testid="stDecoration"], [data-testid="stToolbar"],
[data-testid="stStatusWidget"], #MainMenu, footer { display: none !important; }
html, body, [data-testid="stAppViewContainer"] {
  font-family: var(--font-sans); color: var(--ink-900); background-color: #ffffff;
}
h1 { font-size: 1.75rem; margin: 0 0 0.25rem; letter-spacing: -0.005em; }
[data-testid="stAlert"] svg { display: none !important; }
[data-testid="stAlert"] {
  background: var(--surface-2); border-left: 3px solid var(--ink-400); color: var(--ink-900);
}
.medrag-wordmark {
  color: var(--accent); font-size: 0.7rem; letter-spacing: 0.22em;
  font-weight: 600; text-transform: uppercase; margin-bottom: 0.25rem;
}
.medrag-rule { height: 1px; background: var(--rule); margin: 0.75rem 0 1.5rem; border: 0; }
.medrag-badge {
  border-left: 3px solid var(--ink-400); background: var(--surface-2);
  padding: 0.6rem 0.85rem; margin: 0.75rem 0;
}
.medrag-badge--critical { border-left-color: var(--critical); }
.medrag-badge--warning  { border-left-color: var(--warning); }
.medrag-badge--good     { border-left-color: var(--good); }
.medrag-badge-label {
  font-size: 0.68rem; letter-spacing: 0.14em; text-transform: uppercase;
  color: var(--ink-600); font-weight: 600; margin-bottom: 0.2rem;
}
.medrag-badge-body { color: var(--ink-900); font-size: 0.95rem; line-height: 1.4; }
</style>
"""
st.markdown(_STYLE, unsafe_allow_html=True)


def _badge(kind: str, label: str, body: str) -> None:
    st.markdown(
        f'<div class="medrag-badge medrag-badge--{kind}">'
        f'<div class="medrag-badge-label">{html.escape(label)}</div>'
        f'<div class="medrag-badge-body">{html.escape(body)}</div></div>',
        unsafe_allow_html=True,
    )


st.markdown(
    '<div class="medrag-wordmark">MEDRAG</div><h1>Trial landscape</h1>'
    '<div class="medrag-rule"></div>',
    unsafe_allow_html=True,
)
st.caption(
    "Enter a condition and a biomarker. This lists the trials whose eligibility "
    "references that biomarker — each with the exact criterion that decides it — so "
    "a patient and their clinician can see where they might enrol. It is a research "
    "aid, not medical advice."
)

cfg = load_config()
cfg.ensure_dirs()
db = cfg.raw_dir / TRIALS_DB


def _ensure_trials(condition: str, force: bool) -> None:
    """Make sure the registry has been pulled for this condition. Trial fetches
    are public — no deck text — so there is nothing to confirm before this."""
    have = 0
    if db.exists():
        with TrialStore(db) as store:           # may raise TrialStoreSchemaError
            have = len(store.query(condition=condition, limit=1))
    if cfg.offline:
        if not db.exists():
            raise RuntimeError(
                "Offline mode is on and no trials are stored yet. Turn off offline "
                "mode, or ingest trials first with `medrag trials`.")
        return
    if have and not force:
        return
    records = search_trials(condition=condition, max_records=300, offline=cfg.offline)
    with TrialStore(db) as store:
        store.upsert(records)


with st.form("landscape"):
    col1, col2, col3 = st.columns([2, 1.4, 1.4])
    condition = col1.text_input("Condition", placeholder="e.g. colorectal cancer")
    biomarker = col2.text_input("Biomarker", placeholder="e.g. MSS",
                                help="Microsatellite status is supported: MSS, microsatellite "
                                     "stable, pMMR, proficient mismatch repair, non-MSI-H.")
    location = col3.text_input("Location (optional)", placeholder="e.g. Boston")
    refresh = st.checkbox("Re-download trials for this condition even if some are stored")
    submitted = st.form_submit_button("Build landscape", type="primary")

if submitted:
    if not condition.strip() or not biomarker.strip():
        st.error("Enter both a condition and a biomarker.")
        st.stop()

    try:
        with st.spinner("Fetching trials from the registry…"):
            _ensure_trials(condition.strip(), force=refresh)
        with TrialStore(db) as store:
            landscape = build_landscape(
                store, condition=condition.strip(), biomarker=biomarker.strip(),
                location=location.strip(),
            )
    except TrialStoreSchemaError as exc:
        _badge("critical", "TRIAL DATABASE OUT OF DATE",
               "The stored trial database predates the eligibility fields this page needs. "
               "Delete it and re-ingest, then try again.")
        st.code(str(exc))
        st.stop()
    except Exception as exc:
        st.error(f"Could not build the landscape. {type(exc).__name__}: {exc}")
        st.stop()

    for w in landscape.warnings:
        st.warning(w)

    if not landscape.trials:
        _badge("warning", "NO BIOMARKER-MATCHED TRIALS",
               f"None of the {landscape.n_condition} trials found for “{condition}” reference "
               f"“{biomarker}” in their eligibility. A trial that does not mention it may still "
               "accept the patient — a plain condition search would show those.")
        st.stop()

    _badge("good", "TRIALS FOUND",
           f"{landscape.n_eligible} eligible and {landscape.n_unclear} unclear trial(s) shown. "
           f"{landscape.n_excluded} require the opposite biomarker and "
           f"{landscape.n_not_mentioned} do not mention it — those are not listed.")

    st.markdown("### Trials a patient could enter")
    st.dataframe(
        [
            {
                "NCT ID": t.record.nct_id,
                "Title": t.record.brief_title,
                "Phase": t.record.phase or "—",
                "Status": t.record.overall_status or "—",
                "Biomarker": t.match.status,
                "Nearest site": (
                    f"{(t.nearest_location or {}).get('city', '')}, "
                    f"{(t.nearest_location or {}).get('state', '')}".strip(", ")
                    if t.nearest_location else "—"),
                "PI": (t.record.principal_investigator or {}).get("name", "—"),
                "Contact": (t.contact or {}).get("email")
                or (t.contact or {}).get("phone") or "—",
                "Matched eligibility criterion": t.match.evidence or "—",
            }
            for t in landscape.trials
        ],
        use_container_width=True,
        hide_index=True,
    )

    st.caption(
        "UNCLEAR means the biomarker is referenced but eligibility could not be read "
        "off it — often a trial that excludes the opposite biomarker. It is kept here, "
        "flagged, rather than dropped."
    )

    paths = export_landscape(landscape, OUT_DIR)
    stamp = datetime.now().strftime("%Y-%m-%d")
    left, right = st.columns(2)
    left.download_button("Download landscape (PDF)", data=paths["pdf"].read_bytes(),
                         file_name=f"{paths['pdf'].stem}-{stamp}.pdf",
                         mime="application/pdf", type="primary", use_container_width=True)
    right.download_button("Download landscape (Markdown)", data=paths["markdown"].read_bytes(),
                          file_name=f"{paths['markdown'].stem}-{stamp}.md",
                          mime="text/markdown", use_container_width=True)

st.divider()
st.caption(
    "Trial data from ClinicalTrials.gov. Eligibility is summarised from each trial's "
    "registry text and may be incomplete or out of date — confirm with the trial's "
    "contact. This is a research aid, not medical advice."
)
