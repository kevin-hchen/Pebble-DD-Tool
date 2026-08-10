"""The deterministic device regulatory answer — three pathways, never collapsed.

WHY THIS IS A TYPED OBJECT AND NOT PROSE

Same reason as `drug_store.ApprovalAnswer`: every guard here is a guard in CODE,
and a model asked to summarise the underlying records walks past all of them in
one paraphrase. "No PMA matched" becomes "not FDA approved"; a De Novo
authorisation becomes "cleared as substantially equivalent"; a Class II PMA
becomes "Class III". `render_lines()` is the only thing that turns this into
text, and the memo inserts it as a fixed string. The model may write around it.

THE THREE PATHWAYS ARE THREE FACTS
==================================

  * 510(k) CLEARANCE — substantial equivalence to a legally marketed predicate.
    No clinical evidence of effectiveness is required.
  * De Novo AUTHORISATION — granted precisely BECAUSE no predicate exists. 482
    live records. Describing one as substantially equivalent to a predicate is a
    false statement about a company's regulatory history, and until now this
    tool made exactly that statement.
  * PMA APPROVAL — approval supported by clinical evidence of safety and
    effectiveness. 1,473 original applications.

A device with a PMA and a device with a 510(k) are not comparable, and this
object has no field that spans them. There is deliberately no `is_cleared_or_approved`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .client import Clearance510k
from .pma import (
    PATHWAY_510K,
    PATHWAY_DE_NOVO,
    PATHWAY_PMA,
    PMA_ABSENCE_MEANINGS,
    PMA_APPROVED,
    PMA_APPROVED_THEN_CHANGED,
    PMA_DECISION_UNDOCUMENTED,
    PMAApplication,
    PMARecord,
    group_applications,
)

#: Device sources this tool does NOT consult, declared on every device block.
NOT_SEARCHED = (
    "device registration and listing (330,251 records — who makes and markets a "
    "device today)",
    "UDI/GUDID (5.1M device identifiers)",
    "PMA post-approval study status",
)
NOT_SEARCHED_CAVEAT = (
    "openFDA also covers US submissions only, and a device authorised in the EU "
    "under MDR and nowhere else is absent from it by design."
)


@dataclass
class DeviceRegulatoryAnswer:
    """What the FDA record shows for a device, by pathway.

    `searched` False means no device store was available — never a finding about
    the device. `found_*` are per-pathway, because absence from one pathway says
    nothing about the others: most Class II devices have no PMA and that is
    normal, not a deficiency.
    """
    device: str
    searched: bool = False
    clearances: list[Clearance510k] = field(default_factory=list)
    de_novo: list[Clearance510k] = field(default_factory=list)
    pma_records: list[PMARecord] = field(default_factory=list)
    n_clearances_total: int = 0
    n_pma_total: int = 0            # rows, of which most are supplements
    n_pma_originals_total: int = 0
    product_codes: list[str] = field(default_factory=list)
    bulk_freshness: object | None = None      # BulkFreshness for device/pma
    absence_meanings: tuple[str, ...] = PMA_ABSENCE_MEANINGS

    # ------------------------------------------------------------ derived

    @property
    def applications(self) -> list[PMAApplication]:
        return group_applications(self.pma_records)

    @property
    def found_pma(self) -> bool:
        return bool(self.pma_records)

    @property
    def found_510k(self) -> bool:
        return bool(self.clearances)

    @property
    def found_de_novo(self) -> bool:
        return bool(self.de_novo)

    @property
    def found_anything(self) -> bool:
        return self.found_pma or self.found_510k or self.found_de_novo

    @property
    def approved_applications(self) -> list[PMAApplication]:
        """Original applications whose decision code the FDA documents as an
        approval. Requires positive evidence — an undocumented code is not one."""
        return [a for a in self.applications
                if a.approval_state in (PMA_APPROVED, PMA_APPROVED_THEN_CHANGED)]

    @property
    def has_pma_approval(self) -> bool:
        return self.searched and bool(self.approved_applications)

    @property
    def device_classes(self) -> list[str]:
        """Verbatim, across everything matched. Never inferred from the pathway:
        7,177 PMA records are Class 2."""
        seen = {r.device_class for r in self.pma_records if r.device_class}
        seen |= {c.device_class for c in self.clearances if c.device_class}
        return sorted(seen)

    # ------------------------------------------------------------ rendering

    def statement(self) -> str:
        if not self.searched:
            return (f"The FDA device record for “{self.device}” was NOT checked — no "
                    "openFDA device store is available. This is not a finding about "
                    "the device.")
        if not self.found_anything:
            reasons = "; ".join(self.absence_meanings)
            return (
                f"No FDA device record matching “{self.device}” was found. Absence here "
                f"says nothing either way about regulatory status: it is equally "
                f"consistent with {len(self.absence_meanings)} different situations — "
                f"{reasons}."
            )
        bits = []
        if self.found_510k:
            bits.append(f"{len(self.clearances)} of {self.n_clearances_total} 510(k) "
                        "clearance(s)")
        if self.found_de_novo:
            bits.append(f"{len(self.de_novo)} De Novo authorisation(s)")
        if self.found_pma:
            bits.append(f"{len(self.applications)} premarket approval application(s)")
        return (f"The FDA device record for “{self.device}” holds " + ", ".join(bits)
                + ". These are different regulatory pathways and are reported "
                  "separately below.")

    def axis_510k(self) -> str:
        if not self.found_510k:
            return ("510(k) clearance: no clearance matched. Most Class III devices "
                    "have none, because they take the approval route instead — this "
                    "is not a gap in the device's regulatory history.")
        return (f"510(k) clearance: {len(self.clearances)} of {self.n_clearances_total} "
                "shown. A 510(k) is a finding of substantial equivalence to a legally "
                "marketed predicate device; it does not require clinical evidence of "
                "effectiveness.")

    def axis_de_novo(self) -> str:
        if not self.found_de_novo:
            return ""      # silence, not a claim: De Novo is rare by construction
        nums = ", ".join(c.k_number for c in self.de_novo[:4])
        return (f"De Novo authorisation: {len(self.de_novo)} record(s) ({nums}). A De "
                "Novo is granted BECAUSE no predicate device exists, so these are NOT "
                "substantial-equivalence findings and must not be described as such.")

    def axis_pma(self) -> str:
        if not self.found_pma:
            return ("Premarket approval (PMA): no PMA application matched. This is "
                    "normal for a Class I or Class II device and is not a deficiency.")
        approved = self.approved_applications
        line = (f"Premarket approval (PMA): {len(self.applications)} application(s), "
                f"{self.n_pma_total} record(s) held of which most are supplements. ")
        if approved:
            dates = [a.approval_date for a in approved if a.approval_date]
            nums = ", ".join(a.pma_number for a in approved[:4])
            line += (f"{len(approved)} application(s) carry an FDA-documented approval "
                     f"decision ({nums}"
                     + (f", earliest {min(dates)}" if dates else "") + "). ")
        undocumented = [a for a in self.applications
                        if a.approval_state == PMA_DECISION_UNDOCUMENTED]
        if undocumented:
            line += (f"{len(undocumented)} carry a decision code the FDA data dictionary "
                     "does not define, so no approval is claimed for them. ")
        orphaned = [a for a in self.applications if not a.has_original_record]
        if orphaned:
            line += (f"{len(orphaned)} application(s) appear only as supplements in this "
                     "export — the original approval record is not in the copy held, so "
                     "its decision cannot be read from here. ")
        return line.rstrip()

    def axis_class(self) -> str:
        classes = self.device_classes
        if not classes:
            return "Device class: not stated on any matching record."
        return (f"Device class (verbatim, as filed): {', '.join(classes)}. Class is read "
                "from the record, never inferred from the pathway — 7,177 PMA records in "
                "this source are Class 2, so a PMA does not by itself mean Class III.")

    def coverage_lines(self) -> list[str]:
        lines = []
        if self.searched:
            fresh = self.bulk_freshness
            if fresh is not None:
                lines.extend(fresh.render_lines())
            else:
                lines.append("Searched: openFDA device clearances. Premarket approval "
                             "(device/pma) has NOT been downloaded for this store, so "
                             "the PMA pathway was not checked — that is a gap in the "
                             "search, not a finding about the device.")
        else:
            lines.append("Searched: nothing — no openFDA device store is available. "
                         "This is not a finding about the device.")
        lines.append("Not searched: " + "; ".join(NOT_SEARCHED) + ". " + NOT_SEARCHED_CAVEAT)
        return lines

    def render_lines(self) -> list[str]:
        """The ONLY function that turns this into prose. Markdown and PDF both
        call it verbatim, so the surfaces cannot drift, and the model never
        writes any of it."""
        lines = [self.statement()]
        if self.found_anything:
            lines.append(self.axis_510k())
            de_novo = self.axis_de_novo()
            if de_novo:
                lines.append(de_novo)
            lines.append(self.axis_pma())
            lines.append(self.axis_class())
        lines.extend(self.coverage_lines())
        return lines


def build_device_answer(store, device: str, product_code: str | None = None,
                        limit: int = 10) -> DeviceRegulatoryAnswer:
    """Assemble the answer from the device store. Never raises on an empty
    store: an absent store yields `searched=False`, which renders as "not
    checked" rather than as an absence of authorisations."""
    if store is None or not device:
        return DeviceRegulatoryAnswer(device=device or "the device")

    clearances = store.clearances(product_code=product_code, device_name=device,
                                  limit=limit)
    de_novo = store.de_novo_clearances(product_code=product_code, device_name=device,
                                       limit=limit)
    # De Novo rows are also clearances; showing them twice would double-count the
    # pathway they are being separated OUT of.
    de_novo_ids = {c.k_number for c in de_novo}
    clearances = [c for c in clearances if c.k_number not in de_novo_ids]

    codes = [product_code] if product_code else []
    if not codes:
        codes = store.product_codes_for_device(device, limit=5)

    pma_records = store.pma_records(device_name=device, limit=limit)
    if not pma_records and codes:
        for code in codes:
            pma_records += store.pma_records(product_code=code, limit=limit)

    return DeviceRegulatoryAnswer(
        device=device,
        searched=True,
        clearances=clearances,
        de_novo=de_novo,
        pma_records=pma_records,
        n_clearances_total=store.clearances_total(product_code=product_code,
                                                  device_name=device),
        n_pma_total=store.pma_total(device_name=device)
        or sum(store.pma_total(product_code=c) for c in codes),
        n_pma_originals_total=store.pma_total(device_name=device, originals_only=True)
        or sum(store.pma_total(product_code=c, originals_only=True) for c in codes),
        product_codes=[c for c in codes if c],
        bulk_freshness=store.bulk_freshness("device/pma"),
    )


__all__ = ["DeviceRegulatoryAnswer", "build_device_answer",
           "PATHWAY_510K", "PATHWAY_DE_NOVO", "PATHWAY_PMA",
           "NOT_SEARCHED", "NOT_SEARCHED_CAVEAT"]
