import xml.etree.ElementTree as ET

from qbsync.entities import DEFAULT_ENTITIES, build_query
from qbsync.qbxml import build_request, parse_report, parse_response

RESPONSE = """<?xml version="1.0" ?>
<QBXML><QBXMLMsgsRs>
<InvoiceQueryRs requestID="1" statusCode="0" statusSeverity="Info" statusMessage="Status OK"
    iteratorRemainingCount="3" iteratorID="{abc}">
  <InvoiceRet>
    <TxnID>1-1</TxnID><TimeModified>2026-01-01T10:00:00-05:00</TimeModified>
    <RefNumber>00123</RefNumber><IsPaid>false</IsPaid><Subtotal>1500.00</Subtotal>
    <Memo>bad &#x1; char</Memo><Other/>
    <CustomerRef><ListID>80-1</ListID><FullName>Acme</FullName></CustomerRef>
    <InvoiceLineRet><TxnLineID>L1</TxnLineID><Quantity>3</Quantity><Rate>500.00</Rate></InvoiceLineRet>
  </InvoiceRet>
</InvoiceQueryRs>
</QBXMLMsgsRs></QBXML>"""


def test_parse_response_types_and_lists():
    res = parse_response(RESPONSE)[0]
    assert res.ok and res.iterator_id == "{abc}" and res.iterator_remaining == 3
    tag, inv = res.rets[0]
    assert tag == "InvoiceRet"
    assert inv["RefNumber"] == "00123"  # identifiers stay strings
    assert inv["IsPaid"] is False and inv["Subtotal"] == 1500.0
    assert inv["Memo"] == "bad  char" and inv["Other"] == ""
    assert isinstance(inv["InvoiceLineRet"], list)  # single line still a list
    assert inv["InvoiceLineRet"][0]["Quantity"] == 3


def test_build_query_element_order():
    inv = next(e for e in DEFAULT_ENTITIES if e.name == "Invoice")
    rq = build_query(inv, page_size=100, from_modified="2026-01-01T00:00:00-05:00")
    assert [c.tag for c in rq] == ["MaxReturned", "ModifiedDateRangeFilter", "IncludeLineItems",
                                   "IncludeLinkedTxns", "OwnerID"]
    assert rq.get("iterator") == "Start"
    cont = build_query(inv, page_size=100, iterator_id="{x}")
    assert cont.get("iterator") == "Continue" and cont.get("iteratorID") == "{x}"
    cust = next(e for e in DEFAULT_ENTITIES if e.name == "Customer")
    assert [c.tag for c in build_query(cust, page_size=5, from_modified="x")] == [
        "MaxReturned", "ActiveStatus", "FromModifiedDate", "OwnerID"]
    acct = next(e for e in DEFAULT_ENTITIES if e.name == "Account")
    assert build_query(acct, page_size=5).get("iterator") is None
    xml = build_request(rq, "13.0")
    assert '<?qbxml version="13.0"?>' in xml and 'onError="stopOnError"' in xml


def test_parse_report():
    rs = ET.fromstring("""<GeneralSummaryReportQueryRs statusCode="0"><ReportRet>
      <ReportTitle>Profit &amp; Loss</ReportTitle>
      <ColDesc colID="1" dataType="STRTYPE"><ColTitle titleRow="1"/><ColType>Label</ColType></ColDesc>
      <ColDesc colID="2" dataType="AMTTYPE"><ColTitle titleRow="1" value="TOTAL"/><ColType>Amount</ColType></ColDesc>
      <ReportData>
        <TextRow rowNumber="1" value="Income"/>
        <DataRow rowNumber="2"><RowData rowType="account" value="Sales"/>
          <ColData colID="1" value="Sales"/><ColData colID="2" value="1,234.50"/></DataRow>
      </ReportData></ReportRet></GeneralSummaryReportQueryRs>""")
    rep = parse_report(rs)
    assert rep["title"] == "Profit & Loss" and rep["columns"] == ["Label", "TOTAL"]
    assert rep["rows"][0] == {"type": "text", "row_type": None, "label": "Income", "values": {}}
    assert rep["rows"][1]["label"] == "Sales" and rep["rows"][1]["values"] == {"TOTAL": 1234.5}
