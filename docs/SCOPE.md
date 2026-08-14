# Scope and purpose

Read this before any design decision. It is the definition the tool is built
against, in the owner's words. Where a build choice and this file disagree,
this file wins.

## Who it serves

Pebble, a healthcare VC and accelerator. Analysts and interns who will not open
a terminal. A technically versed analyst who inherits it. And the public,
through the Pebble website.

## What Pebble screens

**Drugs and devices equally.** Not one primary and the other secondary. Both
paths must be equally real, equally tested, equally gated.

Devices and diagnostics span the full range — imaging, monitoring, implants,
IVDs, and everything between. Bilirubin monitors and X-ray machines were
examples, never the category. Less digital health.

Greater Bay Area assets are in scope, which means trials registered outside
ClinicalTrials.gov.

Early stage, because it is an accelerator. **Thin or absent evidence is the
normal case, not the edge case.**

## What the job is

Verifying sources and claims takes the longest. Proving the technology without
the words, information and studies the company supplies is the hard part.

So: independent evidence about the technology, and independent verification of
what the company asserts.

## Hard constraints

- **IPs cannot leave.** Security and confidentiality matter.
- The information must be **100% accurate to what the study actually says.**
- **Every claim cited, and cited cleanly.**
- ~~Two to three pages maximum for a memo.~~ **RELEASED 13 August 2026.**
  Memo length is no longer a hard constraint. This was an owner decision, not a
  build convenience, and it is recorded here rather than quietly dropped.

  Measured state at the time of the decision, so the number is not lost:
  **7 pages when nothing is found**, **11 for a well-evidenced device**,
  **24 for a well-evidenced drug**. 7 is the floor — a memo that finds nothing
  cannot currently be shorter than that.

  Composition of a well-evidenced memo, by character count:
  evidence blocks 46.3%, source lists 26.4%, headers and coverage 21.0%.
  That makes length a renderer and question-count matter rather than a
  prose-trimming one, which is why trimming was never going to reach 2-3 pages.

  A summary-first structure — a standalone 2-3 page front section with the
  evidence behind it — remains available as a small renderer change if the
  owner wants it later. It is not being built now. See `docs/DECISIONS.md`.

- Free to run.
- Usable by someone non-technical. No terminal. Least friction — from the
  website.

## Public service requirements

- All three tools available publicly, not only the landscape.
- Every major disease including rare disease, and the biomarkers biomed and
  medtech are actually targeting.
- Zero retention, terms at the point of submission, and verifiability — the
  privacy claim must be checkable rather than promised.
- **It cannot fail in the public eye.**

## The standard for "good"

Not that it is right about everything.

That it presents the most likely trials, validations and outlook to whoever
needs to see it, so they can make an accurate decision on top of it and trust
what they are shown.

## Recurring failure to guard against

Treating the first example given as the whole category. Colorectal cancer was
one disease of seventy-four. A neonatal bilirubin monitor is one device of
many. Drugs are one half of the domain.

When a decision is being made from a single worked example, that is the signal
to widen before building.
