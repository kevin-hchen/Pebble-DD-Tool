# Terms of use — DRAFT, NOT YET APPROVED BY COUNSEL

**Status: draft. Not legal advice, not reviewed by a lawyer, not to be published
as-is.** Written to be exact about what the software actually does, so that a
lawyer is editing a true description rather than inventing one.

**This file is load-bearing, not documentation.** `public/terms.py` reads the
version and the retention claims below, the public site renders them, and
`tests/test_public_app.py` fails if the software stops matching them — including
if someone changes the model provider without updating the disclosure here. If
you edit this file, run the tests; if they fail, either the text or the software
is wrong and the mismatch is the point.

---

## TERMS_VERSION: 2026-08-10-draft-1

Bump this whenever the substance below changes. The consent record stores this
string, so a consent given under one version is never silently treated as
consent to another.

---

## 1. What this service is

A research aid over public data: the ClinicalTrials.gov registry, the openFDA
databases, and published biomedical literature. It reports what those sources
say and cites the record for every statement.

It is **not medical advice**, not a recommendation, and not a substitute for a
clinician. Trial eligibility shown here is **indicative only** and is determined
solely by the trial team running that study.

## 2. What we do with what you type

These are the retention claims. They are enforced in code, and each is listed
here with the mechanism that makes it true.

1. **Text you submit is never written to disk.** No request path writes a file.
   Documents are parsed in memory, results are rendered in memory, and PDFs are
   streamed from memory. There is no upload directory, no cache of your input,
   and no temporary file.

2. **Text you submit is never logged.** Request logging records the HTTP method,
   the route template, the status code and the elapsed time. It does not record
   request bodies, query-string values, form fields, file names or file
   contents.

3. **Text you submit is never used to train anything.** No submitted text is
   retained after the response is sent, so there is nothing to train on. We do
   not sell, share or transfer submitted text.

4. **Text you submit is never read by our staff.** Nothing persists it for a
   human to read.

5. **Nothing is retained between requests.** Per-request state is cleared when
   the response is sent. Each submission stands alone.

## 3. The one thing that is recorded

When you tick the consent box, we record **two values and nothing else**:

- the time you consented, and
- the version string of these terms.

No copy of what you submitted, no identifier for you, no name, no email, no
account. The consent record is defined in code with no field capable of holding
content, and there is a test asserting that no such field can be added.

## 4. Where your text goes — the model disclosure

Answering a question may involve a language model.

**PROVIDER_DISCLOSURE_BEGIN**

**Current configuration: no external model provider is used.** The public
service runs with no language-model provider configured. Nothing you submit is
sent to any third party. This is the default and it is enforced in code: if no
provider is configured, the features that would use one are switched off rather
than falling back to a hosted service.

Where a deployment is configured to use a model running **on the same host**,
your text does not leave that machine.

Where a deployment is configured to use a **hosted provider**, this section
names that provider explicitly, and the page you submit from names it too,
before you submit. If this section does not name a third party, no third party
receives your text.

**PROVIDER_DISCLOSURE_END**

## 5. Rate limiting and abuse

Requests are rate-limited per IP address. The rate limiter counts requests
against an address; it does not record what was requested.

## 6. Availability and accuracy

The data is a **stored snapshot**, not a live feed. The snapshot date is shown
on every result. Sources are incomplete in ways we state on the page rather than
leaving you to infer: registries we do not search are listed by name, and a
count that could not be verified as complete says so.

Absence of a result is **not** evidence of absence in the world. It means the
snapshot does not contain it.

## 7. No warranty

Provided as-is, without warranty of any kind. Do not rely on it for clinical,
regulatory or investment decisions without independent verification against the
primary sources, which are cited on every result.

---

## Notes for counsel

Three things worth a lawyer's attention:

1. **Section 2 is unusually specific on purpose.** Each claim maps to a code
   mechanism and a test. If a claim is softened, the corresponding guard should
   probably be relaxed too — otherwise the software is stricter than the
   promise, which is safe but means the promise is not describing the product.

2. **Section 4 changes meaning if a provider is ever configured.** The text
   between the `PROVIDER_DISCLOSURE` markers is checked against the deployed
   configuration by a test. Configuring a hosted provider without editing this
   section fails the build. That is deliberate: the terms cannot silently become
   untrue.

3. **Section 3 is the only retention.** If any additional field is ever needed
   on the consent record, that is a terms change and a test change, not an
   implementation detail.
