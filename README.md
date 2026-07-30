# MedRAG (Built for Pebble Accelerator)

MedRAG reads the public evidence on a biomedical asset — published papers, the
clinical-trial registry, and the FDA's device databases — and produces a cited
memo, a claim-by-claim verification, or a patient trial landscape. Every number
it reports is traceable to the PMID, NCT, or FDA record it came from. It's built
for the person doing diligence on a company, not for chat.

## Why it exists

Checking whether a company's claims hold up against independent published
evidence takes an analyst hours per asset, and the slow part isn't reading — it's
verification. Reading a deck is quick. Finding the pivotal study behind each
claim, noticing that the 92% figure came from a different patient group than the
one the slide implies, and working out whether the supporting paper was written
by people the company paid: that's the work, and it's the work nobody has time to
do carefully for every asset. MedRAG does the mechanical parts of that check so
the analyst spends their judgment on the parts that need judgment.

It runs on one machine with no server and no account. There's a genuinely free
model option and a fully local one, so it can cost nothing to run.

## The three things it does

**A screening memo.** Give it an asset and an indication and it runs the same
fixed set of questions against every asset — the same questions in the same order,
because that's what makes two memos comparable. Each answer is grounded in cited
literature and trial records, and there's a stage devoted to evidence *against*
the thesis. That stage is where it earns its keep: it surfaced a trial of the
same compound that had been terminated in a different indication, and, for a
device, an FDA recall and the adverse-event reports filed against it — the things
a confirmatory reading would sail right past.

**Claim verification.** Paste a company's own claims (or the deck text, and it
pulls the claims out for you to edit first) and it checks each one against
independent evidence, scoring two separate things: whether the evidence supports
the claim, and whose evidence it is. Keeping those apart matters. On a real run
against a colorectal-cancer screening test, it caught a specificity figure the
deck had attached to the wrong patient population — the source's number was for
everyone screened, not the subgroup the slide implied — and it flagged that the
pivotal study's support wasn't independent at all once you read the funding line
in the full paper rather than the sentence being cited.

**A trial landscape.** Give it a condition and a biomarker and it lists the trials
a patient could actually enter, each shown with the exact eligibility sentence
that put it on the list. When a trial's eligibility only implies the biomarker
indirectly — it excludes the opposite marker rather than naming the patient's —
it's kept and flagged as uncertain, not dropped. A missed trial is worse than an
uncertain one when someone is looking for an option.

## Why not just ask a chatbot

Fair question. I asked it a lot while building this.

Ask a good general model about a drug and you get a good answer. What you don't
get is a procedure. This tool exists for the parts that aren't a model call at
all:

- **It counts.** "57 of the 500 trials held stopped early, 52 gave a reason" is
  a database query, not a guess — and it tells you the denominator, so you can
  see what it didn't cover.
- **It checks its own citations.** Every figure in the memo has to appear in a
  passage that was actually retrieved. A model cites; nothing verifies the
  citation.
- **It scores independence separately from support.** A claim can be
  well-evidenced and entirely company-funded, and both show up as separate
  columns. No disclosure statement means "no disclosure", never "independent".
- **It asks the same questions in the same order every time**, so two memos are
  comparable.
- **It can run entirely on your own machine**, with nothing sent anywhere.

Where a general model still wins: open-ended synthesis, anything outside PubMed
and ClinicalTrials.gov and openFDA, and questions nobody thought to put in the
question set. This isn't trying to replace that. It's trying to be the part you
can check.

## Running it

```bash
pip install -r requirements.txt
streamlit run app.py
```

The app opens a form in your browser. On first run it asks which model to use —
the free options are first, there's a fully local one that keeps everything on
your machine, and there's a "no model" mode that still produces a fully cited
evidence list. It fetches the research it needs on demand, so you never touch a
terminal. On a Mac you can double-click `Start MedRAG.command` instead.

From the command line, the same three tools:

```bash
python -m medrag diligence --asset "empagliflozin" --indication "heart failure"
python -m medrag verify --claims claims.txt --asset "..." --company "..."
python -m medrag landscape --condition "colorectal cancer" --biomarker "MSS"
```

`python -m medrag doctor` checks that the data sources are reachable. There's a
synthetic sample dataset (`scripts/make_sample_*.py`) so the whole thing runs
end to end with no network and no key.

## Trusting the information

These are the reasons to trust the output, so they're here rather than buried.

**Trial records live in a database, not the search index.** A trial's phase and
status are filters, not meanings. If you turn "Phase 3, terminated" into prose and
drop it into the same search index as the literature, you're hoping a similarity
score recovers a distinction the registry recorded exactly — and you lose the
precision that's the whole reason the registry exists. So trials are queried with
SQL and literature is searched by meaning, and a router decides which store a
question needs.

**"We didn't find anything" is never reported as "this is contradicted."** Those
are different findings, and the code keeps them apart deterministically — if
nothing was retrieved, the answer is "not found," and the model is never given
the chance to talk itself into "contradicted." For the same reason, a section
nobody actually checked never reports as passing. An unchecked section shown as
clean is a false negative wearing a pass, and that's the failure mode most likely
to matter.

**Calling a study independent requires evidence that it is.** No funding
disclosure means unknown, not clean. The point of verification is to not lean on
the company's own materials, so a company-funded pivotal study quietly labelled
"independent" would defeat the whole exercise — that's exactly the mistake an
early version made, reading the funding line only in the cited sentence and
missing it two paragraphs down. Now independence is claimed only on positive
evidence: a named non-industry funder, or an explicit no-conflict statement.

**Contradicting evidence gets its own stage, and half of it can't make things
up.** Retrieval tends to confirm whatever you ask it, so the evidence against a
claim has to be hunted for deliberately. Half that hunt is a plain database query
— trials that stopped early, FDA recalls — which returns facts and can't
hallucinate. The other half asks a model to find contradictions in the retrieved
papers, and it's explicitly allowed to find nothing, because a prompt that
demands a weakness will invent one, and an invented contradiction in a diligence
memo is worse than saying nothing.

## What it doesn't do

It reads abstracts, not full papers, unless you add the PDFs — so detail buried in
a methods section is often out of reach. It doesn't cover conference proceedings,
which is where negative oncology results tend to show up first. It checks that a
number actually appears in the source it's cited to, but not that the number was
used correctly: a figure lifted from the wrong arm of a trial still passes. It
doesn't chain reasoning across several sources to reach a conclusion none of them
states on its own. And the only trial registry it knows is ClinicalTrials.gov.

It's a research aid, not investment advice and not medical advice. Every citation
should be checked against the original source before it informs anything. It's
covered by about 260 tests that run with no network and no API key, and building
it turned up several real bugs — but a tool like this is only as good as the last
source you verified by hand.


