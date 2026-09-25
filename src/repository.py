from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


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
                CREATE TABLE IF NOT EXISTS equipment (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('control','substitute')),
                    rated_capacity REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','retired')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(external_ref)
                );
                CREATE TABLE IF NOT EXISTS outage_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    equipment_id INTEGER NOT NULL REFERENCES equipment(id),
                    item_id INTEGER NOT NULL REFERENCES items(id),
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    resumed_at TEXT,
                    affected_quantity REAL NOT NULL,
                    capacity_provided REAL NOT NULL DEFAULT 0,
                    margin REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','confirmed','returned')),
                    version INTEGER NOT NULL DEFAULT 1,
                    reason TEXT NOT NULL,
                    review_note TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_outages_equipment
                    ON outage_reports(equipment_id);
                CREATE INDEX IF NOT EXISTS ix_outages_item
                    ON outage_reports(item_id);
                CREATE INDEX IF NOT EXISTS ix_outages_status
                    ON outage_reports(status);
                CREATE TABLE IF NOT EXISTS outage_outlets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    outage_id INTEGER NOT NULL REFERENCES outage_reports(id) ON DELETE CASCADE,
                    outlet_code TEXT NOT NULL,
                    UNIQUE(outage_id, outlet_code)
                );
                CREATE TABLE IF NOT EXISTS outage_substitutes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    outage_id INTEGER NOT NULL REFERENCES outage_reports(id) ON DELETE CASCADE,
                    equipment_id INTEGER NOT NULL REFERENCES equipment(id),
                    rated_capacity REAL NOT NULL,
                    UNIQUE(outage_id, equipment_id)
                );
                CREATE INDEX IF NOT EXISTS ix_outage_subs_equipment
                    ON outage_substitutes(equipment_id);
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

    def create_equipment(self, name: str, kind: str, rated_capacity: float,
                         external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO equipment(name, kind, rated_capacity, status,
                       external_ref, created_by, created_at)
                       VALUES(?,?,?,'active',?,?,?)""",
                    (name, kind, rated_capacity, external_ref, actor, now),
                )
                equipment_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_equipment(equipment_id)

    def get_equipment(self, equipment_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM equipment WHERE id=?", (equipment_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("设备不存在")
        return dict(row)

    def list_equipment(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM equipment"
        params: tuple = ()
        if kind:
            sql += " WHERE kind=?"
            params = (kind,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def _outage_substitutes(self, outage_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
        if not outage_ids:
            return {}
        placeholders = ",".join("?" for _ in outage_ids)
        with self._lock:
            rows = self.conn.execute(
                f"""SELECT s.outage_id AS outage_id, s.equipment_id AS equipment_id,
                           s.rated_capacity AS rated_capacity, e.name AS name
                    FROM outage_substitutes s JOIN equipment e ON e.id=s.equipment_id
                    WHERE s.outage_id IN ({placeholders}) ORDER BY s.id""",
                tuple(outage_ids),
            ).fetchall()
        result: Dict[int, List[Dict[str, Any]]] = {}
        for row in rows:
            result.setdefault(row["outage_id"], []).append(dict(row))
        return result

    def _outage_outlets(self, outage_ids: List[int]) -> Dict[int, List[str]]:
        if not outage_ids:
            return {}
        placeholders = ",".join("?" for _ in outage_ids)
        with self._lock:
            rows = self.conn.execute(
                f"""SELECT outage_id, outlet_code FROM outage_outlets
                    WHERE outage_id IN ({placeholders}) ORDER BY id""",
                tuple(outage_ids),
            ).fetchall()
        result: Dict[int, List[str]] = {}
        for row in rows:
            result.setdefault(row["outage_id"], []).append(row["outlet_code"])
        return result

    def _assemble_outages(self, rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
        ids = [int(row["id"]) for row in rows]
        substitutes = self._outage_substitutes(ids)
        outlets = self._outage_outlets(ids)
        result = []
        for row in rows:
            item = dict(row)
            item["outlets"] = outlets.get(item["id"], [])
            item["substitutes"] = substitutes.get(item["id"], [])
            result.append(item)
        return result

    def create_outage(self, equipment_id: int, item_id: int, start_time: str,
                      end_time: str, affected_quantity: float, capacity_provided: float,
                      margin: float, reason: str, outlets: List[str],
                      substitutes: List[tuple], actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO outage_reports(equipment_id, item_id, start_time, end_time,
                   resumed_at, affected_quantity, capacity_provided, margin, status,
                   version, reason, created_by, created_at, updated_at)
                   VALUES(?,?,?,?,NULL,?,?,?,'pending',1,?,?,?,?)""",
                (equipment_id, item_id, start_time, end_time, affected_quantity,
                 capacity_provided, margin, reason, actor, now, now),
            )
            outage_id = int(cur.lastrowid)
            self.conn.executemany(
                "INSERT INTO outage_outlets(outage_id, outlet_code) VALUES(?,?)",
                [(outage_id, code) for code in outlets],
            )
            self.conn.executemany(
                """INSERT INTO outage_substitutes(outage_id, equipment_id, rated_capacity)
                   VALUES(?,?,?)""",
                [(outage_id, eq_id, cap) for eq_id, cap in substitutes],
            )
        return self.get_outage(outage_id)

    def get_outage(self, outage_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM outage_reports WHERE id=?", (outage_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("停运报备不存在")
        return self._assemble_outages([row])[0]

    def list_outages(self, status: Optional[str] = None, item_id: Optional[int] = None,
                     equipment_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM outage_reports"
        clauses = []
        params: List[Any] = []
        if status:
            clauses.append("status=?"); params.append(status)
        if item_id is not None:
            clauses.append("item_id=?"); params.append(item_id)
        if equipment_id is not None:
            clauses.append("equipment_id=?"); params.append(equipment_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return self._assemble_outages(rows)

    def pending_outages_touching(self, equipment_id: int,
                                 substitute_ids: List[int]) -> List[Dict[str, Any]]:
        ids = [equipment_id] + list(substitute_ids)
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self.conn.execute(
                f"""SELECT o.* FROM outage_reports o WHERE o.status='pending' AND (
                    o.equipment_id=? OR EXISTS (
                        SELECT 1 FROM outage_substitutes s
                        WHERE s.outage_id=o.id AND s.equipment_id IN ({placeholders})))
                    ORDER BY o.id""",
                (equipment_id, *ids),
            ).fetchall()
        return self._assemble_outages(rows)

    def review_outage(self, outage_id: int, status: str, review_note: Optional[str],
                      margin: float, capacity_provided: float,
                      expected_version: int) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE outage_reports SET status=?, review_note=?, margin=?,
                   capacity_provided=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (status, review_note, margin, capacity_provided, now,
                 outage_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM outage_reports WHERE id=?", (outage_id,)
                ).fetchone()
                if exists is None:
                    raise NotFoundError("停运报备不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_outage(outage_id)

    _UNSET = object()

    def amend_outage(self, outage_id: int, expected_version: int,
                     start_time: Optional[str] = _UNSET, end_time: Optional[str] = _UNSET,
                     resumed_at=_UNSET, affected_quantity: Optional[float] = _UNSET,
                     capacity_provided: Optional[float] = _UNSET,
                     margin: Optional[float] = _UNSET) -> Dict[str, Any]:
        now = utc_now()
        fields = ["version=version+1", "updated_at=?", "status='pending'",
                  "review_note=NULL"]
        params: List[Any] = [now]
        if start_time is not self._UNSET:
            fields.append("start_time=?"); params.append(start_time)
        if end_time is not self._UNSET:
            fields.append("end_time=?"); params.append(end_time)
        if resumed_at is not self._UNSET:
            fields.append("resumed_at=?"); params.append(resumed_at)
        if affected_quantity is not self._UNSET:
            fields.append("affected_quantity=?"); params.append(affected_quantity)
        if capacity_provided is not self._UNSET:
            fields.append("capacity_provided=?"); params.append(capacity_provided)
        if margin is not self._UNSET:
            fields.append("margin=?"); params.append(margin)
        params.extend([outage_id, expected_version])
        with self._lock, self.conn:
            cur = self.conn.execute(
                f"UPDATE outage_reports SET {', '.join(fields)} WHERE id=? AND version=?",
                tuple(params),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM outage_reports WHERE id=?", (outage_id,)
                ).fetchone()
                if exists is None:
                    raise NotFoundError("停运报备不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_outage(outage_id)

    def unconfirmed_outage_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                """SELECT COUNT(*) AS n FROM outage_reports
                   WHERE item_id=? AND status IN ('pending','returned')""",
                (item_id,),
            ).fetchone()
        return int(row["n"])

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
