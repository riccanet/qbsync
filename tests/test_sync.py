def test_full_then_incremental(service, qb):
    report = service.run_once()
    assert report.status == "ok", report
    counts = service.store.entity_counts()
    assert counts["Customer"]["count"] == 37  # includes inactive, paged in 10s
    assert counts["Invoice"]["count"] == 60
    assert counts["Item"]["count"] == 10
    # paging really used the iterator
    assert sum('iterator="Continue"' in r for r in qb.requests) >= 3

    inv = service.store.query("Invoice", limit=1)["records"][0]
    assert inv["data"]["InvoiceLineRet"] and inv["party"]
    cust = service.store.query("Customer", search="Acme Dental")["records"][0]
    assert cust["data"]["DataExtRet"][0]["DataExtName"] == "Contract Tier"
    pl = service.store.get_report("profit_and_loss_ytd")
    assert pl["rows"][-1]["label"] == "Net Income"

    # Nothing changed: second run fetches only the overlap window and writes nothing new.
    seq_before = service.store.changes_after(0, 100000)[-1]["change_seq"]
    report = service.run_once()
    assert report.totals["inserted"] == 0 and report.totals["updated"] == 0
    assert service.store.changes_after(seq_before) == []

    # Edit one invoice, delete one customer.
    qb.tick(120)
    inv_id = inv["id"]
    qb.modify("Invoice", inv_id, Memo="Changed memo")
    victim = service.store.query("Customer", search="Quarry Brewing")["records"][0]
    qb.delete("Customer", victim["id"])
    report = service.run_once()
    assert report.totals["updated"] == 1 and report.totals["deleted"] == 1
    assert service.store.get("Invoice", inv_id)["data"]["Memo"] == "Changed memo"
    assert service.store.get("Customer", victim["id"])["deleted"] is True
    changed = service.store.changes_after(seq_before)
    assert {c["id"] for c in changed} == {inv_id, victim["id"]}

    # Incremental query used a modified-date filter.
    assert "<ModifiedDateRangeFilter>" in [r for r in qb.requests if "InvoiceQueryRq" in r][-1]


def test_entity_error_does_not_stop_sync(service, qb):
    original = qb._handle

    def broken(rq):
        if rq.tag == "VendorQueryRq":
            rs = qb._rs(rq, 3120, "Object specified in the request cannot be found")
            return rs
        return original(rq)

    qb._handle = broken
    report = service.run_once()
    assert report.status == "partial"
    errors = [s for s in report.steps if s.status == "error"]
    assert [s.name for s in errors] == ["Vendor"] and "3120" in errors[0].error
    assert service.store.entity_counts()["Invoice"]["count"] == 60
    assert service.store.get_state("Vendor")["watermark"] is None


def test_open_only_and_filters(service):
    service.run_once()
    unpaid = service.store.query("Invoice", open_only=True, limit=1000)
    assert 0 < unpaid["total"] < 60
    assert all(r["balance"] > 0 for r in unpaid["records"])
    ranged = service.store.query("Invoice", date_from="2025-03-01", date_to="2025-03-31", with_data=False)
    assert ranged["total"] > 0 and all("2025-03" in r["txn_date"] for r in ranged["records"])
    active = service.store.query("Customer", include_inactive=False, limit=1000)["total"]
    assert active < 37


def test_progress_and_heartbeat_logging(service, qb, caplog):
    import logging
    import time as _time

    from qbsync.entities import resolve_entities
    from qbsync.sync import SyncEngine

    slow = qb.process

    def delayed(xml):
        if "InvoiceQueryRq" in xml and 'iterator="Start"' in xml:
            _time.sleep(0.35)
        return slow(xml)

    qb.process = delayed
    engine = SyncEngine(qb, service.store, resolve_entities(include=["Invoice"]), page_size=25)
    engine.heartbeat_seconds = 0.1
    with caplog.at_level(logging.INFO, logger="qbsync.sync"):
        engine.run(reports=False)
    text = caplog.text
    assert "still waiting for QuickBooks to answer InvoiceQueryRq" in text
    assert "Invoice: 25 so far, 35 remaining" in text
