"""Corpus durability: a bad line is survivable, counted, and never silent.

The reported incident looked like an interrupted write — one record cut off
mid-string, and every later ingest dying at the same offset. It was not. The
corpus on disk was intact; one Cochrane record carried U+2028 LINE SEPARATOR in
its conflict-of-interest statement, and `str.splitlines()` treats that as a line
break while JSON does not require it to be escaped. The reader chopped a valid
record into eight pieces and blamed the file. `medrag/jsonl.py` holds the fix and
the "actual root cause" tests below pin it.

The rest of these cover the defects that made a single bad line catastrophic
rather than survivable, which are real independently of what caused it:
  - a malformed line does not deny access to the good records
  - the count of what was set aside travels to the surfaces a user looks at
  - a failed write leaves the previous corpus exactly as it was
  - fetched abstracts are not thrown away by a local write failure

No network: every document is constructed locally.

    python tests/test_corpus_store.py
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Direct runs do not load conftest.py, so the no-network guard is installed
# here too. See tests/netguard.py.
from tests import netguard  # noqa: E402

netguard.install()

from medrag.documents import Document  # noqa: E402
from medrag.ingest.store import (  # noqa: E402
    IDS_SUFFIX,
    PENDING_SUFFIX,
    QUARANTINE_SUFFIX,
    compact_corpus,
    corpus_health,
    load_corpus,
    read_corpus,
    save_corpus,
    stash_pending,
)

BROKEN_FIXTURE = Path(__file__).parent / "fixtures" / "corpus_broken.jsonl"


def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


def doc(i: int, text: str = "Background: x. Results: y.") -> Document:
    return Document(doc_id=str(i), title=f"Title {i}", text=text, source="pubmed")


def _broken_corpus() -> Path:
    """A copy of the truncated-write fixture, in a scratch directory."""
    d = _tmpdir()
    path = d / "corpus.jsonl"
    shutil.copyfile(BROKEN_FIXTURE, path)
    return path


def _writer(target: Path, lo: int) -> None:
    """Module-level so multiprocessing can pickle it."""
    save_corpus([doc(i) for i in range(lo, lo + 10)], target)


# ------------------------------------------------- the reported failure


def test_truncated_record_does_not_deny_access_to_the_good_records():
    path = _broken_corpus()
    docs, health = read_corpus(path)

    assert len(docs) == 4, "the intact records must still load"
    assert health.loaded == 4
    assert health.quarantined == 1
    assert not health.clean

    titles = [d.title for d in docs]
    assert "Cardiovascular and Renal Outcomes with Empagliflozin" in titles


def test_the_broken_line_is_the_one_the_traceback_named():
    """Guards the fixture itself: it must keep reproducing the reported error."""
    raw = BROKEN_FIXTURE.read_text(encoding="utf-8")
    last = raw.splitlines()[-1]
    try:
        json.loads(last)
    except json.JSONDecodeError as exc:
        assert "Unterminated string" in str(exc)
        assert exc.pos == 4408, f"fixture drifted: break at {exc.pos}, expected 4408"
    else:
        raise AssertionError("the fixture's final line should not parse")


def test_malformed_record_is_kept_not_discarded():
    path = _broken_corpus()
    _, health = read_corpus(path)

    quarantine = path.with_name(path.name + QUARANTINE_SUFFIX)
    assert health.quarantine_path == quarantine
    assert quarantine.exists(), "a skipped record must be recoverable, not gone"

    entries = [json.loads(ln) for ln in quarantine.read_text().splitlines() if ln.strip()]
    assert len(entries) == 1
    assert entries[0]["line"] == 5
    assert "Unterminated string" in entries[0]["error"]
    # The raw text is preserved verbatim so the loss is auditable.
    assert entries[0]["raw"].startswith('{"doc_id": "99999999"')


def test_a_bad_line_no_longer_blocks_the_next_ingest():
    """The defect that made one crash permanent: saving read the corpus first."""
    path = _broken_corpus()

    save_corpus([doc(1), doc(2)], path)

    docs = load_corpus(path)
    ids = {d.doc_id for d in docs}
    assert {"1", "2"}.issubset(ids), "a new ingest must land despite the bad line"
    assert "32865377" in ids, "and must not lose the pre-existing records"


def test_valid_json_that_is_not_a_document_is_also_quarantined():
    """A schema mismatch fails differently from a truncation, and both count."""
    d = _tmpdir()
    path = d / "corpus.jsonl"
    path.write_text(
        json.dumps(doc(1).to_dict()) + "\n"
        + json.dumps({"unexpected": "shape"}) + "\n"
        + "12345\n"
        + json.dumps(doc(2).to_dict()) + "\n",
        encoding="utf-8",
    )

    docs, health = read_corpus(path)
    assert {d.doc_id for d in docs} == {"1", "2"}
    assert health.quarantined == 2


# ------------------------------------------------- the actual root cause
#
# The live corpus was never corrupt. One Cochrane record carried U+2028 LINE
# SEPARATOR inside its conflict-of-interest statement. json.dumps leaves it
# unescaped, correctly — it is legal inside a JSON string — but str.splitlines()
# breaks on it, so the reader chopped one valid record into eight pieces and
# blamed the file. These are the regression tests for that.

LINE_SEP = chr(0x2028)
PARA_SEP = chr(0x2029)


def test_a_unicode_line_separator_does_not_split_a_record():
    d = _tmpdir()
    path = d / "corpus.jsonl"
    coi = f"Declarations of interest{LINE_SEP}CO: none known{LINE_SEP}JS: none known"
    save_corpus([doc(1, text=f"Background: x. Results: y. {coi}")], path)

    docs, health = read_corpus(path)
    assert health.quarantined == 0, "a legal character must not be read as damage"
    assert len(docs) == 1
    assert "CO: none known" in docs[0].text


def test_a_corpus_written_with_a_raw_separator_still_reads_as_one_record():
    """Files already on disk predate the escaping, so the reader must cope."""
    d = _tmpdir()
    path = d / "corpus.jsonl"
    record = doc(1, text=f"Results: y.{LINE_SEP}CO: none known").to_dict()
    # Exactly what json.dumps(ensure_ascii=False) produced before the fix.
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    assert LINE_SEP in path.read_text(encoding="utf-8"), "fixture must hold the raw char"

    docs, health = read_corpus(path)
    assert health.quarantined == 0, "the reader must split on newline only"
    assert len(docs) == 1
    assert docs[0].doc_id == "1"


def test_both_unicode_separators_survive_a_round_trip():
    d = _tmpdir()
    path = d / "corpus.jsonl"
    text = f"Background: a{LINE_SEP}b{PARA_SEP}c. Results: d."
    save_corpus([doc(1, text=text)], path)

    docs = load_corpus(path)
    assert len(docs) == 1
    assert docs[0].text == text, "the characters must come back unchanged"

    # Escaped on disk, so any other JSONL reader stays safe too.
    on_disk = path.read_text(encoding="utf-8")
    assert LINE_SEP not in on_disk
    assert "\\u2028" in on_disk


def test_a_separator_in_a_chunk_does_not_break_the_index():
    """chunks.jsonl had the identical defect; chunk text comes from abstracts."""
    from medrag.documents import Chunk
    from medrag.embeddings import HashingEmbedder
    from medrag.vectorstore import VectorStore

    d = _tmpdir()
    emb = HashingEmbedder(dim=32)
    chunk = Chunk(
        chunk_id="c1",
        doc_id="1",
        text=f"Results: y.{LINE_SEP}CO: none known",
        title="T",
    )
    store = VectorStore(dim=emb.dim, embedder_name=emb.name)
    store.add([chunk], emb.embed([chunk.text]))
    store.save(d)

    reloaded = VectorStore.load(d)
    assert len(reloaded.chunks) == 1, "one chunk in, one chunk out"
    assert "CO: none known" in reloaded.chunks[0].text


# ------------------------------------------------- never silent


def test_quarantine_count_reaches_stats():
    from medrag.cli import cmd_stats

    d = _tmpdir()
    (d / "raw").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(BROKEN_FIXTURE, d / "raw" / "corpus.jsonl")

    class Args:
        offline = True
        encrypt = False

    # cmd_stats resolves its own config, so point the whole data directory at the
    # scratch copy rather than the real one.
    previous = os.environ.get("MEDRAG_DATA_DIR")
    os.environ["MEDRAG_DATA_DIR"] = str(d)
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            cmd_stats(Args())
    finally:
        if previous is None:
            os.environ.pop("MEDRAG_DATA_DIR", None)
        else:
            os.environ["MEDRAG_DATA_DIR"] = previous
    out = buf.getvalue()

    assert "unreadable records set aside: 1" in out, out
    assert "4 documents" in out, out


def test_health_message_is_plain_language_with_the_count():
    path = _broken_corpus()
    message = corpus_health(path).message()

    assert message
    assert "1 stored record" in message
    assert "4 loaded normally" in message
    # Aimed at someone who will not open a terminal: no tracebacks, no jargon,
    # and it says what to do next.
    assert "again" in message
    for jargon in ("JSONDecodeError", "Traceback", "stderr", "None"):
        assert jargon not in message


def test_a_clean_corpus_reports_nothing():
    d = _tmpdir()
    path = d / "corpus.jsonl"
    save_corpus([doc(1), doc(2)], path)

    health = corpus_health(path)
    assert health.clean
    assert health.quarantined == 0
    assert health.message() is None, "a clean corpus must not warn"


def test_a_repaired_corpus_stops_warning():
    """The count is recomputed per read, so it cannot go stale."""
    path = _broken_corpus()
    assert corpus_health(path).quarantined == 1

    compact_corpus(path)  # drops the unreadable line for good

    assert corpus_health(path).clean
    assert corpus_health(path).message() is None


def test_memo_warnings_carry_the_corpus_shortfall():
    """A memo built while records are unreadable says so on its face."""
    from medrag.config import Config
    from medrag.diligence import DiligenceRunner

    d = _tmpdir()
    (d / "raw").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(BROKEN_FIXTURE, d / "raw" / "corpus.jsonl")

    cfg = Config(data_dir=d, offline=True)
    runner = DiligenceRunner(cfg=cfg, rag=object(), trial_store=object(), fda_store=object())
    try:
        joined = " ".join(runner.warnings)
        assert "Stored research incomplete" in joined, runner.warnings
        assert "1 stored record" in joined
    finally:
        runner.trial_store = None
        runner.fda_store = None


# ------------------------------------------------- write durability


def test_an_interrupted_write_leaves_the_previous_corpus_readable():
    d = _tmpdir()
    path = d / "corpus.jsonl"
    save_corpus([doc(1), doc(2)], path)
    before = path.read_bytes()

    # A document that explodes during serialisation, mid-write.
    class Exploding:
        doc_id = "3"

        def to_dict(self):
            raise OSError("disk full")

    try:
        save_corpus([Exploding()], path)
    except OSError:
        pass
    else:
        raise AssertionError("the write should have failed")

    assert path.read_bytes() == before, "a failed write must not alter the corpus"
    assert {d_.doc_id for d_ in load_corpus(path)} == {"1", "2"}


def test_a_rolled_back_append_leaves_no_partial_line():
    """The append records the file length first and restores it on failure."""
    d = _tmpdir()
    path = d / "corpus.jsonl"
    save_corpus([doc(1)], path)
    original = path.read_bytes()

    from medrag.ingest import store as store_mod

    real_fsync = os.fsync
    calls = {"n": 0}

    def flaky_fsync(fd):
        calls["n"] += 1
        raise OSError("simulated I/O error during append")

    os.fsync = flaky_fsync
    try:
        try:
            store_mod._append_bytes(path, b'{"doc_id": "2"}\n')
        except OSError:
            pass
    finally:
        os.fsync = real_fsync

    assert calls["n"] > 0, "the test did not exercise the failure path"
    assert path.read_bytes() == original, "the append must roll back cleanly"
    assert len(load_corpus(path)) == 1


def test_appending_after_a_truncated_line_does_not_destroy_a_good_record():
    """A partial line with no newline would otherwise swallow the next record."""
    path = _broken_corpus()
    save_corpus([doc(7)], path)

    docs, health = read_corpus(path)
    assert "7" in {d_.doc_id for d_ in docs}, "the new record must survive intact"
    assert health.quarantined == 1, "only the originally broken line is bad"


def test_a_second_ingest_appends_rather_than_rewriting():
    d = _tmpdir()
    path = d / "corpus.jsonl"
    save_corpus([doc(1, text="first " * 50)], path)
    first = path.read_bytes()

    save_corpus([doc(2, text="second " * 50)], path)
    grown = path.read_bytes()

    assert grown.startswith(first), "existing bytes must be left where they were"
    assert len(grown) > len(first)


def test_reingesting_a_document_refreshes_it():
    """The documented invariant: one record per doc_id, latest wins."""
    d = _tmpdir()
    path = d / "corpus.jsonl"
    save_corpus([doc(1, text="Background: old. Results: old.")], path)
    save_corpus([doc(1, text="Background: new. Results: new.")], path)

    docs = load_corpus(path)
    assert len(docs) == 1
    assert "new" in docs[0].text


def test_the_id_sidecar_tracks_stored_ids():
    d = _tmpdir()
    path = d / "corpus.jsonl"
    save_corpus([doc(1), doc(2)], path)
    save_corpus([doc(3)], path)

    sidecar = path.with_name(path.name + IDS_SUFFIX)
    assert sidecar.exists()
    assert {ln.strip() for ln in sidecar.read_text().splitlines() if ln.strip()} == {"1", "2", "3"}


def test_the_sidecar_is_rebuilt_when_missing():
    """An existing install has no sidecar; the first save must not lose records."""
    d = _tmpdir()
    path = d / "corpus.jsonl"
    save_corpus([doc(1), doc(2)], path)
    path.with_name(path.name + IDS_SUFFIX).unlink()

    save_corpus([doc(3)], path)

    assert {d_.doc_id for d_ in load_corpus(path)} == {"1", "2", "3"}


def test_concurrent_writers_do_not_lose_records():
    """A fixed temp filename let two ingests share one file and clobber it."""
    import multiprocessing as mp

    d = _tmpdir()
    path = d / "corpus.jsonl"
    save_corpus([doc(0)], path)

    procs = [mp.Process(target=_writer, args=(path, 100 + k * 10)) for k in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()

    ids = {d_.doc_id for d_ in load_corpus(path)}
    expected = {"0"} | {str(i) for k in range(4) for i in range(100 + k * 10, 110 + k * 10)}
    assert ids == expected, f"lost {sorted(expected - ids)}"


def test_no_temp_files_are_left_behind():
    d = _tmpdir()
    path = d / "corpus.jsonl"
    save_corpus([doc(1)], path)
    save_corpus([doc(2)], path)

    assert list(d.glob("*.tmp")) == []


# ------------------------------------------------- fetched work is not thrown away


def test_stashed_records_are_absorbed_by_the_next_save():
    d = _tmpdir()
    path = d / "corpus.jsonl"
    save_corpus([doc(1)], path)

    stash_pending([doc(74)], path)
    assert path.with_name(path.name + PENDING_SUFFIX).exists()

    save_corpus([doc(2)], path)

    ids = {d_.doc_id for d_ in load_corpus(path)}
    assert ids == {"1", "2", "74"}, "a stashed fetch must be recovered"
    assert not path.with_name(path.name + PENDING_SUFFIX).exists()


def test_a_failed_corpus_write_keeps_the_fetched_abstracts():
    """The crash discarded 74 successfully fetched abstracts. It must not."""
    from medrag import pipeline
    from medrag.config import Config

    d = _tmpdir()
    cfg = Config(data_dir=d, offline=True)
    cfg.ensure_dirs()
    fetched = [doc(i) for i in range(74)]

    real_save = pipeline.save_corpus

    def failing_save(*a, **k):
        raise OSError("no space left on device")

    pipeline.save_corpus = failing_save
    pipeline.search_pubmed = lambda *a, **k: [str(i) for i in range(74)]
    pipeline.fetch_pubmed = lambda *a, **k: fetched
    try:
        try:
            pipeline.ingest_pubmed("anything", cfg=cfg)
        except RuntimeError as exc:
            assert "74 abstracts were downloaded" in str(exc)
            assert "nothing needs to be fetched again" in str(exc)
        else:
            raise AssertionError("the failed write should have been reported")
    finally:
        pipeline.save_corpus = real_save

    corpus = cfg.raw_dir / "corpus.jsonl"
    pending = corpus.with_name(corpus.name + PENDING_SUFFIX)
    assert pending.exists(), "the fetched abstracts must survive the write failure"

    # And a later, working save brings them into the corpus.
    save_corpus([], corpus)
    assert len(load_corpus(corpus)) == 74


# ------------------------------------------------- encrypted corpora


def test_an_encrypted_corpus_still_round_trips():
    """AES-GCM wraps the whole file, so that path still rewrites. It must work."""
    d = _tmpdir()
    path = d / "corpus.jsonl"
    save_corpus([doc(1)], path, passphrase="correct horse battery staple")
    save_corpus([doc(2)], path, passphrase="correct horse battery staple")

    docs = load_corpus(path, passphrase="correct horse battery staple")
    assert {d_.doc_id for d_ in docs} == {"1", "2"}
    assert b"Title 1" not in path.read_bytes()


def test_a_locked_corpus_is_reported_as_locked_not_clean():
    """Not-assessed and nothing-wrong stay distinct, as everywhere else."""
    d = _tmpdir()
    path = d / "corpus.jsonl"
    save_corpus([doc(1)], path, passphrase="pw")

    health = corpus_health(path, passphrase=None)
    assert health.locked
    assert health.message() is None


def test_quarantined_lines_from_an_encrypted_corpus_are_not_written_in_clear():
    """The quarantine holds corpus text; it must inherit the same protection."""
    from medrag.crypto import encrypt_bytes

    d = _tmpdir()
    path = d / "corpus.jsonl"
    pw = "pw"
    body = json.dumps(doc(1, text="SENSITIVE MARKER TEXT").to_dict()) + "\n" + '{"doc_id": "2"'
    path.write_bytes(encrypt_bytes(body.encode("utf-8"), pw))

    docs, health = read_corpus(path, passphrase=pw)
    assert health.quarantined == 1
    assert health.quarantine_path is not None
    assert b'{"doc_id": "2"' not in health.quarantine_path.read_bytes()


def _run_all() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
    print("failures:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
