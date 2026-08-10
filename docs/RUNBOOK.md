# Runbook

For whoever operates this. Not a tutorial — the README covers what it does and
how to start it, and CLAUDE.md covers why the code is shaped the way it is. This
is the page you open when something needs doing or something has gone wrong.

**Read this first if you are inheriting the repo:** the three items under
[Facts a code reader will not discover](#facts-a-code-reader-will-not-discover)
are operational properties with no visible presence in the code. They are the
ones that will surprise you.

---

## Refreshing the data

Three stores age independently. Nothing breaks when they get old — the tool
answers just as confidently from a stale snapshot, which is the problem.

| Store | Refresh with | Goes stale after | Why that interval |
|---|---|---|---|
| Clinical trials | `python -m medrag trials -c "<condition>" -n 500` | **30 days** | The registry changes daily: new trials, status flips, results posted. A month-old snapshot will miss recruiting trials that opened since. |
| FDA devices | `python -m medrag fda --product-code "<code>"` | **90 days** | Clearances and recalls arrive in batches; slower-moving than the registry. |
| Literature corpus | `python -m medrag ingest --query "<terms>" --index` | **90 days** | Published literature does not move quickly, and the memo cites what it cites. |
| Search index | `python -m medrag index` | with the corpus | Rebuilt from the corpus, not fetched. |

**Check the age without guessing:**

```bash
python -m medrag stats
```

The `data freshness` block reports each store's last refresh, flags anything past
its threshold as `STALE`, and prints a plain-language caution. The Streamlit app
shows the same thing: a caution banner above the form, and a "Last refreshed"
table in Settings.

**One case worth knowing.** If the corpus has been updated more recently than the
index, `stats` says so explicitly — documents have been ingested that no search
can reach, and the symptom is a memo that comes back thin for no visible reason.
Fix with `python -m medrag index`.

**A note on cadence.** The app fetches on demand: asking for a memo about an
asset with no stored research fetches it. So "refreshing" is mostly about assets
you have already looked at. If you are returning to an asset after a month,
re-run with the "Re-download research" checkbox ticked, or the memo will be built
on the old snapshot.

---

## Running the public site

The public service is a **separate application** (`public/`), not the Streamlit
app with pages hidden. Nothing in `app.py` or `pages/` is imported by it, so the
internal tool is unreachable from the internet by construction rather than by a
route check.

```bash
MEDRAG_READ_ONLY=1 MEDRAG_DATA_DIR=/srv/snapshot \
  uvicorn public.main:app --host 127.0.0.1 --port 8000
```

Routes, and what each may touch:

| Route | Consent | Touches |
|---|---|---|
| `GET /` | — | one template |
| `GET /terms` | — | the terms markdown |
| `GET /landscape` | not required | read-only snapshot |
| `GET /landscape.pdf` | not required | read-only snapshot; PDF built in memory |
| `POST /memo` | **required** | flagged OFF |
| `POST /claims` | **required** | flagged OFF |
| `GET /healthz` | — | nothing |

**Feature flags fail closed.** `PUBLIC_FEATURE_MEMO` and `PUBLIC_FEATURE_CLAIMS`
ship off; absent, empty or mistyped means off. A mistyped value is reported at
startup (`startup_report()`), because otherwise it looks identical to a flag
deliberately left off. Landscape is on.

**Do not pass `--access-log`, and do not expect it to matter.** The server's own
access logger writes the full request line *including the query string*, which
would put visitors' search terms into the log. `public/main` disables
`uvicorn.access` (and the gunicorn/hypercorn equivalents) at import, so the
guarantee does not depend on a command-line flag. The application's own log line
is method, route template, status, milliseconds — nothing else.

This was found by running a real server and grepping its log for a sentinel; the
unit tests could not see it, because `TestClient` never starts uvicorn's logger.

**Changing the model provider is a terms change.** `public/terms.py` compares the
configured provider against the disclosure block in `docs/TERMS-DRAFT.md`, and
`tests/test_public_app.py` fails if they disagree. Configuring a hosted provider
without naming it in the terms breaks the build. That is deliberate.

---

## Deploying read-only (a public site)

Set one environment variable:

```bash
MEDRAG_READ_ONLY=1 streamlit run app.py
```

That does three things, and each closes a hole a public deployment would
otherwise have:

- **Every store opens read-only** (`mode=ro`), with no schema execution, no
  `PRAGMA user_version` write and no commit. A write attempted anywhere on the
  read path raises `ReadOnlyStoreError` by name rather than corrupting the
  snapshot.
- **Nothing fetches.** `read_only` implies `offline` and drops the API key. A
  visitor's search answers from the stored snapshot or says it is not in the
  snapshot; it never makes the server pull from ClinicalTrials.gov or PubMed on
  their behalf. This deliberately outranks the "re-download" checkbox.
- **No directories are created.** `ensure_dirs()` is a no-op, so the pages start
  on a volume they cannot write.

**Preparing the snapshot.** The writable path uses WAL, so a database an ingest
just wrote has `-wal` and `-shm` files beside it. Checkpoint them away before
shipping, or the deployed `.db` is not self-contained:

```bash
sqlite3 data/raw/trials.db "PRAGMA wal_checkpoint(TRUNCATE); VACUUM;"
ls data/raw/trials.db*          # expect only trials.db
chmod 444 data/raw/*.db
```

Then mount the data directory read-only. Do **not** make it writable to get the
app to start — if it will not start read-only, that is a bug to report, not a
permission to grant. A public app with write access to its own database is the
thing this mode exists to prevent.

**Two read modes, and the difference matters.** `mode=ro` alone lets a reader
pick up a concurrent ingest's commits; `immutable=1` (pass `immutable=True`)
additionally promises SQLite the file cannot change, which removes any need for
lock files but makes the connection blind to writes. Use `immutable=True` only
for a genuinely frozen artefact. Measured on this database, a connection held
open across a writer's commit: `mode=ro` sees 501 → 2501 rows, `immutable=1`
stays at 501.

**The internal Streamlit app is for internal use only** — it is not the public
surface and must not be exposed. Two of the three items previously listed here
are now FIXED:

- ~~The Settings "Change provider or key" button~~ — **removed**. It rewrote
  `.env` and mutated the process environment for every concurrent user. Provider
  configuration is a deployment setting; set `MEDRAG_PROVIDER` and restart.
- ~~Exports collide on a user-derived filename~~ — **fixed**. `crypto.unguessable_stem`
  keeps the human label and appends 8 bytes of `secrets.token_hex`, so two people
  exporting the same asset no longer share a path. Still 0600.
- `app.py` sends question text to the configured LLM provider with no consent
  gate (the claims page has one; the memo page does not). **Still open** — it is
  an internal-tool concern, and the public service does not share this code
  path.

---

## When an ingest is interrupted

A trial ingest killed partway — a crash, a closed laptop, Ctrl-C — leaves the
query set it was working on marked `IN_PROGRESS`. Nothing raises when a process
dies, so this marker is the only evidence.

```bash
python -m medrag trials --incomplete
```

Every ingested query set with its state and two numbers: what the store holds,
and what the last fetch recorded. Exits non-zero if any set is unverified. Re-run
those, and only those:

```bash
python -m medrag trials --condition "<set key from the list>"
```

Re-running is safe and idempotent — records upsert by NCT ID, and query
provenance merges rather than overwrites.

Two things this command does **not** tell you. It lists sets that were *started*;
a set in `config/trial_queries.yaml` that was never ingested at all has no row
and does not appear, so compare against that file to find those. And a set marked
`PARTIAL` rather than `IN_PROGRESS` finished its fetch but failed verification —
the reason is printed by the ingest itself, and is usually either a query that
errored or a `--max-records` cap, which is truncation by intent and grades
PARTIAL for exactly that reason.

Until a set verifies, every memo and page that uses it prints `PARTIAL INGEST`
with the count as a stated lower bound. That is the intended behaviour, not a
bug to work around: the number is real, it is just not the whole population.

**If the ingest prints `registry was unreliable: retried N time(s)`,** the fetch
succeeded but ClinicalTrials.gov made it work for the data. A handful of retries
is normal. Dozens, or the same query retrying every run, means the registry is
degrading — check status.clinicaltrials.gov before assuming the problem is here.
The counts are stored per query in `query_coverage.yields`, so you can compare
against previous ingests rather than relying on memory:

```bash
sqlite3 data/raw/trials.db \
  "select set_key, json_extract(value,'\$.query'), json_extract(value,'\$.retries')
   from query_coverage, json_each(yields)
   where json_extract(value,'\$.retries') > 0;"
```

Retries never turn a failure into a success — a query that exhausts its attempts
still errors, and its family still records PARTIAL. If you see no retry line and
no PARTIAL, the fetch was genuinely clean.

---

## When a test fails

```bash
pytest tests/ -q                 # all of it
python tests/test_claims.py      # one file, also supported and CI-checked
ruff check .                     # the linter CI runs
```

Work through this in order.

**1. Is it the network guard?** A failure mentioning `NetworkAccessDenied` means
a test tried to reach the internet. That is the guard working. Do not add
`@pytest.mark.allow_network` to make it pass — find the unmocked call and mock
it against a fixture in `tests/fixtures/`. This exact failure is why the guard
exists: a half-finished test once made a live request to Groq carrying deck text.

**2. Does it fail on a fresh clone but pass locally?** Then it depends on
untracked state — most likely `.env`, which is gitignored and which the claims
page reads from disk to decide whether it is configured. Four tests had this bug.
The fix is for the test to create what it needs; see `_configured_app()` in
`tests/test_consent_gate.py`.

```bash
git clone <this repo> /tmp/check && cd /tmp/check
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q
```

**3. Is it a store schema refusal?** `TrialStoreSchemaError` or
`FDAStoreSchemaError` mean the database on disk predates the columns the code
needs. That is fail-closed behaviour, not a bug. Delete and re-ingest:

```bash
rm data/raw/trials.db
python -m medrag trials -c "<condition>" -n 500
```

**4. Is it the corpus?** `medrag stats` reports unreadable records. If any are
quarantined, the count and a plain-language note appear there, in the app, and in
the warnings block of any memo generated while records are set aside. Re-ingest
to recover them; the originals are kept in `corpus.jsonl.quarantine.jsonl`.

**5. Numerical warnings from `retriever.py:29`** (`invalid value encountered in
matmul`, `divide by zero`) appear during some runs and do not fail anything.
They indicate zero-norm vectors in the index. Known, see below.

---

## Rotating a provider key

The key lives in `.env`, mode 0600, gitignored, and never committed (verified
across the entire git history). It is only ever read from there and from the
process environment.

**Rotate:**

1. Revoke the old key in the provider console (Groq: console.groq.com/keys).
2. Issue a new one.
3. Either open the app, expand **Settings**, click **Change provider or key**, and
   paste it — or edit `.env` directly and restart.
4. Confirm: `python -m medrag doctor`.

**When a key expires or is revoked,** every model call fails and the tool degrades
rather than crashing: routing falls back to its rule-based path, answers become
extractive evidence lists rather than syntheses, and the contradiction hunt does
not run. Memos are still produced and still fully cited. That is intentional, but
it means an expired key looks like "the memos got worse", not like an error. If
quality drops suddenly, check the key first.

**Never** put the key in `.streamlit/secrets.toml`, a shell profile committed
anywhere, or a CI variable. There are tests asserting it cannot reach a
traceback, a log record, stdout, the corpus, the index, or a memo.

---

## Facts a code reader will not discover

These three are operational properties. None is visible from reading the code,
and all three matter for confidentiality.

### 1. The asset name reaches NCBI and ClinicalTrials.gov on every run, whatever model you pick

Choosing a local model (Ollama) or `none` stops the **claims** leaving. It does
**not** stop the **asset name** leaving, because that is how the research is
fetched. Querying PubMed or ClinicalTrials.gov for a compound discloses to those
services what is being researched, and for a stealth-mode company the query
string is often the confidential part.

This is asserted, not assumed —
`tests/test_privacy.py::test_asset_name_reaches_the_registry_even_when_fully_local`
pins it, and the claims page's confirmation notice states it.

**The mitigation, and it is a workflow not a setting:** pre-load broadly, then
work offline.

```bash
# Ahead of time, and by indication rather than by compound:
python -m medrag trials -c "colorectal cancer" -n 500
python -m medrag ingest --query "colorectal cancer" --index

# Then, on the specific asset:
export MEDRAG_OFFLINE=1
```

The registry sees a disease, not your target. `MEDRAG_OFFLINE=1` is a hard block
verified at two layers: every client raises before making a request, and
`make_client` returns `None` so no provider can be reached. In that mode the
stores must already be populated.

### 2. The consent gate governs transmission, not retention — check the provider console

The per-run confirmation controls the moment text is sent. It says nothing about
what the provider does with it afterwards. Whether your prompts are retained,
logged, reviewed by humans, or used for training is an **account setting in the
provider's console**, not a property of this code, and no amount of review here
can establish it.

**Whoever operates this must check it,** for whichever provider is configured:

- Groq: console.groq.com — data-retention and training settings
- OpenAI: platform.openai.com — API data controls; API traffic is not trained on
  by default, but retention windows still apply
- Cerebras / OpenRouter: equivalent settings in their consoles
- Ollama: local, nothing transmitted, nothing to check

This is the one place "IPs cannot leave" can be violated with no code involved.
If the answer is unsatisfactory, the options are Ollama, `none`, or
`MEDRAG_OFFLINE=1`.

### 3. `out/` is outside the encryption boundary

Generated memos live in `out/`, mode 0600, directory 0700, gitignored.

The **Markdown** honours `MEDRAG_ENCRYPT` and is encrypted at rest when a
passphrase is configured. The **PDF is deliberately not** — a memo exists to be
circulated, and an encrypted PDF opens in nothing. Its protection is filesystem
permissions only, which is weaker than encryption.

So `out/` holds readable PDFs containing deck-derived claims, company names, and
assets under diligence. If that is unacceptable in your setting, do not generate
the PDF: take the Markdown and render it inside whatever boundary you need.

Note also that the claims and landscape pages never prompt for a passphrase, so
with `MEDRAG_ENCRYPT=1` those runs stop at export with a `CryptoError` rather
than writing cleartext. Fail-closed and intentional, but uneven against the
diligence page, which does prompt.

---

## Known broken, current

| Thing | Impact | Detail |
|---|---|---|
| **Python 3.9 is end-of-life** | Security fixes are unreachable | `pip-audit` reports 40 findings against the pinned set; for `requests` and `python-dotenv` the fixed versions are `Requires-Python >=3.10` and cannot be installed here. Moving to 3.11 is the real remediation and nothing else on this list matters as much. |
| **`CLAUDE.md` is gitignored** | A fresh clone does not contain it | `.gitignore:34`. The document recording every decision that must not be reversed is not in the repo the next person clones. Decide deliberately: publish it (the repo is public) or hand it over out of band. |
| Known dependency vulnerabilities | Advisory | 40 findings, 10 packages. Direct: `torch` (8), `streamlit` (2), `requests` (1), `python-dotenv` (1). Transitive: `pillow` (18), `transformers` (4), others. CI reports but does not fail on these, because failing on unfixable findings trains people to ignore CI. |
| `streamlit` pinned to 1.50.0 | Cannot patch without visual QA | `theme.py` targets Streamlit's internal `data-testid` DOM, which is not a public API. The two Streamlit advisories are fixed in 1.53.1+; upgrading needs the visual checks re-run. |
| `retriever.py:29` numerical warnings | Cosmetic, so far | `invalid value` / `divide by zero` in matmul, from zero-norm vectors. Results still return. Worth investigating before it becomes a silent relevance bug. |
| The trial-store census is unindexed | Slow at scale | `trials/store.py:363-368` runs 21 `COUNT(*)` queries with leading-wildcard `LIKE`. Fine at 500 rows; a table scan per marker per request at 500k. |
| Opening a store writes to it | Blocks read-only deployment | `TrialStore.__init__` runs `executescript(SCHEMA)` and `PRAGMA user_version` on every open. There is no read-only path. Any public deployment needs one. |
| `biomarker.py` and `biomarker_gating.py` can disagree | Two answers to one question | Duplicated marker tables that have already drifted on `non-MSI-H`. Agreed fix is one shared vocabulary with two policy layers; not yet done. |

---

## Decisions in CLAUDE.md that must not be reversed

CLAUDE.md is the authority. These are the ones most likely to be "cleaned up" by
someone who does not know why they exist. Read the full entry there before
touching any of them.

| Decision | What breaks if reversed |
|---|---|
| **Trial records are not in the vector index** | Phase and status are filters, not semantics. Embedding "Phase 3, TERMINATED" as prose destroys the precision the registry exists for. |
| **"Not assessed" and "nothing found" stay distinct** | `ValidationReport.assessed`, `NegativeEvidence.searched`, `CorpusHealth`. Reporting an unchecked section as passing is a false negative dressed as a pass. This has already been a real bug: the memo once claimed 10/10 sections passing when the honest number was zero. |
| **Support and independence are two axes** | A claim can be well supported and entirely company-sourced. Merging them hides an independent partial behind a scary label and a company-only behind a reassuring one. |
| **Absence of a disclosure is never independence** | `NO DISCLOSURE` is the honest default. Nothing found either way must not read as a clean pass. |
| **NOT FOUND and CONTRADICTED are never swapped** | An empty retrieval is NOT FOUND deterministically; the model is never consulted, so it can never turn "nothing retrieved" into "evidence against". |
| **The stopped-trial lookup ORs intervention and indication** | ANDing hid trials of the same compound stopped in a *different* indication, which is among the most valuable things a diligence pass surfaces. Regression test: `test_stopped_trial_in_other_indication_is_not_hidden`. |
| **The router keeps a rule-based fallback** | A router that silently degrades to always-BOTH looks like it is working while doubling cost and diluting every answer. |
| **JSONL splits on newline only, never `str.splitlines()`** | `splitlines()` breaks on U+2028, which is legal inside a JSON string. This turned a healthy 170-record corpus into "169 loaded, 8 unreadable" and made every later ingest fail at the same offset. `medrag/jsonl.py`. |
| **`save_corpus` appends; it does not read the corpus back** | The read-modify-write is why one unreadable line killed all *future* ingests. |
| **The consent gate is per content, per destination** | `consent_key()` hashes the transmission notice. A fixed widget key made one tick consent to every later deck; a count-derived label let three claims be swapped for three different ones under the same tick. |
| **Fail closed** | `write_secure` refuses plaintext when encryption is on. Stores refuse a stale schema. A run that stops and says so beats one that quietly degrades. |

---

## Routine checks

```bash
python -m medrag doctor     # are the data sources reachable
python -m medrag stats      # counts, corpus health, data freshness
pytest tests/ -q            # 351 tests, no network
ruff check .                # lint, same rules CI uses
pip-audit -r requirements.txt   # advisories against the pinned set
```

Regenerate the lockfile after changing any pin:

```bash
pip-compile --generate-hashes --output-file requirements.lock requirements.txt
```

CI runs on every push and pull request: install from the hashed lockfile, lint,
pytest, then every test file directly. No deploy step — this deploys nowhere.
