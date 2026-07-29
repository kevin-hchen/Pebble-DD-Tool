"""Command-line interface: medrag ingest | index | ask | stats."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load_config
from .crypto import ENV_PASSPHRASE, CryptoError, get_passphrase, is_encrypted
from .diligence import DiligenceRunner, load_question_set
from .ingest.store import load_corpus
from .memo import export
from .pipeline import CORPUS_FILE, TRIALS_DB, MedRAG, build_index, ingest_pdfs, ingest_pubmed
from .router import Router
from .trials.client import search_trials
from .trials.store import TrialStore


def _config_from(args) -> "object":
    """Apply global privacy flags on top of the environment-derived config."""
    cfg = load_config()
    if getattr(args, "encrypt", False):
        cfg.encrypt = True
    if getattr(args, "offline", False):
        cfg.offline = True
        cfg.openai_api_key = None
    return cfg


def cmd_ingest(args) -> int:
    cfg = _config_from(args)
    if not args.query and not args.pdf_dir:
        print("error: pass --query and/or --pdf-dir", file=sys.stderr)
        return 2
    if args.query:
        ingest_pubmed(args.query, retmax=args.max_results, cfg=cfg)
    if args.pdf_dir:
        ingest_pdfs(args.pdf_dir, cfg=cfg, acknowledge_phi=args.yes)
    if args.index:
        build_index(cfg)
    return 0


def cmd_index(args) -> int:
    build_index(_config_from(args))
    return 0


def cmd_ask(args) -> int:
    rag = MedRAG(_config_from(args))
    result = rag.ask(args.question, k=args.k)

    print(f"\n{'=' * 72}\nQ: {result.question}\n{'=' * 72}\n")
    print(result.answer.text)
    if result.answer.sources:
        print(f"\n{'-' * 72}\nSources\n{'-' * 72}")
        print(result.answer.bibliography())
    print(f"\nValidation: {result.validation.summary()}")
    print(f"Model: {result.answer.model} | embedder: {rag.embedder.name}")
    return 0


def cmd_trials(args) -> int:
    """Ingest clinicaltrials.gov records into the structured store."""
    cfg = _config_from(args)
    cfg.ensure_dirs()

    records = search_trials(
        condition=args.condition,
        intervention=args.intervention,
        sponsor=args.sponsor,
        status=args.status,
        max_records=args.max_records,
        offline=cfg.offline,
    )
    print(f"[medrag] {len(records)} trial records fetched")

    with TrialStore(cfg.raw_dir / TRIALS_DB) as store:
        store.upsert(records)
        stats = store.stats()

    print(f"[medrag] trial store now holds {stats['total']} records")
    print(f"[medrag] stopped early: {stats['stopped']}, with a stated reason: "
          f"{stats['stopped_with_reason']} (fill rate {stats['why_stopped_fill_rate']})")
    return 0


def cmd_fda(args) -> int:
    """Ingest openFDA device data (clearances, recalls, adverse events) into the
    structured store. Match on product code and/or device name — never company."""
    from .fda.client import count_510k, search_510k, search_events, search_recalls
    from .fda.store import FDAStore
    from .pipeline import FDA_DB

    cfg = _config_from(args)
    cfg.ensure_dirs()

    if not args.product_code and not args.device_name:
        print("error: pass --product-code and/or --device-name", file=sys.stderr)
        return 2

    clearances = search_510k(product_code=args.product_code, device_name=args.device_name,
                             max_records=args.max_records, offline=cfg.offline)
    # The openFDA-reported total for the category, so the memo can later say how
    # much of it the local store actually holds.
    category_total = count_510k(product_code=args.product_code, device_name=args.device_name,
                                offline=cfg.offline)
    recalls = search_recalls(product_code=args.product_code, device_name=args.device_name,
                             max_records=args.max_records, offline=cfg.offline)
    # MAUDE is enormous; events need a product code and a hard cap.
    events = []
    code = args.product_code
    if not code and clearances:
        code = clearances[0].product_code
    if code:
        events = search_events(product_code=code, max_records=args.max_events, offline=cfg.offline)

    cat = f" (of {category_total} in the category)" if category_total else ""
    print(f"[medrag] fetched {len(clearances)} clearances{cat}, {len(recalls)} recalls, "
          f"{len(events)} adverse events")

    resolved_code = args.product_code or (clearances[0].product_code if clearances else None)
    with FDAStore(cfg.raw_dir / FDA_DB) as store:
        store.upsert_clearances(clearances)
        store.upsert_recalls(recalls)
        store.upsert_events(events)
        store.set_category_total(resolved_code, category_total)
        stats = store.stats()

    print(f"[medrag] FDA store now holds {stats['clearances']} clearances, "
          f"{stats['recalls']} recalls, {stats['events']} events "
          f"across {stats['product_codes']} product code(s)")
    return 0


def cmd_route(args) -> int:
    """Show how a question would be routed, without answering it."""
    cfg = _config_from(args)
    decision = Router(cfg).route(args.question)
    print(f"question : {args.question}")
    print(f"route    : {decision.route.value.upper()}")
    print(f"method   : {decision.method}")
    print(f"reason   : {decision.reason}")
    print(f"filters  : {decision.filters or '(none)'}")
    print(f"consults : trials={decision.needs_trials}, literature={decision.needs_literature}")
    return 0


def cmd_diligence(args) -> int:
    """Run the fixed question set against an asset and export a memo."""
    cfg = _config_from(args)
    cfg.ensure_dirs()

    qs = load_question_set(args.questions)
    print(f"[medrag] question set: {qs.name} ({len(qs)} questions)")

    runner = DiligenceRunner(cfg)
    try:
        memo = runner.run(asset=args.asset, indication=args.indication, question_set=qs)
    finally:
        runner.close()

    paths = export(memo, args.out_dir, stem=args.stem)
    cov = memo.coverage()

    print()
    for w in memo.warnings:
        print(f"[warn] {w}")
    print(f"[medrag] sections with evidence: "
          f"{cov['sections_with_evidence']}/{cov['sections']}")
    print(f"[medrag] checked for faithfulness: "
          f"{cov['sections_assessed']}/{cov['sections']}")
    if cov["sections_assessed"]:
        print(f"[medrag] of those, passing:       "
              f"{cov['sections_passing_validation']}/{cov['sections_assessed']}")
    else:
        print("[medrag] of those, passing:       n/a — no model ran, so nothing was checked")
    print(f"[medrag] trials stopped early:   {cov['stopped_trials_found']}")
    print(f"[medrag] findings against:       {cov['contradicting_findings']}")
    print(f"[medrag] markdown -> {paths['markdown']}")
    print(f"[medrag] pdf      -> {paths['pdf']}")
    return 0


def cmd_verify(args) -> int:
    """Check a list of claims (one per line) against independent evidence."""
    from .claims import (
        ClaimVerifier,
        ConfirmationRequired,
        parse_claims_text,
        transmission_notice,
    )
    from .claims_memo import export as export_claims

    cfg = _config_from(args)
    cfg.ensure_dirs()

    path = Path(args.claims)
    if not path.exists():
        print(f"error: claims file not found: {path}", file=sys.stderr)
        return 2
    claims = parse_claims_text(path.read_text(encoding="utf-8"))
    if not claims:
        print(f"error: no claims found in {path} (one claim per line)", file=sys.stderr)
        return 2
    print(f"[medrag] {len(claims)} claim(s) to verify")

    # Confidentiality gate. Show exactly what would leave the machine and to
    # which provider, and require a per-run yes. A local provider skips this.
    notice = transmission_notice(cfg, claims, kind="claims")
    if notice.local:
        print(f"[medrag] {notice.render()}")
        confirmed = True
    else:
        print()
        print(notice.render())
        print()
        if args.yes:
            confirmed = True
        elif not sys.stdin.isatty():
            print(
                "error: this run would transmit claim text to an external provider. "
                "Re-run with --yes to confirm, or configure a local provider "
                "(ollama / none / --offline).",
                file=sys.stderr,
            )
            return 2
        else:
            reply = input("Transmit these claims for this run? [y/N] ").strip().lower()
            confirmed = reply in ("y", "yes")
        if not confirmed:
            print("[medrag] not confirmed — nothing was transmitted.")
            return 1

    verifier = ClaimVerifier(cfg)
    try:
        report = verifier.verify(
            claims,
            asset=args.asset,
            indication=args.indication,
            company=args.company,
            confirmed=confirmed,
            progress=True,
        )
    except ConfirmationRequired as exc:
        # Defence in depth: verify() gates too, so a mismatch here is a bug.
        print(exc.notice.render(), file=sys.stderr)
        return 1
    finally:
        verifier.close()

    paths = export_claims(report, args.out_dir, stem=args.stem)
    support = report.support_counts()
    independence = report.independence_counts()

    print()
    for w in report.warnings:
        print(f"[warn] {w}")
    print("[medrag] support (does the evidence back the claim?)")
    for verdict in ("SUPPORTED", "PARTIALLY SUPPORTED", "CONTRADICTED", "NOT FOUND",
                    "NOT VERIFIABLE", "UNVERIFIED"):
        print(f"[medrag]   {verdict:<22} {support.get(verdict, 0)}")
    print("[medrag] independence (whose evidence is it?)")
    for label in ("COMPANY-LINKED", "MIXED", "NO DISCLOSURE", "INDEPENDENT", "N/A"):
        print(f"[medrag]   {label:<22} {independence.get(label, 0)}")
    print(f"[medrag] markdown -> {paths['markdown']}")
    print(f"[medrag] pdf      -> {paths['pdf']}")
    return 0


def cmd_landscape(args) -> int:
    """Enumerate the trials a patient with a condition + biomarker could enter."""
    from .landscape import build_landscape
    from .landscape_memo import export as export_landscape

    cfg = _config_from(args)
    cfg.ensure_dirs()

    db = cfg.raw_dir / TRIALS_DB
    if not db.exists():
        print("error: no trial store found. Ingest trials for the condition first, e.g.\n"
              f'    python -m medrag trials --condition "{args.condition}"',
              file=sys.stderr)
        return 2

    # A stale (pre-eligibility) database raises TrialStoreSchemaError here; the
    # top-level handler prints its rebuild instruction.
    with TrialStore(db) as store:
        landscape = build_landscape(
            store, condition=args.condition, biomarker=args.biomarker,
            location=args.location or "",
        )

    paths = export_landscape(landscape, args.out_dir, stem=args.stem)

    print()
    for w in landscape.warnings:
        print(f"[warn] {w}")
    print(f"[medrag] {landscape.counts_line()}")
    print(f"[medrag] markdown -> {paths['markdown']}")
    print(f"[medrag] pdf      -> {paths['pdf']}")
    return 0


def cmd_doctor(args) -> int:
    """Check that the services this tool depends on are actually reachable.

    Exists because a blocked outbound request looks exactly like a broken
    client, and telling those apart by reading a traceback wastes an afternoon.
    """
    import requests

    from .providers import effective_provider, make_client

    cfg = _config_from(args)
    provider = effective_provider(cfg)
    ok = True

    print("MedRAG connectivity check\n" + "=" * 46)

    print(f"{'offline mode':<22} {'ON (all network disabled)' if cfg.offline else 'off'}")
    print(f"{'provider':<22} {provider.key}")
    if provider.needs_key:
        print(f"{provider.env_var:<22} {'set' if cfg.openai_api_key else 'NOT SET'}")

    if cfg.offline:
        print("\nOffline mode is on, so no network checks were run.")
        return 0

    checks = [
        ("PubMed E-utilities", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
         {"db": "pubmed", "term": "aspirin", "retmax": 1, "retmode": "json"}),
        ("ClinicalTrials.gov v2", "https://clinicaltrials.gov/api/v2/studies",
         {"pageSize": 1}),
    ]
    for name, url, params in checks:
        try:
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            print(f"{name:<22} OK ({r.status_code})")
        except Exception as exc:
            ok = False
            kind = type(exc).__name__
            print(f"{name:<22} FAILED — {kind}")
            if "Proxy" in kind or "proxy" in str(exc).lower():
                print(f"{'':<22}   a proxy is intercepting and blocking this host.")
                print(f"{'':<22}   The client is fine; the network is not.")
                print(f"{'':<22}   Try again off a restricted network, or unset")
                print(f"{'':<22}   HTTPS_PROXY / https_proxy.")

    # Model provider is a degradation, not a hard dependency: a memo can still be
    # produced without a model. So its failure does NOT set the exit code — the
    # data sources above are the real must-haves. Also, the check must use the
    # configured provider's client (via make_client), not a bare OpenAI() call,
    # so a Groq key does not get sent to api.openai.com.
    provider_label = f"{provider.label.split(' — ')[0]} API"
    if provider.key == "none":
        print(f"{provider_label:<22} SKIPPED — no provider configured (memos list evidence only)")
    else:
        client = make_client(cfg)
        if client is None:
            print(f"{provider_label:<22} SKIPPED — set {provider.env_var} to enable this check")
        else:
            try:
                client.models.list()
                print(f"{provider_label:<22} OK")
            except Exception as exc:
                print(f"{provider_label:<22} WARN — {type(exc).__name__}: {str(exc)[:80]}")
                print(f"{'':<22}   memos will fall back to extractive evidence lists.")

    print("\n" + ("All checks passed." if ok else "Some checks failed — see above."))
    return 0 if ok else 1


def cmd_stats(args) -> int:
    cfg = _config_from(args)
    corpus_path = cfg.raw_dir / CORPUS_FILE

    if is_encrypted(corpus_path):
        print(f"corpus: encrypted at rest ({corpus_path})")
        if not (cfg.encrypt or cfg.passphrase):
            print("  (pass --encrypt to unlock and show document counts)")
            docs = []
        else:
            docs = load_corpus(corpus_path, get_passphrase())
    else:
        docs = load_corpus(corpus_path)
        print(f"corpus: {len(docs)} documents ({corpus_path}, plaintext)")

    by_source: dict[str, int] = {}
    for d in docs:
        by_source[d.source] = by_source.get(d.source, 0) + 1
    for src, n in sorted(by_source.items()):
        print(f"  {src}: {n}")

    manifest = cfg.index_dir / "manifest.json"
    print(f"index: {manifest.read_text()}" if manifest.exists() else "index: not built")

    trials_path = cfg.raw_dir / TRIALS_DB
    if trials_path.exists():
        with TrialStore(trials_path) as store:
            s = store.stats()
        print(f"trials: {s['total']} records, {s['stopped']} stopped early "
              f"({s['stopped_with_reason']} with a stated reason, "
              f"fill rate {s['why_stopped_fill_rate']})")
        for status, n in list(s["by_status"].items())[:6]:
            print(f"  {status}: {n}")
    else:
        print("trials: not ingested")

    print(f"offline mode: {'on' if cfg.offline else 'off'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="medrag", description="Clinical literature RAG assistant")

    # Privacy flags apply to every subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--encrypt",
        action="store_true",
        help="encrypt the corpus and index at rest (prompts for a passphrase, "
        f"or reads ${ENV_PASSPHRASE})",
    )
    common.add_argument(
        "--offline",
        action="store_true",
        help="hard-block all outbound network calls; nothing leaves this machine",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_ing = sub.add_parser("ingest", parents=[common], help="fetch PubMed records and/or load local PDFs")
    p_ing.add_argument("--query", "-q", help='PubMed search string, e.g. "SGLT2 inhibitors heart failure"')
    p_ing.add_argument("--max-results", "-n", type=int, default=50)
    p_ing.add_argument("--pdf-dir", help="directory of PDFs to ingest")
    p_ing.add_argument("--index", action="store_true", help="build the index immediately after")
    p_ing.add_argument(
        "--yes", action="store_true", help="acknowledge the PHI notice when ingesting local PDFs"
    )
    p_ing.set_defaults(func=cmd_ingest)

    p_idx = sub.add_parser("index", parents=[common], help="chunk, embed and index the stored corpus")
    p_idx.set_defaults(func=cmd_index)

    p_ask = sub.add_parser("ask", parents=[common], help="ask a question against the index")
    p_ask.add_argument("question")
    p_ask.add_argument("-k", type=int, default=None, help="number of passages to retrieve")
    p_ask.set_defaults(func=cmd_ask)

    p_tr = sub.add_parser("trials", parents=[common], help="ingest clinicaltrials.gov records")
    p_tr.add_argument("--condition", "-c", help='indication, e.g. "heart failure"')
    p_tr.add_argument("--intervention", "-i", help='drug or intervention, e.g. "empagliflozin"')
    p_tr.add_argument("--sponsor", "-s", help="lead sponsor name")
    p_tr.add_argument(
        "--status",
        nargs="+",
        help="filter by overall status, e.g. --status TERMINATED WITHDRAWN SUSPENDED",
    )
    p_tr.add_argument("--max-records", "-n", type=int, default=200)
    p_tr.set_defaults(func=cmd_trials)

    p_fda = sub.add_parser("fda", parents=[common],
                           help="ingest openFDA device clearances, recalls and adverse events")
    p_fda.add_argument("--product-code", "-p", help='device product code, e.g. "FRN" (infusion pump)')
    p_fda.add_argument("--device-name", "-d", help='device name, e.g. "infusion pump"')
    p_fda.add_argument("--max-records", "-n", type=int, default=200,
                       help="cap on clearances and recalls fetched")
    p_fda.add_argument("--max-events", type=int, default=100,
                       help="cap on MAUDE adverse-event reports (the endpoint is enormous)")
    p_fda.set_defaults(func=cmd_fda)

    p_rt = sub.add_parser("route", parents=[common], help="show how a question would be routed")
    p_rt.add_argument("question")
    p_rt.set_defaults(func=cmd_route)

    p_dg = sub.add_parser("diligence", parents=[common],
                          help="run the fixed question set and export a memo")
    p_dg.add_argument("--asset", "-a", required=True, help='asset or drug name, e.g. "Compound X"')
    p_dg.add_argument("--indication", "-i", default="", help='indication, e.g. "heart failure"')
    p_dg.add_argument("--questions", "-q", default=None,
                      help="path to a question set YAML (defaults to config/diligence_questions.yaml)")
    p_dg.add_argument("--out-dir", "-o", default="out", help="directory for the memo files")
    p_dg.add_argument("--stem", default=None, help="filename stem for the outputs")
    p_dg.set_defaults(func=cmd_diligence)

    p_vf = sub.add_parser("verify", parents=[common],
                          help="check a founder's claims against independent evidence")
    p_vf.add_argument("--claims", "-f", required=True,
                      help="path to a claims file, one claim per line ('#' lines are comments)")
    p_vf.add_argument("--asset", "-a", default="", help='asset or drug name, e.g. "Compound X"')
    p_vf.add_argument("--indication", "-i", default="", help='indication, e.g. "heart failure"')
    p_vf.add_argument("--company", "-c", default="",
                      help="manufacturer name, used to flag company-authored evidence")
    p_vf.add_argument("--out-dir", "-o", default="out", help="directory for the result files")
    p_vf.add_argument("--stem", default=None, help="filename stem for the outputs")
    p_vf.add_argument("--yes", action="store_true",
                      help="confirm transmitting claim text to the configured provider for this run")
    p_vf.set_defaults(func=cmd_verify)

    p_ls = sub.add_parser("landscape", parents=[common],
                          help="enumerate trials a patient could enter, by condition + biomarker")
    p_ls.add_argument("--condition", "-c", required=True, help='condition, e.g. "colorectal cancer"')
    p_ls.add_argument("--biomarker", "-b", required=True,
                      help='biomarker the patient has, e.g. "MSS" (microsatellite stable)')
    p_ls.add_argument("--location", "-l", default="",
                      help="optional city/state/country; matching sites sort to the top")
    p_ls.add_argument("--out-dir", "-o", default="out", help="directory for the table files")
    p_ls.add_argument("--stem", default=None, help="filename stem for the outputs")
    p_ls.set_defaults(func=cmd_landscape)

    p_dr = sub.add_parser("doctor", parents=[common],
                          help="check that PubMed, ClinicalTrials.gov and OpenAI are reachable")
    p_dr.set_defaults(func=cmd_doctor)

    p_st = sub.add_parser("stats", parents=[common], help="show corpus, index and trial status")
    p_st.set_defaults(func=cmd_stats)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, RuntimeError, CryptoError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
