"""Which QuickBooks objects get synced, and how to query each one.

qbXML element order matters and not every query supports every element, so each
entity carries flags for what its query accepts. Anything here can be overridden
per entity in the config file (``sync.entities``).
"""
from __future__ import annotations

import dataclasses
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

from .qbxml import sub

LIST = "list"
TXN = "txn"


@dataclass(frozen=True)
class Entity:
    name: str
    kind: str  # LIST or TXN
    iterator: bool = True  # supports iterator="Start"/"Continue" paging
    active_status: bool = True  # list query accepts <ActiveStatus>All</ActiveStatus>
    owner_id: bool = False  # accepts <OwnerID>0</OwnerID> (returns custom fields)
    include_line_items: bool = False
    include_linked_txns: bool = False
    deleted_types: tuple[str, ...] = ()  # ListDelType / TxnDelType values
    enabled: bool = True

    @property
    def query_rq(self) -> str:
        return f"{self.name}QueryRq"

    @property
    def id_field(self) -> str:
        return "ListID" if self.kind == LIST else "TxnID"


def _list(name: str, **kw: Any) -> Entity:
    kw.setdefault("deleted_types", (name,))
    return Entity(name, LIST, **kw)


def _txn(name: str, **kw: Any) -> Entity:
    kw.setdefault("deleted_types", (name,))
    return Entity(name, TXN, **kw)


ITEM_DELETE_TYPES = (
    "ItemDiscount",
    "ItemFixedAsset",
    "ItemGroup",
    "ItemInventory",
    "ItemInventoryAssembly",
    "ItemNonInventory",
    "ItemOtherCharge",
    "ItemPayment",
    "ItemSalesTax",
    "ItemSalesTaxGroup",
    "ItemService",
    "ItemSubtotal",
)

DEFAULT_ENTITIES: list[Entity] = [
    # Small reference lists: no iterator support needed.
    _list("Account", iterator=False, owner_id=True),
    _list("Class", iterator=False),
    _list("StandardTerms", iterator=False),
    _list("PaymentMethod", iterator=False),
    _list("ShipMethod", iterator=False),
    _list("SalesRep", iterator=False),
    _list("SalesTaxCode", iterator=False),
    _list("CustomerType", iterator=False),
    _list("VendorType", iterator=False),
    _list("Employee", iterator=False, owner_id=True),
    _list("OtherName", iterator=False, owner_id=True),
    # Big lists: paged with iterators.
    _list("Customer", owner_id=True),
    _list("Vendor", owner_id=True),
    _list("Item", owner_id=True, deleted_types=ITEM_DELETE_TYPES),
    # Transactions.
    _txn("Invoice", owner_id=True, include_line_items=True, include_linked_txns=True),
    _txn("SalesReceipt", owner_id=True, include_line_items=True),
    _txn("ReceivePayment", owner_id=True, include_line_items=True),
    _txn("CreditMemo", owner_id=True, include_line_items=True, include_linked_txns=True),
    _txn("Estimate", owner_id=True, include_line_items=True, include_linked_txns=True),
    _txn("SalesOrder", owner_id=True, include_line_items=True, include_linked_txns=True),
    _txn("Bill", owner_id=True, include_line_items=True, include_linked_txns=True),
    _txn("BillPaymentCheck", owner_id=True, include_line_items=True),
    _txn("BillPaymentCreditCard", owner_id=True, include_line_items=True),
    _txn("Check", owner_id=True, include_line_items=True, include_linked_txns=True),
    _txn("CreditCardCharge", owner_id=True, include_line_items=True),
    _txn("CreditCardCredit", owner_id=True, include_line_items=True),
    _txn("Deposit", owner_id=True, include_line_items=True),
    _txn("JournalEntry", owner_id=True, include_line_items=True),
    _txn("PurchaseOrder", owner_id=True, include_line_items=True, include_linked_txns=True),
    _txn("ItemReceipt", owner_id=True, include_line_items=True, include_linked_txns=True),
    _txn("VendorCredit", owner_id=True, include_line_items=True, include_linked_txns=True),
    _txn("InventoryAdjustment", include_line_items=True),
    _txn("TimeTracking"),
]


def resolve_entities(
    overrides: dict[str, dict[str, Any]] | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[Entity]:
    """Apply config overrides; unknown names in ``overrides`` define new entities."""
    overrides = dict(overrides or {})
    result = []
    for ent in DEFAULT_ENTITIES:
        ov = overrides.pop(ent.name, None)
        if ov:
            ov = {k: tuple(v) if k == "deleted_types" else v for k, v in ov.items()}
            ent = dataclasses.replace(ent, **ov)
        result.append(ent)
    for name, ov in overrides.items():
        kind = ov.pop("kind", TXN)
        if "deleted_types" in ov:
            ov["deleted_types"] = tuple(ov["deleted_types"])
        result.append(Entity(name, kind, **ov))
    if include:
        wanted = {n.lower() for n in include}
        result = [e for e in result if e.name.lower() in wanted]
    if exclude:
        unwanted = {n.lower() for n in exclude}
        result = [e for e in result if e.name.lower() not in unwanted]
    return [e for e in result if e.enabled]


def build_query(
    ent: Entity,
    *,
    page_size: int,
    from_modified: str | None = None,
    from_txn_date: str | None = None,
    iterator_id: str | None = None,
) -> ET.Element:
    rq = ET.Element(ent.query_rq, requestID="1")
    if ent.iterator:
        if iterator_id:
            rq.set("iterator", "Continue")
            rq.set("iteratorID", iterator_id)
        else:
            rq.set("iterator", "Start")
        sub(rq, "MaxReturned", page_size)
    if ent.kind == LIST:
        if ent.active_status:
            sub(rq, "ActiveStatus", "All")
        if from_modified:
            sub(rq, "FromModifiedDate", from_modified)
    else:
        if from_modified:
            sub(sub(rq, "ModifiedDateRangeFilter"), "FromModifiedDate", from_modified)
        elif from_txn_date:
            sub(sub(rq, "TxnDateRangeFilter"), "FromTxnDate", from_txn_date)
        if ent.include_line_items:
            sub(rq, "IncludeLineItems", "true")
        if ent.include_linked_txns:
            sub(rq, "IncludeLinkedTxns", "true")
    if ent.owner_id:
        sub(rq, "OwnerID", 0)
    return rq


def build_deleted_query(kind: str, del_types: list[str], from_deleted: str) -> ET.Element:
    prefix = "List" if kind == LIST else "Txn"
    rq = ET.Element(f"{prefix}DeletedQueryRq", requestID="1")
    for t in del_types:
        sub(rq, f"{prefix}DelType", t)
    sub(sub(rq, "DeletedDateRangeFilter"), "FromDeletedDate", from_deleted)
    return rq


def build_report_query(report: dict[str, Any]) -> ET.Element:
    """``report`` keys: type, and either macro or from/to dates; optional summarize_by, basis."""
    rq = ET.Element("GeneralSummaryReportQueryRq", requestID="1")
    sub(rq, "GeneralSummaryReportType", report["type"])
    if report.get("from") or report.get("to"):
        period = sub(rq, "ReportPeriod")
        if report.get("from"):
            sub(period, "FromReportDate", report["from"])
        if report.get("to"):
            sub(period, "ToReportDate", report["to"])
    elif report.get("macro"):
        sub(rq, "ReportDateMacro", report["macro"])
    if report.get("summarize_by"):
        sub(rq, "SummarizeColumnsBy", report["summarize_by"])
    if report.get("basis"):
        sub(rq, "ReportBasis", report["basis"])
    return rq
