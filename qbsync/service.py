"""Ties config, connection, engine, store and sinks together; runs the sync loop."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from .connection import QBConnection, RequestProcessorConnection
from .entities import resolve_entities
from .sinks import build_sinks
from .store import Store
from .sync import RunReport, SyncEngine

log = logging.getLogger(__name__)

_mock_instance = None


class SyncService:
    def __init__(self, cfg: dict[str, Any], store: Store | None = None):
        self.cfg = cfg
        sync_cfg = cfg["sync"]
        self.store = store or Store(cfg["database"])
        self.entities = resolve_entities(sync_cfg.get("entities"), sync_cfg.get("include"), sync_cfg.get("exclude"))
        self.sinks = build_sinks(cfg)
        self.last_report: RunReport | None = None

    def make_connection(self) -> QBConnection:
        if self.cfg["connection"] == "mock":
            global _mock_instance
            from .mock import MockQuickBooks

            if _mock_instance is None:  # keep one simulated company per process
                _mock_instance = MockQuickBooks()
            return _mock_instance
        return RequestProcessorConnection(
            company_file=self.cfg["company_file"],
            app_name=self.cfg["app_name"],
            app_id=self.cfg["app_id"],
            qbxml_version=str(self.cfg["qbxml_version"]),
            file_mode=self.cfg["file_mode"],
            unattended_mode=self.cfg["auth"]["unattended_mode"],
            read_only=self.cfg["auth"]["read_only"],
            personal_data=self.cfg["auth"]["personal_data"],
        )

    def run_once(self, full: bool = False, only: list[str] | None = None, push: bool = True) -> RunReport:
        sync_cfg = self.cfg["sync"]
        conn = self.make_connection()
        try:
            conn.open()
        except Exception as exc:
            report = RunReport(started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                               status="failed", error=str(exc))
            report.finished_at = report.started_at
            log.error("Could not connect to QuickBooks: %s", exc)
            self.last_report = report
            if push:
                self._notify(report)
            return report
        try:
            failed_sinks: set[str] = set()

            def push_now(step=None) -> None:
                # Push as each entity lands so large first syncs show up in n8n early. A sink that
                # failed is skipped for the rest of this run (its retries would slow the pull down)
                # and tried again at the end.
                for sink in self.sinks:
                    if step is not None and sink.name in failed_sinks:
                        continue
                    try:
                        sink.push(self.store)
                        failed_sinks.discard(sink.name)
                    except Exception as exc:
                        failed_sinks.add(sink.name)
                        log.error("Sink %s failed (will retry): %s", sink.name, exc)

            engine = SyncEngine(
                conn, self.store, self.entities,
                page_size=int(sync_cfg["page_size"]),
                overlap_seconds=int(sync_cfg["overlap_seconds"]),
                history_start_date=sync_cfg.get("history_start_date"),
                reports=self.cfg.get("reports") or [],
                after_entity=push_now if push and self.sinks else None,
            )
            report = engine.run(full=full, only=only)
        finally:
            conn.close()
        totals = report.totals
        log.info("Sync %s: %d fetched, %d new, %d changed, %d deleted", report.status, totals["fetched"],
                 totals["inserted"], totals["updated"], totals["deleted"])
        if push:
            push_now()
            self._notify(report)
        self.last_report = report
        return report

    def _notify(self, report: RunReport) -> None:
        for sink in self.sinks:
            try:
                sink.notify_run(report.to_dict())
            except Exception as exc:
                log.error("Sink %s status notification failed: %s", sink.name, exc)

    # ---- long-running mode --------------------------------------------------------

    def run_forever(self) -> None:
        """Sync and push every ``interval_minutes`` until interrupted."""
        interval = max(1.0, float(self.cfg["sync"]["interval_minutes"])) * 60
        log.info("Syncing every %g minutes", interval / 60)
        while True:
            try:
                self.run_once()
            except Exception:
                log.exception("Unexpected error during sync")
            time.sleep(interval)
