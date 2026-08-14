"""Evidence grading for DIAGNOSTIC and PROGNOSTIC studies.

`evidence_grade.py` grades therapeutic evidence: meta-analysis, systematic
review, RCT, cohort, case-control, case series. That hierarchy answers "does
this treatment work", and it is untouched by this module.

A diagnostic accuracy study answers a different question — "does this test
identify the condition" — and the designs rank differently for it. Graded on the
therapeutic scale, a prospective validation study lands at tier 4 of 8 because
PubMed types it `Validation Study` and the map sends that to `cohort`, and a
diagnostic case-control lands at tier 5, adjacent to case reports. Measured on
the audit corpus, 44 of 86 diagnostic-accuracy studies came back `Unclassified`
and the rest clustered at tier 4.

**The fix is not to re-rank the therapeutic map or to add the missing labels to
it.** The two hierarchies answer different questions and a single ordering
cannot serve both. This module sits BESIDE it, is selected by what the study is,
and never overwrites what the therapeutic map says.

## The nuance that makes this an honest hierarchy rather than merely a different one

A two-gate diagnostic case-control — cases recruited from one place, controls
from another — genuinely overestimates accuracy. Lijmer et al. (JAMA 1999;
282:1061) measured it: two-gate designs inflate diagnostic odds ratios roughly
threefold. Ranking it below a consecutive series is correct and it stays low
here.

What was wrong was not that it ranked low. It was that it ranked low **on a
scale built for a different question**, at tier 5 of 8 next to case reports,
when for early diagnostic validation it is a recognised and expected design. On
this scale it sits at tier 4 of 6, below the designs it is genuinely weaker than
and above nothing it is stronger than. Same direction, honest reason.

## Sources — cited rather than invented

  * **Oxford CEBM Levels of Evidence for Diagnosis** (2011; and the 2009
    diagnosis table) supplies the TIER ORDERING. It is the only widely used
    hierarchy built for diagnostic accuracy rather than adapted from therapy.
  * **QUADAS-2** (Whiting et al., Ann Intern Med 2011;155:529) supplies the
    DESIGN FACTS that decide the tier. Of its four domains, the two that
    separate designs rather than grade their conduct are patient selection
    (consecutive/random vs two-gate) and flow & timing (was the reference
    standard applied to everyone).
  * **STARD 2015** (Bossuyt et al., BMJ 2015;351:h5527) supplies
    stated-versus-absent. Its reporting items are what a well-reported abstract
    contains, so their absence is the signal that a design was not stated —
    which is `CANNOT_GRADE`, not a low tier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------- the hierarchy
#
# Ordered strongest to weakest. Rank 1 is strongest. The two states at the end
# are NOT tiers and carry no rank that can be compared — see `DiagnosticGrade`.

TIERS = [
    ("sr-meta-dta", 1, "Systematic review / meta-analysis of accuracy studies"),
    ("consecutive-cohort", 2, "Consecutive or random series, complete verification"),
    ("diagnostic-rct", 3, "Randomised comparison of test-and-treat strategies"),
    ("nonconsecutive-cohort", 4, "Selected series, or incomplete verification"),
    ("case-control-two-gate", 5, "Two-gate case-control (cases and controls separately)"),
    ("prognostic", 6, "Prognostic marker or prediction model"),
]

#: Not a tier. The study is diagnostic and the record does not state a design
#: that can be placed. A study whose design nobody wrote down is not a weak
#: study, and giving it a rank would assert something the record does not say —
#: the same rule as `ValidationReport.assessed` and `NOT_MENTIONED`.
CANNOT_GRADE = "cannot-grade"

#: Not a tier either. The study is not a diagnostic or prognostic study at all,
#: so this hierarchy does not apply to it and the therapeutic one does.
NOT_DIAGNOSTIC = "not-diagnostic"

#: The ROUTING decision, which is a different question from the tier and needs
#: its own third state for the same reason the tier has `CANNOT_GRADE`.
#:
#: Routing was binary, and a binary router has to send every record somewhere.
#: A primary study about a device that never uses accuracy vocabulary —
#: "Association Between Lung Ultrasound Patterns and Pneumonia" — then lands on
#: the therapeutic map and is given a confident tier on the wrong scale, which
#: is the precise harm this module exists to remove. Eight of the development
#: set do that.
#:
#: `CANNOT_TELL` declines instead. A declined study gets no tier from either
#: hierarchy and is reported as unrouted. That is worse for coverage and better
#: for the reader, which is the trade this codebase makes everywhere else:
#: NOT_MENTIONED, NOT_ASSESSED, UNVERIFIED, and CANNOT_GRADE one layer down.
ROUTE_DIAGNOSTIC = "diagnostic"
ROUTE_THERAPEUTIC = "therapeutic"
ROUTE_CANNOT_TELL = "cannot-tell"

_RANK = {key: rank for key, rank, _ in TIERS}
_LABEL = {key: label for key, _, label in TIERS}
_LABEL[CANNOT_GRADE] = "Diagnostic study, design not stated"
_LABEL[NOT_DIAGNOSTIC] = "Not a diagnostic or prognostic study"

#: The tiers whose ordering is load-bearing, strongest first. A grader must
#: never place a later one above an earlier one. `DIAGNOSTIC_RCT` is
#: deliberately absent: it answers a different question (does testing change
#: patient outcomes) and comparing it to an accuracy tier is a category error.
#: `PROGNOSTIC` is absent for the same reason.
ORDERING_INVARIANT = (
    "consecutive-cohort", "nonconsecutive-cohort", "case-control-two-gate",
)


@dataclass(frozen=True)
class DiagnosticGrade:
    key: str
    label: str
    #: `None` for `CANNOT_GRADE` and `NOT_DIAGNOSTIC`. Deliberately not a large
    #: sentinel number: a rank that sorts is a rank that will be compared, and
    #: neither of those states is a position on the scale.
    rank: int | None
    #: Which fact decided it, for the reader and for a disagreement to be traced
    #: back to a sentence rather than to a verdict.
    basis: str = ""

    @property
    def is_tier(self) -> bool:
        return self.rank is not None

    def tag(self) -> str:
        return self.label


def _grade(key: str, basis: str) -> DiagnosticGrade:
    return DiagnosticGrade(key=key, label=_LABEL[key], rank=_RANK.get(key), basis=basis)


# ------------------------------------------------------------- the selection rule
#
# Stated explicitly and tested on its own, because it is over half the problem:
# routing a diagnostic study to the therapeutic scale is exactly what sends a
# validation study to tier 4 of 8, and no amount of tier accuracy recovers from
# being on the wrong scale.
#
# It answers one question — is this study's OBJECT a test, a marker or a
# prediction? — and it answers it from the abstract, never from the topic. A
# narrative review about MRI is about a test and is not a study of one.

#: Phrases that mean the study measured how well something identifies a state.
#: Deliberately narrow: "sensitivity" alone is not here, because "contrast
#: sensitivity", "corneal sensitivity" and "baroreflex sensitivity" are
#: physiological measures, a false-positive pattern already measured at 6 of 17
#: on this codebase's own outcome-field probe.
_ACCURACY = re.compile(
    r"\b(diagnostic (accuracy|performance|yield|value)|"
    # Plain "accuracy"/"performance" ATTACHED to a test or device noun. The
    # attachment is what keeps this from matching "the accuracy of the estimate";
    # "Accuracy of pulse oximetry in children" is the shape it has to catch, and
    # requiring the phrase "diagnostic accuracy" missed every one of them.
    r"(accuracy|precision|performance|reliability|validity) (of|and bias of|"
    r"between|comparison) .{0,60}?"
    r"(test|assay|device|monitor|monitoring|oximet\w+|sensor|camera|imaging|scan|"
    r"ultrasound|MRI|CT\b|ECG|EEG|algorithm|software|model|score|index|biomarker|"
    r"marker|system|meter|analy[sz]er|screen\w*|measurement|method)|"
    r"(test|assay|device|monitor|sensor|algorithm|system|method)\w* .{0,30}"
    r"(accuracy|performance) (was|were|is|of|assessed|evaluated|compared)|"
    r"point accuracy|analytical performance|measurement (accuracy|agreement|error)|"
    r"sensitivit(y|ies) and specificit|specificity and sensitivit|"
    r"positive predictive value|negative predictive value|\bPPV\b|\bNPV\b|"
    r"area under the (receiver|ROC|curve)|\bAUROC\b|"
    r"receiver operating characteristic|"
    r"reference standard|gold standard|"
    r"(index|screening) test|test accuracy|"
    r"false[- ](positive|negative) rate|detection rate|"
    r"agreement (between|with) .{0,40}(measurement|method|reference|standard)|"
    r"Bland[- ]Altman|limits of agreement|"
    r"validation of (a|an|the) .{0,40}(test|assay|device|score|algorithm|model)|"
    r"clinical validation|"
    # QUADAS-2 INDEX TEST domain: interpretation blinded to the reference
    # standard is a hallmark of an accuracy study and appears in abstracts that
    # never use the word "accuracy". "Two radiologists, blinded to laboratory
    # results, performed measurements independently" is the shape.
    r"blinded to .{0,40}(result|reference|standard|diagnosis|outcome|clinical)|"
    r"(readers?|radiologists?|cardiologists?|observers?|assessors?|"
    r"pathologists?|clinicians?) .{0,30}(independently|blinded|masked)|"
    r"(independently|blinded|masked) (assessed|classified|reviewed|interpreted|read)|"
    # A study of whether a test finds the condition. "Detection of X by Y",
    # "Y detected by Z" — the endpoint is identification, which is the question.
    r"detect(ed|ion) (of )?.{0,40}\bby (the )?.{0,40}"
    r"(test|assay|device|scan|imaging|ultrasound|MRI|CT\b|ECG|algorithm|biopsy)|"
    r"(detect|identif\w+) .{0,40}(compared (with|to)|versus|vs\.?) |"
    # Rule-in / rule-out management studies: the endpoint is whether the
    # strategy safely excludes disease, which is a diagnostic question.
    r"(ruled out|rule[- ]out|excluded) without further (testing|imaging|investigation)|"
    r"(safely )?(exclude|rule out) .{0,30}(pulmonary embolism|deep vein|disease|"
    r"cancer|infection|the condition))", re.I)

#: Prognostic: the endpoint is a FUTURE state rather than a present one.
_PROGNOSTIC = re.compile(
    r"\b(prognostic (value|accuracy|marker|model|index)|"
    # "X predicts all-cause mortality" — the bare verb with an outcome after it.
    r"predicts? .{0,40}(mortality|survival|outcome|risk|event|progression|recurrence)|"
    r"(forecast|prediction) (of|model)|"
    r"predict(s|ion|ive) (of|model|score|rule)|"
    r"risk (score|model|prediction)|"
    r"discrimination and calibration|c[- ]statistic|"
    r"(develop|derive|validat)\w* .{0,30}(prediction|prognostic) model)", re.I)

#: A randomised trial of a SCREENING or TEST-DIRECTED strategy. Its endpoint is
#: mortality or detection under a policy, so none of the accuracy vocabulary
#: above appears — measured, this missed all 5 such trials in the development
#: set. The question is still diagnostic, which is why CEBM gives it its own
#: level rather than filing it under therapy.
_STRATEGY_TRIAL = re.compile(
    r"\b((invitation|invited) to (screening|colonoscopy|be screened)|"
    r"screening (trial|programme|program|strategy|interventions?)|"
    r"screen\w* (with|using) .{0,40}(versus|vs\.?|compared with)|"
    r"(versus|vs\.?) .{0,30}(screening|cytology|colonoscopy|mammograph\w+)|"
    r"biomarker[- ]guided|test[- ]and[- ]treat|"
    r"guided (antibiotic|treatment|therapy) (duration|decisions?|strategy)|"
    r"supplemental (screening|MRI)|"
    r"randomi[sz]ed .{0,40}(screening|diagnostic|testing))", re.I)

#: The study is ABOUT a test but is not a study of one. Checked first, because
#: the accuracy vocabulary appears freely in reviews of accuracy studies.
_NOT_A_STUDY = re.compile(
    r"\b(this (review|article|chapter|paper) (review|discuss|describ|present|outlin|"
    r"provid|explor|summari)|"
    r"purpose of (this )?review|"
    r"we review the|narrative review|"
    r"is an overview|an overview of|"
    r"this (editorial|commentary|viewpoint)|"
    r"clinical practice guideline|the task force|"
    r"recommendations (were|are) (developed|graded|based)|"
    r"we (report|describe|present) (a|two|three|\d+) case)", re.I)

#: PubMed types that settle it on their own, in either direction.
_TYPE_NOT_A_STUDY = {"review", "editorial", "comment", "news", "practice guideline",
                     "guideline", "case reports", "letter", "historical article",
                     "published erratum", "biography", "interview"}
_TYPE_SYNTHESIS = {"meta-analysis", "systematic review"}

#: A test, device or marker named in the TITLE — i.e. the study's subject is a
#: test rather than a treatment. Checked on the title alone: an abstract
#: mentions a scanner in passing, a title states what the paper is about.
_TEST_IN_TITLE = re.compile(
    r"\b(test|assay|screening|screen\b|diagnos\w+|detect\w+|imaging|scan\b|"
    r"ultrasound|ultrasonograph\w+|sonograph\w+|MRI|magnetic resonance|CT\b|"
    r"tomograph\w+|radiograph\w+|mammograph\w+|angiograph\w+|scintigraph\w+|"
    r"ECG|EKG|electrocardiogra\w+|EEG|oximet\w+|monitor\w*|sensor|wearable|"
    r"biomarker|marker\b|troponin|d-dimer|procalcitonin|calprotectin|"
    r"algorithm|artificial intelligence|machine learning|"
    r"polysomnograph\w+|capnograph\w+|glucose monitoring|device)\b", re.I)

#: Evidence that subjects were studied, rather than discussed.
_HAS_SAMPLE = re.compile(
    r"(\b\d[\d,]{1,6}\s+(patients|participants|subjects|men|women|children|"
    r"individuals|adults|cases|samples|volunteers|eligible)|"
    r"\b(patients|participants|subjects) (were|was|underwent|received|enrolled|"
    r"recruited|included|invited|randomi[sz]ed)|"
    r"\b(we|this study|the study|this analysis) (enrolled|recruited|included|"
    r"studied|analy[sz]ed|assessed|evaluated|performed|conducted|compared|"
    r"measured|examined|identified|followed)|"
    r"\b(this|a) (prospective|retrospective|cross[- ]sectional|observational|"
    r"multicent\w+|pilot|cohort|randomi[sz]ed)\b.{0,30}(study|trial|analysis)|"
    r"^\s*(Methods|Design|Materials And Methods|Patients|Setting)\s*:)",
    re.I | re.M)


def route(title: str, abstract: str,
          publication_types: list[str] | None = None) -> tuple[str, str]:
    """THE SELECTION RULE. Returns (route, why) — three outcomes, not two.

      ROUTE_DIAGNOSTIC   grade on this hierarchy
      ROUTE_THERAPEUTIC  grade on `evidence_grade`; this module does not apply
      ROUTE_CANNOT_TELL  a primary study whose SUBJECT is a test but which
                         states no accuracy, prognostic or strategy question.
                         Declined by both hierarchies rather than assigned to
                         one of them.

    The third outcome is not a hedge. Routing a diagnostic study to the
    therapeutic map is what gives a validation study tier 4 of 8, and a router
    with only two outputs must do that to every study it cannot read. Declining
    costs coverage, which is reported; misrouting costs the reader, which is
    not recoverable from the output.
    """
    types = {t.strip().lower() for t in (publication_types or [])}
    text = f"{title}\n{abstract}"

    diagnostic_signal = bool(_ACCURACY.search(text))
    prognostic_signal = bool(_PROGNOSTIC.search(text))
    strategy_signal = bool(_STRATEGY_TRIAL.search(text)) and bool(
        _RANDOMISED_STRATEGY.search(text))
    if not (diagnostic_signal or prognostic_signal or strategy_signal):
        # No question-defining vocabulary. Two very different records land here:
        # a therapy trial or a narrative review (correctly therapeutic), and a
        # primary study whose subject IS a test but whose abstract never says
        # what it measured. Only the second is undecidable.
        if _HAS_SAMPLE.search(text) and _TEST_IN_TITLE.search(title) \
                and not (types & _TYPE_NOT_A_STUDY) and not _NOT_A_STUDY.search(text):
            return ROUTE_CANNOT_TELL, (
                "a primary study whose subject is a test or device, stating no "
                "accuracy, prognostic or strategy question — which hierarchy applies "
                "cannot be read from the record")
        return ROUTE_THERAPEUTIC, "no accuracy, prognostic or screening-strategy vocabulary"

    # A synthesis OF accuracy studies is a diagnostic study; a narrative review
    # that merely discusses them is not. The publication type separates them,
    # which is why it is checked before the prose.
    if types & _TYPE_SYNTHESIS:
        return ROUTE_DIAGNOSTIC, "systematic review or meta-analysis carrying accuracy vocabulary"

    if types & _TYPE_NOT_A_STUDY:
        return ROUTE_THERAPEUTIC, f"publication type is {sorted(types & _TYPE_NOT_A_STUDY)[0]}"
    if _NOT_A_STUDY.search(text):
        return ROUTE_THERAPEUTIC, "the abstract describes a review, guideline or case report"

    # A STUDY has subjects. A narrative review that happens to use the words
    # "reference standard" does not, and that is what let six reviews through on
    # the first pass. Requiring a countable sample or an explicit methods
    # section is the difference between a study of a test and prose about one.
    if not _HAS_SAMPLE.search(text):
        return ROUTE_THERAPEUTIC, \
            "accuracy vocabulary but no sample, cohort or methods section stated"

    if strategy_signal and not diagnostic_signal:
        return ROUTE_DIAGNOSTIC, "randomised comparison of screening or testing strategies"
    return ROUTE_DIAGNOSTIC, ("prognostic vocabulary in the abstract" if prognostic_signal
                              and not diagnostic_signal
                              else "accuracy vocabulary in the abstract")


def is_diagnostic_study(title: str, abstract: str,
                        publication_types: list[str] | None = None) -> tuple[bool, str]:
    """Backwards-compatible two-way view of `route`.

    `CANNOT_TELL` reports False here — it is not a diagnostic study as far as
    this hierarchy is concerned — but a caller that needs to tell a decline from
    a therapeutic routing must call `route` directly. Kept so the boolean
    reading is available without making the three-way one optional.
    """
    decision, why = route(title, abstract, publication_types)
    return decision == ROUTE_DIAGNOSTIC, why


# ------------------------------------------------------------------- the tiering

_SAMPLING_TWO_GATE = re.compile(
    r"\b(case[- ]control|"
    r"(healthy|asymptomatic|normal|non[- ]?\w+) (volunteer|control|subject)s?\b|"
    r"control group of|"
    r"\d+\s+controls?\b|"
    r"controls? (with|were|from) |"
    r"(cases|patients) (and|versus|vs\.?) (healthy |matched |non[- ]\w+ )?controls|"
    r"matched controls?|"
    # Selection BY confirmed status, which is two-gate however the groups are
    # described. "This study evaluates D-dimer in PE-positive and PE-negative
    # adolescents" recruits on the target condition and has no control group by
    # that name.
    r"\b\w+[- ](positive|negative) (and|versus|vs\.?) \w+[- ](negative|positive)\b|"
    r"\b(patients|subjects|participants) with (confirmed|known|established) .{0,40}"
    r"(and|versus|vs\.?) .{0,30}(without|healthy|normal|control))", re.I)

_SAMPLING_CONSECUTIVE = re.compile(
    r"\b(consecutive(ly)? (patients|subjects|participants|cases|series|enrolled|recruited)|"
    # The subject noun is REQUIRED. Without it "11 consecutive" matched a run of
    # days in the Fitbit study and promoted a self-enrolled cohort to a
    # consecutive series — an ordering inversion caused by a duration.
    r"\d+\s+consecutive\s+(patients|subjects|participants|cases|men|women|"
    r"children|adults|individuals|samples)\b|"
    r"unselected (patients|cohort|series)|"
    r"all (eligible |consecutive )?(patients|participants) (who|were|attending|referred))",
    re.I)

_TIMING_PROSPECTIVE = re.compile(r"\bprospectiv\w+", re.I)
_TIMING_RETROSPECTIVE = re.compile(r"\bretrospectiv\w+", re.I)

_INCOMPLETE_VERIFICATION = re.compile(
    r"\b(only .{0,40}(underwent|received|were) (the )?(biops|reference|confirmatory)|"
    r"(patients|those|participants|men|women) with .{0,40}(positive|suspicious|"
    r"PI[- ]RADS|abnormal|score) .{0,30}(underwent|were|had) |"
    r"partial verification|differential verification|"
    r"if .{0,40}(identified|flagged|positive).{0,40}(patch|reference|confirmator)|"
    r"was (mailed|sent) to (the )?participant|"
    # QUADAS-2 FLOW AND TIMING, in the passive voice the wearable studies use:
    # "Eligible participants with an irregular heart rhythm detection ... were
    # mailed a 1-week ambulatory ECG patch monitor." Only those the index test
    # flagged reach the reference standard, which is partial verification
    # however it is phrased.
    r"(participants|patients|subjects|those|individuals) with (an? |a )?[^.]{0,60}"
    r"(detection|notification|alert|result|finding|flag)[^.]{0,40}"
    r"(were|was) (mailed|sent|invited|referred|scheduled|offered|asked)|"
    r"(were|was) (mailed|sent|given|fitted with) (a |an )?[^.]{0,40}"
    r"(patch|monitor|ECG|Holter|reference standard|confirmatory))", re.I)

_RANDOMISED_STRATEGY = re.compile(
    r"\b(random(ly|ised|ized)? (assigned|allocated|invited)|"
    r"randomi[sz]ed (controlled |clinical |health[- ]care policy |screening )?trial|"
    r"non[- ]inferiority trial|parallel[- ]group)", re.I)


def grade_diagnostic(title: str, abstract: str,
                     publication_types: list[str] | None = None) -> DiagnosticGrade:
    """Place a study on the diagnostic hierarchy, or say it cannot be placed.

    Order of checks matters and follows CEBM: synthesis first, then whether the
    design is a randomised strategy comparison, then sampling (QUADAS-2 patient
    selection), then verification (QUADAS-2 flow and timing).
    """
    decision, why = route(title, abstract, publication_types)
    if decision == ROUTE_CANNOT_TELL:
        # Declined by BOTH hierarchies. Not `NOT_DIAGNOSTIC`, which would send it
        # to the therapeutic map and hand it a confident tier on a scale that
        # may not apply.
        return _grade(CANNOT_GRADE, why)
    if decision != ROUTE_DIAGNOSTIC:
        return _grade(NOT_DIAGNOSTIC, why)

    types = {t.strip().lower() for t in (publication_types or [])}
    text = f"{title}\n{abstract}"

    if types & _TYPE_SYNTHESIS or re.search(
            r"\b(systematic review|meta[- ]analys)", text, re.I):
        return _grade("sr-meta-dta", "systematic review or meta-analysis of accuracy studies")

    # A prognostic study is on its own axis; checked before sampling because its
    # tier does not depend on how the sample was drawn.
    if _PROGNOSTIC.search(text) and not _ACCURACY.search(text):
        return _grade("prognostic", "endpoint is a future state, not a present one")

    # Two-gate BEFORE consecutive. "Consecutive 120 patients with AF and 60
    # controls" contains both words, and the sampling that decides the tier is
    # the second one: recruiting by known status is two-gate however the cases
    # were assembled. QUADAS-2 patient selection.
    if _SAMPLING_TWO_GATE.search(text):
        return _grade("case-control-two-gate",
                      "cases and controls assembled separately by known status")

    # A randomised comparison of strategies is not an accuracy study, and its
    # RCT publication type alone must not decide that — an accuracy analysis
    # nested inside a randomised trial carries the same type.
    if _RANDOMISED_STRATEGY.search(text) and not _ACCURACY.search(title):
        if re.search(r"\b(random(ly|ised|ized)? (assigned|allocated|invited))", text, re.I):
            return _grade("diagnostic-rct",
                          "participants randomly allocated between testing strategies")

    if _INCOMPLETE_VERIFICATION.search(text):
        return _grade("nonconsecutive-cohort",
                      "reference standard applied to only part of the sample")

    if _SAMPLING_CONSECUTIVE.search(text):
        return _grade("consecutive-cohort", "consecutive or unselected series")

    if _TIMING_RETROSPECTIVE.search(text):
        return _grade("nonconsecutive-cohort",
                      "retrospective record review, sampling not stated")
    if _TIMING_PROSPECTIVE.search(text):
        return _grade("consecutive-cohort",
                      "prospective enrolment in a defined population, sampling not stated")

    # Diagnostic, and the record states neither sampling nor timing. Not a low
    # tier — an ungraded one.
    return _grade(CANNOT_GRADE, "neither sampling nor prospective/retrospective stated")


def grade_document(doc) -> DiagnosticGrade:
    """Grade a corpus `Document`."""
    meta = getattr(doc, "meta", None) or {}
    return grade_diagnostic(getattr(doc, "title", "") or "",
                            getattr(doc, "text", "") or "",
                            meta.get("publication_types"))
