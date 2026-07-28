# Prompts for Claude Code

Copy one block at a time into Claude Code, running in `~/Desktop/medrag`.

They are ordered. Prompts 1–3 verify the things that have never run for real,
because everything else rests on them. Do those before building anything new.

A note on why these are worded the way they are: each one asks Claude Code to
*run* something and report what actually happened, rather than to make a change
and assume it worked. The whole project was built without network access, so the
failure mode to guard against is confident code that has never met a real API.

---

## 1. First live run — does any of this actually work?

```
Read CLAUDE.md first, especially the "Known-unverified" section.

This project has never made a real network call — it was built in a sandbox with
all outbound traffic blocked. Your job is to find out what breaks in reality.

Run, in order, and report what actually happens at each step rather than
assuming:

1. python -m medrag doctor
2. python -m medrag ingest --query "empagliflozin heart failure" -n 30 --index
3. python -m medrag trials --condition "heart failure" --intervention empagliflozin
4. python -m medrag stats

Then fix whatever broke. I expect problems in the E-utilities XML parsing and
the ClinicalTrials.gov v2 field paths, because both were written against
fixtures rather than live responses.

For each bug: show me the real response that broke it, fix the code, and add a
fixture to tests/fixtures/ reproducing it so it can never silently regress.
Do not weaken any existing test to make things pass.

Finally, tell me the whyStopped fill rate from `medrag stats`. If it is low, say
so plainly — it determines how much the negative-evidence section is worth.
```

---

## 2. First live model call — is the prompting sound?

```
Read CLAUDE.md first.

I have set up a provider (check .env). No LLM call in this project has ever run
against a real model — only mocks. Verify the three places we call one:

1. Generation in medrag/generator.py and medrag/diligence.py
2. The router's JSON-mode classification in medrag/router.py
3. The contradiction hunter in medrag/negative_evidence.py

Run a real diligence memo:
  python -m medrag diligence --asset "empagliflozin" --indication "heart failure"

Then assess, with evidence from the actual output:

- Does the model cite with [n] markers consistently, or does it drop them?
- Does the router return parseable JSON, or is it falling back to rules? Check
  which by adding temporary logging, then remove it.
- Does the contradiction hunter ever return an empty findings list, or is it
  manufacturing weaknesses to fill the section? This matters most — read its
  findings against the source passages yourself and tell me if any are invented.
- Report the coverage line: how many sections were checked for faithfulness, and
  how many passed.

If the validator is producing false alarms on sound answers — most likely the
numeric-grounding check on rounded figures like "roughly 30%" against a source
saying 28.4% — show me the specific cases before changing thresholds. I want to
see the failures, not a silently loosened check.
```

---

## 3. Local embeddings — the free quality win

```
Read CLAUDE.md first.

Right now retrieval runs on the hashing fallback, which is deliberately weak.
sentence-transformers has never successfully loaded a model in this project
because the sandbox blocked the download.

1. pip install -r requirements-offline.txt
2. Confirm SentenceTransformerEmbedder actually constructs and encodes.
3. Rebuild the index: python -m medrag index
4. Confirm the manifest records the new embedder and dimension, and that
   querying an index built with a different embedder is still refused.
5. Run python scripts/eval.py

Report the recall@5 and MRR before and after evidence grading. If the delta is
zero, say so and explain why rather than tuning until it looks good — a zero
delta on a small corpus is a real result about the corpus.

Also time it: how long does indexing 100 abstracts take on this machine? If it
is slow enough to matter for a non-technical user, tell me.
```

---

## 4. A real evaluation set

```
Read CLAUDE.md first.

scripts/eval.py currently runs against a synthetic sample corpus, so its numbers
verify the harness and nothing else. I need a real one.

Build an evaluation set of 12-15 questions over a real ingested corpus:

- Write them to a JSON file matching the format scripts/eval.py expects.
- The questions should look like real diligence questions, not retrieval tests.
- For gold labels, propose which document contains each answer, but mark every
  one as UNVERIFIED and list them for me to confirm. Do NOT label them yourself
  and present the resulting number as a measurement — if the tool grades its own
  homework the number is circular and I cannot quote it.

Then run the eval with grading on and off and report both.
```

---

## 5. openFDA integration (only if scope has reopened)

```
Read CLAUDE.md first — note that FDA integration is on the "deliberately not
built" list. Confirm with me before starting; this is roughly two days.

If we proceed: build it as a THIRD structured store shaped like medrag/trials/,
not as prose in the vector index. Same reasoning — approval status and route are
filters, not semantics.

Scope:
- medrag/fda/client.py against the openFDA drug endpoints (no key needed for
  low volume; note the rate limits in the module docstring)
- medrag/fda/store.py, SQLite, matching the shape of trials/store.py
- Match approvals to assets by active ingredient, not brand name. Naming is
  messy across brand, generic, and development code names — handle that
  explicitly and tell me what your matching strategy misses.
- Extend router.py so approval questions route to the new store
- Extend context.py with an FDA RECORD provenance label
- Fixtures + mocked-transport tests, no network in the test suite

Report what fraction of a real ingested asset list you can actually match. If
matching is unreliable, I would rather know that than ship a section that
silently misses approvals.
```

---

## 6. Tuning the question set (do this with a practitioner, not alone)

```
Read CLAUDE.md and config/diligence_questions.yaml.

The question set is a draft I wrote as a placeholder. I am replacing it with a
version from someone who actually underwrites deals.

Do not rewrite the questions yourself. Instead:

1. Show me the current set as a plain numbered list I can put in front of
   someone, with each question's route and whether it gets a negative-evidence
   pass, in language a non-engineer can follow.
2. Tell me which questions the current retrieval is likely to answer badly and
   why — for example, questions needing several retrievals in sequence, since
   there is no multi-hop decomposition.
3. Once I bring back an edited list, apply it to the YAML and confirm the loader
   accepts it and section ordering follows the file.
```

---

## Small maintenance prompts

```
Run the full test suite and report the results honestly. Do not modify any test
to make it pass; if a test fails, either the code is wrong or the test encodes a
decision that changed — tell me which before touching anything.

  for f in tests/test_*.py; do python $f; done
```

```
Review the last set of changes against CLAUDE.md. Flag anything that reverses a
decision listed under "Decisions that must not be quietly reversed", and explain
what would break.
```
