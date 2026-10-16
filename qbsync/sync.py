"""Incremental sync engine.

For each entity it pages through QuickBooks with iterators, filtered by
``TimeModified`` since the last successful run (minus an overlap window to cover
clock/DST quirks; re-fetched records are de-duplicated by content hash). Deletes
are picked up with ``ListDeletedQuery``/``TxnDeletedQuery`` and soft-deleted in
the store. Only *Query* requests are ever sent: the sync never writes to QuickBooks.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable

from .connection import QBConnection, QBConnectionError
from .entities import LIST, TXN, Entity, build_deleted_query, build_query, build_report_query
from .qbxml import QueryResult, build_request, parse_report, parse_response
from .store import Store, utcnow

log = logging.getLogger(__name__)


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.astimezone()


def fmt_ts(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _later(a: str | None, b: str | None) -> str | None:
    if not a:
        return b
    if not b:
        return a
    return b if parse_ts(b) > parse_ts(a) else a


@dataclass
class StepResult:
    name: str
    status: str = "ok"  # ok | error | baseline
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    error: str | None = None
    seconds: float = 0.0


@dataclass
class RunReport:
    started_at: str
    finished_at: str | None = None
    full: bool = False
    status: str = "running"
    qbxml_version: str | None = None
    steps: list[StepResult] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def totals(self) -> dict[str, int]:
        keys = ("fetched", "inserted", "updated", "unchanged", "deleted")
        return {k: sum(getattr(s, k) for s in self.steps) for k in keys}


class SyncEngine:
    def __init__(
        self,
        conn: QBConnection,
        store: Store,
        entities: list[Entity],
        *,
        page_size: int = 250,
        overlap_seconds: int = 3600,
        history_start_date: str | None = None,
        reports: Iterable[dict[str, Any]] = (),
        after_entity: Callable[[StepResult], None] | None = None,
    ):
        self.conn = conn
        self.store = store
        self.entities = entities
        self.page_size = page_size
        self.overlap = timedelta(seconds=overlap_seconds)
        self.history_start_date = history_start_date
        self.reports = list(reports)
        self.heartbeat_seconds = 60.0
        self.after_entity = after_entity  # e.g. push to sinks as each entity lands
        self._max_seen: str | None = None  # newest TimeModified seen, in QuickBooks' own clock

    def _call(self, rq) -> QueryResult:
        # QuickBooks can take minutes to answer the first page of a big query (it builds the whole
        # result first), so say so in the log instead of going quiet.
        started = time.monotonic()
        done = threading.Event()

        def heartbeat() -> None:
            while not done.wait(self.heartbeat_seconds):
                log.info("  still waiting for QuickBooks to answer %s (%d min); this is normal for large "
                         "files on the first sync", rq.tag, (time.monotonic() - started) // 60)

        watcher = threading.Thread(target=heartbeat, name="qbsync-heartbeat", daemon=True)
        watcher.start()
        try:
            response = self.conn.process(build_request(rq, self.conn.qbxml_version))
        finally:
            done.set()
        result = parse_response(response)[0]
        result.raise_for_status()
        return result

    def _from(self, watermark: str | None) -> str | None:
        return fmt_ts(parse_ts(watermark) - self.overlap) if watermark else None

    # ---- steps ----------------------------------------------------------------

    def sync_entity(self, ent: Entity, full: bool = False) -> StepResult:
        res = StepResult(ent.name)
        t0 = time.monotonic()
        state = self.store.get_state(ent.name)
        watermark = None if full else state.get("watermark")
        from_modified = self._from(watermark)
        from_txn_date = self.history_start_date if ent.kind == TXN and not from_modified else None
        new_watermark = watermark
        iterator_id = None
        try:
            while True:
                rq = build_query(
                    ent,
                    page_size=self.page_size,
                    from_modified=from_modified,
                    from_txn_date=from_txn_date,
                    iterator_id=iterator_id,
                )
                page = self._call(rq)
                counts = self.store.upsert(ent.name, ent.id_field, page.rets)
                res.fetched += len(page.rets)
                for k, v in counts.items():
                    setattr(res, k, getattr(res, k) + v)
                for _, data in page.rets:
                    if data.get("TimeModified"):
                        new_watermark = _later(new_watermark, data["TimeModified"])
                        self._max_seen = _later(self._max_seen, data["TimeModified"])
                if ent.iterator and page.iterator_remaining > 0:
                    log.info("  %s: %d so far, %d remaining", ent.name, res.fetched, page.iterator_remaining)
                if not ent.iterator or not page.iterator_id or page.iterator_remaining <= 0:
                    break
                iterator_id = page.iterator_id
        except QBConnectionError:
            raise
        except Exception as exc:  # one bad entity should not stop the rest
            res.status, res.error = "error", str(exc)
            log.warning("%s: %s", ent.name, exc)
        res.seconds = round(time.monotonic() - t0, 2)
        self.store.set_state(
            ent.name,
            # On failure keep the old watermark so the next run re-fetches what was missed.
            watermark=new_watermark if res.status == "ok" else state.get("watermark"),
            last_run=utcnow(),
            last_status=res.status,
            last_error=res.error,
            last_count=res.fetched,
        )
        log.info(
            "%s: fetched %d (new %d, changed %d)%s",
            ent.name, res.fetched, res.inserted, res.updated, f" ERROR {res.error}" if res.error else "",
        )
        return res

    def sync_deletes(self, kind: str, baseline: str) -> StepResult | None:
        type_map = {t: e.name for e in self.entities if e.kind == kind for t in e.deleted_types}
        if not type_map:
            return None
        key = f"_deleted_{kind}"
        res = StepResult(key)
        t0 = time.monotonic()
        watermark = self.store.get_state(key).get("watermark")
        if not watermark:
            # First run: everything that exists was just pulled, so start tracking deletes from about now.
            # The baseline uses QuickBooks' clock when possible; an older baseline is harmless.
            self.store.set_state(key, watermark=baseline, last_run=utcnow(), last_status="baseline")
            res.status = "baseline"
            return res
        new_watermark = watermark
        try:
            page = self._call(build_deleted_query(kind, list(type_map), self._from(watermark)))
            res.fetched = len(page.rets)
            for _, d in page.rets:
                del_type = d.get("ListDelType") or d.get("TxnDelType")
                rec_id = d.get("ListID") or d.get("TxnID")
                entity = type_map.get(del_type)
                if entity and rec_id and self.store.mark_deleted(entity, rec_id, d.get("TimeDeleted")):
                    res.deleted += 1
                new_watermark = _later(new_watermark, d.get("TimeDeleted"))
        except QBConnectionError:
            raise
        except Exception as exc:
            res.status, res.error = "error", str(exc)
            log.warning("%s: %s", key, exc)
        res.seconds = round(time.monotonic() - t0, 2)
        self.store.set_state(
            key, watermark=new_watermark, last_run=utcnow(), last_status=res.status,
            last_error=res.error, last_count=res.fetched,
        )
        if res.deleted:
            log.info("%s: %d records marked deleted", key, res.deleted)
        return res

    def sync_report(self, report: dict[str, Any]) -> StepResult:
        res = StepResult(f"report:{report['name']}")
        t0 = time.monotonic()
        try:
            page = self._call(build_report_query(report))
            data = parse_report(page.element)
            data["request"] = {k: v for k, v in report.items() if k != "name"}
            self.store.save_report(report["name"], data)
            res.fetched = len(data["rows"])
        except QBConnectionError:
            raise
        except Exception as exc:
            res.status, res.error = "error", str(exc)
            log.warning("report %s: %s", report["name"], exc)
        res.seconds = round(time.monotonic() - t0, 2)
        return res

    # ---- orchestration ----------------------------------------------------------

    def run(self, full: bool = False, only: list[str] | None = None, reports: bool = True) -> RunReport:
        report = RunReport(started_at=utcnow(), full=full, qbxml_version=self.conn.qbxml_version)
        wanted = {n.lower() for n in only} if only else None
        try:
            for ent in self.entities:
                if wanted is None or ent.name.lower() in wanted:
                    step = self.sync_entity(ent, full=full)
                    report.steps.append(step)
                    if self.after_entity and (step.inserted or step.updated):
                        self.after_entity(step)
            if wanted is None:
                host_now = datetime.now().astimezone()
                baseline = self._max_seen if self._max_seen and parse_ts(self._max_seen) < host_now else fmt_ts(host_now)
                for kind in (LIST, TXN):
                    step = self.sync_deletes(kind, baseline)
                    if step:
                        report.steps.append(step)
                if reports:
                    for r in self.reports:
                        report.steps.append(self.sync_report(r))
            errors = [s for s in report.steps if s.status == "error"]
            report.status = "partial" if errors else "ok"
        except QBConnectionError as exc:
            report.status, report.error = "failed", str(exc)
            log.error("Sync aborted: %s", exc)
        report.finished_at = utcnow()
        return report
