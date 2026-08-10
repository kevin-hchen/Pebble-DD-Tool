"""Orange Book — listed patents and exclusivity, and what they are not.

THE ABSENCE PROBLEM HERE IS THE WORST IN THE TOOL
=================================================

An investigational asset CANNOT appear in the Orange Book. Listing requires an
approved application, so a preclinical or Phase 2 asset is absent by
construction. If that renders as "no patents found", it reads as "this company
has no intellectual property" — a false statement about the single thing a
preclinical company is worth, produced by a lookup that was never applicable.

So `ProtectionAnswer.applicable` is False whenever the asset has no approved
small-molecule application, and the section renders NOT APPLICABLE with the
reason. It never renders an absence of protection. `phrasing.CLAIM_PHRASES`
carries a `protection` group specifically so the lint catches any caveat here
that drifts into saying "no patents".

WHAT THE ORANGE BOOK IS NOT — SAID IN THE SECTION, NOT ONLY IN CLAUDE.md
=======================================================================

  * These are the patents the SPONSOR CHOSE TO LIST against this application.
    An approved asset with two listed patents may hold forty. It is not a patent
    estate.
  * It is not freedom-to-operate. Nothing here says whether making or selling
    something infringes a third party's patent.
  * Listed dates ignore litigation, settlements and patent challenges. A listed
    expiry is what the sponsor filed, not an outcome.
  * Small molecules only. Biologics are licensed under a BLA and are absent by
    design — for a BLA asset this section is not evidence of anything, and the
    Purple Book is where that question goes.

THE RENDERED SENTENCE IS ABOUT PROTECTION LAPSING, NEVER ABOUT GENERIC ENTRY.
"Earliest listed protection lapses <date>" is supported by the data. "Generics
enter <date>" is a different claim that depends on litigation, settlements,
first-filer exclusivity and whether anyone bothers, none of which is here.

MEASURED SHAPE (openFDA Orange Book export, 2026-08-06)
=======================================================

48,502 records, one per (application, product_number); the bulk export and the
API agree exactly. Contrary to the assumption this phase started with, there IS
an `api.fda.gov/drug/orangebook.json` endpoint — an early probe returning HTTP
000 was a connection failure, not a 404 — but the bulk export is still the right
ingest for the same reason as PMA: it is 2.33 MB in one partition and states a
complete denominator.

Patents and exclusivity are sparse and that sparsity is the point:

    records with listed patents      2,634 of 48,502   (5.4%)
    records with exclusivity         1,192 of 48,502   (2.5%)
    application_type A (generic)    37,651
    application_type N (NDA)        10,851

So 24% of NDAs carry listed patents and the rest carry none — which for an
approved product usually means the listed patents have already expired and been
removed, NOT that none ever existed. That distinction is rendered too.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CODES_CONFIG = (
    Path(__file__).resolve().parents[2] / "config" / "fda_exclusivity_codes.yaml")

#: Why an Orange Book answer can be inapplicable rather than empty. These are
#: NOT absence meanings — they are reasons the question does not apply, which is
#: a different thing and renders differently.
NOT_APPLICABLE_NO_APPROVAL = (
    "listing requires an approved application, and no approved US application "
    "matching this asset was found — an investigational asset is absent from the "
    "Orange Book by construction, which says nothing about what the sponsor owns"
)
NOT_APPLICABLE_BIOLOGIC = (
    "this asset was found under a BLA, and biologics are licensed rather than "
    "approved under the pathway the Orange Book covers — they are absent from it "
    "by design, and their exclusivity is recorded in the Purple Book instead"
)

#: What the Orange Book is not, rendered in the section itself.
LIMITS = (
    "These are the patents the SPONSOR CHOSE TO LIST against this application. A "
    "sponsor may hold many more that are not listed here, so this is not a patent "
    "estate and a small number of listed patents is not a small portfolio.",
    "This is not freedom-to-operate. Nothing here says whether making or selling a "
    "product would infringe someone else's patent.",
    "Listed dates ignore litigation, settlements and patent challenges. A listed "
    "expiry is what the sponsor filed, not an outcome that has happened.",
    "Small molecules only. Biologics are licensed under a BLA and do not appear "
    "here at all.",
)

NOT_SEARCHED = (
    "patent litigation and settlements (Paragraph IV challenges, consent decrees)",
    "patents the sponsor did not list against this application",
    "non-US patents and non-US exclusivity",
)


@dataclass(frozen=True)
class ExclusivityKind:
    prefix: str
    kind: str          # orphan | paediatric
    meaning: str
    curated: bool


def load_exclusivity_codes(path: str | Path | None = None):
    """(prefix rules, observed-without-meaning counts) from config.

    Curated, not sourced — openFDA's field reference does not enumerate these
    codes. `curated` rides along so a renderer can say which it is.
    """
    p = Path(path or DEFAULT_CODES_CONFIG)
    if not p.exists():
        return {}, {}
    data = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("exclusivity_codes", {})
    prefixes = {
        str(k).upper(): ExclusivityKind(
            prefix=str(k).upper(), kind=str(v.get("kind", "")),
            meaning=str(v.get("meaning", "")), curated=bool(v.get("curated", True)))
        for k, v in (data.get("prefixes") or {}).items()
    }
    return prefixes, dict(data.get("observed_without_meaning") or {})


EXCLUSIVITY_PREFIXES, EXCLUSIVITY_OBSERVED = load_exclusivity_codes()


def classify_exclusivity(code: str) -> ExclusivityKind | None:
    """Orphan / paediatric / unknown, by curated prefix. Never guesses beyond
    the two the question turns on."""
    up = (code or "").strip().upper()
    for prefix, kind in EXCLUSIVITY_PREFIXES.items():
        if up.startswith(prefix):
            return kind
    return None


def _as_date(compact: str) -> date | None:
    try:
        return datetime.strptime((compact or "")[:8], "%Y%m%d").date()
    except (ValueError, TypeError):
        return None


def human_date(compact: str) -> str:
    d = _as_date(compact)
    return d.strftime("%-d %b %Y") if d else (compact or "not stated")


@dataclass
class ListedPatent:
    patent_number: str
    expiration_date: str = ""            # YYYYMMDD
    patent_use_code: str = ""
    patent_submission_date: str = ""
    drug_substance_flag: bool = False
    drug_product_flag: bool = False
    delist_requested: bool = False       # patent_delist_flag

    @property
    def expires(self) -> date | None:
        return _as_date(self.expiration_date)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ListedExclusivity:
    exclusivity_code: str
    exclusivity_expiration_date: str = ""

    @property
    def expires(self) -> date | None:
        return _as_date(self.exclusivity_expiration_date)

    @property
    def kind(self) -> ExclusivityKind | None:
        return classify_exclusivity(self.exclusivity_code)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OrangeBookEntry:
    """One (application, product) row with its listed protection."""
    application_number: str              # "N021880" — type letter + number
    product_number: str = ""
    brand_name: str = ""
    active_ingredients: list[str] = field(default_factory=list)
    application_type: str = ""           # N (NDA) | A (ANDA/generic)
    applicant: str = ""
    dosage_form: str = ""
    route: str = ""
    marketing_status: str = ""
    therapeutic_equivalence_codes: list[str] = field(default_factory=list)
    reference_listed_drug: bool = False
    approval_date: str = ""
    approved_prior_to_1982: bool = False
    patents: list[ListedPatent] = field(default_factory=list)
    exclusivity: list[ListedExclusivity] = field(default_factory=list)

    @property
    def is_generic(self) -> bool:
        return self.application_type.upper() == "A"

    @property
    def match_names(self) -> list[str]:
        return [n for n in ([self.brand_name] + list(self.active_ingredients)) if n]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["patents"] = [p.to_dict() for p in self.patents]
        d["exclusivity"] = [e.to_dict() for e in self.exclusivity]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "OrangeBookEntry":
        d = dict(d)
        d["patents"] = [ListedPatent(**p) for p in (d.get("patents") or [])]
        d["exclusivity"] = [ListedExclusivity(**e) for e in (d.get("exclusivity") or [])]
        return cls(**d)


def parse_entries(record: dict[str, Any]) -> list[OrangeBookEntry]:
    """One openFDA Orange Book record yields one entry per product.

    Patents and exclusivity sit at the RECORD level, not inside `products`, so
    they attach to every product of that record — which is how the source models
    it and not something to second-guess here.
    """
    patents = [
        ListedPatent(
            patent_number=str(p.get("patent_number", "") or ""),
            expiration_date=str(p.get("expiration_date", "") or ""),
            patent_use_code=str(p.get("patent_use_code", "") or ""),
            patent_submission_date=str(p.get("patent_submission_date", "") or ""),
            drug_substance_flag=bool(p.get("drug_substance_flag")),
            drug_product_flag=bool(p.get("drug_product_flag")),
            delist_requested=bool(p.get("patent_delist_flag")),
        )
        for p in (record.get("patents") or []) if p.get("patent_number")
    ]
    exclusivity = [
        ListedExclusivity(
            exclusivity_code=str(e.get("exclusivity_code", "") or ""),
            exclusivity_expiration_date=str(e.get("exclusivity_expiration_date", "") or ""),
        )
        for e in (record.get("exclusivity") or []) if e.get("exclusivity_code")
    ]

    out = []
    for p in record.get("products") or []:
        number = str(p.get("application_number", "") or "")
        atype = str(p.get("application_type", "") or "")
        if not number:
            continue
        out.append(OrangeBookEntry(
            application_number=f"{atype}{number}",
            product_number=str(record.get("product_number", "") or ""),
            brand_name=str(p.get("brand_name", "") or ""),
            active_ingredients=[a.get("name", "") for a in (p.get("active_ingredients") or [])
                                if a.get("name")],
            application_type=atype,
            applicant=str(p.get("application_full_name") or p.get("application_name") or ""),
            dosage_form=str(p.get("dosage_form", "") or ""),
            route=str(p.get("route", "") or ""),
            marketing_status=str(p.get("marketing_status", "") or ""),
            therapeutic_equivalence_codes=list(p.get("therapeutic_equivalence_codes") or []),
            reference_listed_drug=bool(p.get("reference_listed_drug")),
            approval_date=str(record.get("approval_date", "") or ""),
            approved_prior_to_1982=bool(record.get("approved_prior_to_1982")),
            patents=patents,
            exclusivity=exclusivity,
        ))
    return out


@dataclass
class ProtectionAnswer:
    """Listed protection for an asset — or why the question does not apply.

    `applicable` False is the load-bearing state. It means the Orange Book was
    never going to have anything for this asset, and it renders as NOT
    APPLICABLE with the reason. It is not, and must never render as, an absence
    of intellectual property.
    """
    asset: str
    searched: bool = False
    applicable: bool = True
    not_applicable_reason: str = ""
    entries: list[OrangeBookEntry] = field(default_factory=list)
    generic_entries: list[OrangeBookEntry] = field(default_factory=list)
    export_date: str = ""

    # ------------------------------------------------------------ derived

    @property
    def innovator_entries(self) -> list[OrangeBookEntry]:
        return [e for e in self.entries if not e.is_generic]

    @property
    def all_patents(self) -> list[ListedPatent]:
        seen, out = set(), []
        for e in self.entries:
            for p in e.patents:
                if p.patent_number not in seen:
                    seen.add(p.patent_number)
                    out.append(p)
        return out

    @property
    def all_exclusivity(self) -> list[ListedExclusivity]:
        seen, out = set(), []
        for e in self.entries:
            for x in e.exclusivity:
                key = (x.exclusivity_code, x.exclusivity_expiration_date)
                if key not in seen:
                    seen.add(key)
                    out.append(x)
        return out

    def _protection_dates(self) -> list[date]:
        dates = [p.expires for p in self.all_patents if p.expires]
        dates += [x.expires for x in self.all_exclusivity if x.expires]
        return sorted(d for d in dates if d)

    @property
    def earliest_protection_lapse(self) -> date | None:
        dates = self._protection_dates()
        return dates[0] if dates else None

    @property
    def latest_protection_lapse(self) -> date | None:
        dates = self._protection_dates()
        return dates[-1] if dates else None

    @property
    def has_orphan_exclusivity(self) -> bool:
        return any((x.kind and x.kind.kind == "orphan") for x in self.all_exclusivity)

    @property
    def has_paediatric_exclusivity(self) -> bool:
        return any((x.kind and x.kind.kind == "paediatric") for x in self.all_exclusivity)

    @property
    def generics_exist(self) -> bool:
        """Whether an ANDA referencing this molecule is already listed."""
        return bool(self.generic_entries)

    @property
    def found(self) -> bool:
        """Any Orange Book entry at all, innovator OR generic.

        Generic-only is a real and common shape — an old molecule whose
        originator application has lapsed out of the listing while dozens of
        ANDAs remain — and treating it as "not found" would report a matching
        gap where there is none.
        """
        return bool(self.entries) or bool(self.generic_entries)

    # ------------------------------------------------------------ rendering

    def statement(self) -> str:
        if not self.searched:
            return (f"Listed patent and exclusivity protection for “{self.asset}” was NOT "
                    "checked — no Orange Book data is available. This is not a finding "
                    "about the asset.")
        if not self.applicable:
            return (f"The Orange Book does not apply to “{self.asset}”: "
                    f"{self.not_applicable_reason}. Nothing about the sponsor's patent "
                    "position can be read from this section, in either direction.")
        if not self.found:
            return (f"“{self.asset}” has an approved application but no Orange Book entry "
                    "was matched. This is a matching gap, not a statement about what is "
                    "listed against it.")
        n_pat, n_exc = len(self.all_patents), len(self.all_exclusivity)
        return (f"The Orange Book lists {n_pat} patent(s) and {n_exc} exclusivity "
                f"grant(s) against {len(self.innovator_entries)} application(s) matching "
                f"“{self.asset}”.")

    def axis_lapse(self) -> str:
        """The one sentence this section exists for — and the one it must not say."""
        earliest = self.earliest_protection_lapse
        if not earliest:
            if self.innovator_entries or not self.generic_entries:
                return ("Earliest listed protection: none listed. For an approved product "
                        "this usually means the listed patents have already expired and "
                        "been removed, not that none ever existed — 24% of NDAs in this "
                        "source carry listed patents at any one time.")
            # Only generics matched. A generic does not list patents of its own,
            # so their absence here is the expected shape and says nothing about
            # the originator's position.
            return ("Earliest listed protection: only generic (ANDA) entries matched this "
                    "molecule, and a generic application does not list patents of its "
                    "own. That is the expected shape for an off-patent molecule and is "
                    "not a statement about what any sponsor holds.")
        latest = self.latest_protection_lapse
        line = f"Earliest listed protection lapses {human_date(earliest.strftime('%Y%m%d'))}"
        if latest and latest != earliest:
            line += f"; the latest listed lapses {human_date(latest.strftime('%Y%m%d'))}"
        line += (". That is the date a listed protection expires on the face of the "
                 "filing. It is NOT a date on which a competing product arrives: that "
                 "depends on litigation, settlements, first-filer exclusivity and "
                 "whether anyone chooses to enter, none of which is recorded here.")
        return line

    def axis_exclusivity(self) -> str:
        if not self.all_exclusivity:
            return ""
        bits = []
        for x in self.all_exclusivity:
            kind = x.kind
            label = (f"{x.exclusivity_code} ({kind.meaning})" if kind and kind.meaning
                     else f"{x.exclusivity_code} — meaning not published in a machine-"
                          "readable FDA source, so none is asserted here")
            bits.append(f"{label} to {human_date(x.exclusivity_expiration_date)}")
        line = "Listed exclusivity: " + "; ".join(bits) + "."
        flags = []
        if self.has_orphan_exclusivity:
            flags.append("orphan")
        if self.has_paediatric_exclusivity:
            flags.append("paediatric")
        if flags:
            line += (f" This application carries {' and '.join(flags)} exclusivity. "
                     "That classification is CURATED in config/fda_exclusivity_codes.yaml "
                     "from the code prefix, not taken from an FDA-published machine-"
                     "readable legend, because openFDA does not publish one.")
        return line

    def axis_generics(self) -> str:
        if self.generics_exist:
            n = len({e.application_number for e in self.generic_entries})
            return (f"Generic filings: {n} generic (ANDA) application(s) referencing this "
                    "molecule are already listed. Listing means an application exists, "
                    "not that a product is on the market or that it is being sold today.")
        return ("Generic filings: no generic (ANDA) application referencing this molecule "
                "was matched. That is not a statement that none exists or that none can "
                "be filed.")

    def axis_patents(self) -> str:
        if not self.all_patents:
            return ""
        substance = sum(1 for p in self.all_patents if p.drug_substance_flag)
        product = sum(1 for p in self.all_patents if p.drug_product_flag)
        use = sum(1 for p in self.all_patents if p.patent_use_code)
        delist = sum(1 for p in self.all_patents if p.delist_requested)
        line = (f"Listed patents: {len(self.all_patents)} — {substance} flagged as "
                f"claiming the drug substance, {product} the drug product, {use} carrying "
                "a method-of-use code.")
        if delist:
            line += (f" {delist} carry a delist-requested flag, which the FDA sets when a "
                     "holder has asked for a patent to be delisted but it cannot be.")
        return line

    def limits_lines(self) -> list[str]:
        return ["What this section is not: " + LIMITS[0]] + ["  " + x for x in LIMITS[1:]]

    def coverage_lines(self) -> list[str]:
        lines = []
        if self.searched:
            lines.append(
                "Searched: the FDA Orange Book via openFDA's bulk export"
                + (f", FDA export date {self.export_date}" if self.export_date else "")
                + ". Small-molecule approvals only.")
        else:
            lines.append("Searched: nothing — no Orange Book data is available.")
        lines.append("Not searched: " + "; ".join(NOT_SEARCHED) + ".")
        return lines

    def render_lines(self) -> list[str]:
        """The ONLY function that turns listed protection into prose."""
        lines = [self.statement()]
        if self.searched and self.applicable and self.found:
            for axis in (self.axis_lapse(), self.axis_exclusivity(),
                         self.axis_patents(), self.axis_generics()):
                if axis:
                    lines.append(axis)
        if self.searched:
            lines.extend(self.limits_lines())
        lines.extend(self.coverage_lines())
        return lines


__all__ = [
    "ListedPatent", "ListedExclusivity", "OrangeBookEntry", "ProtectionAnswer",
    "ExclusivityKind", "classify_exclusivity", "load_exclusivity_codes",
    "parse_entries", "human_date", "LIMITS", "NOT_SEARCHED",
    "NOT_APPLICABLE_NO_APPROVAL", "NOT_APPLICABLE_BIOLOGIC",
    "EXCLUSIVITY_PREFIXES", "EXCLUSIVITY_OBSERVED",
]
