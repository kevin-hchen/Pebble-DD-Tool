"""openFDA DRUG API client — approvals, labels, and recalls.

    GET https://api.fda.gov/drug/drugsfda.json      applications, products, submissions
    GET https://api.fda.gov/drug/label.json         SPL labelling, section by section
    GET https://api.fda.gov/drug/enforcement.json   drug recalls

Alongside `client.py` (devices), not inside it: the two share a transport shape
and nothing else. A 510(k) clearance and an NDA approval are different regulatory
objects with different identifiers, and folding them into one record type would
force every consumer to branch on which kind it really had.

FIELD REALITIES, MEASURED AGAINST THE LIVE API (2026-08-05), NOT ASSUMED
=======================================================================

Sizes. drugsfda 29,252 applications; label 261,379 documents; enforcement
17,860 drug recalls; event (FAERS) 20,692,690 reports. The bulk exports agree
with the API totals exactly on all four, and the whole of drugsFDA is a single
8.9 MB partition — so an exhaustive local drugsFDA ingest is affordable in a way
the 1.76 GB label corpus and the 113 GB FAERS corpus are not. Labels are fetched
per asset; FAERS is not integrated at all (see `coverage.py`'s not-searched rule
— a gap that is declared is not the same as a gap nobody noticed).

**The matching field is products[].active_ingredients[].name, NOT
openfda.generic_name.** This is the single most consequential thing in this file.
The `openfda` block is a CONVENIENCE JOIN derived from SPL linkage and it is
absent from most of the database:

    products.active_ingredients.name present on  28,904 of 29,252   (99%)
    openfda.generic_name        present on       12,488 of 29,252   (43%)

Matching on the obvious field would silently drop 57% of applications —
NDA017488 (Modicon) carries no `openfda` block at all yet states ETHINYL
ESTRADIOL and NORETHINDRONE in its products. Measured across this tool's own
42-agent asset list, the two fields find the same 31 assets, but the ingredient
field reaches 261 applications against openfda's 138 — the difference is
generics and biosimilars, which is exactly the competitive picture a diligence
memo is asking about.

Identifiers, and how they relate:
  * `application_number` — "NDA017488", "BLA125514", "ANDA213576". The primary
    key. Namespace measured: 22,898 ANDA, 5,874 NDA, 480 BLA.
  * `products[].product_number` — "001", "002". Unique only WITHIN an
    application, so the real product key is (application_number, product_number).
  * `openfda.spl_id` / `spl_set_id` — the SPL document and its stable set ID.
    `set_id` is what joins drugsFDA to a label; `id` changes with each version.
  * `openfda.unii`, `rxcui`, `product_ndc` — other join keys, carried but unused.

Approval is a SUBMISSION fact, not a product fact. `submissions[]` carries
`submission_type` (ORIG | SUPPL), `submission_status` (AP | TA) and
`submission_status_date` (YYYYMMDD). The original approval is the ORIG
submission; its date is the approval date. Measured status vocabulary: AP
25,490, TA 1,140. **TA is Tentative Approval and is NOT approval** — the FDA has
found the application meets requirements but cannot approve it, usually for
patent or exclusivity reasons. `products[].marketing_status` is a separate axis
again: Discontinued 14,762, Prescription 13,382, None (Tentative Approval) 716,
Over-the-counter 610. An approved-then-discontinued drug is not an unapproved
drug, and this file keeps the two apart.

Labels. Sections are ARRAYS of strings and some are enormous — on the Keytruda
label, adverse_reactions is 188 KB and clinical_studies 213 KB — so every
section is truncated on ingest and the truncation is recorded, never silent.
Only 74,827 of 261,379 labels (29%) carry `openfda.application_number`, so a
label often cannot be joined back to an application at all; `DrugLabel.linked`
says which case a given label is.

Recalls. `openfda` is usually EMPTY on enforcement records — only 3,169 of
17,860 (18%) carry `openfda.application_number` — so a drug recall mostly cannot
be joined to an application and is matched on product_description text instead.

Rate limits. The published ceilings are 240 requests/minute and 1,000/day
without a key, 120,000/day with one. **openFDA returns no rate-limit headers**
(measured: no `X-RateLimit-*` on any response), so remaining quota cannot be
read back and the client self-throttles on the published number, exactly as the
device client does. The two share `client._throttle`, deliberately: they are the
same 240/min bucket per IP and two independent throttles would each think they
had the whole budget.

A 404 means "no matches", not an error — the same convention as the device
endpoints.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .. import agents
from .client import API_BASE, _fetch, _total  # one transport, one throttle bucket

# ---------------------------------------------------------------- approval vocabulary

APPROVED = "APPROVED"
TENTATIVE_APPROVAL = "TENTATIVE APPROVAL"
SUBMITTED_NOT_APPROVED = "SUBMITTED, NOT APPROVED"
STATUS_UNKNOWN = "STATUS NOT STATED"

#: What `marketing_status` says about a product TODAY, which is a different axis
#: from whether it was ever approved. An approved-then-withdrawn drug is not an
#: unapproved one.
MARKETING_DISCONTINUED = "Discontinued"

#: The four things absence from drugsFDA can mean. This tuple exists so the
#: renderers can print them verbatim rather than each inventing a phrasing, and
#: so a test can assert every one of them survives into the output.
#: Worded so that no SUBSTRING of any one of them reads as a claim about this
#: asset when quoted out of context. The second meaning originally said
#: "submitted but not approved (or still under review)" — which contains the
#: literal phrase "not approved", so a memo stating the four possibilities
#: contained a string a downstream text match, or a reader skimming, takes for
#: an assertion. Same lesson as the earlier "is unapproved" draft: the denial and
#: the claim must not share a substring.
ABSENCE_MEANINGS = (
    "never submitted to the FDA",
    "submitted and still under review, or refused",
    "approved under a name this search did not match",
    "approved outside the US, which this database does not cover",
)


@dataclass
class DrugProduct:
    """One product within an application. The key is (application, number)."""
    product_number: str
    brand_name: str = ""
    active_ingredients: list[str] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    dosage_form: str = ""
    route: str = ""
    marketing_status: str = ""
    reference_drug: str = ""

    @property
    def is_discontinued(self) -> bool:
        return self.marketing_status == MARKETING_DISCONTINUED

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DrugApplication:
    """One drugsFDA application — the unit an approval claim cites."""
    application_number: str
    sponsor_name: str = ""
    products: list[DrugProduct] = field(default_factory=list)
    #: The ORIG submission's outcome and date; SUPPL submissions are efficacy
    #: supplements and do not change whether the drug was ever approved.
    approval_status: str = STATUS_UNKNOWN
    approval_date: str = ""              # YYYYMMDD, verbatim from the API
    review_priority: str = ""            # PRIORITY | STANDARD
    submission_class: str = ""           # e.g. "Type 1 - New Molecular Entity"
    n_supplements: int = 0
    latest_supplement_date: str = ""
    brand_names: list[str] = field(default_factory=list)
    generic_names: list[str] = field(default_factory=list)
    substance_names: list[str] = field(default_factory=list)
    routes: list[str] = field(default_factory=list)
    spl_set_ids: list[str] = field(default_factory=list)
    unii: list[str] = field(default_factory=list)
    pharm_class: list[str] = field(default_factory=list)
    has_openfda: bool = False            # False => matched on products only

    @property
    def application_type(self) -> str:
        """NDA | BLA | ANDA — read off the identifier, which always carries it."""
        for prefix in ("ANDA", "BLA", "NDA"):
            if self.application_number.upper().startswith(prefix):
                return prefix
        return ""

    @property
    def display_number(self) -> str:
        """"NDA 021923", not "NDA021923" — the form the FDA prints and an
        analyst can paste into Drugs@FDA. The stored value keeps the API's own
        spelling so a round-trip is lossless; only the display is spaced."""
        kind = self.application_type
        digits = "".join(c for c in self.application_number if c.isdigit())
        return f"{kind} {digits}" if kind and digits else self.application_number

    @property
    def is_approved(self) -> bool:
        """Strictly the ORIG submission being AP. Tentative approval is not
        approval, and an unknown status is not approval either."""
        return self.approval_status == APPROVED

    @property
    def all_ingredients(self) -> list[str]:
        """Every active-ingredient name across the application's products —
        the field this store matches on."""
        out: list[str] = []
        for p in self.products:
            for name in p.active_ingredients:
                if name and name not in out:
                    out.append(name)
        return out

    @property
    def match_names(self) -> list[str]:
        """Every name this application should be findable under. Ingredients
        first because they are present on 99% of records against openfda's 43%."""
        return [*self.all_ingredients, *self.brand_names, *self.generic_names,
                *self.substance_names, *[p.brand_name for p in self.products if p.brand_name]]

    @property
    def marketing_statuses(self) -> list[str]:
        return sorted({p.marketing_status for p in self.products if p.marketing_status})

    @property
    def all_discontinued(self) -> bool:
        """Every product withdrawn from the market. A real and different fact
        from never approved — and one a diligence memo must not blur."""
        return bool(self.products) and all(p.is_discontinued for p in self.products)

    @property
    def url(self) -> str:
        # Drugs@FDA keys on the bare application number; the NDA/BLA/ANDA prefix
        # is not part of the URL.
        num = "".join(c for c in self.application_number if c.isdigit())
        return ("https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm"
                f"?event=overview.process&ApplNo={num}") if num else ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["products"] = [p.to_dict() for p in self.products]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DrugApplication":
        d = dict(d)
        d["products"] = [DrugProduct(**p) for p in (d.get("products") or [])]
        return cls(**d)


#: Label sections a diligence question actually asks about. Ordered by how often
#: they answer one; `indications_and_usage` is the indication history this whole
#: module exists to supply.
LABEL_SECTIONS = (
    "indications_and_usage",
    "boxed_warning",
    "warnings_and_cautions",
    "contraindications",
    "adverse_reactions",
    "clinical_studies",
    "mechanism_of_action",
    "dosage_and_administration",
    "description",
)

#: Per-section cap. Real sections reach 213 KB; a memo cites a paragraph, not a
#: monograph. Truncation is recorded on the record, never silent.
MAX_SECTION_CHARS = 4000


@dataclass
class DrugLabel:
    """One SPL labelling document, reduced to the sections diligence reads."""
    set_id: str                      # stable across versions — the join key
    spl_id: str = ""                 # this version's document id
    version: str = ""
    effective_time: str = ""         # YYYYMMDD
    application_numbers: list[str] = field(default_factory=list)
    brand_names: list[str] = field(default_factory=list)
    generic_names: list[str] = field(default_factory=list)
    routes: list[str] = field(default_factory=list)
    manufacturer: str = ""
    sections: dict = field(default_factory=dict)      # name -> text
    truncated_sections: list[str] = field(default_factory=list)

    @property
    def linked(self) -> bool:
        """Whether this label can be joined back to a drugsFDA application at
        all. 71% of labels cannot, and a renderer must not imply otherwise."""
        return bool(self.application_numbers)

    @property
    def indications(self) -> str:
        return self.sections.get("indications_and_usage", "")

    @property
    def has_boxed_warning(self) -> bool:
        return bool(self.sections.get("boxed_warning"))

    @property
    def url(self) -> str:
        return (f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={self.set_id}"
                if self.set_id else "")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DrugLabel":
        return cls(**d)


@dataclass
class DrugRecall:
    """A drug enforcement report. Mostly unlinkable to an application (82% carry
    no openfda block), so it is matched on product description text."""
    recall_number: str
    classification: str = ""          # Class I | II | III
    status: str = ""                  # Ongoing | Terminated | Completed
    product_description: str = ""
    reason_for_recall: str = ""
    recalling_firm: str = ""
    report_date: str = ""
    recall_initiation_date: str = ""
    voluntary_mandated: str = ""
    application_numbers: list[str] = field(default_factory=list)
    generic_names: list[str] = field(default_factory=list)
    brand_names: list[str] = field(default_factory=list)

    @property
    def severity_rank(self) -> int:
        """Class I is a reasonable probability of serious harm or death."""
        return {"Class I": 0, "Class II": 1, "Class III": 2}.get(self.classification, 3)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DrugRecall":
        return cls(**d)


# ---------------------------------------------------------------- parsers


def _strlist(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value if v]


def parse_application(rec: dict[str, Any]) -> DrugApplication | None:
    app_no = (rec.get("application_number") or "").strip()
    if not app_no:
        return None
    of = rec.get("openfda") or {}

    products = []
    for p in rec.get("products") or []:
        ingredients = p.get("active_ingredients") or []
        products.append(DrugProduct(
            product_number=str(p.get("product_number", "") or ""),
            brand_name=p.get("brand_name", "") or "",
            active_ingredients=[a.get("name", "") for a in ingredients if a.get("name")],
            strengths=[a.get("strength", "") for a in ingredients if a.get("strength")],
            dosage_form=p.get("dosage_form", "") or "",
            route=p.get("route", "") or "",
            marketing_status=p.get("marketing_status", "") or "",
            reference_drug=p.get("reference_drug", "") or "",
        ))

    # Approval is the ORIG submission. SUPPL rows are efficacy/labelling
    # supplements — they show the drug is alive and expanding, but a supplement
    # can never make an unapproved application approved.
    subs = rec.get("submissions") or []
    origs = [s for s in subs if (s.get("submission_type") or "").upper() == "ORIG"]
    suppls = [s for s in subs if (s.get("submission_type") or "").upper() == "SUPPL"]

    status, date, priority, sclass = STATUS_UNKNOWN, "", "", ""
    if origs:
        # Prefer an approved ORIG if any exists; otherwise report what is there.
        approved = [s for s in origs if (s.get("submission_status") or "").upper() == "AP"]
        chosen = approved[0] if approved else origs[0]
        raw = (chosen.get("submission_status") or "").upper()
        status = {"AP": APPROVED, "TA": TENTATIVE_APPROVAL}.get(raw, SUBMITTED_NOT_APPROVED)
        date = chosen.get("submission_status_date", "") or ""
        priority = chosen.get("review_priority", "") or ""
        sclass = chosen.get("submission_class_code_description", "") or ""

    ap_suppls = [s for s in suppls if (s.get("submission_status") or "").upper() == "AP"]
    return DrugApplication(
        application_number=app_no,
        sponsor_name=rec.get("sponsor_name", "") or "",
        products=products,
        approval_status=status,
        approval_date=date,
        review_priority=priority,
        submission_class=sclass,
        n_supplements=len(ap_suppls),
        latest_supplement_date=max(
            (s.get("submission_status_date", "") or "" for s in ap_suppls), default=""),
        brand_names=_strlist(of.get("brand_name")),
        generic_names=_strlist(of.get("generic_name")),
        substance_names=_strlist(of.get("substance_name")),
        routes=_strlist(of.get("route")) or sorted({p.route for p in products if p.route}),
        spl_set_ids=_strlist(of.get("spl_set_id")),
        unii=_strlist(of.get("unii")),
        pharm_class=_strlist(of.get("pharm_class_epc")),
        has_openfda=bool(of),
    )


def parse_label(rec: dict[str, Any]) -> DrugLabel | None:
    set_id = (rec.get("set_id") or "").strip()
    if not set_id:
        return None
    of = rec.get("openfda") or {}
    sections, truncated = {}, []
    for name in LABEL_SECTIONS:
        blocks = rec.get(name) or []
        text = "\n\n".join(b for b in _strlist(blocks) if b).strip()
        if not text:
            continue
        if len(text) > MAX_SECTION_CHARS:
            text = text[:MAX_SECTION_CHARS].rstrip() + " …"
            truncated.append(name)
        sections[name] = text
    return DrugLabel(
        set_id=set_id,
        spl_id=(rec.get("id") or "").strip(),
        version=str(rec.get("version", "") or ""),
        effective_time=rec.get("effective_time", "") or "",
        application_numbers=_strlist(of.get("application_number")),
        brand_names=_strlist(of.get("brand_name")),
        generic_names=_strlist(of.get("generic_name")),
        routes=_strlist(of.get("route")),
        manufacturer=(_strlist(of.get("manufacturer_name")) or [""])[0],
        sections=sections,
        truncated_sections=truncated,
    )


def parse_drug_recall(rec: dict[str, Any]) -> DrugRecall | None:
    num = (rec.get("recall_number") or "").strip()
    if not num:
        return None
    of = rec.get("openfda") or {}
    return DrugRecall(
        recall_number=num,
        classification=rec.get("classification", "") or "",
        status=rec.get("status", "") or "",
        product_description=(rec.get("product_description", "") or "").strip(),
        reason_for_recall=(rec.get("reason_for_recall", "") or "").strip(),
        recalling_firm=rec.get("recalling_firm", "") or "",
        report_date=rec.get("report_date", "") or "",
        recall_initiation_date=rec.get("recall_initiation_date", "") or "",
        voluntary_mandated=rec.get("voluntary_mandated", "") or "",
        application_numbers=_strlist(of.get("application_number")),
        generic_names=_strlist(of.get("generic_name")),
        brand_names=_strlist(of.get("brand_name")),
    )


# ---------------------------------------------------------------- searches


def _ingredient_search(asset: str) -> str:
    """An openFDA query that finds an asset under any name it may be filed under.

    ORed across ingredient, generic, substance and brand, and across every alias
    `config/agents.yaml` knows — the SAME alias table the trial store uses, so
    "Keytruda" and "MK-3475" reach the same application "pembrolizumab" does.
    ANDing the agents of a combination is done LOCALLY, not here: drugsFDA files
    each ingredient of a combination product as a separate `active_ingredients`
    entry, so a two-word phrase query matches neither half — measured, a live
    `products.active_ingredients.name:"trifluridine tipiracil"` returns 404 while
    "trifluridine" returns 7 and "tipiracil" returns 4.
    """
    query = agents.parse_asset(asset)
    forms: list[str] = []
    for term in query.terms:
        forms.extend(term.forms)
    if not forms:
        return ""
    fields = ("products.active_ingredients.name", "openfda.generic_name",
              "openfda.substance_name", "openfda.brand_name", "products.brand_name")
    clauses = [f'{f}:"{form}"' for form in dict.fromkeys(forms) for f in fields]
    return " OR ".join(clauses)


def search_applications(asset: str, max_records: int = 100, timeout: int = 45,
                        offline: bool = False) -> list[DrugApplication]:
    """drugsFDA applications for an asset, under any name the alias table knows."""
    search = _ingredient_search(asset)
    if not search:
        return []
    out = [parse_application(r)
           for r in _fetch("drug/drugsfda", search, max_records, 100, timeout, offline)]
    return [a for a in out if a]


def count_applications(asset: str, timeout: int = 45, offline: bool = False) -> int | None:
    """openFDA's own reported total for this asset — the denominator a memo
    needs to say "N of M", never inferred from what was fetched."""
    search = _ingredient_search(asset)
    if not search:
        return None
    return _total("drug/drugsfda", search, timeout, offline)


def search_labels(asset: str, max_records: int = 5, timeout: int = 45,
                  offline: bool = False) -> list[DrugLabel]:
    """SPL labels for an asset. Deliberately a small cap: labels are large, a
    diligence pass reads the current one, and the corpus is 1.76 GB."""
    search = _ingredient_search(asset)
    if not search:
        return []
    out = [parse_label(r)
           for r in _fetch("drug/label", search, max_records, max_records, timeout, offline)]
    return [lab for lab in out if lab]


def search_drug_recalls(asset: str, max_records: int = 50, timeout: int = 45,
                        offline: bool = False) -> list[DrugRecall]:
    """Drug enforcement reports. Matched on openfda names where present and on
    product_description text otherwise, because 82% of recalls carry no openfda
    block at all."""
    query = agents.parse_asset(asset)
    forms = [f for term in query.terms for f in term.forms]
    if not forms:
        return []
    clauses = []
    for form in dict.fromkeys(forms):
        clauses += [f'openfda.generic_name:"{form}"', f'openfda.brand_name:"{form}"',
                    f'product_description:"{form}"']
    out = [parse_drug_recall(r) for r in _fetch(
        "drug/enforcement", " OR ".join(clauses), max_records, 100, timeout, offline)]
    return [r for r in out if r]


__all__ = [
    "API_BASE", "APPROVED", "TENTATIVE_APPROVAL", "SUBMITTED_NOT_APPROVED",
    "STATUS_UNKNOWN", "ABSENCE_MEANINGS", "LABEL_SECTIONS", "MAX_SECTION_CHARS",
    "DrugApplication", "DrugProduct", "DrugLabel", "DrugRecall",
    "parse_application", "parse_label", "parse_drug_recall",
    "search_applications", "count_applications", "search_labels", "search_drug_recalls",
]
