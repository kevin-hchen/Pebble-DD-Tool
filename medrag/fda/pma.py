"""openFDA device/pma — premarket APPROVAL, which is not 510(k) clearance.

    bulk: https://download.open.fda.gov/device/pma/device-pma-0001-of-0001.json.zip

THE DISTINCTION THIS MODULE EXISTS TO PROTECT
=============================================

A 510(k) is a CLEARANCE by substantial equivalence to a predicate device. A PMA
is an APPROVAL supported by clinical evidence of safety and effectiveness. They
are different regulatory facts about different risk classes, achieved by
different evidence, and a memo that renders them into one "cleared or approved"
field tells a reader that a Class II thermometer and a Class III implantable
defibrillator stand in the same relation to the FDA. `RegulatoryPathway` keeps
them apart everywhere, and nothing in this package produces a value that spans
both.

MEASURED FIELD REALITIES (2026-08-06), NOT ASSUMED
==================================================

Size: 56,853 records, 1,743 distinct `pma_number`, of which **1,473 are original
applications** and 55,380 are supplements — 97% of the file is supplements. 270
`pma_number`s appear ONLY as supplements, their original absent from the export.

**Supplements are separate records here.** This is the opposite of drugsFDA,
where submissions nest inside the application, and it means the key is
`(pma_number, supplement_number)`. Originals are cleanly separable:
`supplement_type` is empty on all 1,473 of them and populated on every
supplement.

**There is no `device_name`.** The 510(k) endpoint has one; this one has
`trade_name` and `generic_name`. A matcher that assumes symmetry with the
clearance path silently finds nothing — which is precisely how six of eighteen
real device types were invisible to this tool.

**A PMA is not automatically Class III.** Measured across the whole export:
class 3 = 48,473, but class 2 = 7,177, plus 797 with no class, 236 "U", 162 "f"
and 8 class 1. Collapsing "has a PMA" into "is Class III" would be wrong on 14%
of records, so `device_class` is carried through verbatim and never inferred
from the pathway.

**`product_code` is present on 56,085 of 56,853 (98.6%)** and is the join key to
the existing clearances table — the same key the device store already uses.
Note the API's `_exists_:product_code` reports all 56,853, because it counts
empty strings; the bulk export is what gives the honest figure.

**Decision codes are read from config, never from the letters.** See
`config/fda_decision_codes.yaml`: every meaning is verbatim FDA text, and the
49% of records carrying the undocumented `OK30` are rendered as undocumented
rather than guessed at.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CODES_CONFIG = (
    Path(__file__).resolve().parents[2] / "config" / "fda_decision_codes.yaml")

# ---------------------------------------------------------------- pathways

PATHWAY_510K = "510(k) clearance"
PATHWAY_DE_NOVO = "De Novo authorisation"
PATHWAY_PMA = "Premarket approval (PMA)"

#: What a PMA record's decision code lets us say. Deliberately four values, not
#: a boolean, for the same reason drugsFDA's submission status is:
#: "has a record" is not "was approved".
PMA_APPROVED = "APPROVED"
PMA_APPROVED_THEN_CHANGED = "APPROVED, THEN WITHDRAWN OR RECLASSIFIED"
PMA_DECISION_UNDOCUMENTED = "DECISION CODE NOT DOCUMENTED BY THE FDA"
PMA_DECISION_UNKNOWN = "DECISION NOT STATED"

#: The four things absence from device/pma can mean. Same construction as
#: `drugs.ABSENCE_MEANINGS`, worded so no substring reads as a claim about the
#: device when quoted out of context.
PMA_ABSENCE_MEANINGS = (
    "never submitted for premarket approval",
    "submitted and still under review, or refused",
    "listed under a trade or generic name this search did not match",
    "authorised by a different route — 510(k) clearance, De Novo, or an "
    "exemption — which is a different regulatory fact, not a lesser one",
)


@dataclass(frozen=True)
class DecisionCode:
    code: str
    meaning: str          # verbatim FDA text, or "" when the FDA does not define it
    documented: bool
    observed: int
    implies_approval: bool = False
    de_novo: bool = False

    def describe(self) -> str:
        """How this code is allowed to appear in prose."""
        if self.meaning:
            return f"{self.code} — {self.meaning}"
        return (f"{self.code} — the FDA data dictionary does not define this code, "
                "so its meaning is not stated here")


def load_decision_codes(path: str | Path | None = None) -> tuple[dict, dict]:
    """(pma_codes, clearance_codes) from config. A missing file yields empty
    tables: every code then reads as undocumented, which is degraded but never
    wrong — the failure posture `load_markers` and `load_agents` take."""
    p = Path(path or DEFAULT_CODES_CONFIG)
    if not p.exists():
        return {}, {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    def table(key, **extra):
        out = {}
        for code, spec in (data.get(key) or {}).items():
            spec = spec or {}
            out[str(code).upper()] = DecisionCode(
                code=str(code).upper(),
                meaning=str(spec.get("meaning", "") or ""),
                documented=bool(spec.get("documented", False)),
                observed=int(spec.get("observed", 0) or 0),
                implies_approval=bool(spec.get("implies_approval", False)),
                de_novo=bool(spec.get("de_novo", False)),
            )
        return out

    return table("pma_decision_codes"), table("clearance_decision_codes")


PMA_CODES, CLEARANCE_CODES = load_decision_codes()


def is_de_novo(decision_code: str) -> bool:
    """Whether a 510(k) record is actually a De Novo authorisation.

    A De Novo is granted BECAUSE no predicate exists, so describing one as
    substantially equivalent to a predicate is a false statement about a
    company's regulatory history. 482 records in the live data are De Novo.
    """
    entry = CLEARANCE_CODES.get((decision_code or "").upper())
    return bool(entry and entry.de_novo)


def clearance_pathway(decision_code: str) -> str:
    return PATHWAY_DE_NOVO if is_de_novo(decision_code) else PATHWAY_510K


# ---------------------------------------------------------------- records


@dataclass
class PMARecord:
    """One device/pma row: an original application OR one supplement to one.

    The key is (pma_number, supplement_number) because supplements are separate
    records in this source. `is_original` is decided by `supplement_type` being
    empty, which separates all 1,473 originals cleanly.
    """
    pma_number: str
    supplement_number: str = ""
    supplement_type: str = ""
    supplement_reason: str = ""
    product_code: str = ""
    decision_code: str = ""
    decision_date: str = ""
    date_received: str = ""
    trade_name: str = ""
    generic_name: str = ""
    applicant: str = ""
    advisory_committee: str = ""
    advisory_committee_description: str = ""
    ao_statement: str = ""
    expedited_review_flag: str = ""
    device_class: str = ""          # verbatim from openfda; NEVER inferred as "3"
    device_name: str = ""           # openfda.device_name, when present
    regulation_number: str = ""
    medical_specialty: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.pma_number, self.supplement_number)

    @property
    def is_original(self) -> bool:
        """An original application has an empty `supplement_number`.

        NOT `supplement_type`, which was the first answer and is wrong. Both
        fields are empty on all 1,473 originals, so `supplement_type` looks like
        it separates them — but it is ALSO empty on 1,885 genuine supplements,
        older records (N16993 S007, N18078 S020) that predate the field being
        populated. Using it would have counted 3,358 originals where there are
        1,473, inflating the approval count by 128%. Caught by cross-checking
        the two discriminators against the full export rather than trusting the
        one that looked right on a sample.
        """
        return not (self.supplement_number or "").strip()

    @property
    def is_expedited(self) -> bool:
        return (self.expedited_review_flag or "").upper() == "Y"

    @property
    def decision(self) -> DecisionCode | None:
        return PMA_CODES.get((self.decision_code or "").upper())

    @property
    def approval_state(self) -> str:
        """What this record's decision code supports saying — four states, not a
        boolean. An undocumented code yields its own state rather than being
        read as approval or as denial."""
        entry = self.decision
        if entry is None:
            return PMA_DECISION_UNKNOWN if self.decision_code else PMA_DECISION_UNKNOWN
        if not entry.documented:
            return PMA_DECISION_UNDOCUMENTED
        if entry.code == "APPR":
            return PMA_APPROVED
        if entry.implies_approval:
            return PMA_APPROVED_THEN_CHANGED
        return PMA_DECISION_UNKNOWN

    @property
    def match_names(self) -> list[str]:
        """Every name this record can be found under. There is NO device_name on
        this endpoint — trade_name and generic_name are the equivalents, and
        assuming symmetry with the 510(k) path is what made six of eighteen real
        device types invisible."""
        return [n for n in (self.trade_name, self.generic_name, self.device_name) if n]

    @property
    def display_number(self) -> str:
        """"P910007 S020", the way the FDA prints a supplement."""
        return (f"{self.pma_number} {self.supplement_number}".strip()
                if self.supplement_number else self.pma_number)

    @property
    def url(self) -> str:
        return ("https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpma/pma.cfm"
                f"?id={self.pma_number}") if self.pma_number else ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PMARecord":
        return cls(**d)


def parse_pma(rec: dict[str, Any]) -> PMARecord | None:
    num = (rec.get("pma_number") or "").strip()
    if not num:
        return None
    of = rec.get("openfda") or {}
    return PMARecord(
        pma_number=num,
        supplement_number=(rec.get("supplement_number") or "").strip(),
        supplement_type=(rec.get("supplement_type") or "").strip(),
        supplement_reason=(rec.get("supplement_reason") or "").strip(),
        product_code=(rec.get("product_code") or "").strip().upper(),
        decision_code=(rec.get("decision_code") or "").strip().upper(),
        decision_date=rec.get("decision_date", "") or "",
        date_received=rec.get("date_received", "") or "",
        trade_name=(rec.get("trade_name") or "").strip(),
        generic_name=(rec.get("generic_name") or "").strip(),
        applicant=(rec.get("applicant") or "").strip(),
        advisory_committee=rec.get("advisory_committee", "") or "",
        advisory_committee_description=rec.get("advisory_committee_description", "") or "",
        ao_statement=(rec.get("ao_statement") or "").strip(),
        expedited_review_flag=rec.get("expedited_review_flag", "") or "",
        # Carried verbatim. A PMA is NOT automatically Class III: 7,177 records
        # are class 2 and 1,203 carry no usable class at all.
        device_class=str(of.get("device_class", "") or ""),
        device_name=of.get("device_name", "") or "",
        regulation_number=of.get("regulation_number", "") or "",
        medical_specialty=of.get("medical_specialty_description", "") or "",
    )


@dataclass
class PMAApplication:
    """One `pma_number` with its supplements folded in — the unit a memo cites.

    Assembled locally rather than fetched, because the source publishes the
    original and its supplements as separate rows and a reader thinks in
    applications.
    """
    pma_number: str
    original: PMARecord | None = None
    supplements: list[PMARecord] = field(default_factory=list)

    @property
    def has_original_record(self) -> bool:
        """False for the 270 pma_numbers whose original is absent from the
        export. Their supplements prove an approval existed, but this copy of
        the source does not contain the approval record itself, and that gap is
        stated rather than papered over."""
        return self.original is not None

    @property
    def representative(self) -> PMARecord | None:
        return self.original or (self.supplements[0] if self.supplements else None)

    @property
    def approval_state(self) -> str:
        if self.original is not None:
            return self.original.approval_state
        return PMA_DECISION_UNKNOWN

    @property
    def approval_date(self) -> str:
        return self.original.decision_date if self.original else ""

    @property
    def device_class(self) -> str:
        rep = self.representative
        return rep.device_class if rep else ""

    @property
    def trade_name(self) -> str:
        rep = self.representative
        return rep.trade_name if rep else ""

    @property
    def applicant(self) -> str:
        rep = self.representative
        return rep.applicant if rep else ""

    @property
    def product_code(self) -> str:
        rep = self.representative
        return rep.product_code if rep else ""

    @property
    def latest_supplement_date(self) -> str:
        return max((s.decision_date for s in self.supplements if s.decision_date),
                   default="")

    @property
    def url(self) -> str:
        rep = self.representative
        return rep.url if rep else ""


def group_applications(records: list[PMARecord]) -> list[PMAApplication]:
    """Fold rows into applications, original first."""
    by_number: dict[str, PMAApplication] = {}
    for r in records:
        app = by_number.setdefault(r.pma_number, PMAApplication(pma_number=r.pma_number))
        if r.is_original and app.original is None:
            app.original = r
        else:
            app.supplements.append(r)
    for app in by_number.values():
        app.supplements.sort(key=lambda s: s.decision_date or "", reverse=True)
    return list(by_number.values())


__all__ = [
    "PATHWAY_510K", "PATHWAY_DE_NOVO", "PATHWAY_PMA",
    "PMA_APPROVED", "PMA_APPROVED_THEN_CHANGED", "PMA_DECISION_UNDOCUMENTED",
    "PMA_DECISION_UNKNOWN", "PMA_ABSENCE_MEANINGS",
    "DecisionCode", "PMARecord", "PMAApplication",
    "PMA_CODES", "CLEARANCE_CODES", "load_decision_codes",
    "is_de_novo", "clearance_pathway", "parse_pma", "group_applications",
]
