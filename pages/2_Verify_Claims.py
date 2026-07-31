"""Second page: verify a founder's claims against independent evidence.

The mirror image of the memo page. Instead of asking a fixed question set about
an asset, the analyst supplies the deck's assertions and the tool checks each one
against PubMed and the registry — the thing an analyst does to prove a technology
without leaning on the company's own materials.

Two confidentiality rules shape this page, because deck content is more sensitive
than an asset name:
  - extracted claims are ALWAYS shown for editing before anything is verified;
    the analyst owns what gets checked
  - nothing leaves the machine until the analyst has seen exactly what would be
    sent and to which provider, and confirmed it for this run
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import streamlit as st

import theme

from medrag.autoload import ensure_data
from medrag.claims import (
    ClaimVerifier,
    ConfirmationRequired,
    ExtractedClaim,
    extract_claims,
    parse_claims_text,
    transmission_notice,
)
from medrag.claims_memo import export as export_claims
from medrag.config import load_config
from medrag.setup_env import read_env

st.set_page_config(
    page_title="MedRAG — Verify claims",
    page_icon=None,
    layout="centered",
    # "auto", not "expanded": a forced-open sidebar is an overlay on a narrow
    # window and covers the form. The nav rail under the wordmark keeps the
    # three tools reachable from every page when the sidebar is collapsed.
    initial_sidebar_state="auto",
)
OUT_DIR = Path("out")

theme.apply()

_badge = theme.badge

cfg = load_config()
cfg.ensure_dirs()
env = read_env()

if not env.get("MEDRAG_PROVIDER"):
    theme.page_header(
        "Verify claims",
        "Check a founder's claims against independent literature and the trial "
        "registry — the inverse of the diligence memo.",
        active="Verify claims",
    )
    _badge("warning", "SETUP NEEDED",
           "Open the Diligence memo page first to choose a provider. That one-time "
           "setup applies here too.")
    st.stop()

theme.page_header(
    "Verify claims",
    "Paste the claims from a deck — or the deck text itself — and MedRAG checks each "
    "one against PubMed and the trial registry, without relying on the company's own "
    "materials. Every verdict is cited.",
    active="Verify claims",
)

# --------------------------------------------------------------- inputs

company = st.text_input("Company / manufacturer", placeholder="e.g. Example Therapeutics",
                        help="Used to flag evidence that is the company's own — the most "
                             "important thing to detect.")
col_a, col_b = st.columns(2)
asset = col_a.text_input("Drug or asset name", placeholder="e.g. Compound X")
indication = col_b.text_input("Disease or indication", placeholder="e.g. solid tumor")

mode = st.radio(
    "How do you want to provide the claims?",
    ["Paste claims (one per line)", "Paste deck text (extract claims for me)"],
    label_visibility="collapsed",
)

# The transmission notice governs both extraction and verification: both send
# analyst-supplied text to the model when the provider is remote.
notice = transmission_notice(cfg, [], kind="claims")

for _key in ("claims_text", "not_verifiable_text"):
    if _key not in st.session_state:
        st.session_state[_key] = ""

if mode.startswith("Paste deck"):
    deck = st.text_area("Deck text", height=200,
                        placeholder="Paste the slide text here. Claims are extracted, "
                                    "then shown to you to edit before anything is checked.")
    if not notice.local:
        st.caption(
            f"Extracting will send the deck text to {notice.provider_label}. "
            "Confirm below before extracting."
        )
        extract_ok = st.checkbox("I confirm sending this deck text for extraction", key="extract_ok")
    else:
        st.caption(notice.render())
        extract_ok = True

    if st.button("Extract claims", disabled=not deck.strip()):
        if not extract_ok:
            st.error("Confirm the transmission first, or switch to a local provider.")
        else:
            try:
                with st.spinner("Extracting claims from the deck…"):
                    extracted = extract_claims(deck.strip(), cfg, confirmed=True)
                checkable = [e.text for e in extracted if e.verifiable]
                # Not silently discarded: the unverifiable ones are surfaced with
                # their reason so the analyst can rewrite or drop them.
                unchecked = [f"{e.text}  —  {e.reason}" if e.reason else e.text
                             for e in extracted if not e.verifiable]
                st.session_state["claims_text"] = "\n".join(checkable)
                st.session_state["not_verifiable_text"] = "\n".join(unchecked)
                if extracted:
                    msg = f"Extracted {len(extracted)} claim(s): {len(checkable)} checkable"
                    if unchecked:
                        msg += f", {len(unchecked)} with no checkable assertion (see below)"
                    st.success(msg + ". Edit them, then verify — you decide what gets checked.")
                else:
                    st.warning("No claims were found in that text.")
            except ConfirmationRequired as exc:
                st.error(exc.notice.render())
            except Exception as exc:
                st.error(f"Extraction failed: {type(exc).__name__}: {exc}")

claims_text = st.text_area(
    "Claims to verify (one per line)",
    key="claims_text",
    height=180,
    help="'#' lines are treated as comments. Edit freely before verifying.",
)
claims = parse_claims_text(claims_text)

# Claims the extractor judged to make no checkable assertion. Shown for the
# analyst to rewrite into something specific (move a line up to the box above) or
# leave here to be recorded as NOT VERIFIABLE — a finding, not a silent drop.
not_verifiable_text = st.text_area(
    "Claims with no checkable assertion (recorded as NOT VERIFIABLE)",
    key="not_verifiable_text",
    height=90,
    help="Marketing language like 'best-in-class' lands here. Rewrite it to be "
         "specific and move it up, or leave it to record that the deck makes a "
         "claim that cannot be checked.",
)
not_verifiable = [line.split("  —  ")[0].strip()
                  for line in parse_claims_text(not_verifiable_text)]

# --------------------------------------------------------------- confidentiality gate

if notice.local:
    _badge("good", "STAYS ON THIS MACHINE", notice.render())
    confirmed = True
elif not claims:
    # Only unverifiable claims are recorded — nothing is transmitted, so there is
    # nothing to confirm.
    confirmed = True
else:
    live = transmission_notice(cfg, claims, kind="claims")
    body = (
        f"{len(claims)} claim(s) will be sent to {live.provider_label} "
        f"({live.endpoint}) to be checked. Deck-derived claims are confidential — "
        "confirm you want to send them for this run."
    )
    _badge("warning", "WILL BE TRANSMITTED", body)
    if claims:
        with st.expander("Show exactly what will be sent"):
            st.code("\n".join(f"{i}. {c}" for i, c in enumerate(claims, 1)))
    confirmed = st.checkbox(
        f"I confirm sending these {len(claims)} claim(s) to {live.provider_key} for this run"
    )

run = st.button("Verify claims", type="primary", use_container_width=True,
                disabled=not (claims or not_verifiable))

# Verifiable claims go through retrieval + the model; the ones the analyst left
# in the NOT VERIFIABLE box are recorded without being sent anywhere.
to_verify = list(claims) + [ExtractedClaim(text=t, verifiable=False) for t in not_verifiable]

# --------------------------------------------------------------- run

if run:
    if not to_verify:
        st.error("Add at least one claim to verify.")
        st.stop()
    if not notice.local and not confirmed:
        st.error("Confirm the transmission first, or switch to a local provider "
                 "(Ollama, or offline mode).")
        st.stop()

    bar = st.progress(0.0, text="Starting…")
    verifier = None
    try:
        def on_progress(fraction: float, message: str) -> None:
            bar.progress(min(0.3 * fraction, 0.3), text=message)

        # Fetches public research for the asset/indication only — no claim text is
        # transmitted here. The claims are only used once verification runs.
        report_data = ensure_data(cfg, asset.strip(), indication.strip(), progress=on_progress)
        for err in report_data.errors:
            st.warning(err)

        bar.progress(0.35, text="Checking each claim…")
        verifier = ClaimVerifier(cfg)
        result = verifier.verify(
            to_verify,
            asset=asset.strip(),
            indication=indication.strip(),
            company=company.strip(),
            confirmed=True,
        )
        bar.progress(0.95, text="Writing the results…")
        paths = export_claims(result, OUT_DIR)
        bar.empty()
    except ConfirmationRequired as exc:
        bar.empty()
        st.error(exc.notice.render())
        st.stop()
    except Exception as exc:
        bar.empty()
        st.error(f"Something went wrong and nothing was saved. {type(exc).__name__}: {exc}")
        st.stop()
    finally:
        if verifier is not None:
            verifier.close()

    for w in result.warnings:
        st.warning(w)

    support = result.support_counts()
    independence = result.independence_counts()
    if support.get("CONTRADICTED"):
        _badge("critical", "CONTRADICTED CLAIMS",
               f"{support['CONTRADICTED']} claim(s) are contradicted by the retrieved evidence.")
    if independence.get("COMPANY-LINKED"):
        _badge("warning", "COMPANY-LINKED SUPPORT",
               f"{independence['COMPANY-LINKED']} supported claim(s) rest only on evidence "
               "disclosed as tied to the manufacturer — no independent source was retrieved.")
    if support.get("NOT VERIFIABLE"):
        _badge("warning", "CLAIMS THAT CANNOT BE CHECKED",
               f"{support['NOT VERIFIABLE']} claim(s) make no specific, checkable assertion. "
               "That is itself a finding — they are recorded, not dropped.")

    # The result table: support and independence are separate columns, because a
    # claim can be well supported and entirely company-sourced. Each verdict
    # carries a shape as well as its label, so the two axes stay readable in
    # greyscale and to a colourblind reader.
    st.markdown("## Results")
    theme.legend(sorted({v.support for v in result.verdicts}))
    theme.data_table(
        ["Claim", "Support", "Independence", "Sources"],
        [
            [
                v.claim,
                v.support,
                v.independence_display(),
                ", ".join(e.identifier for e in v.cited_evidence) or "—",
            ]
            for v in result.verdicts
        ],
        verdict_cols={1, 2},
        mono_cols={3},
    )

    stamp = datetime.now().strftime("%Y-%m-%d")
    left, right = st.columns(2)
    left.download_button("Download results (PDF)", data=paths["pdf"].read_bytes(),
                         file_name=f"{paths['pdf'].stem}-{stamp}.pdf",
                         mime="application/pdf", type="primary", use_container_width=True)
    right.download_button("Download results (Markdown)", data=paths["markdown"].read_bytes(),
                          file_name=f"{paths['markdown'].stem}-{stamp}.md",
                          mime="text/markdown", use_container_width=True)

    with st.expander("Preview the full results"):
        with st.container(key="mr_memo"):
            st.markdown(paths["markdown"].read_text(encoding="utf-8"))

st.divider()
st.caption(
    "MedRAG checks claims against public sources — PubMed and the ClinicalTrials.gov "
    "registry. Every verdict is traceable to a cited passage. It is a research aid, not "
    "investment advice, and citations should be checked against the original source."
)
