"""Invariants that `CLAUDE.md` used to hold only as prose.

Prose can be ignored; a test cannot. Each of these was a paragraph in
`CLAUDE.md` beginning "do not", "never", or "must not be quietly reversed", with
nothing in the suite that would fail if someone reversed it. The paragraph is
still there — trimmed to the statement plus one sentence of why — but the
enforcement now lives here.

Deliberately NOT pinned here: anything already asserted elsewhere in the suite
(the marker parity check, the FAERS `dir()` guard, the phrasing lint, the
ranking config pins). A second copy of an existing assertion is not coverage; it
is two things to keep in sync.

Run: python -m pytest tests/test_invariants_pinned.py -q
     (or: python tests/test_invariants_pinned.py)
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Direct runs do not load conftest.py, so the no-network guard is installed
# here too. See tests/netguard.py.
from tests import netguard  # noqa: E402

netguard.install()

from medrag import claims  # noqa: E402
from medrag.config import Config  # noqa: E402
from medrag.documents import Document  # noqa: E402
from medrag.negative_evidence import CONTRADICTION_PROMPT  # noqa: E402
from medrag.pipeline import CORPUS_FILE, TRIALS_DB, build_index  # noqa: E402
from medrag.trials.store import TrialRecord, TrialStore  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# "Trial records are NOT in the vector index."
# --------------------------------------------------------------------------

def test_the_index_build_path_never_embeds_a_trial_record(tmp_path, capsys):
    """Phase and status are filters, not semantics.

    Embedding "Phase 3, TERMINATED" as prose and hoping cosine similarity
    recovers it destroys the precision the registry exists for — so the index
    build reads the corpus and nothing else. Driven end to end with a populated
    trial store sitting in the same data directory, because the way this
    invariant would actually be reversed is someone adding trials to the
    build's document source "so the router has one place to look".
    """
    cfg = Config(data_dir=tmp_path, offline=True, embed_backend="hashing")
    cfg.ensure_dirs()

    store = TrialStore(cfg.raw_dir / TRIALS_DB)
    store.upsert([
        TrialRecord(
            nct_id="NCT99999999",
            brief_title="A study of quaxifentanil in refractory widgetosis",
            brief_summary="Quaxifentanil versus placebo in widgetosis.",
            eligibility_criteria="Inclusion Criteria: histologically confirmed widgetosis.",
            phase="Phase 3",
            overall_status="TERMINATED",
            why_stopped="Sponsor decision",
        )
    ])

    corpus_doc = Document(
        doc_id="12345678",
        title="Widgetosis: a review",
        text=(
            "Background: widgetosis is rare. "
            "Results: no approved therapy exists. "
            "Conclusions: further study is needed."
        ),
    )
    from medrag.ingest.store import save_corpus

    save_corpus([corpus_doc], cfg.raw_dir / CORPUS_FILE)

    index = build_index(cfg)
    capsys.readouterr()

    blob = " ".join(c.text for c in index.chunks)
    assert index.chunks, "the fixture must actually build an index, or this passes vacuously"
    assert "NCT99999999" not in blob, (
        "a trial record reached the vector index. Trials live in SQLite with "
        "indexes because phase and status are filters; embedding them as prose "
        "destroys the precision the registry exists for. See CLAUDE.md."
    )
    assert "quaxifentanil" not in blob.lower(), (
        "trial text reached the vector index even though the NCT id did not — "
        "the identifier is not the only thing that must stay out"
    )
    assert {c.doc_id for c in index.chunks} == {"12345678"}, (
        "the index build must take its documents from the corpus and nowhere else"
    )


# --------------------------------------------------------------------------
# "Support and independence are two orthogonal axes and must never re-merge."
# --------------------------------------------------------------------------

def test_the_support_and_independence_vocabularies_never_re_merge(sample=None):
    """The earlier design folded independence into the support value.

    `SUPPORTED - COMPANY SOURCE` hid an independent partial behind a scary
    label and a company-only support behind a reassuring one. The behavioural
    half of this is `test_support_and_independence_are_orthogonal` in
    test_claims.py, which checks one claim can carry any combination. This is
    the structural half: the two VOCABULARIES must stay disjoint, so the merge
    cannot be reintroduced by adding a value that means both.
    """
    support = {v.upper() for v in claims.SUPPORT_VALUES}
    independence = {v.upper() for v in claims.INDEPENDENCE_VALUES}

    assert support and independence, "both vocabularies must be non-empty"
    assert not (support & independence), (
        f"a value appears in both axes: {sorted(support & independence)}. "
        "Support and independence do not trade off and must not share a value."
    )

    independence_words = ("COMPANY", "DISCLOSURE", "INDEPENDENT", "MIXED")
    for value in support:
        for word in independence_words:
            assert word not in value, (
                f"support value {value!r} names the independence axis ({word}). "
                "This is the SUPPORTED - COMPANY SOURCE merge coming back: it "
                "hides an independent partial behind a scary label."
            )

    support_words = ("SUPPORT", "CONTRADICT", "NOT FOUND", "VERIFI")
    for value in independence:
        for word in support_words:
            assert word not in value, (
                f"independence value {value!r} names the support axis ({word})"
            )


# --------------------------------------------------------------------------
# "The negative-evidence model half may return an empty findings list."
# --------------------------------------------------------------------------

def test_the_contradiction_prompt_still_permits_an_empty_findings_list():
    """A forced-contradiction prompt manufactures one.

    An invented contradiction in a diligence memo is worse than silence, so the
    prompt says outright that an empty list is a normal and useful result and
    tells the model not to fill the section. Removing that permission reads
    like tightening the prompt and is the reversal this pins.
    """
    prompt = " ".join(CONTRADICTION_PROMPT.split()).lower()

    assert "empty findings list" in prompt, (
        "the contradiction prompt no longer tells the model an empty findings "
        "list is allowed. A forced-contradiction prompt manufactures one, and "
        "an invented contradiction is worse than silence. See CLAUDE.md."
    )
    assert "normal and useful" in prompt, (
        "the prompt permits an empty list but no longer says it is a normal and "
        "useful result — a permission the model reads as a last resort is not "
        "the permission that was written"
    )
    assert "do not manufacture" in prompt, (
        "the prompt no longer forbids manufacturing a weakness to fill the section"
    )


# --------------------------------------------------------------------------
# "JSONL is split on newline only, never str.splitlines()."
# --------------------------------------------------------------------------

#: The modules that read or write stored records. `str.splitlines()` is
#: legitimate elsewhere in the codebase — on a `.env` file, on user-pasted claim
#: text, on an exception message — so this is deliberately not a repo-wide ban.
#: These three are the ones where the data is a JSON string that may legally
#: contain U+2028.
STORED_DATA_READERS = (
    "medrag/jsonl.py",
    "medrag/ingest/store.py",
    "medrag/vectorstore.py",
)


def test_the_stored_data_readers_never_call_str_splitlines():
    """The single most expensive lesson in CLAUDE.md, as a check.

    One Cochrane record carried U+2028 inside its conflict-of-interest
    statement. `json.dumps` correctly leaves it unescaped — it is legal inside a
    JSON string — but `str.splitlines()` treats it as a line break, so a reader
    chopped one valid record into eight pieces, reported the corpus corrupt, and
    every later ingest died at the same offset.

    Parsed with `ast` rather than grepped, because `jsonl.py`'s own docstring
    names `splitlines()` several times explaining why it must not be used, and a
    grep cannot tell a warning from a call.
    """
    offenders = []
    for rel in STORED_DATA_READERS:
        path = REPO / rel
        assert path.exists(), f"{rel} no longer exists; update STORED_DATA_READERS"
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "splitlines":
                offenders.append(f"{rel}:{node.lineno}")

    assert not offenders, (
        f"str.splitlines() is called in a stored-data path: {offenders}. It "
        "breaks on U+2028, U+2029, U+000B, U+000C, U+001C-U+001E and U+0085, "
        "all legal inside a JSON string. Use medrag.jsonl.split_lines / "
        "iter_lines, which split on '\\n' alone. See CLAUDE.md."
    )


def test_the_splitlines_check_would_actually_catch_one():
    """The check above passes on a codebase with no calls at all, which is also
    what it would do if `ast.Attribute` stopped matching. Drive it against a
    module that does call `splitlines()` and assert it fires."""
    tree = ast.parse("def f(text):\n    return text.splitlines()\n")
    found = [n for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and n.attr == "splitlines"]
    assert found, "the AST pattern no longer matches a real .splitlines() call"


if __name__ == "__main__":
    import tempfile

    failures = 0
    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        try:
            if "tmp_path" in fn.__code__.co_varnames[:fn.__code__.co_argcount]:
                with tempfile.TemporaryDirectory() as td:
                    class _Cap:
                        def readouterr(self):
                            return None
                    fn(Path(td), _Cap())
            else:
                fn()
            print(f"  ok  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {name}: {exc}")
    print("\nall invariant pins passed" if not failures else f"\n{failures} failure(s)")
    raise SystemExit(1 if failures else 0)
