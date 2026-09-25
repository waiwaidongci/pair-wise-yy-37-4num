from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, SHUTDOWN_STATUSES, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        shutdown_statuses = ",".join("'" + s.replace("'", "''") + "'" for s in SHUTDOWN_STATUSES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS shutdown_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    equipment_id TEXT NOT NULL,
                    equipment_name TEXT NOT NULL,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    backup_device_id TEXT,
                    backup_device_name TEXT,
                    backup_capacity REAL NOT NULL DEFAULT 0,
                    affected_quantity REAL NOT NULL DEFAULT 0,
                    capacity_margin REAL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ({shutdown_statuses})),
                    decision_note TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_shutdown_status ON shutdown_reports(status);
                CREATE INDEX IF NOT EXISTS ix_shutdown_item ON shutdown_reports(item_id);
                CREATE TABLE IF NOT EXISTS shutdown_outlets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_id INTEGER NOT NULL REFERENCES shutdown_reports(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    outlet_code TEXT NOT NULL,
                    declared_quantity REAL NOT NULL DEFAULT 0,
                    UNIQUE(report_id, outlet_code)
                );
                CREATE INDEX IF NOT EXISTS ix_shutdown_outlets_item ON shutdown_outlets(item_id);
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def create_shutdown_report(self, item_id: int, equipment_id: str, equipment_name: str,
                               period_start: str, period_end: str,
                               backup_device_id: Optional[str], backup_device_name: Optional[str],
                               backup_capacity: float, affected_quantity: float,
                               outlets: List[Dict[str, Any]], actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO shutdown_reports(item_id, equipment_id, equipment_name,
                   period_start, period_end, backup_device_id, backup_device_name,
                   backup_capacity, affected_quantity, capacity_margin, status, decision_note,
                   version, created_by, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,1,?,?,?)""",
                (item_id, equipment_id, equipment_name, period_start, period_end,
                 backup_device_id, backup_device_name, backup_capacity, affected_quantity,
                 round(backup_capacity - affected_quantity, 6), "pending", actor, now, now),
            )
            report_id = int(cur.lastrowid)
            self._replace_outlets(report_id, outlets)
        return self.get_shutdown_report(report_id)

    def _replace_outlets(self, report_id: int, outlets: List[Dict[str, Any]]) -> None:
        self.conn.executemany(
            """INSERT INTO shutdown_outlets(report_id, item_id, outlet_code, declared_quantity)
               VALUES(?,?,?,?)""",
            [(report_id, outlet["item_id"], outlet["outlet_code"], outlet["declared_quantity"])
             for outlet in outlets],
        )

    def _shutdown_outlets(self, report_id: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM shutdown_outlets WHERE report_id=? ORDER BY id", (report_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def get_shutdown_report(self, report_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM shutdown_reports WHERE id=?", (report_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("停运报备不存在")
            report = dict(row)
            report["outlets"] = self._shutdown_outlets(report_id)
        return report

    def list_shutdown_reports(self, status: Optional[str] = None,
                              item_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM shutdown_reports"
        clauses = []
        params: List[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if item_id is not None:
            clauses.append("item_id=?")
            params.append(item_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
            result = []
            for row in rows:
                report = dict(row)
                report["outlets"] = self._shutdown_outlets(report["id"])
                result.append(report)
        return result

    def unconfirmed_shutdown_report_ids(self, item_id: int) -> List[int]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT r.id FROM shutdown_reports r
                   LEFT JOIN shutdown_outlets o ON o.report_id=r.id
                   WHERE r.status != 'confirmed'
                     AND (r.item_id=? OR o.item_id=?)
                   GROUP BY r.id ORDER BY r.id""",
                (item_id, item_id),
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def review_shutdown_report(self, report_id: int, status: str, decision_note: Optional[str],
                               capacity_margin_value: Optional[float],
                               expected_version: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE shutdown_reports SET status=?, decision_note=?, capacity_margin=?,
                   version=version+1, updated_at=?
                   WHERE id=? AND version=? AND status='pending'""",
                (status, decision_note, capacity_margin_value, now,
                 report_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM shutdown_reports WHERE id=?", (report_id,)
                ).fetchone()
                if exists is None:
                    raise NotFoundError("停运报备不存在")
                raise ConflictError("版本冲突或报备不在待审状态")
        return self.get_shutdown_report(report_id)

    def amend_shutdown_report(self, report_id: int, values: Dict[str, Any],
                              outlets: Optional[List[Dict[str, Any]]],
                              expected_version: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        fields = []
        params: List[Any] = []
        for column in ("equipment_id", "equipment_name", "period_start", "period_end",
                       "backup_device_id", "backup_device_name", "backup_capacity",
                       "affected_quantity"):
            if column in values:
                fields.append(f"{column}=?")
                params.append(values[column])
        if "backup_capacity" in values or "affected_quantity" in values:
            fields.append("capacity_margin=?")
            params.append(round(float(values["backup_capacity"])
                                - float(values["affected_quantity"]), 6))
        fields.append("status='pending'")
        fields.append("decision_note=NULL")
        fields.append("version=version+1")
        fields.append("updated_at=?")
        params.append(now)
        params.extend([report_id, expected_version])
        with self._lock, self.conn:
            cur = self.conn.execute(
                f"UPDATE shutdown_reports SET {', '.join(fields)} WHERE id=? AND version=?",
                tuple(params),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM shutdown_reports WHERE id=?", (report_id,)
                ).fetchone()
                if exists is None:
                    raise NotFoundError("停运报备不存在")
                raise ConflictError("版本冲突，请刷新后重试")
            if outlets is not None:
                self.conn.execute(
                    "DELETE FROM shutdown_outlets WHERE report_id=?", (report_id,)
                )
                self._replace_outlets(report_id, outlets)
        return self.get_shutdown_report(report_id)

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
