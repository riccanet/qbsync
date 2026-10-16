"""qbXML request building and response parsing.

QuickBooks Desktop speaks qbXML: an XML document of query requests (``*QueryRq``)
answered by responses (``*QueryRs``) holding ``*Ret`` records. This module turns
those records into plain JSON-friendly dicts.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

# Child elements that are repeating collections even when only one is present,
# so consumers always get a list (e.g. invoice lines).
ALWAYS_LIST_SUFFIXES = (
    "LineRet",
    "LineGroupRet",
    "LinkedTxn",
    "DataExtRet",
    "AppliedToTxnRet",
    "ContactsRet",
    "AdditionalContactRef",
    "AdditionalNotesRet",
    "JournalDebitLine",
    "JournalCreditLine",
)

# Leaf elements whose values are numbers. Anything that fails to parse stays a string.
NUMERIC_SUFFIXES = (
    "Amount",
    "Balance",
    "BalanceRemaining",
    "Price",
    "Quantity",
    "Cost",
    "Total",
    "Subtotal",
    "Percent",
    "Percentage",
    "Rate",
    "CreditLimit",
    "Sublevel",
    "OnHand",
    "OnOrder",
    "OnSalesOrder",
    "ReorderPoint",
    "UnusedPayment",
    "UnusedCredits",
)

_INVALID_RAW = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_INVALID_REF = re.compile(
    r"&#(?:x0*(?:[0-8bcef]|1[0-9a-f])|0*(?:[0-8]|1[124-9]|2[0-9]|3[01]));", re.IGNORECASE
)
_INT = re.compile(r"-?\d+")


class QBRequestError(Exception):
    """QuickBooks rejected a request (non-zero status that is not 'no match')."""

    def __init__(self, request: str, code: int, message: str):
        super().__init__(f"{request}: QuickBooks status {code}: {message}")
        self.request = request
        self.code = code
        self.message = message


def sub(parent: ET.Element, tag: str, text: Any = None) -> ET.Element:
    """Append a child element (qbXML is order-sensitive, so call in schema order)."""
    elem = ET.SubElement(parent, tag)
    if text is not None:
        elem.text = str(text)
    return elem


def build_request(rq: ET.Element, version: str, on_error: str = "stopOnError") -> str:
    root = ET.Element("QBXML")
    msgs = ET.SubElement(root, "QBXMLMsgsRq", onError=on_error)
    msgs.append(rq)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<?qbxml version="{version}"?>\n' + ET.tostring(root, encoding="unicode")
    )


def sanitize(xml_text: str) -> str:
    """Drop control characters QuickBooks occasionally lets into memos/notes."""
    return _INVALID_REF.sub("", _INVALID_RAW.sub("", xml_text))


def _convert(tag: str, text: str | None) -> Any:
    if text is None:
        return ""
    if text in ("true", "false"):
        return text == "true"
    if tag.endswith(NUMERIC_SUFFIXES):
        try:
            return int(text) if _INT.fullmatch(text) else float(text)
        except ValueError:
            return text
    return text


def element_to_dict(elem: ET.Element) -> dict[str, Any]:
    out: dict[str, Any] = {}
    repeated: set[str] = set()
    for child in elem:
        tag = child.tag
        value = element_to_dict(child) if len(child) else _convert(tag, child.text)
        if tag.endswith(ALWAYS_LIST_SUFFIXES):
            out.setdefault(tag, []).append(value)
        elif tag in repeated:
            out[tag].append(value)
        elif tag in out:
            out[tag] = [out[tag], value]
            repeated.add(tag)
        else:
            out[tag] = value
    return out


@dataclass
class QueryResult:
    tag: str
    status_code: int
    status_severity: str
    status_message: str
    rets: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    iterator_id: str | None = None
    iterator_remaining: int = 0
    element: ET.Element | None = None

    @property
    def ok(self) -> bool:
        # 1 = "query did not find a matching object" which just means an empty result.
        return self.status_code in (0, 1) or self.status_severity.lower() in ("info", "warn")

    def raise_for_status(self) -> None:
        if not self.ok:
            raise QBRequestError(self.tag, self.status_code, self.status_message)


def parse_response(xml_text: str) -> list[QueryResult]:
    root = ET.fromstring(sanitize(xml_text))
    msgs = root.find("QBXMLMsgsRs")
    if msgs is None:
        raise ValueError("Response has no QBXMLMsgsRs element")
    results = []
    for rs in msgs:
        remaining = rs.get("iteratorRemainingCount")
        results.append(
            QueryResult(
                tag=rs.tag,
                status_code=int(rs.get("statusCode", "0")),
                status_severity=rs.get("statusSeverity", "Info"),
                status_message=rs.get("statusMessage", ""),
                rets=[(c.tag, element_to_dict(c)) for c in rs if c.tag.endswith("Ret")],
                iterator_id=rs.get("iteratorID"),
                iterator_remaining=int(remaining) if remaining else 0,
                element=rs,
            )
        )
    return results


_NUMERIC_COL_TYPES = {"AMTTYPE", "PERCENTTYPE", "QUANTYPE", "PRICETYPE", "INTTYPE"}


def parse_report(rs: ET.Element) -> dict[str, Any]:
    """Flatten a ``ReportRet`` (values live in attributes) into rows keyed by column title."""
    ret = rs.find("ReportRet")
    if ret is None:
        return {"title": None, "columns": [], "rows": []}
    columns: dict[str, dict[str, str]] = {}
    for cd in ret.findall("ColDesc"):
        titles = [t.get("value") for t in cd.findall("ColTitle") if t.get("value")]
        col_type = cd.findtext("ColType") or ""
        columns[cd.get("colID", "")] = {
            "title": " ".join(titles) or col_type or f"col{cd.get('colID')}",
            "type": col_type,
            "data_type": cd.get("dataType", ""),
        }
    rows = []
    data = ret.find("ReportData")
    for row in data if data is not None else []:
        row_type = row.tag[: -len("Row")].lower() if row.tag.endswith("Row") else row.tag
        rd = row.find("RowData")
        cols = row.findall("ColData")
        label = row.get("value") or (rd.get("value") if rd is not None else None)
        values: dict[str, Any] = {}
        for i, cd in enumerate(cols):
            col = columns.get(cd.get("colID", ""), {"title": cd.get("colID"), "data_type": ""})
            raw = cd.get("value")
            if i == 0 and col.get("type") == "Label":
                label = raw or label
                continue
            if raw is not None and col.get("data_type") in _NUMERIC_COL_TYPES:
                try:
                    raw = float(raw.replace(",", "").rstrip("%"))
                except ValueError:
                    pass
            values[col["title"]] = raw
        rows.append(
            {
                "type": row_type,
                "row_type": rd.get("rowType") if rd is not None else None,
                "label": label,
                "values": values,
            }
        )
    return {
        "title": ret.findtext("ReportTitle"),
        "subtitle": ret.findtext("ReportSubtitle"),
        "basis": ret.findtext("ReportBasis"),
        "columns": [c["title"] for c in columns.values()],
        "rows": rows,
    }
