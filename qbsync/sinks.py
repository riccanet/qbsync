"""Push changed records to external destinations after each sync.

Each sink keeps its own cursor (the last ``change_seq`` it delivered) in the
store's meta table and only advances it after a successful delivery, so a
failed push is retried on the next run without losing anything.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlsplit
from typing import Any

from . import __version__
from .store import Store

log = logging.getLogger(__name__)
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


class PayloadTooLarge(Exception):
    """The destination rejected a batch as too big (HTTP 413); push() retries with smaller batches."""


class Sink:
    name = "sink"

    def __init__(self, company_id: str, batch_size: int = 200):
        self.company_id = company_id
        self.batch_size = batch_size

    @property
    def cursor_key(self) -> str:
        return f"sink_cursor:{self.name}"

    def push(self, store: Store) -> int:
        cursor = int(store.get_meta(self.cursor_key) or 0)
        size = self.batch_size
        sent = 0
        while True:
            batch = store.changes_after(cursor, size)
            if not batch:
                break
            try:
                self.deliver(batch, store)
            except PayloadTooLarge:
                if size == 1:
                    raise RuntimeError(f"{self.name}: record {batch[0]['entity']} {batch[0]['id']} is too large "
                                       "for the destination (HTTP 413)") from None
                size = max(1, size // 2)
                log.warning("%s: batch too large, retrying with %d records per request", self.name, size)
                continue
            cursor = batch[-1]["change_seq"]
            store.set_meta(self.cursor_key, str(cursor))
            sent += len(batch)
        self.after_push(store)
        if sent:
            log.info("%s: pushed %d changes", self.name, sent)
        return sent

    def deliver(self, batch: list[dict[str, Any]], store: Store) -> None:
        raise NotImplementedError

    def reset(self, store: Store) -> None:
        """Forget what was delivered, so the next push sends every record (and report) again."""
        store.set_meta(self.cursor_key, "0")
        store.set_meta(f"sink_reports_hash:{self.name}", "")

    def after_push(self, store: Store) -> None:
        pass

    def notify_run(self, report: dict[str, Any]) -> None:
        """Called after every sync run with the run report."""


class WebhookSink(Sink):
    """POSTs JSON events over outbound HTTPS, so nothing on this machine has to accept connections.

    Every request has a ``type``:
      changes  new/changed/deleted records (batched, in order, at-least-once)
      reports  report snapshots, sent when any of them changed
      status   sync run result (on errors only, or every run, per ``send_status``)
      test     sent by ``qbsync test-webhook``
    With a secret set, requests carry ``X-QBSync-Token: <secret>`` (for n8n's Header Auth) and
    ``X-QBSync-Signature: sha256=<HMAC of the body>``.
    """

    name = "webhook"
    RETRY_DELAYS = (2, 10, 30)

    def __init__(self, company_id: str, url: str, secret: str = "", batch_size: int = 200,
                 timeout: int = 120, headers: dict[str, str] | None = None, send_status: str = "errors"):
        super().__init__(company_id, batch_size)
        if send_status not in ("never", "errors", "always"):
            raise ValueError("webhook send_status must be never, errors or always")
        self.url = url
        self.secret = secret
        self.timeout = timeout
        self.headers = headers or {}
        self.send_status = send_status

    def post(self, event_type: str, payload: dict[str, Any]) -> None:
        body = json.dumps({
            "type": event_type,
            "source": "quickbooks-desktop",
            "company_id": self.company_id,
            "host": socket.gethostname(),
            "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **payload,
        }, default=str).encode()
        headers = {"Content-Type": "application/json", "User-Agent": f"qbsync/{__version__}", **self.headers}
        if self.secret:
            headers["X-QBSync-Token"] = self.secret
            sig = hmac.new(self.secret.encode(), body, hashlib.sha256).hexdigest()
            headers["X-QBSync-Signature"] = f"sha256={sig}"
        for attempt, delay in enumerate((*self.RETRY_DELAYS, None)):
            req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout):
                    return
            except urllib.error.HTTPError as exc:
                if exc.code == 413:
                    raise PayloadTooLarge() from None
                retryable = exc.code == 429 or exc.code >= 500
                error = f"HTTP {exc.code}"
                if exc.code == 404:
                    error += " (n8n: is the workflow active? Test URLs only work while 'Listen for test event' is on)"
                elif exc.code in (401, 403):
                    error += " (check the webhook secret / Header Auth credential)"
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                retryable, error = True, str(getattr(exc, "reason", exc))
            if not retryable or delay is None:
                raise RuntimeError(f"webhook {event_type} failed: {error}")
            log.warning("webhook %s failed (%s); retrying in %ss", event_type, error, delay)
            time.sleep(delay)

    def deliver(self, batch: list[dict[str, Any]], store: Store) -> None:
        self.post("changes", {"cursor": batch[-1]["change_seq"], "count": len(batch), "changes": batch})

    def after_push(self, store: Store) -> None:
        reports = [r for r in (store.get_report(x["name"]) for x in store.list_reports()) if r]
        if not reports:
            return
        content = json.dumps([{k: v for k, v in r.items() if k != "generated_at"} for r in reports], sort_keys=True)
        digest = hashlib.sha1(content.encode()).hexdigest()
        key = f"sink_reports_hash:{self.name}"
        if store.get_meta(key) == digest:
            return
        self.post("reports", {"count": len(reports), "reports": reports})
        store.set_meta(key, digest)

    def notify_run(self, report: dict[str, Any]) -> None:
        if self.send_status == "never" or (self.send_status == "errors" and report.get("status") == "ok"):
            return
        steps = report.get("steps") or []
        self.post("status", {
            "status": report.get("status"),
            "error": report.get("error"),
            "started_at": report.get("started_at"),
            "finished_at": report.get("finished_at"),
            "totals": {k: sum(s.get(k, 0) for s in steps) for k in ("fetched", "inserted", "updated", "deleted")},
            "errors": [{"step": s["name"], "error": s["error"]} for s in steps if s.get("status") == "error"],
        })


class PostgresSink(Sink):
    """Upserts into a Postgres table (works with Supabase, Neon, RDS, ...).

    Uses pg8000 (pure Python) so it runs in the 32-bit Windows build. ``dsn`` is a URL:
    ``postgresql://user:password@host:5432/dbname?sslmode=require``.
    """

    name = "postgres"

    def __init__(self, company_id: str, dsn: str, schema: str = "public", table: str = "qb_records",
                 reports_table: str = "qb_reports", batch_size: int = 500):
        super().__init__(company_id, batch_size)
        for ident in (schema, table, reports_table):
            if not _IDENT.match(ident):
                raise ValueError(f"Invalid Postgres identifier: {ident!r}")
        self.dsn = dsn
        self.table = f'"{schema}"."{table}"'
        self.reports_table = f'"{schema}"."{reports_table}"'
        self._ready = False

    def _connect(self):
        import pg8000.dbapi

        url = urlsplit(self.dsn)
        if url.scheme not in ("postgres", "postgresql"):
            raise ValueError("postgres dsn must look like postgresql://user:pass@host:5432/db")
        params = {k: v[-1] for k, v in parse_qs(url.query).items()}
        sslmode = params.get("sslmode", "prefer")
        kwargs = dict(
            user=unquote(url.username or "postgres"), password=unquote(url.password or ""),
            host=url.hostname or "localhost", port=url.port or 5432,
            database=unquote(url.path.lstrip("/")) or "postgres", timeout=60,
        )
        if sslmode == "disable":
            return pg8000.dbapi.connect(**kwargs)
        if sslmode in ("verify-ca", "verify-full"):
            ctx = ssl.create_default_context(cafile=params.get("sslrootcert"))
            ctx.check_hostname = sslmode == "verify-full"
        else:  # allow / prefer / require: encrypt without verifying, like libpq
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        try:
            return pg8000.dbapi.connect(ssl_context=ctx, **kwargs)
        except pg8000.dbapi.InterfaceError as exc:
            if sslmode in ("prefer", "allow") and "SSL" in str(exc):
                return pg8000.dbapi.connect(**kwargs)
            raise

    def _ensure_schema(self, cur) -> None:
        if self._ready:
            return
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.table} (
                company_id    text NOT NULL,
                entity        text NOT NULL,
                id            text NOT NULL,
                ret_type      text,
                label         text,
                party         text,
                txn_date      date,
                amount        numeric,
                balance       numeric,
                is_active     boolean,
                time_modified timestamptz,
                deleted       boolean NOT NULL DEFAULT false,
                deleted_at    timestamptz,
                data          jsonb NOT NULL,
                synced_at     timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (company_id, entity, id)
            )""")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.reports_table} (
                company_id   text NOT NULL,
                name         text NOT NULL,
                generated_at timestamptz NOT NULL,
                data         jsonb NOT NULL,
                PRIMARY KEY (company_id, name)
            )""")
        # Locked down by default: on Supabase, tables without RLS are readable through the public API.
        cur.execute(f"ALTER TABLE {self.table} ENABLE ROW LEVEL SECURITY")
        cur.execute(f"ALTER TABLE {self.reports_table} ENABLE ROW LEVEL SECURITY")
        self._ready = True

    def _run(self, sql: str, rows: list[tuple]) -> None:
        conn = self._connect()
        try:
            cur = conn.cursor()
            self._ensure_schema(cur)
            if rows:
                cur.executemany(sql, rows)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def deliver(self, batch: list[dict[str, Any]], store: Store) -> None:
        rows = [(
            self.company_id, r["entity"], r["id"], r["ret_type"], r["label"], r["party"],
            r["txn_date"] or None, r["amount"], r["balance"], r["is_active"], r["time_modified"] or None,
            r["deleted"], r["deleted_at"], json.dumps(r["data"]),
        ) for r in batch]
        self._run(f"""
            INSERT INTO {self.table} (company_id, entity, id, ret_type, label, party, txn_date, amount,
                balance, is_active, time_modified, deleted, deleted_at, data, synced_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s::date,%s,%s,%s,%s::timestamptz,%s,%s::timestamptz,%s::jsonb, now())
            ON CONFLICT (company_id, entity, id) DO UPDATE SET
                ret_type=EXCLUDED.ret_type, label=EXCLUDED.label, party=EXCLUDED.party,
                txn_date=EXCLUDED.txn_date, amount=EXCLUDED.amount, balance=EXCLUDED.balance,
                is_active=EXCLUDED.is_active, time_modified=EXCLUDED.time_modified,
                deleted=EXCLUDED.deleted, deleted_at=EXCLUDED.deleted_at, data=EXCLUDED.data,
                synced_at=now()""", rows)

    def after_push(self, store: Store) -> None:
        reports = [r for r in (store.get_report(x["name"]) for x in store.list_reports()) if r]
        if not reports:
            return
        self._run(f"""
            INSERT INTO {self.reports_table} (company_id, name, generated_at, data)
            VALUES (%s,%s,%s::timestamptz,%s::jsonb)
            ON CONFLICT (company_id, name) DO UPDATE SET generated_at=EXCLUDED.generated_at, data=EXCLUDED.data""",
            [(self.company_id, r["name"], r["generated_at"], json.dumps(r)) for r in reports])


def build_sinks(cfg: dict[str, Any]) -> list[Sink]:
    sinks: list[Sink] = []
    company = cfg["company_id"]
    wh = (cfg.get("sinks") or {}).get("webhook")
    if wh and wh.get("url"):
        sinks.append(WebhookSink(company, wh["url"], wh.get("secret", ""), int(wh.get("batch_size", 200)),
                                 int(wh.get("timeout", 120)), wh.get("headers"), wh.get("send_status", "errors")))
    pg = (cfg.get("sinks") or {}).get("postgres")
    if pg and pg.get("dsn"):
        sinks.append(PostgresSink(company, pg["dsn"], pg.get("schema", "public"), pg.get("table", "qb_records"),
                                  pg.get("reports_table", "qb_reports"), int(pg.get("batch_size", 500))))
    return sinks
