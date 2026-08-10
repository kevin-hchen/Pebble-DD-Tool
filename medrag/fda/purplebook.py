"""Purple Book — licensed biologics, biosimilars, and interchangeability.

NOT AN openFDA SOURCE, AND THE 404 THAT WAS NOT ONE
===================================================

`drug/purplebook` returns HTTP 404 (probed twice — the phase-3 lesson), and the
Purple Book is absent from openFDA's `download.json` entirely. The real
distribution is a monthly CSV at
`accessdata.fda.gov/drugsatfda_docs/PurpleBook/{year}/purplebook-search-{month}-data-download.csv`,
published back to February 2020.

Worth recording because it nearly cost this phase: `purplebooksearch.fda.gov`
answered three consecutive requests with HTTP **404** and a 420-byte body that
was Akamai's bot-detection "apology" page, triggered by request rate. A browser
User-Agent after a pause returned HTTP 200 and 47 KB. A status code alone would
have recorded this source as unavailable. `bulk.check_not_blocked` now raises
`BlockedByBotDetection` on that body rather than letting it read as absence.

THE FILE IS TWO SECTIONS AND ONLY THE SECOND IS THE DATABASE
============================================================

Each monthly file opens with a CHANGES report (newly approved / added /
updated), then repeats the same header and lists ALL products in the database
that month. `load_delimited(section=1)` selects the second. Taking the first
would silently reduce the Purple Book to whatever changed in one month.

MEASURED (June 2026 file): 2,205 product rows, 847 distinct BLAs.

    351(a) originator biologic   1,977   89.7%
    351(k) Interchangeable         128    5.8%
    351(k) Biosimilar              100    4.5%

EXCLUSIVITY COVERAGE IS THE FINDING, NOT AN ASSUMPTION
======================================================

Phase 3's Orange Book equivalent — "earliest listed protection lapses <date>" —
is barely renderable here, and this was measured rather than assumed:

    Exclusivity Expiration Date                     0 of 2,205    0.0%
    First Interchangeable Exclusivity Exp. Date    34 of 2,205    1.5%
    Ref. Product Exclusivity Exp. Date             36 of 2,205    1.6%
    Date of First Licensure                        37 of 2,205    1.7%
    Patent List Provided                           49 of 2,205    2.2%
    Orphan Exclusivity Exp. Date                  564 of 2,205   25.6%

The headline exclusivity column is empty on EVERY row. So this section leads on
what the source answers well — reference product, licensed biosimilars,
interchangeability, all at 100% linkage on the 228 biosimilar rows — and treats
an exclusivity date as an occasional extra that states its own fill rate. A
section shaped like phase 3's would print a blank field 98% of the time and
invite the reader to conclude something from the blank.

BIOSIMILAR ENTRY IS NOT GENERIC ENTRY
=====================================

Rendered in a deliberately different shape from the Orange Book's generic line.
A biosimilar requires its own clinical programme, is NOT automatically
substitutable at the pharmacy unless separately designated interchangeable, and
uptake behaves nothing like generic substitution. So the gap between "reference
product exclusivity expires <date>" and "competition arrives <date>" is wider
here than for a small molecule, and the section says so rather than leaving a
reader to assume the small-molecule mental model transfers.

Interchangeability is a SEPARATE regulatory finding from biosimilarity and is
recorded separately: `License Type` distinguishes "351(k) Biosimilar" from
"351(k) Interchangeable", and `Inter. Approval Date` /
`First Interchangeable Exclusivity Exp. Date` are its own fields.

THE THIRD ABSENCE VARIANT, SPECIFIC TO THIS SOURCE
==================================================

A licensed biologic with no biosimilars listed means no biosimilar has been
LICENSED. Biosimilar development programmes are invisible until licensure —
there is no public register of them — so rendering that as "no biosimilar
competition" would be a false statement about the competitive position of
exactly the assets a healthcare investor cares most about. `no_biosimilars_note`
is the fixed text that says so.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CODES_CONFIG = (
    Path(__file__).resolve().parents[2] / "config" / "fda_biologic_exclusivity.yaml")

#: The published monthly file. `{year}` and `{month}` are substituted; the FDA
#: capitalises the month inconsistently across years, which the loader handles.
DOWNLOAD_URL = ("https://www.accessdata.fda.gov/drugsatfda_docs/PurpleBook/"
                "{year}/purplebook-search-{month}-data-download.csv")

#: Column 0 of both header rows. `section=1` selects the full database.
HEADER_MARKER = "N/R/U"
FULL_DATABASE_SECTION = 1

LICENSE_ORIGINATOR = "351(a)"
LICENSE_BIOSIMILAR = "351(k) Biosimilar"
LICENSE_INTERCHANGEABLE = "351(k) Interchangeable"

#: Why the Purple Book can be inapplicable rather than empty. Three variants,
#: worded so no substring reads as a claim about the asset.
NOT_APPLICABLE_SMALL_MOLECULE = (
    "this asset was found under an NDA, and small molecules are approved rather "
    "than licensed — they are absent from the Purple Book by design, and their "
    "listed patents and exclusivity are recorded in the Orange Book, which is the "
    "book that was consulted for it"
)
NOT_APPLICABLE_NO_LICENCE = (
    "listing requires a licensed biologic, and no licensed US application matching "
    "this asset was found — an investigational biologic is absent from the Purple "
    "Book by construction, which says nothing about what the sponsor owns or what "
    "it has in development"
)

#: The load-bearing note for variant 3.
NO_BIOSIMILARS_NOTE = (
    "No biosimilar to this reference product has been LICENSED. That is the only "
    "thing this source can show: biosimilar development programmes are not "
    "publicly registered and are invisible here until the day they are licensed, "
    "so this is not a statement that none is in development, and it is not a "
    "measure of how contested the molecule is."
)

#: Why biosimilar entry is not generic entry. Rendered whenever a reference
#: product is discussed, so the small-molecule mental model is never imported
#: silently.
BIOSIMILAR_IS_NOT_GENERIC = (
    "A biosimilar is not a generic. It requires its own clinical programme, it is "
    "NOT automatically substitutable at the pharmacy unless the FDA has separately "
    "designated it interchangeable, and uptake behaves nothing like generic "
    "substitution. The distance between a listed exclusivity date and actual "
    "competition is therefore wider here than for a small molecule."
)

LIMITS = (
    "Interchangeability is a SEPARATE FDA finding from biosimilarity. A product "
    "licensed as a biosimilar is not interchangeable unless the FDA has designated "
    "it so, and the two are recorded separately here.",
    "Listed exclusivity dates are sparse in this source and are stated with their "
    "fill rate; an empty exclusivity field is not a finding.",
    "This covers US licensure only. A biologic licensed in the EU or Japan and "
    "nowhere else is absent by design.",
)

NOT_SEARCHED = (
    "biosimilar development programmes, which are not publicly registered before "
    "licensure",
    "patent litigation and the biosimilar 'patent dance' under BPCIA",
    "non-US licensure and non-US exclusivity",
)


def load_exclusivity_config(path: str | Path | None = None) -> dict:
    """Curated code meanings, following the phase-3 pattern: nothing is asserted
    for a value the FDA does not document in a machine-readable form."""
    p = Path(path or DEFAULT_CODES_CONFIG)
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


EXCLUSIVITY_CONFIG = load_exclusivity_config()


def _as_date(text: str) -> date | None:
    """The Purple Book writes '5-Jun-26', not ISO. Two-digit years are read the
    way the FDA means them: these are approval and expiry dates, so a year that
    would land far in the past is a 21st-century year."""
    text = (text or "").strip()
    if not text or text in ("N/A", "0"):
        return None
    for fmt in ("%d-%b-%y", "%d-%b-%Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def human_date(text: str) -> str:
    d = _as_date(text)
    return d.strftime("%-d %b %Y") if d else (text or "not stated")


@dataclass
class BiologicProduct:
    """One product row from the Purple Book."""
    bla_number: str
    product_number: str = ""
    applicant: str = ""
    proprietary_name: str = ""
    proper_name: str = ""
    license_type: str = ""
    strength: str = ""
    dosage_form: str = ""
    route: str = ""
    marketing_status: str = ""
    licensure: str = ""
    approval_date: str = ""
    interchangeable_approval_date: str = ""
    ref_product_proper_name: str = ""
    ref_product_proprietary_name: str = ""
    center: str = ""
    date_of_first_licensure: str = ""
    exclusivity_expiration_date: str = ""
    first_interchangeable_exclusivity_date: str = ""
    ref_product_exclusivity_date: str = ""
    orphan_exclusivity_date: str = ""
    patent_list_provided: str = ""

    @property
    def is_biosimilar(self) -> bool:
        return "351(k)" in self.license_type

    @property
    def is_interchangeable(self) -> bool:
        """A SEPARATE finding from biosimilarity — never inferred from it."""
        return "Interchangeable" in self.license_type

    @property
    def is_originator(self) -> bool:
        return self.license_type.strip().startswith(LICENSE_ORIGINATOR)

    @property
    def is_marketed(self) -> bool:
        return self.marketing_status.strip().upper() in ("RX", "OTC")

    @property
    def match_names(self) -> list[str]:
        return [n for n in (self.proper_name, self.proprietary_name) if n and n != "N/A"]

    @property
    def display_number(self) -> str:
        return f"BLA {self.bla_number}" if self.bla_number else ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BiologicProduct":
        return cls(**d)


#: CSV header -> field. Explicit rather than derived, so a publisher renaming a
#: column fails loudly at ingest instead of silently emptying a field.
COLUMNS = {
    "Applicant": "applicant",
    "BLA Number": "bla_number",
    "Proprietary Name": "proprietary_name",
    "Proper Name": "proper_name",
    "License Type": "license_type",
    "Strength": "strength",
    "Dosage Form": "dosage_form",
    "Route of Administration": "route",
    "Marketing Status": "marketing_status",
    "Licensure": "licensure",
    "Approval Date": "approval_date",
    "Inter. Approval Date": "interchangeable_approval_date",
    "Ref. Product Proper Name": "ref_product_proper_name",
    "Ref. Product Proprietary Name": "ref_product_proprietary_name",
    "Product Number": "product_number",
    "Center": "center",
    "Date of First Licensure": "date_of_first_licensure",
    "Exclusivity Expiration Date": "exclusivity_expiration_date",
    "First Interchangeable Exclusivity Exp. Date": "first_interchangeable_exclusivity_date",
    "Ref. Product Exclusivity Exp. Date": "ref_product_exclusivity_date",
    "Orphan Exclusivity Exp. Date": "orphan_exclusivity_date",
    "Patent List Provided": "patent_list_provided",
}


class PurpleBookLayoutError(RuntimeError):
    """The published columns are not the ones this parser was written against."""


def parse_row(row: dict[str, str]) -> BiologicProduct | None:
    bla = (row.get("BLA Number") or "").strip()
    if not bla:
        return None
    kwargs = {}
    for column, field_name in COLUMNS.items():
        value = (row.get(column) or "").strip()
        kwargs[field_name] = "" if value == "N/A" else value
    return BiologicProduct(**kwargs)


def check_layout(header: list[str]) -> None:
    """Fail loudly if the publisher has renamed a column this parser needs.

    A CSV has no schema version, so a renamed column would otherwise empty a
    field silently — which for `License Type` would turn every biosimilar into
    an originator.
    """
    missing = [c for c in COLUMNS if c not in header]
    if missing:
        raise PurpleBookLayoutError(
            "the Purple Book file no longer carries these columns: "
            + ", ".join(missing)
            + ". Refusing to parse it into fields that would silently read empty; "
              "update medrag/fda/purplebook.py COLUMNS against the new layout.")


@dataclass
class BiologicProtectionAnswer:
    """Licensure, biosimilars and interchangeability for an asset — or why the
    question does not apply.

    Three inapplicable states, not one: the wrong book (small molecule), no
    licence (investigational), and never checked. None of them is an absence of
    protection or of competition.
    """
    asset: str
    searched: bool = False
    applicable: bool = True
    not_applicable_reason: str = ""
    consulted_book: str = ""          # which book was used, and said out loud
    products: list[BiologicProduct] = field(default_factory=list)
    biosimilars: list[BiologicProduct] = field(default_factory=list)
    published: str = ""
    exclusivity_fill_note: str = ""

    # ------------------------------------------------------------ derived

    @property
    def found(self) -> bool:
        return bool(self.products)

    @property
    def originators(self) -> list[BiologicProduct]:
        return [p for p in self.products if p.is_originator]

    @property
    def interchangeables(self) -> list[BiologicProduct]:
        """Interchangeable products — a separate finding from biosimilarity."""
        return [p for p in self.biosimilars if p.is_interchangeable]

    @property
    def biosimilars_only(self) -> list[BiologicProduct]:
        return [p for p in self.biosimilars if not p.is_interchangeable]

    @property
    def has_licensed_biosimilar(self) -> bool:
        """Positive evidence only. False means none is LICENSED — never that
        none exists or is in development."""
        return bool(self.biosimilars)

    @property
    def distinct_biosimilar_blas(self) -> list[str]:
        seen = []
        for p in self.biosimilars:
            if p.bla_number not in seen:
                seen.append(p.bla_number)
        return seen

    def _dates(self, attr: str) -> list[date]:
        out = []
        for p in self.products:
            d = _as_date(getattr(p, attr, ""))
            if d:
                out.append(d)
        return sorted(out)

    @property
    def earliest_reference_exclusivity(self) -> date | None:
        d = self._dates("ref_product_exclusivity_date")
        return d[0] if d else None

    @property
    def earliest_orphan_exclusivity(self) -> date | None:
        d = self._dates("orphan_exclusivity_date")
        return d[0] if d else None

    # ------------------------------------------------------------ rendering

    def statement(self) -> str:
        if not self.searched:
            return (f"Biologic licensure and biosimilar status for “{self.asset}” was "
                    "NOT checked — no Purple Book data is available. This is not a "
                    "finding about the asset.")
        if not self.applicable:
            return (f"The Purple Book does not apply to “{self.asset}”: "
                    f"{self.not_applicable_reason}."
                    + (f" The {self.consulted_book} was consulted instead."
                       if self.consulted_book else ""))
        if not self.found:
            return (f"“{self.asset}” has a licensed application but no Purple Book "
                    "product row was matched. This is a matching gap, not a statement "
                    "about its licensure or its competitors.")
        n_bla = len({p.bla_number for p in self.products})
        return (f"The Purple Book lists {len(self.products)} licensed product row(s) "
                f"across {n_bla} BLA(s) matching “{self.asset}”.")

    def axis_licensure(self) -> str:
        if not self.found:
            return ""
        marketed = sum(1 for p in self.products if p.is_marketed)
        dates = sorted({p.approval_date for p in self.originators if p.approval_date})
        line = (f"Licensure: {len(self.originators)} originator (351(a)) product row(s), "
                f"{marketed} of {len(self.products)} currently marketed")
        if dates:
            line += f"; earliest listed approval {human_date(dates[0])}"
        return line + "."

    def axis_biosimilars(self) -> str:
        """The load-bearing line — variant 3 lives here."""
        if not self.found:
            return ""
        if not self.has_licensed_biosimilar:
            return "Licensed biosimilars: none. " + NO_BIOSIMILARS_NOTE
        blas = ", ".join(self.distinct_biosimilar_blas[:5])
        names = ", ".join(sorted({p.proprietary_name for p in self.biosimilars
                                  if p.proprietary_name})[:5])
        return (f"Licensed biosimilars: {len(self.distinct_biosimilar_blas)} BLA(s) "
                f"({blas}){f' — {names}' if names else ''}, across "
                f"{len(self.biosimilars)} product row(s). " + BIOSIMILAR_IS_NOT_GENERIC)

    def axis_interchangeability(self) -> str:
        """Recorded separately from biosimilarity, because they are separate
        regulatory findings."""
        if not self.found:
            return ""
        if not self.biosimilars:
            return ("Interchangeability: not applicable — no biosimilar to this "
                    "reference product is licensed, so none can be designated "
                    "interchangeable.")
        n = len({p.bla_number for p in self.interchangeables})
        if not n:
            return ("Interchangeability: none of the licensed biosimilars carries an "
                    "FDA interchangeability designation. They are biosimilar but NOT "
                    "automatically substitutable at the pharmacy; a prescriber has to "
                    "specify them.")
        dates = [p.interchangeable_approval_date for p in self.interchangeables
                 if p.interchangeable_approval_date]
        line = (f"Interchangeability: {n} BLA(s) carry an FDA interchangeability "
                "designation, a separate finding from biosimilarity")
        if dates:
            line += f", earliest designated {human_date(sorted(dates)[0])}"
        return line + ". An interchangeable product may be substituted at the pharmacy "\
                      "without prescriber intervention, subject to state law."

    def axis_exclusivity(self) -> str:
        """Sparse by measurement, and says so rather than printing a blank."""
        if not self.found:
            return ""
        ref = self.earliest_reference_exclusivity
        orphan = self.earliest_orphan_exclusivity
        if not ref and not orphan:
            # Worded to avoid "no exclusivity", which reads as a claim about the
            # asset when quoted out of context — the fourth time this pattern has
            # come up, caught here by the test rather than by inspection. See
            # phrasing.CLAIM_PHRASES["protection"].
            return ("Listed exclusivity: the FDA has not published a date in these "
                    "fields for these rows. " + (self.exclusivity_fill_note or
                    "Exclusivity coverage in this source is sparse, so an empty field "
                    "reflects what the FDA publishes rather than a finding."))
        bits = []
        if ref:
            bits.append("reference-product exclusivity listed to "
                        f"{human_date(ref.strftime('%d-%b-%y'))}")
        if orphan:
            bits.append(f"orphan exclusivity listed to {human_date(orphan.strftime('%d-%b-%y'))}")
        return ("Listed exclusivity: " + "; ".join(bits) + ". That is a date on the face "
                "of the listing. It is NOT a date on which a biosimilar arrives: a "
                "biosimilar needs its own clinical programme and its own licensure, "
                "and nothing here says whether one is being developed.")

    def limits_lines(self) -> list[str]:
        return ["What this section is not: " + LIMITS[0]] + ["  " + x for x in LIMITS[1:]]

    def coverage_lines(self) -> list[str]:
        lines = []
        if self.searched:
            lines.append(
                "Searched: the FDA Purple Book monthly data file"
                + (f", published {self.published}" if self.published else "")
                + ". Licensed biologics only; this is not an openFDA endpoint and the "
                  "FDA publishes no record count for it, so the row count is what "
                  "parsed rather than a total checked against a declared one.")
        else:
            lines.append("Searched: nothing — no Purple Book data is available.")
        lines.append("Not searched: " + "; ".join(NOT_SEARCHED) + ".")
        return lines

    def render_lines(self) -> list[str]:
        """The ONLY function that turns Purple Book data into prose."""
        lines = [self.statement()]
        if self.searched and self.applicable and self.found:
            for axis in (self.axis_licensure(), self.axis_biosimilars(),
                         self.axis_interchangeability(), self.axis_exclusivity()):
                if axis:
                    lines.append(axis)
        if self.searched:
            lines.extend(self.limits_lines())
        lines.extend(self.coverage_lines())
        return lines


__all__ = [
    "BiologicProduct", "BiologicProtectionAnswer", "PurpleBookLayoutError",
    "COLUMNS", "DOWNLOAD_URL", "HEADER_MARKER", "FULL_DATABASE_SECTION",
    "LICENSE_ORIGINATOR", "LICENSE_BIOSIMILAR", "LICENSE_INTERCHANGEABLE",
    "NOT_APPLICABLE_SMALL_MOLECULE", "NOT_APPLICABLE_NO_LICENCE",
    "NO_BIOSIMILARS_NOTE", "BIOSIMILAR_IS_NOT_GENERIC", "LIMITS", "NOT_SEARCHED",
    "parse_row", "check_layout", "human_date", "load_exclusivity_config",
]
