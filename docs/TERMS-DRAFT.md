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

## TERMS_VERSION: 2026-08-10-draft-2

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

## 5. What submitting material does NOT create

This section protects Pebble. It is the reason a venture firm can accept
unsolicited third-party material at all, and it is deliberately separate from
section 2: those are promises about what the software does, these are statements
about what a submission does and does not bring into being. Nothing in section 2
limits this section, and nothing here weakens section 2.

**5.1 No confidential relationship.** Submitting material through this service
does **not** create a confidential relationship, a non-disclosure obligation, or
a duty of confidence of any kind. Pebble does not agree to keep submitted
material confidential and is under no obligation to do so. If you need material
treated as confidential, **do not submit it here** — approach Pebble directly and
agree terms in writing first.

**5.2 No fiduciary duty.** Submission creates no fiduciary duty, no agency, no
partnership, no joint venture and no employment relationship between you and
Pebble.

**5.3 No investment relationship.** A submission is **not a pitch**. It is not an
offer to sell or a solicitation of an offer to buy any security, and it is not an
offer by Pebble to invest, advise or transact. Submission creates no expectation
that Pebble will review the material, respond to it, evaluate it, or take any
action at all. Pebble may ignore a submission entirely, and silence means
nothing.

**5.4 No restriction on Pebble's activities.** Submitting material does **not**
restrict Pebble in any way. Pebble is free to evaluate, invest in, advise, work
with, acquire, or otherwise engage with any company or technology, including:

- companies that compete directly with you;
- companies working on the same problem, in the same indication, or with the same
  mechanism or approach;
- companies whose material has been submitted through this service by someone
  else; and
- companies Pebble was already looking at, or finds independently, before or
  after your submission.

Pebble evaluates many companies in the same fields and receives material from
many sources. **A submission through this service does not taint, block or create
any claim against Pebble in respect of any of that activity**, and does not
create any obligation of non-use.

**5.5 You keep your rights; Pebble claims none.** Pebble claims no ownership of,
and no licence to, submitted material beyond what is technically necessary to
process the request and return a result to you. The material remains yours (or
its rightful owner's). Pebble does not acquire any intellectual property rights
in it by virtue of your submission. Consistent with section 2, nothing submitted
is retained after the response is sent, so there is nothing held to which rights
could attach.

**5.6 Pebble does not review or police submissions, and cannot verify your right
to submit.** Submissions are processed automatically. Pebble does not screen,
moderate, review or approve what is submitted, and has no practical means of
verifying that a submitter is entitled to submit the material — that it is
theirs, that it is not subject to someone else's confidentiality obligation, and
that submitting it breaches no agreement.

**That verification is your responsibility.** By submitting, you confirm that you
have the right to do so and that submitting does not breach any obligation you
owe to anyone else. **Do not submit material that belongs to a third party, or
that you received in confidence.**

## 6. Rate limiting and abuse

Requests are rate-limited per IP address. The rate limiter counts requests
against an address; it does not record what was requested.

## 7. Availability and accuracy

The data is a **stored snapshot**, not a live feed. The snapshot date is shown
on every result. Sources are incomplete in ways we state on the page rather than
leaving you to infer: registries we do not search are listed by name, and a
count that could not be verified as complete says so.

Absence of a result is **not** evidence of absence in the world. It means the
snapshot does not contain it.

## 8. No warranty

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

4. **Section 5 protects Pebble and is the reason unsolicited material can be
   accepted at all.** It is drafted to be readable rather than maximal, on the
   view that a clause a submitter actually understands is worth more than one
   that is broader and unread. Two places a lawyer may want to go further: 5.4
   currently disclaims restriction and non-use but does not include an express
   residuals clause covering unaided memory, and 5.6 places the
   right-to-submit warranty on the submitter without an indemnity. Both were
   left out deliberately as escalations for counsel to make knowingly.

5. **Sections 2 and 5 must not be collapsed.** Section 2 says what the software
   does; section 5 says what a submission does not create. A reader who takes
   "we do not keep it" as implying "so it is confidential" has drawn exactly
   the wrong inference, which is why 5.1 says so in terms.
