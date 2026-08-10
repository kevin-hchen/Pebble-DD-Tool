"""The self-contradicting-caveat lint.

WHY THIS EXISTS

Three times, a caveat written to DENY a claim contained the literal text of the
claim it was denying:

    "this is NOT a finding that the asset is unapproved"   contains  "is unapproved"
    "submitted but not approved (or still under review)"   contains  "not approved"
    "these counts are not rates, not incidences"           contains  "incidence"

Each was caught by hand, by a forbidden-phrase test firing on the tool's own
disclaimer, and each was fixed by rewording. Three is enough to stop relying on
catching the fourth by hand.

THE RULE THIS ENFORCES

A caveat is read two ways it was not written for. A downstream text match — the
forbidden-phrase checks this codebase already runs over rendered memos — sees
the claim, not the negation around it. And a human skimming a dense paragraph
sees the claim, not the negation around it. So a caveat that denies a claim must
not contain that claim's words at all; it has to be rewritten to say the same
thing in different words. That is a stronger constraint than "be careful", and
it is mechanically checkable, which is what this module makes it.

It is deliberately a LINT over the caveat CONSTANTS rather than a runtime check
over rendered text. The rendered text legitimately contains claim phrases in
other roles — `diligence._flag_approval_overreach` quotes the offending phrase
back at the reader on purpose, and a test's own message names what it is looking
for. What must be clean is the fixed text this tool asserts in its own voice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Claim phrases, by the domain whose caveats must not contain them. These are
#: the assertions the tool is at risk of appearing to make while trying to rule
#: them out — each group was derived from a real regression, not anticipated.
CLAIM_PHRASES: dict[str, tuple[str, ...]] = {
    "approval": (
        "is unapproved", "not approved", "unapproved", "never approved",
        "not been approved", "no fda approval", "lacks approval",
        "not fda approved", "not fda-approved", "denied approval",
    ),
    "clearance": (
        "not cleared", "no fda record", "not authorised", "not authorized",
        "not licensed",
    ),
    "epidemiology": (
        # A count with no denominator must not appear to be any of these.
        "incidence", "prevalence", "rate of", "risk of", "frequency of",
        "occurs in", "caused by",
    ),
    "safety": (
        "no adverse events", "no safety signal", "is safe", "well tolerated",
        "no side effects",
    ),
    "protection": (
        # Phase 3. "No patents" about an unapproved asset is a false statement
        # about the one thing a preclinical company is worth.
        "no patents", "no intellectual property", "no protection",
        "unprotected", "no exclusivity", "patent-free", "off patent",
        "generics enter", "generic entry on",
    ),
    "retention": (
        # The public service's zero-retention claims. Same trap, fifth instance:
        # a first draft of the no-model sentence read "Nothing you type is sent
        # to any third party", which CONTAINS "is sent to any third party" —
        # caught by a test rather than by inspection, exactly like the four
        # before it. A denial must not share a substring with the claim.
        "is sent to any third party", "sent to a third party", "is logged",
        "is stored", "is retained", "we keep your", "used for training",
        "read by staff", "written to disk",
    ),
}


@dataclass(frozen=True)
class Finding:
    """One caveat that contains the claim it is trying to deny."""
    where: str          # a name the author can find, e.g. "faers.WHAT_THIS_IS_NOT[0]"
    domain: str         # which CLAIM_PHRASES group matched
    phrase: str         # the claim phrase found
    context: str        # the surrounding text, so the fix is obvious

    def __str__(self) -> str:
        return (f"{self.where}: contains the {self.domain} claim “{self.phrase}” "
                f"inside a caveat that denies it — “…{self.context}…”. Reword to say "
                "the same thing without the claim's words; a substring match and a "
                "skimming reader both see the claim, not the negation.")


def _context(text: str, at: int, phrase: str, width: int = 44) -> str:
    lo = max(0, at - width)
    hi = min(len(text), at + len(phrase) + width)
    return re.sub(r"\s+", " ", text[lo:hi]).strip()


def audit_text(text: str, where: str, domains: tuple[str, ...] | None = None) -> list[Finding]:
    """Find claim phrases inside one caveat string.

    `domains` narrows which claim groups apply — an approval caveat is not at
    risk of accidentally asserting a safety rate. Passing None checks all of
    them, which is the right default for a general audit and produces the
    occasional irrelevant hit rather than missing a real one.
    """
    lowered = text.lower()
    groups = domains or tuple(CLAIM_PHRASES)
    findings: list[Finding] = []
    for domain in groups:
        for phrase in CLAIM_PHRASES.get(domain, ()):
            at = lowered.find(phrase)
            if at >= 0:
                findings.append(Finding(where=where, domain=domain, phrase=phrase,
                                        context=_context(text, at, phrase)))
    return findings


def audit(caveats, where: str, domains: tuple[str, ...] | None = None) -> list[Finding]:
    """Audit a caveat constant: a string, or any iterable of strings.

    Indexes into the name so a finding points at the exact element rather than
    at a tuple the author then has to search.
    """
    if isinstance(caveats, str):
        return audit_text(caveats, where, domains)
    out: list[Finding] = []
    for i, item in enumerate(caveats or ()):
        if isinstance(item, str):
            out.extend(audit_text(item, f"{where}[{i}]", domains))
    return out


def report(findings: list[Finding]) -> str:
    if not findings:
        return "no self-contradicting caveats"
    return "\n".join(str(f) for f in findings)


__all__ = ["CLAIM_PHRASES", "Finding", "audit", "audit_text", "report"]
