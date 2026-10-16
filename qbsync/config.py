"""YAML config loading with defaults and ${ENV_VAR} expansion for secrets."""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path, PureWindowsPath
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "company_file": "",
    "company_id": None,  # label sent to sinks; defaults to the company file name
    "connection": "qbxmlrp2",  # qbxmlrp2 | mock
    "app_name": "QB Sync",
    "app_id": "",
    "qbxml_version": "auto",
    "file_mode": "dont_care",  # dont_care | single_user | multi_user
    "database": "qbsync.db",
    "log_file": None,
    "sync": {
        "interval_minutes": 15,
        "page_size": 250,
        "overlap_seconds": 3600,
        "history_start_date": None,
        "include": [],
        "exclude": [],
        "entities": {},
    },
    "reports": [
        {"name": "profit_and_loss_ytd", "type": "ProfitAndLossStandard", "macro": "ThisYearToDate"},
        {"name": "profit_and_loss_last_month", "type": "ProfitAndLossStandard", "macro": "LastMonth"},
        {"name": "balance_sheet", "type": "BalanceSheetStandard", "macro": "ThisYearToDate"},
        {"name": "customer_balances", "type": "CustomerBalanceSummary", "macro": "All"},
        {"name": "vendor_balances", "type": "VendorBalanceSummary", "macro": "All"},
    ],
    "sinks": {
        "webhook": None,  # {url, secret, batch_size, timeout}
        "postgres": None,  # {dsn, schema, table, reports_table}
    },
}

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | None) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    if path:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cfg = _expand(_merge(DEFAULTS, raw))
    if not cfg.get("company_id"):
        cfg["company_id"] = PureWindowsPath(cfg["company_file"]).stem if cfg["company_file"] else "default"
    return cfg
