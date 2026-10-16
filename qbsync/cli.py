"""Command line entry point: ``qbsync <command>``."""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Any

from .config import load_config
from .service import SyncService

log = logging.getLogger("qbsync")


def _setup_logging(cfg: dict[str, Any], verbose: bool) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if cfg.get("log_file"):
        Path(cfg["log_file"]).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(cfg["log_file"], maxBytes=5_000_000, backupCount=5))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
    )


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    # Under pythonw.exe (background task, no console) there are no std streams.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    p = argparse.ArgumentParser(prog="qbsync", description="Read-only QuickBooks Desktop sync with push to "
                                                           "webhooks (n8n) and Postgres.")
    p.add_argument("-c", "--config", default=os.environ.get("QBSYNC_CONFIG", "config.yaml"),
                   help="config file (default: $QBSYNC_CONFIG or config.yaml)")
    p.add_argument("-v", "--verbose", action="store_true")
    cmd = p.add_subparsers(dest="command", required=True)

    cmd.add_parser("test-connection", help="open QuickBooks, print company info and qbXML version")
    s = cmd.add_parser("sync", help="run one sync, push to sinks, and exit")
    s.add_argument("--full", action="store_true", help="ignore watermarks and re-pull everything")
    s.add_argument("--entity", action="append", help="only this entity (repeatable)")
    s.add_argument("--no-push", action="store_true", help="skip sinks")
    cmd.add_parser("run", help="sync and push every interval_minutes: the normal long-running mode")
    cmd.add_parser("test-webhook", help="send a test event (with sample records) to the configured webhook")
    e = cmd.add_parser("export", help="write every entity to JSON Lines files (+ reports) for bulk ingestion")
    e.add_argument("--out", default="export")
    cmd.add_parser("status", help="show sync state and record counts")
    i = cmd.add_parser("init", help="write a new config file at --config")
    i.add_argument("--company-file", default="", help=r"full path to the .QBW, e.g. \\SERVER\QBData\Co.QBW")
    i.add_argument("--company-id", help="short label for this company (default: file name)")
    i.add_argument("--data-dir", help="where the database and logs go (default: next to the config)")
    i.add_argument("--webhook-url", default="", help="push changes to this URL (e.g. an n8n Webhook node)")
    i.add_argument("--webhook-secret", default="", help="shared secret (generated if omitted)")
    i.add_argument("--mock", action="store_true", help="use simulated sample data instead of QuickBooks")
    i.add_argument("--force", action="store_true", help="overwrite an existing config")
    sv = cmd.add_parser("service", help="manage the Windows Scheduled Task that runs `qbsync run`")
    sv.add_argument("action", choices=["install", "uninstall", "start", "stop", "status"])
    sv.add_argument("--user", help="Windows account to run as (default: current user)")
    sv.add_argument("--password", help="run at boot whether or not the user is logged on (stores the password "
                                       "in Task Scheduler); without it, runs when the user logs on")
    sv.add_argument("--name", default="QBSync", help="task name (use one per company file)")

    args = p.parse_args(argv)

    if args.command == "init":
        from .setup_cmds import write_config

        written = write_config(
            Path(args.config), args.company_file, args.company_id,
            Path(args.data_dir) if args.data_dir else None,
            "mock" if args.mock else "qbxmlrp2", args.force, args.webhook_url, args.webhook_secret,
        )
        if not written:
            print(f"{args.config} already exists; leaving it unchanged (use --force to overwrite).")
            return 0
        print(f"Wrote {Path(args.config).resolve()}")
        if args.webhook_url:
            print("Webhook configured; its secret is in the config file (sinks.webhook.secret).")
        return 0

    if args.command == "service":
        from .setup_cmds import service_command, service_install

        if args.action == "install":
            if not Path(args.config).exists():
                p.error(f"config file not found: {args.config}")
            return service_install(Path(args.config), args.user, args.password, args.name)
        return service_command(args.action, args.name)

    try:
        config_path = args.config if Path(args.config).exists() else None
        if args.config != "config.yaml" and not config_path:
            p.error(f"config file not found: {args.config}")
        cfg = load_config(config_path)
        _setup_logging(cfg, args.verbose)
        service = SyncService(cfg)
    except PermissionError as exc:
        folder = Path(exc.filename or args.config).parent
        user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
        print(f"Access denied: {exc.filename}\n\n"
              f"The Windows account '{user}' cannot open QB Sync's files. Machine-wide installs only allow\n"
              "administrators, SYSTEM and the account chosen at install time.\n\n"
              "Fix (from an elevated Command Prompt), then try again:\n"
              f'  icacls "{folder}" /grant "{user}":(OI)(CI)M\n'
              f'  icacls "{folder}\\*" /reset /T /C\n'
              "Or re-run the installer as administrator with /USER=<this account>.", file=sys.stderr)
        return 1

    if args.command == "test-connection":
        import xml.etree.ElementTree as ET

        from .connection import QBConnectionError
        from .qbxml import build_request, parse_response

        conn = service.make_connection()
        try:
            conn.open()
        except QBConnectionError as exc:
            print(f"Could not connect to QuickBooks:\n  {exc}\n\nFile: {cfg['company_file'] or '(not set)'}\n"
                  "First run? Open the company file in QuickBooks as Admin on this machine, run this test again "
                  "and approve the access prompt (see README: First-time authorization).")
            return 1
        try:
            res = parse_response(conn.process(build_request(ET.Element("CompanyQueryRq", requestID="1"),
                                                            conn.qbxml_version)))[0]
            res.raise_for_status()
            company = res.rets[0][1] if res.rets else {}
            _print({"ok": True, "qbxml_version": conn.qbxml_version, "company_name": company.get("CompanyName"),
                    "legal_name": company.get("LegalCompanyName"), "file": cfg["company_file"],
                    "file_opened_by_quickbooks": getattr(conn, "open_file", None)})
        finally:
            conn.close()
        return 0

    if args.command == "sync":
        report = service.run_once(full=args.full, only=args.entity, push=not args.no_push)
        _print({"status": report.status, "error": report.error, "totals": report.totals,
                "errors": [{"step": s.name, "error": s.error} for s in report.steps if s.status == "error"]})
        return 0 if report.status in ("ok", "partial") else 1

    if args.command == "run":
        if not service.sinks:
            log.warning("No sinks configured: data is synced to %s but not pushed anywhere.", cfg["database"])
        try:
            service.run_forever()
        except KeyboardInterrupt:
            log.info("Stopped.")
        return 0

    if args.command == "test-webhook":
        from .sinks import WebhookSink

        sink = next((x for x in service.sinks if isinstance(x, WebhookSink)), None)
        if sink is None:
            print("No webhook configured (sinks.webhook.url in the config).")
            return 1
        sample = service.store.query(None, limit=3)["records"]
        try:
            sink.post("test", {"message": "Test event from QB Sync", "count": len(sample), "changes": sample})
        except Exception as exc:
            print(f"FAILED: {exc}")
            return 1
        print(f"OK: test event delivered to {sink.url}")
        return 0

    if args.command == "export":
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        counts = {}
        for ent in service.entities:
            n = 0
            with (out / f"{ent.name}.jsonl").open("w", encoding="utf-8") as fh:
                for rec in service.store.iter_entity(ent.name):
                    fh.write(json.dumps(rec, default=str) + "\n")
                    n += 1
            counts[ent.name] = n
        reports = [service.store.get_report(r["name"]) for r in service.store.list_reports()]
        (out / "reports.json").write_text(json.dumps(reports, indent=2, default=str), encoding="utf-8")
        _print({"out": str(out.resolve()), "records": counts, "reports": len(reports)})
        return 0

    if args.command == "status":
        _print({"company_id": cfg["company_id"], "counts": service.store.entity_counts(),
                "state": service.store.all_states(), "reports": service.store.list_reports(),
                "sinks": {s.name: int(service.store.get_meta(s.cursor_key) or 0) for s in service.sinks}})
        return 0
    return 2
