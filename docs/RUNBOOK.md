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

## The data artifact: build, verify, swap, roll back

Written for someone who was not here. You need a checkout, a Python 3.11+ venv,
and the stores under `data/raw/` (build those with `python -m medrag trials`).

### Build

```bash
python scripts/build_artifact.py --out dist/artifact
```

One command. It compacts each store with `VACUUM INTO` (read-only on the source,
so this is safe to run while the tool is in use), stamps the snapshot date and
content version **inside** each database, and writes `manifest.json` and
`SHA256SUMS`. Takes about 30 seconds and produces roughly 1.9 GB.

The snapshot date lives inside the file, in a `snapshot_meta` table, **not in
the filename**. Renaming `trials.db` changes nothing about what the app believes
its age to be — which is the point, because a filename is a label anyone can
change with `mv`.

Confirm the build is a pure function of its inputs:

```bash
python scripts/build_artifact.py --verify-reproducible   # builds twice, compares
```

### Verify — do this on the machine that will serve it, after copying

```bash
cd /srv/artifact && shasum -a 256 -c SHA256SUMS
```

Every line must say `OK`. This is the check that catches a truncated upload,
which is the most common way a deployment ends up serving half a database.

### Swap

The service verifies the artifact **at startup** and refuses to run on one that
is missing, corrupt, or older than `PUBLIC_MAX_SNAPSHOT_AGE_DAYS`. So a swap is:
put the new artifact beside the old one, point at it, restart, confirm.

```bash
# 1. Copy the NEW artifact to a new directory — never overwrite the live one.
rsync -a --checksum dist/artifact/ /srv/artifacts/2026-08-10/

# 2. Verify it in place.
cd /srv/artifacts/2026-08-10 && shasum -a 256 -c SHA256SUMS

# 3. Repoint and restart.
ln -sfn /srv/artifacts/2026-08-10 /srv/artifact-current
systemctl restart trialfinder     # or: docker compose up -d

# 4. Confirm what is actually live — do not skip this.
curl -s https://YOUR-REAL-HOST/healthz | python -m json.tool
```

Step 4 should show the new `snapshot_date`, `artifact_verified: true`,
`snapshot_stale: false`, and `checksums_verified: true`. If it shows the old
date, the restart did not pick up the new directory.

**`/healthz` returns 503 once the snapshot passes the threshold, without a
restart.** Staleness is recomputed per request rather than read from the value
captured at import — a process that started one day inside the threshold and ran
for a month used to keep serving and keep answering `snapshot_stale: false`,
because the refusal in `verify()` only fires at startup and nothing re-asked in
between. The payload carries both numbers: `snapshot_age_days` (now) and
`snapshot_age_days_at_startup` (what the startup check acted on); the gap between
them is how long the process has been up.

Operationally this means an ageing deployment takes itself out of a load-balancer
rotation and, under an orchestrator that restarts on health failure, hits the
startup refusal — a restart loop and a log line, which is the intended way for
this to become impossible to ignore rather than a page that gets quietly more
wrong. If you accept the age, raise `PUBLIC_MAX_SNAPSHOT_AGE_DAYS` deliberately.

**Never overwrite the live artifact in place.** A partially-copied 1.9 GB file
is a broken deployment, and it is broken for as long as the copy takes.

### Roll back

Because each artifact is its own directory, rollback is repointing:

```bash
ln -sfn /srv/artifacts/2026-08-01 /srv/artifact-current
systemctl restart trialfinder
curl -s https://YOUR-REAL-HOST/healthz | python -m json.tool   # confirm the date moved back
```

Keep the previous two artifacts on disk (≈4 GB) so this is always available.
Rolling back to an artifact older than the staleness threshold **will not
start** — that is deliberate. If you need it anyway, raise
`PUBLIC_MAX_SNAPSHOT_AGE_DAYS` explicitly and knowingly.

### After ANY infrastructure change

New host, new CDN, new proxy, new WAF, changed logging config, new error
tracker — all of these reintroduce layers that log URLs by default.

1. Re-read the URL-logging table below and re-check every layer.
2. **Re-run the sentinel test against the real hostname. This is mandatory, not
   advisory** — passing locally proves the application layer only and says
   nothing about what now sits in front of it.
3. `curl /healthz` and confirm the snapshot date, terms version and provider are
   what you expect.

---

## Deployment checklist: every layer that can log a URL

**The application cannot keep this promise alone.** Silencing `uvicorn.access`
closes the layer we own. Every other layer in a normal deployment logs request
URLs *by default*, and each one is a separate place a search term can land. Work
through all of them before the site takes traffic.

The search itself is now a **POST**, so the terms are in a request body rather
than a URL — which is what makes this checklist survivable, because bodies are
not logged by default anywhere below. The checklist still matters: a body is
only unlogged until someone turns body logging on, and any *other* route that
takes a query string reopens the hole.

| Layer | Default | What to do |
|---|---|---|
| **This app** | safe | `log_request` writes method, route template, status, ms. Nothing to do. |
| **This app, on a 500** | safe | `log_exception` writes the exception TYPE and one `file:line in function` per frame — never `str(exc)`, which routinely quotes the input, and never a local. Before this a 500 left one access-log line and nothing to debug from. The cost: a `KeyError` no longer tells you which key. The file and line do. |
| **uvicorn / gunicorn** | **logs full URL** | Silenced at import by `public/main`. Do not re-enable; `--access-log` will not defeat it, but a custom `logging.config` might. |
| **nginx / Apache** | **logs full URL** | The default `combined` format includes `$request` (method + full URI) *and* `$http_referer`. Use a format with neither, or `access_log off;`. |
| **Cloudflare / CDN** | **logs full URL** | Logpush and the HTTP Requests analytics both capture the URI. Disable Logpush for this hostname, or restrict fields to method/status/timing. Check WAF sampling too — blocked requests are logged with their URI. |
| **PaaS router** (Heroku, Fly, Render, Railway, App Runner) | **logs full URL** | The platform router log is usually not configurable. Assume the path is recorded; this is the strongest argument for POST. |
| **Load balancer** (ALB, GCLB) | **logs full URL** | S3/Cloud Logging access logs include the full request URL. Turn access logging off for this target group, or accept it and rely on POST. |
| **Browser history** | records URLs | POST results are not in history. Do not add a "share this search" link. |
| **`Referer` header** | sent to any external host | `Referrer-Policy: no-referrer` is set on every response, and the page loads nothing external. Both are tested. |
| **Error tracking** (Sentry etc.) | **captures URL, body, headers** | Not installed. If one is ever added, configure `send_default_pii=False` and scrub request bodies *before* deploying it. |
| **Process listing** | `ps` shows argv | Never pass a search or a key on a command line. |

**Then re-run the sentinel test against the real deployed URL.** Passing locally
proves the application layer only — it says nothing about the nginx in front of
it or the PaaS router above that.

```bash
SENTINEL="ZZQX-$(date +%s)-deployment-check-ZZQX"

# Through the REAL hostname, so every intermediary sees it.
curl -s -X POST https://YOUR-REAL-HOST/landscape \
     -d "condition=$SENTINEL&biomarker=$SENTINEL" -o /dev/null

# Then search every log you can reach. Not only the app's.
grep -r "$SENTINEL" /var/log/nginx/ /var/log/ 2>/dev/null
journalctl -u YOUR-SERVICE --since "10 minutes ago" | grep "$SENTINEL"
# Cloudflare: Logpush destination, and the Security Events UI
# PaaS: `heroku logs --tail`, `fly logs`, `render logs`, etc.
```

A hit anywhere is a leak, and the fix belongs at that layer — not in the app,
which has already done what it can.

**Do this again after any infrastructure change.** Adding a CDN, moving hosts, or
turning on a WAF each reintroduce a layer that logs URLs by default.

---

## What it costs to run

Measured on the real 241,298-trial artifact (2026-08-10), Python 3.9 on macOS,
single core per worker. Numbers, not estimates.

### Disk

| | |
|---|---|
| `trials.db` | **1,860.6 MB** |
| `fda.db` | 39.1 MB |
| `drugs.db` | 16.9 MB |
| **artifact total** | **1,916.6 MB** (2.01 GB as the filesystem reports it) |
| Keep 2 previous artifacts for rollback | **≈6 GB** provisioned |

The trial store is 80% text, and it cannot be trimmed without changing what the
tool answers: `eligibility_criteria` 416 MB (27%), `locations` 361 MB (23%),
`detailed_description` 302 MB (20%), `brief_summary` 155 MB (10%). The two
description fields look like dead weight and are not — `build_landscape`
screens them live, which is how ADG126-P001 is found at all (it states its MSS
focus only in its detailed description).

### Memory

| | |
|---|---|
| Python + imports | 26 MB |
| After opening the 1.9 GB store | 26 MB — SQLite is paged, not loaded |
| Peak during a colorectal search | **167 MB** |
| Steady state after several searches | ~167 MB |

**512 MB per worker is comfortable; 1 GB for two workers plus headroom.** The
artifact does not need to fit in RAM, but the OS page cache will use whatever is
spare, and a box with 2 GB free will serve noticeably faster than one with 256 MB.

### Latency

Two optimisations landed after the first measurement; both numbers below are
measured on the same machine and the same 241,298-trial store.

| Search | Population | Before | After |
|---|---|---|---|
| `rett syndrome` | 104 | 3.1 s | **0.05 s** |
| `colorectal cancer` | 12,095 | 12.8 s | **1.8 s** |
| `breast cancer` (HER2) | 17,089 | ~17 s (projected) | **1.9 s** |

**1. Query-set membership is an indexed join table.** `query_sets LIKE '% key %'`
could not use an index, so every search full-scanned all 241k rows six times —
a fixed ~3 s regardless of family size. `trial_query_sets(set_key, nct_id)`
turns that into an index range scan. The old token column was DROPPED, not kept
alongside; the migration asserts the join table reproduces it exactly for every
family first.

**2. The live biomarker screen is prefiltered by the ingest-time census.** The
gating tokens were already computed at ingest, so SQL narrows colorectal from
12,095 records to 826 before any Python runs. Proven equivalent before shipping
— see below.

### Throughput and concurrency

Two workers, measured end to end through uvicorn:

| Search | Concurrency | Before | After |
|---|---|---|---|
| `rett syndrome` | 1 | 0.32 req/s | **16.5 req/s** |
| | 2 | 0.33 req/s | **33.7 req/s** |
| | 4 | 0.42 req/s | **46.1 req/s** |
| | 8 | 0.29 req/s | **34.4 req/s** |
| `colorectal cancer` | 1 | ~0.08 req/s | **0.75 req/s** |
| | 8 | — | **1.38 req/s** |
| | 16 | — | **1.02 req/s** |

Throughput now **scales with concurrency** up to the worker count instead of
being flat — the work per request is small enough that the threadpool can
overlap it. The health check answers in 1.5 ms under load.

The honest envelope has moved: a cheap search is now genuinely cheap
(~46 req/s on two workers), and the expensive families are ~1.3 req/s rather
than 0.08. **A campaign is now survivable** for common searches; a page where
every visitor searches a 12,000-trial family still wants more workers, and
throughput is still `workers × per-request-cost`, so size for the searches you
expect rather than the average.

### Precomputed results

The artifact ships answers for every **condition x curated marker** pair —
74 families x 7 markers = 518 pairs, 10,453 ranked rows, **+8 MB** on a 1.9 GB
artifact. Computed at build time, so there is no request-time cache and
therefore no retention surface: nothing is keyed, nothing expires, and the terms
need no amendment.

**Location is deliberately not precomputed** (combinatorial across free-text
place names). Proximity is a ranking pass over rows that are already selected,
so it is applied per request to the precomputed candidate set.

| Search | Live path | Precomputed |
|---|---|---|
| `colorectal cancer` + MSS | 1.8 s | **0.030 s** |
| `breast cancer` + HER2 | 1.9 s | **0.030 s** |
| `rett syndrome` + MSS | 0.05 s | **0.036 s** |
| `colorectal` + MSS + location | — | **0.069 s** |
| `colorectal` + FGFR2 (uncurated, not precomputed) | 2.0 s | **2.0 s** — falls through, by design |

Throughput on two workers: **~47 req/s** at concurrency 4–8 (was 0.08 req/s
before any of this work).

**Two startup gates, and both fail closed.** The precompute is stamped with a
`code_version` fingerprint derived from every source file and config that can
change an answer — so a serving-code change with a stale artifact is refused.
And because a fingerprint proves the bytes matched rather than that the
behaviour did, startup also **re-runs a sample of precomputed pairs through the
live path** and compares. Either mismatch refuses to start, naming the remedy.

If you change `markers.py`, `biomarker.py`, `biomarker_gating.py`,
`landscape.py`, `ranking.py`, `config/markers.yaml` or `config/ranking.yaml`,
**rebuild the artifact** — the service will not start otherwise, which is the
intended behaviour rather than an inconvenience.

### Where the remaining time goes

For colorectal the 1.8 s is now dominated by loading and screening the 826
prefiltered records. Further gains would need either a narrower SQL prefilter
(the census cannot narrow further — 826 IS the admitting set) or caching, which
has a retention question attached and is not built.


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
not run. Memos are still produced and still fully cited.

**This was documented before it was true.** The router and the contradiction
hunter did catch a provider error; the ANSWER path did not, so a configured key
returning 403 raised `openai.PermissionDeniedError` out of `diligence._answer`
and killed the run with a traceback on question 1 of 11 — the two halves that
degraded were the two nobody would have noticed. Every model call now goes
through `providers.call_chat`, which returns a failure instead of raising.

The degradation is no longer silent either, which was the other half of the
complaint this paragraph used to make about itself. The memo's warnings block
names the provider and what it returned:

    the configured model provider (groq) returned HTTP 403: the provider refused
    the request — the key is revoked, out of quota, or not permitted to use this
    model. No further model calls will be made in this run. ...

A 400, 401, 403 or 404 latches the model off for the rest of the run — asking
eleven times produces eleven identical refusals — while a 429, 5xx or timeout is
retried on the next question, because a blip should not cost ten syntheses. The
message is BUILT from the status code and the provider name, never from the
SDK exception, which renders the response body.

So an expired key no longer looks like "the memos got worse". It says so. If
quality drops suddenly and there is no warning, the key is not the cause.

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
