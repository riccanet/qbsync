import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from qbsync.sinks import WebhookSink


class _Hook:
    """Local HTTP endpoint standing in for an n8n Webhook node."""

    def __init__(self):
        self.events = []
        self.fail_next = []  # status codes to return before succeeding
        self.max_records = None
        hook = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                event = json.loads(body)
                if hook.fail_next:
                    code = hook.fail_next.pop(0)
                elif hook.max_records and len(event.get("changes", [])) > hook.max_records:
                    code = 413
                else:
                    code = 200
                    hook.events.append((self.headers, body, event))
                self.send_response(code)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/webhook/qb"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def of_type(self, t):
        return [e for _, _, e in self.events if e["type"] == t]


@pytest.fixture
def hook(monkeypatch):
    monkeypatch.setattr(WebhookSink, "RETRY_DELAYS", (0, 0, 0))
    h = _Hook()
    yield h
    h.server.shutdown()


def test_webhook_sink(service, qb, hook):
    service.sinks = [WebhookSink("testco", hook.url, "s3cret", batch_size=100)]
    service.run_once()
    changes = hook.of_type("changes")
    assert sum(e["count"] for e in changes) == len(service.store.changes_after(0, 100000))
    assert changes[0]["company_id"] == "testco" and changes[0]["host"]
    headers, body, _ = hook.events[0]
    assert headers["X-QBSync-Token"] == "s3cret"
    assert headers["X-QBSync-Signature"] == "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    reports = hook.of_type("reports")
    assert len(reports) == 1 and any(r["name"] == "profit_and_loss_ytd" for r in reports[0]["reports"])
    assert hook.of_type("status") == []  # send_status=errors and the run was ok

    # Nothing changed -> nothing sent (reports unchanged too).
    n = len(hook.events)
    service.run_once()
    assert len(hook.events) == n

    # A change is delivered even after a transient 500 (retried in the same run).
    qb.tick(120)
    inv = service.store.query("Invoice", limit=1)["records"][0]
    qb.modify("Invoice", inv["id"], Memo="after outage")
    hook.fail_next = [500]
    service.run_once()
    last = hook.of_type("changes")[-1]
    assert [c["id"] for c in last["changes"]] == [inv["id"]]
    assert last["changes"][0]["data"]["Memo"] == "after outage"


def test_webhook_status_on_failure_and_413(service, qb, hook):
    hook.max_records = 30  # destination rejects bigger batches with 413
    service.sinks = [WebhookSink("testco", hook.url, "s3cret", batch_size=200)]
    service.run_once()
    assert all(e["count"] <= 30 for e in hook.of_type("changes"))
    assert sum(e["count"] for e in hook.of_type("changes")) == len(service.store.changes_after(0, 100000))

    def broken():
        raise RuntimeError("QuickBooks is not available")

    qb.open = broken
    service.run_once()
    status = hook.of_type("status")
    assert len(status) == 1 and status[0]["status"] == "failed"
    assert "not available" in status[0]["error"]


def test_webhook_auth_failure_keeps_cursor(service, hook):
    service.sinks = [WebhookSink("testco", hook.url, "s3cret", batch_size=100)]
    hook.fail_next = [401, 401]  # the mid-run push and the end-of-run retry both fail
    service.run_once()  # logged, not raised
    assert service.store.get_meta("sink_cursor:webhook") is None
    service.run_once()  # next run delivers everything
    assert sum(e["count"] for e in hook.of_type("changes")) == len(service.store.changes_after(0, 100000))


def test_webhook_pushes_as_each_entity_lands(service, hook):
    service.sinks = [WebhookSink("testco", hook.url, "s3cret", batch_size=500)]
    service.run_once()
    batches = hook.of_type("changes")
    entities_per_batch = [{c["entity"] for c in b["changes"]} for b in batches]
    # Customers went out in their own request, before the invoices were pulled.
    assert len(batches) > 3
    assert any(e == {"Customer"} for e in entities_per_batch)
    assert sum(b["count"] for b in batches) == len(service.store.changes_after(0, 100000))


def test_failed_sink_is_not_retried_for_every_entity(service, hook, monkeypatch):
    calls = []
    sink = WebhookSink("testco", hook.url, "s3cret")
    monkeypatch.setattr(sink, "post", lambda *a, **k: (calls.append(a[0]), (_ for _ in ()).throw(RuntimeError("down")))[0])
    service.sinks = [sink]
    service.run_once()
    # one failed attempt mid-run, then one more at the end (+ the status event), not one per entity
    assert calls.count("changes") == 2
