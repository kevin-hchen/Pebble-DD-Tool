"""Third page: trial landscape from the patient's perspective.

Given a condition and a biomarker, enumerate the trials a patient could actually
enter — filtered to trials whose eligibility references the biomarker, each shown
with the exact criterion that placed it there. No model and no confidentiality
gate: this is a structured query over the public registry, nothing leaves beyond
the ClinicalTrials.gov fetch itself.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import streamlit as st

import theme
from medrag.config import load_config
from medrag.crypto import read_secure
from medrag.landscape import build_landscape
from medrag.landscape_memo import export as export_landscape
from medrag.pipeline import TRIALS_DB
from medrag.trials.queries import fetch_query_set, resolve_query_set
from medrag.trials.store import TrialStore, TrialStoreSchemaError

st.set_page_config(
    page_title="MedRAG — Trial landscape",
    page_icon=None,
    layout="wide",
    # "auto", not "expanded": a forced-open sidebar is an overlay on a narrow
    # window and covers the form. The nav rail under the wordmark keeps the
    # three tools reachable from every page when the sidebar is collapsed.
    initial_sidebar_state="auto",
)
OUT_DIR = Path("out")

theme.apply()

_badge = theme.badge

theme.page_header(
    "Trial landscape",
    "Enter a condition and a biomarker. This lists the trials whose eligibility "
    "references that biomarker — each with the exact criterion that decides it — so "
    "a patient and their clinician can see where they might enrol. It is a research "
    "aid, not medical advice.",
    active="Trial landscape",
)

cfg = load_config()
cfg.ensure_dirs()
db = cfg.raw_dir / TRIALS_DB


def _ensure_trials(condition: str, force: bool, progress=None) -> str:
    """Make sure the registry has been pulled for this condition, and return the
    query set that defines the population. Trial fetches are public — no deck
    text — so there is nothing to confirm before this.

    "Have we already got this?" is asked of the query set, not of a condition
    substring: the fetch defines the population, so the same handle has to decide
    whether it exists and later select it.
    """
    qset = resolve_query_set(condition)
    have = 0
    if db.exists():
        with TrialStore(db) as store:           # may raise TrialStoreSchemaError
            have = store.count(query_set=qset.key)
    if cfg.offline:
        if not db.exists():
            raise RuntimeError(
                "Offline mode is on and no trials are stored yet. Turn off offline "
                "mode, or ingest trials first with `medrag trials`.")
        return qset.key
    if have and not force:
        return qset.key

    records, provenance, coverage = fetch_query_set(
        qset, offline=cfg.offline, progress=progress)
    with TrialStore(db) as store:
        store.upsert(records, provenance=provenance, set_key=qset.key)
        store.record_coverage(coverage)
    return qset.key


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
        status = st.status("Fetching trials from the registry…", expanded=False)
        qset_key = _ensure_trials(
            condition.strip(), force=refresh,
            progress=lambda _f, msg: status.update(label=msg),
        )
        status.update(label="Registry fetch complete.", state="complete")
        with TrialStore(db) as store:
            landscape = build_landscape(
                store, condition=condition.strip(), biomarker=biomarker.strip(),
                location=location.strip(), query_set=qset_key,
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

    st.markdown("## Trials a patient could enter")
    theme.legend(sorted({t.match.status for t in landscape.trials}))
    theme.data_table(
        ["NCT ID", "Title", "Phase", "Status", "Biomarker", "Nearest site", "PI",
         "Contact", "Matched eligibility criterion"],
        [
            [
                t.record.nct_id,
                t.record.brief_title,
                t.record.phase or "—",
                t.record.overall_status or "—",
                t.match.status,
                (f"{(t.nearest_location or {}).get('city', '')}, "
                 f"{(t.nearest_location or {}).get('state', '')}".strip(", ")
                 if t.nearest_location else "—"),
                (t.record.principal_investigator or {}).get("name", "—"),
                (t.contact or {}).get("email") or (t.contact or {}).get("phone") or "—",
                t.match.evidence or "—",
            ]
            for t in landscape.trials
        ],
        verdict_cols={4},
        mono_cols={0},
    )

    st.caption(
        "UNCLEAR means the biomarker is referenced but eligibility could not be read "
        "off it — often a trial that excludes the opposite biomarker. It is kept here, "
        "flagged, rather than dropped."
    )

    paths = export_landscape(landscape, OUT_DIR, passphrase=cfg.passphrase)
    # Decrypted on the way out: readable bytes for the browser.
    _ls_md = read_secure(paths["markdown"], cfg.passphrase)
    stamp = datetime.now().strftime("%Y-%m-%d")
    left, right = st.columns(2)
    left.download_button("Download landscape (PDF)", data=paths["pdf"].read_bytes(),
                         file_name=f"{paths['pdf'].stem}-{stamp}.pdf",
                         mime="application/pdf", type="primary", use_container_width=True)
    right.download_button("Download landscape (Markdown)", data=_ls_md,
                          file_name=f"{paths['markdown'].stem}-{stamp}.md",
                          mime="text/markdown", use_container_width=True)

st.divider()
st.caption(
    "Trial data from ClinicalTrials.gov. Eligibility is summarised from each trial's "
    "registry text and may be incomplete or out of date — confirm with the trial's "
    "contact. This is a research aid, not medical advice."
)
