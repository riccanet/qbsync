"""Local SQLite copy of the QuickBooks data.

One generic ``records`` table holds every entity as JSON plus a few extracted
columns (label, party, date, amount, balance) for filtering. Every insert,
change or delete bumps ``change_seq``, a monotonic cursor that the push sinks
use to send only what changed.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    entity        TEXT NOT NULL,
    id            TEXT NOT NULL,
    ret_type      TEXT,
    label         TEXT,
    party         TEXT,
    txn_date      TEXT,
    amount        REAL,
    balance       REAL,
    is_active     INTEGER,
    edit_sequence TEXT,
    time_created  TEXT,
    time_modified TEXT,
    data          TEXT NOT NULL,
    data_hash     TEXT NOT NULL,
    deleted       INTEGER NOT NULL DEFAULT 0,
    deleted_at    TEXT,
    synced_at     TEXT NOT NULL,
    change_seq    INTEGER NOT NULL,
    PRIMARY KEY (entity, id)
);
CREATE INDEX IF NOT EXISTS ix_records_change_seq ON records(change_seq);
CREATE INDEX IF NOT EXISTS ix_records_entity_date ON records(entity, txn_date);
CREATE INDEX IF NOT EXISTS ix_records_entity_party ON records(entity, party);
CREATE TABLE IF NOT EXISTS sync_state (
    key         TEXT PRIMARY KEY,
    watermark   TEXT,
    last_run    TEXT,
    last_status TEXT,
    last_error  TEXT,
    last_count  INTEGER
);
CREATE TABLE IF NOT EXISTS reports (
    name         TEXT PRIMARY KEY,
    data         TEXT NOT NULL,
    generated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

SUMMARY_COLUMNS = (
    "entity", "id", "ret_type", "label", "party", "txn_date", "amount", "balance",
    "is_active", "time_modified", "deleted", "deleted_at", "synced_at", "change_seq",
)

_PARTY_REFS = ("CustomerRef", "VendorRef", "PayeeEntityRef", "EntityRef", "EmployeeRef")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def summarize(data: dict[str, Any]) -> dict[str, Any]:
    """Pull the commonly-filtered fields out of a record."""
    label = data.get("FullName") or data.get("Name") or data.get("RefNumber") or data.get("TxnNumber")
    party = None
    for ref in _PARTY_REFS:
        if isinstance(data.get(ref), dict):
            party = data[ref].get("FullName")
            break
    amount = _num(data.get("TotalAmount"))
    if amount is None:
        amount = _num(data.get("Amount"))
    if amount is None and _num(data.get("Subtotal")) is not None:
        amount = data["Subtotal"] + (_num(data.get("SalesTaxTotal")) or 0.0)
    balance = None
    for key in ("BalanceRemaining", "OpenAmount", "TotalBalance", "Balance"):
        balance = _num(data.get(key))
        if balance is not None:
            break
    active = data.get("IsActive")
    return {
        "label": str(label) if label is not None else None,
        "party": party,
        "txn_date": data.get("TxnDate"),
        "amount": amount,
        "balance": balance,
        "is_active": None if active is None else int(bool(active)),
    }


def _row_to_dict(row: sqlite3.Row, with_data: bool = True) -> dict[str, Any]:
    out = {k: row[k] for k in SUMMARY_COLUMNS if k in row.keys()}
    out["deleted"] = bool(out.get("deleted"))
    if out.get("is_active") is not None:
        out["is_active"] = bool(out["is_active"])
    if with_data and "data" in row.keys():
        out["data"] = json.loads(row["data"])
    return out


class Store:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- writes -------------------------------------------------------------

    def upsert(self, entity: str, id_field: str, rets: list[tuple[str, dict[str, Any]]]) -> dict[str, int]:
        counts = {"inserted": 0, "updated": 0, "unchanged": 0}
        if not rets:
            return counts
        now = utcnow()
        with self._write_lock, self._conn() as conn:
            seq = conn.execute("SELECT COALESCE(MAX(change_seq), 0) FROM records").fetchone()[0]
            for ret_type, data in rets:
                rec_id = data.get(id_field)
                if not rec_id:
                    continue
                blob = json.dumps(data, separators=(",", ":"), sort_keys=True)
                digest = hashlib.sha1(blob.encode()).hexdigest()
                existing = conn.execute(
                    "SELECT data_hash, deleted FROM records WHERE entity=? AND id=?", (entity, rec_id)
                ).fetchone()
                if existing and existing["data_hash"] == digest and not existing["deleted"]:
                    counts["unchanged"] += 1
                    continue
                seq += 1
                s = summarize(data)
                conn.execute(
                    """INSERT INTO records (entity, id, ret_type, label, party, txn_date, amount, balance,
                           is_active, edit_sequence, time_created, time_modified, data, data_hash,
                           deleted, deleted_at, synced_at, change_seq)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,?,?)
                       ON CONFLICT(entity, id) DO UPDATE SET
                           ret_type=excluded.ret_type, label=excluded.label, party=excluded.party,
                           txn_date=excluded.txn_date, amount=excluded.amount, balance=excluded.balance,
                           is_active=excluded.is_active, edit_sequence=excluded.edit_sequence,
                           time_created=excluded.time_created, time_modified=excluded.time_modified,
                           data=excluded.data, data_hash=excluded.data_hash, deleted=0, deleted_at=NULL,
                           synced_at=excluded.synced_at, change_seq=excluded.change_seq""",
                    (
                        entity, rec_id, ret_type, s["label"], s["party"], s["txn_date"], s["amount"],
                        s["balance"], s["is_active"], data.get("EditSequence"), data.get("TimeCreated"),
                        data.get("TimeModified"), blob, digest, now, seq,
                    ),
                )
                counts["updated" if existing else "inserted"] += 1
        return counts

    def mark_deleted(self, entity: str, rec_id: str, deleted_at: str | None) -> bool:
        with self._write_lock, self._conn() as conn:
            seq = conn.execute("SELECT COALESCE(MAX(change_seq), 0) FROM records").fetchone()[0] + 1
            cur = conn.execute(
                """UPDATE records SET deleted=1, deleted_at=?, synced_at=?, change_seq=?
                   WHERE entity=? AND id=? AND deleted=0""",
                (deleted_at or utcnow(), utcnow(), seq, entity, rec_id),
            )
            return cur.rowcount > 0

    def set_state(self, key: str, **fields: Any) -> None:
        cols = ["key", *fields]
        with self._write_lock, self._conn() as conn:
            conn.execute(
                f"INSERT INTO sync_state ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
                f"ON CONFLICT(key) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in fields)}",
                (key, *fields.values()),
            )

    def save_report(self, name: str, data: dict[str, Any]) -> None:
        with self._write_lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO reports (name, data, generated_at) VALUES (?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET data=excluded.data, generated_at=excluded.generated_at",
                (name, json.dumps(data), utcnow()),
            )

    def set_meta(self, key: str, value: str) -> None:
        with self._write_lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # ---- reads --------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def get_state(self, key: str) -> dict[str, Any]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM sync_state WHERE key=?", (key,)).fetchone()
        return dict(row) if row else {}

    def all_states(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM sync_state ORDER BY key")]

    def entity_counts(self) -> dict[str, dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT entity, SUM(deleted=0) AS active, SUM(deleted) AS deleted,
                          MAX(time_modified) AS last_modified
                   FROM records GROUP BY entity ORDER BY entity"""
            ).fetchall()
        return {r["entity"]: {"count": r["active"], "deleted": r["deleted"], "last_modified": r["last_modified"]} for r in rows}

    def get(self, entity: str, rec_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM records WHERE entity=? AND id=?", (entity, rec_id)).fetchone()
        return _row_to_dict(row) if row else None

    def query(
        self,
        entity: str | None = None,
        *,
        search: str | None = None,
        party: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        modified_since: str | None = None,
        open_only: bool = False,
        include_deleted: bool = False,
        include_inactive: bool = True,
        with_data: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        where, args = [], []
        if entity:
            where.append("entity = ?")
            args.append(entity)
        if not include_deleted:
            where.append("deleted = 0")
        if not include_inactive:
            where.append("(is_active IS NULL OR is_active = 1)")
        if search:
            like = f"%{search}%"
            where.append("(label LIKE ? OR party LIKE ? OR data LIKE ?)")
            args += [like, like, like]
        if party:
            where.append("party LIKE ?")
            args.append(f"%{party}%")
        if date_from:
            where.append("txn_date >= ?")
            args.append(date_from)
        if date_to:
            where.append("txn_date <= ?")
            args.append(date_to)
        if modified_since:
            # QuickBooks timestamps are local ISO strings; a date or datetime prefix compares correctly.
            where.append("time_modified >= ?")
            args.append(modified_since)
        if open_only:
            where.append("balance IS NOT NULL AND balance != 0")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        order = "txn_date DESC, label" if entity else "entity, label"
        with self._conn() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM records {clause}", args).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM records {clause} ORDER BY {order} LIMIT ? OFFSET ?", (*args, limit, offset)
            ).fetchall()
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "records": [_row_to_dict(r, with_data) for r in rows],
        }

    def changes_after(self, cursor: int, limit: int = 500) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM records WHERE change_seq > ? ORDER BY change_seq LIMIT ?", (cursor, limit)
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def iter_entity(self, entity: str, include_deleted: bool = False) -> Iterator[dict[str, Any]]:
        with self._conn() as conn:
            sql = "SELECT * FROM records WHERE entity=?" + ("" if include_deleted else " AND deleted=0")
            for row in conn.execute(sql + " ORDER BY id", (entity,)):
                yield _row_to_dict(row)

    def get_report(self, name: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM reports WHERE name=?", (name,)).fetchone()
        return {"name": row["name"], "generated_at": row["generated_at"], **json.loads(row["data"])} if row else None

    def list_reports(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT name, generated_at, data FROM reports ORDER BY name").fetchall()
        return [
            {"name": r["name"], "title": json.loads(r["data"]).get("title"), "generated_at": r["generated_at"]}
            for r in rows
        ]
