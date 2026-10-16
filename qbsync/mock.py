"""A fake QuickBooks that answers qbXML queries from generated sample data.

Used by the test suite and by ``connection: mock`` so the sync and push sinks
can be tried on any OS without QuickBooks. Supports iterators,
modified-date filters, ActiveStatus, deleted-object queries and one report.
"""
from __future__ import annotations

import random
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any

from .sync import parse_ts

TZ = timezone(timedelta(hours=-5))
LIST_ENTITIES = {"Account", "Customer", "Vendor", "Employee", "Item", "Class", "StandardTerms"}


def _xml(parent: ET.Element, tag: str, value: Any) -> None:
    if isinstance(value, list):
        for v in value:
            _xml(parent, tag, v)
        return
    el = ET.SubElement(parent, tag)
    if isinstance(value, dict):
        for k, v in value.items():
            _xml(el, k, v)
    elif isinstance(value, bool):
        el.text = "true" if value else "false"
    elif isinstance(value, float):
        el.text = f"{value:.2f}"
    else:
        el.text = str(value)


class MockQuickBooks:
    qbxml_version = "13.0"

    def __init__(self, seed: int = 7, customers: int = 37, invoices: int = 60):
        self.rng = random.Random(seed)
        self.clock = datetime(2026, 1, 5, 9, 0, 0, tzinfo=TZ)
        self.data: dict[str, dict[str, tuple[str, dict[str, Any]]]] = {}
        self.deleted: list[dict[str, Any]] = []
        self.iterators: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        self.requests: list[str] = []
        self.is_open = False
        self._seq = 0
        self._generate(customers, invoices)

    # ---- connection protocol ------------------------------------------------

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    def process(self, request_xml: str) -> str:
        self.requests.append(request_xml)
        root = ET.fromstring(request_xml)
        msgs = root.find("QBXMLMsgsRq")
        out = ET.Element("QBXML")
        rs_parent = ET.SubElement(out, "QBXMLMsgsRs")
        for rq in msgs:
            rs_parent.append(self._handle(rq))
        return '<?xml version="1.0" ?>\n' + ET.tostring(out, encoding="unicode")

    # ---- data generation ----------------------------------------------------

    def tick(self, minutes: int = 1) -> str:
        self.clock += timedelta(minutes=minutes)
        return self.clock.isoformat()

    def _id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq:04X}-1700000000"

    def _add(self, entity: str, ret_type: str, data: dict[str, Any]) -> dict[str, Any]:
        id_field = "ListID" if entity in LIST_ENTITIES else "TxnID"
        now = self.tick()
        record = {id_field: self._id("80000" if id_field == "ListID" else "1A"), "TimeCreated": now,
                  "TimeModified": now, "EditSequence": "1700000001", **data}
        self.data.setdefault(entity, {})[record[id_field]] = (ret_type, record)
        return record

    def _generate(self, n_customers: int, n_invoices: int) -> None:
        r = self.rng
        accounts = {}
        for name, typ, num in [
            ("Checking", "Bank", "1000"), ("Accounts Receivable", "AccountsReceivable", "1200"),
            ("Accounts Payable", "AccountsPayable", "2000"), ("Services Income", "Income", "4000"),
            ("Product Sales", "Income", "4100"), ("Rent Expense", "Expense", "6100"),
            ("Software Subscriptions", "Expense", "6200"), ("Utilities", "Expense", "6300"),
        ]:
            accounts[name] = self._add("Account", "AccountRet", {
                "Name": name, "FullName": name, "IsActive": True, "Sublevel": 0, "AccountType": typ,
                "AccountNumber": num, "Balance": 0.0, "TotalBalance": 0.0})
        for name in ("Net 30", "Due on receipt"):
            self._add("StandardTerms", "StandardTermsRet", {"Name": name, "IsActive": True,
                                                            "StdDueDays": 30 if "30" in name else 0})
        for name in ("Managed Services", "Projects"):
            self._add("Class", "ClassRet", {"Name": name, "FullName": name, "IsActive": True, "Sublevel": 0})
        for i, name in enumerate(("Pat Rivera", "Sam Okafor", "Lee Chen")):
            self._add("Employee", "EmployeeRet", {"Name": name, "IsActive": True, "Email": f"emp{i}@example.com"})

        items = []
        for name, ret, price in [
            ("Managed Services - Per User", "ItemServiceRet", 125.0),
            ("Onsite Labor", "ItemServiceRet", 150.0), ("Remote Labor", "ItemServiceRet", 110.0),
            ("Project Hours", "ItemServiceRet", 165.0), ("Backup - Per Device", "ItemServiceRet", 25.0),
            ("Firewall Appliance", "ItemInventoryRet", 899.0), ("Network Switch 24p", "ItemInventoryRet", 649.0),
            ("Wireless AP", "ItemInventoryRet", 229.0), ("Patch Cables", "ItemNonInventoryRet", 6.5),
            ("Microsoft 365 Business Premium", "ItemNonInventoryRet", 22.0),
        ]:
            d = {"Name": name, "FullName": name, "IsActive": True, "Sublevel": 0}
            if ret == "ItemInventoryRet":
                d.update({"SalesPrice": price, "PurchaseCost": round(price * 0.62, 2),
                          "QuantityOnHand": r.randint(0, 40),
                          "IncomeAccountRef": {"FullName": "Product Sales"}})
            else:
                d["SalesOrPurchase"] = {"Price": price, "AccountRef": {"FullName": "Services Income"}}
            items.append((ret, self._add("Item", ret, d)))

        companies = ["Acme Dental", "Birch & Co CPAs", "Cedar Logistics", "Delta Veterinary", "Evergreen Realty",
                     "Fulton Law Group", "Granite Manufacturing", "Harbor Clinic", "Ironwood Builders",
                     "Juniper Schools", "Keystone Insurance", "Lakeside Church", "Maple Orthodontics",
                     "Northstar Credit Union", "Oakridge Pharmacy", "Pinnacle Engineering", "Quarry Brewing"]
        customers = []
        for i in range(n_customers):
            base = companies[i % len(companies)]
            name = base if i < len(companies) else f"{base} - Site {i // len(companies) + 1}"
            customers.append(self._add("Customer", "CustomerRet", {
                "Name": name, "FullName": name, "IsActive": i % 11 != 10, "Sublevel": 0, "CompanyName": name,
                "BillAddress": {"Addr1": f"{100 + i} Main St", "City": "Springfield", "State": "IL",
                                "PostalCode": f"627{i:02d}"},
                "Phone": f"555-01{i:02d}", "Email": f"billing{i}@example.com",
                "TermsRef": {"FullName": "Net 30"}, "Balance": 0.0, "TotalBalance": 0.0,
                "DataExtRet": [{"OwnerID": "0", "DataExtName": "Contract Tier",
                                "DataExtType": "STR255TYPE", "DataExtValue": r.choice(["Gold", "Silver", "Bronze"])}],
            }))
        vendors = [self._add("Vendor", "VendorRet", {"Name": v, "IsActive": True, "Balance": 0.0, "CompanyName": v})
                   for v in ("Pax8", "Ingram Micro", "Comcast Business", "Springfield Properties",
                             "Dell Technologies", "Ubiquiti", "Hudu", "SentinelOne")]

        start = datetime(2025, 1, 6)
        for n in range(n_invoices):
            cust = customers[r.randrange(len(customers))]
            lines = []
            for _ in range(r.randint(1, 3)):
                ret, item = items[r.randrange(len(items))]
                qty = r.randint(1, 20)
                price = item.get("SalesPrice") or item["SalesOrPurchase"]["Price"]
                lines.append({"TxnLineID": self._id("L"), "ItemRef": {"ListID": item["ListID"],
                              "FullName": item["FullName"]}, "Desc": item["Name"], "Quantity": qty,
                              "Rate": price, "Amount": round(qty * price, 2)})
            subtotal = round(sum(l["Amount"] for l in lines), 2)
            paid = r.random() < 0.6
            txn_date = (start + timedelta(days=n * 5)).date()
            inv = self._add("Invoice", "InvoiceRet", {
                "TxnNumber": 1000 + n, "CustomerRef": {"ListID": cust["ListID"], "FullName": cust["FullName"]},
                "ARAccountRef": {"FullName": "Accounts Receivable"}, "TxnDate": txn_date.isoformat(),
                "RefNumber": str(5000 + n), "DueDate": (txn_date + timedelta(days=30)).isoformat(),
                "TermsRef": {"FullName": "Net 30"}, "Subtotal": subtotal, "SalesTaxTotal": 0.0,
                "AppliedAmount": -subtotal if paid else 0.0, "BalanceRemaining": 0.0 if paid else subtotal,
                "IsPaid": paid, "Memo": "Monthly services" if n % 2 else "Hardware & labor",
                "InvoiceLineRet": lines,
            })
            if paid:
                self._add("ReceivePayment", "ReceivePaymentRet", {
                    "CustomerRef": inv["CustomerRef"], "TxnDate": (txn_date + timedelta(days=12)).isoformat(),
                    "RefNumber": f"CHK{7000 + n}", "TotalAmount": subtotal, "UnusedPayment": 0.0,
                    "UnusedCredits": 0.0, "PaymentMethodRef": {"FullName": "Check"},
                    "AppliedToTxnRet": [{"TxnID": inv["TxnID"], "TxnType": "Invoice", "RefNumber": inv["RefNumber"],
                                         "BalanceRemaining": 0.0, "Amount": subtotal}],
                })
        for n in range(20):
            vend = vendors[n % len(vendors)]
            amt = round(r.uniform(80, 4200), 2)
            txn_date = (start + timedelta(days=n * 15)).date()
            self._add("Bill", "BillRet", {
                "VendorRef": {"ListID": vend["ListID"], "FullName": vend["Name"]},
                "APAccountRef": {"FullName": "Accounts Payable"}, "TxnDate": txn_date.isoformat(),
                "DueDate": (txn_date + timedelta(days=30)).isoformat(), "RefNumber": f"B-{300 + n}",
                "AmountDue": amt, "OpenAmount": 0.0 if n < 14 else amt, "IsPaid": n < 14,
                "ExpenseLineRet": [{"TxnLineID": self._id("L"), "AccountRef": {"FullName": "Software Subscriptions"},
                                    "Amount": amt, "Memo": "Monthly"}],
            })
        for n in range(6):
            amt = round(r.uniform(150, 3000), 2)
            self._add("Check", "CheckRet", {
                "AccountRef": {"FullName": "Checking"}, "PayeeEntityRef": {"FullName": "Springfield Properties"},
                "RefNumber": str(2100 + n), "TxnDate": (start + timedelta(days=30 * n)).date().isoformat(),
                "Amount": amt, "Memo": "Office rent",
                "ExpenseLineRet": [{"TxnLineID": self._id("L"), "AccountRef": {"FullName": "Rent Expense"},
                                    "Amount": amt}],
            })

    # ---- mutation helpers for tests/demo -----------------------------------

    def modify(self, entity: str, rec_id: str, **fields: Any) -> dict[str, Any]:
        _, rec = self.data[entity][rec_id]
        rec.update(fields)
        rec["TimeModified"] = self.tick()
        rec["EditSequence"] = str(int(rec["EditSequence"]) + 1)
        return rec

    def delete(self, entity: str, rec_id: str) -> None:
        ret_type, rec = self.data[entity].pop(rec_id)
        is_list = entity in LIST_ENTITIES
        self.deleted.append({
            "kind": "list" if is_list else "txn", "type": ret_type[: -len("Ret")], "id": rec_id,
            "TimeCreated": rec["TimeCreated"], "TimeDeleted": self.tick(),
            "name": rec.get("FullName") or rec.get("Name") or rec.get("RefNumber"),
        })

    # ---- request handling -----------------------------------------------------

    def _rs(self, rq: ET.Element, code: int = 0, message: str = "Status OK") -> ET.Element:
        rs = ET.Element(rq.tag[:-2] + "Rs", requestID=rq.get("requestID", "1"), statusCode=str(code),
                        statusSeverity="Info" if code in (0, 1) else "Error", statusMessage=message)
        return rs

    def _handle(self, rq: ET.Element) -> ET.Element:
        tag = rq.tag
        if tag in ("ListDeletedQueryRq", "TxnDeletedQueryRq"):
            return self._deleted(rq)
        if tag == "GeneralSummaryReportQueryRq":
            return self._report(rq)
        entity = tag[: -len("QueryRq")]
        if rq.get("iterator") == "Continue":
            pending = self.iterators.get(rq.get("iteratorID", ""))
            if pending is None:
                return self._rs(rq, 3170, "iterator not found")
        else:
            pending = self._select(entity, rq)
        max_returned = int(rq.findtext("MaxReturned") or 10**9)
        page, rest = pending[:max_returned], pending[max_returned:]
        rs = self._rs(rq, 0 if page else 1, "Status OK" if page else "A query request did not find a matching object")
        if rq.get("iterator"):
            it_id = rq.get("iteratorID") or "{" + str(uuid.uuid4()) + "}"
            self.iterators[it_id] = rest
            rs.set("iteratorRemainingCount", str(len(rest)))
            rs.set("iteratorID", it_id)
        for ret_type, rec in page:
            _xml(rs, ret_type, self._render(rec, rq))
        return rs

    def _render(self, rec: dict[str, Any], rq: ET.Element) -> dict[str, Any]:
        out = dict(rec)
        if rq.findtext("IncludeLineItems") != "true":
            for k in [k for k in out if k.endswith("LineRet") or k == "AppliedToTxnRet"]:
                del out[k]
        if rq.findtext("OwnerID") is None:
            out.pop("DataExtRet", None)
        return out

    def _select(self, entity: str, rq: ET.Element) -> list[tuple[str, dict[str, Any]]]:
        rows = list(self.data.get(entity, {}).values())
        from_mod = rq.findtext("FromModifiedDate") or rq.findtext("ModifiedDateRangeFilter/FromModifiedDate")
        if from_mod:
            cutoff = parse_ts(from_mod)
            rows = [r for r in rows if parse_ts(r[1]["TimeModified"]) >= cutoff]
        from_txn = rq.findtext("TxnDateRangeFilter/FromTxnDate")
        if from_txn:
            rows = [r for r in rows if r[1].get("TxnDate", "") >= from_txn]
        status = rq.findtext("ActiveStatus") or "ActiveOnly"
        if entity in LIST_ENTITIES and status != "All":
            want = status == "ActiveOnly"
            rows = [r for r in rows if r[1].get("IsActive", True) == want]
        return rows

    def _deleted(self, rq: ET.Element) -> ET.Element:
        kind = "list" if rq.tag.startswith("List") else "txn"
        prefix = "List" if kind == "list" else "Txn"
        types = {e.text for e in rq.findall(f"{prefix}DelType")}
        cutoff = parse_ts(rq.findtext("DeletedDateRangeFilter/FromDeletedDate"))
        hits = [d for d in self.deleted if d["kind"] == kind and d["type"] in types
                and parse_ts(d["TimeDeleted"]) >= cutoff]
        rs = self._rs(rq, 0 if hits else 1)
        for d in hits:
            ret = {f"{prefix}DelType": d["type"], f"{prefix}ID": d["id"], "TimeCreated": d["TimeCreated"],
                   "TimeDeleted": d["TimeDeleted"]}
            ret["FullName" if kind == "list" else "RefNumber"] = d["name"]
            _xml(rs, f"{prefix}DeletedRet", ret)
        return rs

    def _report(self, rq: ET.Element) -> ET.Element:
        rtype = rq.findtext("GeneralSummaryReportType")
        if rtype != "ProfitAndLossStandard":
            return self._rs(rq, 1)
        income = sum(rec["Subtotal"] for _, rec in self.data.get("Invoice", {}).values())
        expense = sum(rec["AmountDue"] for _, rec in self.data.get("Bill", {}).values())
        expense += sum(rec["Amount"] for _, rec in self.data.get("Check", {}).values())
        rs = self._rs(rq)
        ret = ET.SubElement(rs, "ReportRet")
        ET.SubElement(ret, "ReportTitle").text = "Profit & Loss"
        ET.SubElement(ret, "ReportSubtitle").text = "January 1 through October 16, 2026"
        ET.SubElement(ret, "ReportBasis").text = "Accrual"
        for col_id, title, ctype, dtype in (("1", None, "Label", "STRTYPE"), ("2", "TOTAL", "Amount", "AMTTYPE")):
            cd = ET.SubElement(ret, "ColDesc", colID=col_id, dataType=dtype)
            ct = ET.SubElement(cd, "ColTitle", titleRow="1")
            if title:
                ct.set("value", title)
            ET.SubElement(cd, "ColType").text = ctype
        data = ET.SubElement(ret, "ReportData")
        ET.SubElement(data, "TextRow", rowNumber="1", value="Ordinary Income/Expense")
        rows = [("DataRow", "Services Income", income), ("SubtotalRow", "Total Income", income),
                ("DataRow", "Rent & Subscriptions", expense), ("SubtotalRow", "Total Expense", expense),
                ("TotalRow", "Net Income", income - expense)]
        for i, (tag, label, amt) in enumerate(rows, start=2):
            row = ET.SubElement(data, tag, rowNumber=str(i))
            if tag != "TotalRow":
                ET.SubElement(row, "RowData", rowType="account", value=label)
            ET.SubElement(row, "ColData", colID="1", value=label)
            ET.SubElement(row, "ColData", colID="2", value=f"{amt:.2f}")
        return rs
