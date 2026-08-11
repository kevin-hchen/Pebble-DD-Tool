"""Retrieval RELEVANCE — the property nothing tested, which is why 0.05 survived.

The full suite passed unchanged when `score_floor` moved 0.05 -> 0.35, a change
that alters what evidence reaches every memo this tool produces. It would have
passed just as happily at 0.0. Every existing test asks whether the machinery
runs; none asks whether what comes back is about the thing that was asked. So an
asset with nothing published anywhere returned the k nearest passages in the
corpus — bilirubinometry meta-analyses cited as a hidradenitis drug's efficacy
evidence — and eight colorectal trials with real NCT IDs, and reported "sections
answered with evidence: 11/11".

The property pinned here, in one sentence: **an asset with no data returns no
evidence, and an on-topic asset still returns its evidence.** Both halves are
required. Either alone is trivially satisfiable — return nothing, or return
everything — and it is the pair that has to hold.

Each half is paired with a NEGATIVE CONTROL asserting the fixture can still
produce the defect: at floor 0.05 the off-topic passages ARE admitted, and with
the anchor check removed the free-text fallback DOES return unrelated trials. A
guard test whose fixture has quietly stopped being able to fail is decoration,
the same failure `test_pdf_render.py`'s width sweep was found doing vacuously.

The similarity numbers in `_StubEmbedder` are not invented. They are the
measured distributions from the real 820-chunk index under all-MiniLM-L6-v2
(2026-08-11, 11 questions x 33 asset/indication pairs, 363 retrievals) — see the
`score_floor` comment in medrag/config.py for the full table. A stub is used
rather than the real embedder because the model weights are a 90 MB download
that CI does not have and the network guard would refuse anyway; what it
reproduces is the measurement, not a guess.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Direct runs do not load conftest.py, so the no-network guard is installed
# here too. See tests/netguard.py.
from tests import netguard  # noqa: E402

netguard.install()
sys.path.insert(0, str(Path(__file__).resolve().parent))

from medrag.config import Config  # noqa: E402
from medrag.diligence import DiligenceQuestion, DiligenceRunner, QuestionSet  # noqa: E402
from medrag.documents import Chunk  # noqa: E402
from medrag.embeddings import Embedder  # noqa: E402
from medrag.memo import render_markdown  # noqa: E402
from medrag.retriever import Retriever  # noqa: E402
from medrag.trials.anchors import anchor_for  # noqa: E402
from medrag.trials.client import TrialRecord  # noqa: E402
from medrag.trials.queries import QuerySet, TrialQuery  # noqa: E402
from medrag.trials.store import TrialStore  # noqa: E402
from medrag.vectorstore import INDEX_SCHEMA, VectorStore  # noqa: E402

# ---------------------------------------------------------------- measured constants
#
# From the real index, real embedder, real rendered question set. Quoted here so
# a future reweight has to argue with a number rather than an impression.
MEASURED = {
    "on_topic_min": 0.334,
    "on_topic_p05": 0.436,
    "on_topic_p25": 0.530,
    "on_topic_median": 0.643,
    "off_topic_median": 0.361,
    "off_topic_p95": 0.482,
    "off_topic_p99": 0.521,
    "off_topic_max": 0.555,
}

# The two representative scores the stub reproduces: an on-topic passage at the
# measured on-topic median, an off-topic one at the measured off-topic median.
ON_TOPIC_SCORE = MEASURED["on_topic_median"]
OFF_TOPIC_SCORE = MEASURED["off_topic_median"]


class _StubEmbedder(Embedder):
    """Two orthogonal topic axes, mixed to hit an exact target cosine.

    A query about topic A returns cos(theta) = ON_TOPIC_SCORE against an A
    passage and OFF_TOPIC_SCORE against a B passage — the two numbers measured
    on the real index. Deterministic, no weights, no network.
    """

    name = "stub:measured-distributions"
    dim = 4

    #: Axis 0 = colorectal, axis 1 = neonatal jaundice. A subject the corpus
    #: holds nothing about (hidradenitis) is not a third axis of its own — it is
    #: a query that leans slightly on both and mostly on axis 3, which is where
    #: the measured off-topic score comes from. That is the real shape: an
    #: absent asset does not score ZERO against a medical corpus, it scores
    #: 0.36, because the questions are still medical questions.
    COLORECTAL, NEONATAL = 0, 1

    def _axis(self, text: str) -> int | None:
        t = text.lower()
        if any(w in t for w in ("colorectal", "botensilimab", "microsatellite", "mss")):
            return self.COLORECTAL
        if any(w in t for w in ("jaundice", "bilirubin", "neonat", "phototherapy")):
            return self.NEONATAL
        return None

    def _passage(self, axis: int) -> np.ndarray:
        """On-topic score along its own axis, off-topic along the other."""
        other = 1 - axis
        v = np.zeros(self.dim)
        v[axis] = ON_TOPIC_SCORE
        v[other] = OFF_TOPIC_SCORE
        v[3] = np.sqrt(max(0.0, 1.0 - v[axis] ** 2 - v[other] ** 2))
        return v

    def _embed(self, texts: list[str]) -> np.ndarray:
        """The PASSAGE side. A passage always belongs to one of the two real
        topics — a corpus is made of what it contains, not of what it lacks."""
        return np.array(
            [self._passage(self.COLORECTAL if self._axis(t) is None else self._axis(t))
             for t in texts],
            dtype="float32",
        )

    def embed_query(self, text: str) -> np.ndarray:
        """The QUERY side, which is where the two distributions come from.

        Overridden rather than folded into `_embed` because queries and
        passages are genuinely different inputs here — the base class already
        splits them, and keying off a marker in the text would make the stub
        answer differently depending on how a caller happened to phrase it.
        """
        axis = self._axis(text)
        if axis is not None:
            v = np.zeros(self.dim)
            v[axis] = 1.0      # cosine == the passage's weight on this axis
            return v.astype("float32")
        # A query about NEITHER subject: a unit vector whose dot product with
        # either passage is exactly the measured off-topic score. Leaning
        # equally on both axes gives (on + off)/sqrt(2); scaling that down to
        # the measured value and putting the rest of the norm on an axis no
        # passage occupies is what lands it there. An absent asset does not
        # score zero against a medical corpus — it scores 0.36, because the
        # question is still a medical question.
        both = np.array([1.0, 1.0, 0.0, 0.0]) / np.sqrt(2)
        lean = OFF_TOPIC_SCORE / float(self._passage(self.COLORECTAL) @ both)
        v = lean * both
        v[2] = np.sqrt(max(0.0, 1.0 - lean ** 2))
        return v.astype("float32")


def _index() -> VectorStore:
    """Four passages, two per topic. Neither topic mentions hidradenitis."""
    store = VectorStore(dim=_StubEmbedder.dim, embedder_name=_StubEmbedder.name,
                        index_schema=INDEX_SCHEMA)
    emb = _StubEmbedder()
    texts = [
        ("PMID001", "Botensilimab plus balstilimab in microsatellite stable colorectal cancer."),
        ("PMID002", "Regorafenib in refractory metastatic colorectal cancer: a phase 3 trial."),
        ("PMID003", "Transcutaneous bilirubinometry for detecting jaundice in term neonates."),
        ("PMID004", "Phototherapy thresholds in neonatal hyperbilirubinaemia: a meta-analysis."),
    ]
    chunks = [
        Chunk(chunk_id=f"{doc}::0", doc_id=doc, text=text, title=text,
              citation="Author et al., J Test, 2024")
        for doc, text in texts
    ]
    store.add(chunks, emb.embed([c.text for c in chunks]))
    return store


def _trial_store() -> TrialStore:
    """A store that has ingested colorectal and nothing else — the shape of the
    real one, where 74 families are recorded and hidradenitis is not among them."""
    store = TrialStore(Path(tempfile.mkdtemp()) / "t.db")
    records = [
        TrialRecord(
            nct_id="NCT00000001",
            brief_title="Botensilimab and balstilimab in refractory MSS colorectal cancer",
            overall_status="RECRUITING",
            phase="Phase 2",
            conditions=["Colorectal Neoplasms"],
            interventions=["Botensilimab", "Balstilimab"],
            lead_sponsor="Agenus",
        ),
        TrialRecord(
            nct_id="NCT00000002",
            brief_title="Cold snare polypectomy for large colorectal polyps",
            overall_status="COMPLETED",
            phase="NA",
            conditions=["Adenomatous Polyps"],
            interventions=["Cold snare polypectomy"],
            # A real sponsor name, chosen because it contains an ordinary word
            # the rendered question also contains. That is the whole mechanism:
            # `store.search` ORs every token, so "trials" in a question matches
            # "Trials" in a sponsor and the registry answers a hidradenitis
            # query with a colorectal polyp study. On the live 241,298-record
            # store there is always such a record.
            lead_sponsor="Canadian Cancer Trials Group",
        ),
        TrialRecord(
            nct_id="NCT00000003",
            brief_title="Regorafenib and radiotherapy for metastatic colorectal cancer",
            overall_status="TERMINATED",
            phase="Phase 2",
            conditions=["Colorectal Cancer"],
            interventions=["Regorafenib"],
            lead_sponsor="Another University",
            why_stopped="slow accrual",
        ),
    ]
    store.upsert(records, set_key="colorectal",
                 provenance={r.nct_id: ["cond:colorectal cancer"] for r in records})
    # The ingest lifecycle row is what distinguishes "searched" from "never
    # asked". Only colorectal gets one.
    store.begin_ingest(QuerySet(key="colorectal", label="Colorectal cancer",
                                queries=(TrialQuery("condition", "colorectal cancer"),)))
    return store


def _runner(floor: float | None = None) -> DiligenceRunner:
    cfg = Config(openai_api_key=None, data_dir=Path(tempfile.mkdtemp()))
    if floor is not None:
        cfg.score_floor = floor
    cfg.ensure_dirs()
    runner = DiligenceRunner(cfg, rag=None, trial_store=_trial_store())

    class _RAG:
        """Only what the runner touches. `generator` carries no client, so the
        runner takes its extractive path — which is the right shape here: this
        is a test about what reaches the memo, not about what a model writes
        with it."""
        generator = None

    rag = _RAG()
    rag.embedder = _StubEmbedder()
    rag.retriever = Retriever(_index(), rag.embedder, cfg)
    runner.rag = rag
    return runner


# ------------------------------------------------------- the floor, both halves


def test_an_off_topic_query_is_admitted_at_0_05_and_refused_at_the_shipped_floor():
    """The negative control and the property, in one test so they cannot drift.

    The first assertion is what makes this test non-vacuous: at the old floor
    the fixture DOES hand back neonatal-jaundice passages for a colorectal
    query, which is the defect. The second is the fix. Lower `score_floor` back
    towards 0.05 and the second assertion fails.
    """
    query = "hidradenitis suppurativa"      # neither topic in the index

    at_005 = Retriever(_index(), _StubEmbedder(), Config(score_floor=0.05)).retrieve(query, k=6)
    assert at_005, "fixture can no longer reproduce the defect — the test would pass vacuously"

    shipped = Config()
    at_shipped = Retriever(_index(), _StubEmbedder(), shipped).retrieve(query, k=6)
    assert at_shipped == [], (
        f"an off-topic query returned {len(at_shipped)} passages at the shipped floor "
        f"{shipped.score_floor}; off-topic passages score {OFF_TOPIC_SCORE} on the measured "
        "distribution and must not be admitted"
    )


def test_an_on_topic_query_still_returns_its_evidence_at_the_shipped_floor():
    """The other half. A floor that silences everything would satisfy the test
    above and destroy the tool."""
    hits = Retriever(_index(), _StubEmbedder(), Config()).retrieve(
        "botensilimab in microsatellite stable colorectal cancer", k=6
    )
    assert hits, "an on-topic query must still retrieve"
    assert {h.chunk.doc_id for h in hits} == {"PMID001", "PMID002"}, (
        "only the colorectal passages belong here; a neonatal-jaundice passage "
        "reaching an on-topic colorectal query is the same defect in the other direction"
    )


def test_the_shipped_floor_sits_between_the_measured_distributions():
    """Pins the CALIBRATION, not just the mechanism.

    The two distributions overlap — there is no floor that admits every real
    passage and no false one — so this asserts the boundary sits in the band the
    measurement argues for: above the off-topic p95, and below the on-topic p25
    so the bulk of real evidence survives. A reweight outside that band has to
    change this test and say why in config.py.
    """
    floor = Config().score_floor
    assert MEASURED["off_topic_p95"] < floor < MEASURED["on_topic_p25"], (
        f"score_floor {floor} is outside the measured band "
        f"({MEASURED['off_topic_p95']}, {MEASURED['on_topic_p25']})"
    )
    assert MEASURED["on_topic_min"] < MEASURED["off_topic_max"], (
        "this pins that the distributions OVERLAP — if a re-measurement ever "
        "separates them cleanly, the trade-off recorded in config.py no longer "
        "applies and the comment must be rewritten"
    )


# ------------------------------------------------------------- the trial gate


def test_a_free_text_fallback_never_returns_a_trial_about_something_else():
    """The negative control and the property for the registry half.

    `store.search` ORs every token it is given. Handed the rendered question it
    matches on `trials`, `other`, `run` — words in every trial record — and
    answers a hidradenitis query with colorectal trials. That is asserted first,
    against the store directly, so the fixture is known to reproduce it.
    """
    store = _trial_store()
    question = "Which other sponsors have run clinical trials on this mechanism or target?"

    ungated = store.search(f"PBX-7749 hidradenitis suppurativa {question}", limit=10)
    assert ungated, "fixture can no longer reproduce the defect — the test would pass vacuously"
    assert not any("hidradenitis" in (r.brief_title or "").lower() for r in ungated), (
        "the fixture holds no hidradenitis trial, so every row above is unrelated"
    )

    anchor = anchor_for("PBX-7749", "hidradenitis suppurativa", store)
    kept = [r for r in store.search(anchor.search_text(), limit=10, query_set=anchor.query_set)
            if anchor.is_about(r)]
    assert kept == [], f"the anchor check let {len(kept)} unrelated trials through"


def test_an_indication_never_ingested_is_reported_as_such_not_as_empty():
    """"Never searched" and "searched, found nothing" are different answers.

    Same rule as `ValidationReport.assessed` and `NegativeEvidence.searched`,
    one layer down. Without this an empty trial section reads as a finding about
    the world rather than about the snapshot.
    """
    store = _trial_store()
    absent = anchor_for("PBX-7749", "hidradenitis suppurativa", store)
    assert absent.query_set is None
    assert absent.notes(), "an un-ingested indication must produce a note"
    assert "never searched" in absent.notes()[0]

    present = anchor_for("botensilimab and balstilimab", "colorectal cancer", store)
    assert present.query_set == "colorectal"
    assert present.notes() == [], "an ingested indication must not be warned about"


def test_an_unanchored_query_returns_nothing_rather_than_the_whole_store():
    """With neither an agent name nor an ingested query set, `store.query`
    degrades to `SELECT * FROM trials LIMIT k`. It must not be called at all."""
    runner = _runner()
    q = DiligenceQuestion(id="t", section="Trials", question="Which trials ran?",
                          route="structured", k=5)

    unanchored = runner._trials_for("Which trials ran?", asset="", indication="alopecia",
                                    filters={}, limit=5)
    assert unanchored == [], "an unanchored query returned trials from an unrelated store"

    anchored = runner._trials_for(q.question, asset="botensilimab and balstilimab",
                                  indication="colorectal cancer", filters={}, limit=5)
    assert [r.nct_id for r in anchored] == ["NCT00000001"], (
        "the asset's own trial must still be returned"
    )


# ------------------------------------------------------- the two together, end to end


def _memo(asset: str, indication: str, floor: float | None = None):
    runner = _runner(floor)
    questions = [
        DiligenceQuestion(id="efficacy", section="Efficacy evidence",
                          question="What efficacy has been demonstrated for {asset} in "
                                   "{indication}?", route="both", k=6),
        DiligenceQuestion(id="competitors", section="Who else has run trials",
                          question="Which other sponsors have run clinical trials on this "
                                   "target?", route="structured", k=6),
        DiligenceQuestion(id="quality", section="Evidence quality",
                          question="Is the evidence base for {asset} in {indication} built on "
                                   "randomised trials?", route="semantic", k=6),
    ]
    memo = runner.run(asset, indication,
                      question_set=QuestionSet(name="test", version=1, questions=questions),
                      progress=False)
    return memo


def test_an_asset_with_no_data_produces_a_memo_that_says_so():
    """The whole point, end to end: 0 of 3 sections, and the banner fires.

    Verified to fail at floor 0.05 (the literature half fills every section) and
    with the anchor check removed from `_trials_for` (the registry half fills
    the structured one).
    """
    memo = _memo("PBX-7749", "hidradenitis suppurativa")
    cov = memo.coverage()
    assert cov["sections_with_evidence"] == 0, (
        f"{cov['sections_with_evidence']} of {cov['sections']} sections were evidenced for an "
        "asset with no trial and no publication"
    )
    md = render_markdown(memo)
    assert "NOTHING WAS FOUND FOR THIS ASSET" in md, "the thin-evidence banner did not fire"
    assert "never searched the registry" in md, (
        "the memo must say the indication was never ingested, not merely show empty sections"
    )


def test_the_same_memo_at_the_old_floor_is_the_defect_this_pins():
    """Negative control for the end-to-end test. If this stops finding evidence
    the test above has stopped proving anything."""
    memo = _memo("PBX-7749", "hidradenitis suppurativa", floor=0.05)
    assert memo.coverage()["sections_with_evidence"] > 0, (
        "at floor 0.05 the memo must still show the defect, or the fixture no longer "
        "exercises it"
    )


def test_an_on_topic_asset_still_gets_an_evidenced_memo():
    """The half that stops the fix being 'return nothing'."""
    memo = _memo("botensilimab and balstilimab", "colorectal cancer")
    cov = memo.coverage()
    assert cov["sections_with_evidence"] == cov["sections"], (
        f"only {cov['sections_with_evidence']} of {cov['sections']} sections were evidenced "
        "for an asset the store and the corpus both cover"
    )
    md = render_markdown(memo)
    assert "NOTHING WAS FOUND" not in md and "LITTLE WAS FOUND" not in md, (
        "the thin-evidence banner must stay silent on a well-evidenced memo"
    )
    assert "PMID001" in md and "NCT00000001" in md, (
        "both stores must contribute — the asset's own trial and its own paper"
    )
    assert "PMID003" not in md and "PMID004" not in md, (
        "neonatal-jaundice passages must not reach a colorectal memo"
    )


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all retrieval-relevance tests passed")
