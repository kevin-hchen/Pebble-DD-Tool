"""MedRAG — diligence memo generator.

One form, one button. Type an asset and an indication; if the research isn't
loaded yet it fetches it, then writes a cited memo and hands back a PDF. No
terminal, at any point.

    streamlit run app.py        (or double-click run.sh / run.bat)

Design rules, because the users are analysts and interns, not engineers:
  - never show a traceback as the primary message; say what to do instead
  - never block on a terminal prompt (getpass in a web server hangs forever)
  - loading is a consequence of asking for a memo, not a prerequisite step
  - the default configuration costs nothing
  - color never carries meaning alone; text badges name the state
  - no emoji anywhere; a diligence tool needs to look like documentation
"""

from __future__ import annotations

import html
import json
import traceback
from datetime import datetime
from pathlib import Path

import streamlit as st

from medrag.autoload import ensure_data
from medrag.config import load_config
from medrag.crypto import CryptoError, is_encrypted
from medrag.diligence import DiligenceRunner, MemoResult, load_question_set
from medrag.memo import export
from medrag.pipeline import CORPUS_FILE, TRIALS_DB
from medrag.providers import FREE_PROVIDERS, PROVIDERS, get_provider
from medrag.setup_env import key_looks_valid, read_env, write_env
from medrag.trials.store import TrialStore

st.set_page_config(page_title="MedRAG — Diligence", page_icon=None, layout="centered")
OUT_DIR = Path("out")


# --------------------------------------------------------------- style
# One tokenised block — every rule references a semantic role, no raw hex. All
# selectors are [data-testid] where Streamlit ships one; generated class names
# change between releases, so revisit this block after a Streamlit upgrade.
_STYLE = """
<style>
:root {
  --ink-900: #14181f;
  --ink-600: #4a5568;
  --ink-400: #64748b;
  --rule: #e2e5ea;
  --surface-2: #f7f8fa;
  --accent: #1e4f8f;
  --accent-weak: #eef3fa;
  --critical: #d03b3b;
  --warning: #fab219;
  --good: #0ca30c;
  --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
    Oxygen, Ubuntu, Cantarell, "Helvetica Neue", Arial, sans-serif;
  --font-mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
    "Liberation Mono", monospace;
}

/* Kill the whole Streamlit top strip: rainbow bar, deploy button, running
   indicator, kebab menu. Anything we want to show belongs to the app itself. */
[data-testid="stHeader"],
[data-testid="stDecoration"],
[data-testid="stToolbar"],
[data-testid="stStatusWidget"],
#MainMenu,
footer { display: none !important; }

html, body, [data-testid="stAppViewContainer"] {
  font-family: var(--font-sans);
  color: var(--ink-900);
  background-color: #ffffff;
}

[data-testid="stMainBlockContainer"],
[data-testid="stAppViewBlockContainer"],
.block-container {
  max-width: 720px !important;
  padding-top: 2rem !important;
  padding-bottom: 3rem !important;
}

h1, h2, h3, h4 {
  color: var(--ink-900);
  font-weight: 600;
  letter-spacing: -0.005em;
}
h1 { font-size: 1.75rem; margin: 0 0 0.25rem; }
h2 { font-size: 1.15rem; margin-top: 1.5rem; }
p, li, label { color: var(--ink-900); }
small, [data-testid="stCaptionContainer"] { color: var(--ink-600); }

code, kbd, samp,
[data-testid="stMarkdownContainer"] code {
  font-family: var(--font-mono) !important;
  font-variant-numeric: tabular-nums;
  background: var(--accent-weak) !important;
  padding: 0 0.25rem;
  border-radius: 2px;
  color: var(--ink-900) !important;
}

/* Streamlit alerts carry meaning in a coloured icon; we replace with text
   badges below. Hide the built-in icon so no emoji leak through. */
[data-testid="stAlert"] svg { display: none !important; }
[data-testid="stAlert"] {
  background: var(--surface-2);
  border-left: 3px solid var(--ink-400);
  color: var(--ink-900);
}

/* Primary buttons need white text against --accent for contrast. Streamlit
   themes only the background. */
[data-testid="stBaseButton-primaryFormSubmit"],
[data-testid="stBaseButton-primary"] {
  color: #ffffff !important;
  border-color: var(--accent) !important;
}
[data-testid="stBaseButton-primaryFormSubmit"]:hover,
[data-testid="stBaseButton-primary"]:hover {
  background: #163e70 !important;
  border-color: #163e70 !important;
}

.medrag-wordmark {
  color: var(--accent);
  font-family: var(--font-sans);
  font-size: 0.7rem;
  letter-spacing: 0.22em;
  font-weight: 600;
  text-transform: uppercase;
  margin-bottom: 0.25rem;
}
.medrag-rule {
  height: 1px;
  background: var(--rule);
  margin: 0.75rem 0 1.5rem;
  border: 0;
}

.medrag-stats {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  border-top: 1px solid var(--rule);
  border-bottom: 1px solid var(--rule);
  margin: 1.25rem 0;
}
.medrag-stat {
  padding: 0.75rem 1rem;
  border-right: 1px solid var(--rule);
}
.medrag-stat:last-child { border-right: 0; }
.medrag-stat-label {
  font-size: 0.7rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--ink-400);
  margin-bottom: 0.3rem;
}
.medrag-stat-value {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  font-size: 1.5rem;
  color: var(--ink-900);
  font-weight: 500;
  line-height: 1.2;
}

.medrag-badge {
  border-left: 3px solid var(--ink-400);
  background: var(--surface-2);
  padding: 0.6rem 0.85rem;
  margin: 0.75rem 0;
}
.medrag-badge--critical { border-left-color: var(--critical); }
.medrag-badge--warning  { border-left-color: var(--warning); }
.medrag-badge--good     { border-left-color: var(--good); }
.medrag-badge-label {
  font-size: 0.68rem;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--ink-600);
  font-weight: 600;
  margin-bottom: 0.2rem;
}
.medrag-badge-body { color: var(--ink-900); font-size: 0.95rem; line-height: 1.4; }
</style>
"""

st.markdown(_STYLE, unsafe_allow_html=True)


def _header() -> None:
    st.markdown(
        '<div class="medrag-wordmark">MEDRAG</div>'
        '<h1>Diligence memo</h1>'
        '<div class="medrag-rule"></div>',
        unsafe_allow_html=True,
    )


def _stat_row(stats: list[tuple[str, str]]) -> None:
    parts = ['<div class="medrag-stats">']
    for label, value in stats:
        parts.append(
            '<div class="medrag-stat">'
            f'<div class="medrag-stat-label">{html.escape(label)}</div>'
            f'<div class="medrag-stat-value">{html.escape(value)}</div>'
            '</div>'
        )
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


def _badge(kind: str, label: str, body: str) -> None:
    """Text badge with a coloured left rule; text stays --ink-900 so hue is
    never the only signal. `kind` is one of: critical, warning, good."""
    st.markdown(
        f'<div class="medrag-badge medrag-badge--{kind}">'
        f'<div class="medrag-badge-label">{html.escape(label)}</div>'
        f'<div class="medrag-badge-body">{html.escape(body)}</div>'
        "</div>",
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------- helpers


def needs_passphrase(cfg) -> bool:
    manifest = cfg.index_dir / "manifest.json"
    if manifest.exists():
        try:
            if json.loads(manifest.read_text()).get("encrypted"):
                return True
        except (json.JSONDecodeError, OSError):
            pass
    return is_encrypted(cfg.raw_dir / CORPUS_FILE)


def loaded_summary(cfg) -> str:
    bits = []
    trials_path = cfg.raw_dir / TRIALS_DB
    if trials_path.exists():
        try:
            with TrialStore(trials_path) as store:
                s = store.stats()
            bits.append(f"{s['total']} trial records")
        except Exception:
            pass
    manifest = cfg.index_dir / "manifest.json"
    if manifest.exists():
        try:
            bits.append(f"{json.loads(manifest.read_text()).get('n_chunks', 0)} literature passages")
        except (json.JSONDecodeError, OSError):
            pass
    return " · ".join(bits) if bits else "nothing loaded yet"


def render_setup() -> None:
    """First-run configuration. Free options first, paid last."""
    st.subheader("One-time setup")
    st.write(
        "Choose how MedRAG should write its summaries. **The free options are "
        "genuinely free** — no credit card, no trial period."
    )

    order = [p.key for p in FREE_PROVIDERS] + ["openai", "none"]
    labels = {k: PROVIDERS[k].label for k in order}
    choice = st.radio(
        "Provider",
        order,
        format_func=lambda k: labels[k],
        index=0,
        label_visibility="collapsed",
    )
    provider = PROVIDERS[choice]
    st.caption(provider.notes)

    key_value = None
    if provider.needs_key:
        if provider.signup_url:
            st.markdown(
                f"**Get a key:** [{provider.signup_url}]({provider.signup_url}) — "
                "sign up, create a key, copy it, paste it below."
            )
        key_value = st.text_input(
            f"{provider.label.split('—')[0].strip()} API key",
            type="password",
            help="Stored in a local file on this computer only. Never displayed again.",
        )
    elif provider.key == "ollama":
        st.markdown(
            f"**Install Ollama first:** [{provider.signup_url}]({provider.signup_url}), "
            "then run `ollama pull llama3.1:8b` once. Nothing leaves this computer."
        )

    if st.button("Save and continue", type="primary", use_container_width=True):
        if provider.needs_key:
            ok, message = key_looks_valid(provider.key, key_value or "")
            if not ok:
                st.error(message)
                return
            if message:
                st.warning(message)
            write_env({"MEDRAG_PROVIDER": provider.key, provider.env_var: key_value.strip()})
        else:
            write_env({"MEDRAG_PROVIDER": provider.key})
        st.success("Saved. Starting up…")
        st.rerun()

    with st.expander("What if I pick “No AI”?"):
        st.write(
            "Everything still works and the memo is still fully cited — but instead "
            "of written summaries you get the retrieved evidence listed under each "
            "question, and the contradicting-evidence hunt does not run. That last "
            "part is the most useful section of the memo, so it is worth setting up "
            "one of the free options."
        )


# --------------------------------------------------------------- page

cfg = load_config()
cfg.ensure_dirs()

_header()

env = read_env()
configured = bool(env.get("MEDRAG_PROVIDER"))

if not configured:
    st.caption(
        "Enter a drug and a disease, and this produces a diligence memo from the "
        "clinical trial registry and published literature, with a citation on every claim."
    )
    render_setup()
    st.stop()

provider = get_provider(cfg.provider)

st.caption(
    "Enter a drug and a disease. MedRAG finds the research, reads it, and writes a "
    "memo where every claim is cited. First run for a new drug takes a few minutes."
)

passphrase = None
if needs_passphrase(cfg):
    # Collected here, never via getpass: a terminal prompt inside a web server
    # blocks forever with no visible error.
    _badge(
        "warning",
        "ENCRYPTED CORPUS",
        "The stored research is encrypted. Enter the passphrase to unlock it.",
    )
    passphrase = st.text_input("Passphrase", type="password") or None
    if not passphrase:
        st.stop()
    cfg.encrypt = True
    cfg.passphrase = passphrase

if provider.key == "none":
    _badge(
        "warning",
        "NO MODEL CONFIGURED",
        "Memos will list evidence rather than summarise it, and the "
        "contradicting-evidence section will not run. Change this under Settings below.",
    )

# --------------------------------------------------------------- form

with st.form("diligence"):
    asset = st.text_input("Drug or asset name", placeholder="e.g. empagliflozin")
    indication = st.text_input("Disease or indication", placeholder="e.g. heart failure")
    submitted = st.form_submit_button("Generate memo", type="primary", use_container_width=True)

with st.expander("Settings"):
    st.markdown(f"**Provider:** {provider.label}")
    st.markdown(f"**Model:** `{cfg.chat_model}`")
    st.caption(f"Research loaded: {loaded_summary(cfg)}")

    question_files = sorted(Path("config").glob("*.yaml"))
    labels = [p.name for p in question_files] or ["diligence_questions.yaml"]
    chosen = st.selectbox("Question set", labels)
    st.caption(
        "The list of questions asked about every asset. Edit the file in `config/` "
        "to change what every memo covers."
    )
    refresh = st.checkbox(
        "Re-download research even if some is already stored",
        help="Use this when the stored research is out of date.",
    )
    if st.button("Change provider or key"):
        write_env({"MEDRAG_PROVIDER": ""})
        st.rerun()

# --------------------------------------------------------------- run

if submitted:
    if not asset.strip():
        st.error("Please enter a drug or asset name.")
        st.stop()

    try:
        qs = load_question_set(Path("config") / chosen)
    except Exception as exc:
        st.error(
            f"The question set '{chosen}' could not be read ({exc}). It may have been "
            "edited into an invalid state — restore the previous version of that file."
        )
        st.stop()

    bar = st.progress(0.0, text="Starting…")
    note = st.empty()
    runner = None

    try:
        # --- stage 1: make sure the research exists (no terminal step) ---
        def on_progress(fraction: float, message: str) -> None:
            bar.progress(min(0.35 * fraction, 0.35), text=message)

        report = ensure_data(
            cfg, asset.strip(), indication.strip(), progress=on_progress, force=refresh
        )
        for err in report.errors:
            st.warning(err)

        if not report.skipped and not report.literature_added and not report.trials_added:
            bar.empty()
            st.error(
                f"No research could be found or downloaded for “{asset}”. Check the "
                "spelling of the drug name, or try a broader indication. If this "
                "computer is on a company or campus network, it may be blocking the "
                "sources — try a different network."
            )
            st.stop()

        note.caption(report.summary())

        # --- stage 2: answer each question ---
        runner = DiligenceRunner(cfg)
        sections = []
        for i, q in enumerate(qs.questions, 1):
            bar.progress(0.35 + 0.6 * (i - 1) / len(qs), text=f"{i} of {len(qs)} — {q.section}")
            sections.append(runner.run_question(q, asset.strip(), indication.strip()))

        memo = MemoResult(
            asset=asset.strip(),
            indication=indication.strip(),
            question_set=qs.name,
            sections=sections,
            embedder=runner.rag.embedder.name if runner.rag else "none",
            model=cfg.chat_model,
            warnings=list(runner.warnings),
        )

        bar.progress(0.97, text="Writing the memo…")
        paths = export(memo, OUT_DIR)
        bar.empty()
        note.empty()

    except CryptoError:
        bar.empty()
        st.error("That passphrase did not unlock the stored research. Check it and try again.")
        st.stop()
    except Exception as exc:
        bar.empty()
        st.error(
            "Something went wrong and the memo was not completed. Nothing was saved. "
            "If this keeps happening, send the details below to whoever maintains this tool."
        )
        with st.expander("Technical details"):
            st.code(f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}")
        st.stop()
    finally:
        if runner is not None:
            runner.close()

    # ------------------------------------------------------------ result

    cov = memo.coverage()

    _stat_row([
        ("Sections with evidence", f"{cov['sections_with_evidence']}/{cov['sections']}"),
        ("Trials stopped early", str(cov["stopped_trials_found"])),
        ("Findings against", str(cov["contradicting_findings"])),
    ])

    # Priority-ordered: an outright warning takes precedence over "COMPLETE".
    # Both non-clean states are shown when both apply so nothing is buried.
    something_wrong = False
    if cov["stopped_trials_found"]:
        _badge(
            "critical",
            "TRIALS STOPPED EARLY",
            f"{cov['stopped_trials_found']} trial(s) here were stopped early. They are "
            "listed under Contradicting or unsupportive evidence — read that first.",
        )
        something_wrong = True
    if cov["sections_with_evidence"] < cov["sections"]:
        missing = cov["sections"] - cov["sections_with_evidence"]
        _badge(
            "warning",
            "INCOMPLETE COVERAGE",
            f"{missing} section(s) found no evidence. The memo is thin here rather "
            "than reassuring — there may simply be little published on this asset.",
        )
        something_wrong = True
    if cov["sections_assessed"] == 0:
        _badge(
            "warning",
            "UNVERIFIED",
            "No faithfulness check ran on this memo — that requires an AI provider. "
            "The citations are still real; nothing verified the wording around them.",
        )
        something_wrong = True
    if not something_wrong:
        _badge("good", "COMPLETE", f"Memo ready for {memo.asset}.")

    stamp = datetime.now().strftime("%Y-%m-%d")
    left, right = st.columns(2)
    left.download_button(
        "Download memo (PDF)",
        data=paths["pdf"].read_bytes(),
        file_name=f"{paths['pdf'].stem}-{stamp}.pdf",
        mime="application/pdf",
        type="primary",
        use_container_width=True,
    )
    right.download_button(
        "Download memo (Markdown)",
        data=paths["markdown"].read_bytes(),
        file_name=f"{paths['markdown'].stem}-{stamp}.md",
        mime="text/markdown",
        use_container_width=True,
    )
    st.caption(f"Also saved in the `{OUT_DIR}` folder. The PDF is the version to circulate.")

    with st.expander("Preview the memo"):
        st.markdown(paths["markdown"].read_text(encoding="utf-8"))

st.divider()
st.caption(
    "MedRAG summarises public sources — PubMed and the ClinicalTrials.gov registry. "
    "Every claim is traceable to a cited passage. It is a research aid, not investment "
    "advice and not medical advice, and citations should be checked against the "
    "original source before they inform a decision."
)
