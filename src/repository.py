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
                CREATE TABLE IF NOT EXISTS work_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','in_progress')),
                    team TEXT,
                    site_record TEXT,
                    basis_severity TEXT NOT NULL,
                    basis_open_records INTEGER NOT NULL DEFAULT 0,
                    basis_priority INTEGER NOT NULL,
                    basis_version INTEGER NOT NULL,
                    revised_count INTEGER NOT NULL DEFAULT 0,
                    is_historical INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(batch_no, item_id)
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

    def update_item(self, item_id: int, severity: str, quantity: float,
                    threshold: float, expected_version: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET severity=?, quantity=?, threshold=?,
                   version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (severity, quantity, threshold, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def close_record(self, record_id: int) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM records WHERE id=?", (record_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("记录不存在")
            if row["status"] != "open":
                raise ConflictError("记录已关闭")
            self.conn.execute(
                "UPDATE records SET status='closed' WHERE id=? AND status='open'",
                (record_id,),
            )
        return dict(row)

    def create_work_order(self, batch_no: str, item_id: int, basis: Dict[str, Any],
                          actor: str, historical: int = 0) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO work_orders(batch_no, item_id, status, team, site_record,
                       basis_severity, basis_open_records, basis_priority, basis_version,
                       revised_count, is_historical, created_by, created_at, updated_at)
                       VALUES(?,?, 'pending', NULL, NULL, ?,?,?,?, 0, ?, ?, ?, ?)""",
                    (batch_no, item_id, basis["severity"], basis["open_records"],
                     basis["priority"], basis["version"], historical, actor, now, now),
                )
                order_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("工单已存在，批次内不可重复") from exc
        return self.get_work_order(order_id)

    def get_work_order(self, order_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM work_orders WHERE id=?", (order_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("工单不存在")
        return dict(row)

    def find_work_order(self, batch_no: str, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM work_orders WHERE batch_no=? AND item_id=?",
                (batch_no, item_id),
            ).fetchone()
        return None if row is None else dict(row)

    def has_work_orders(self, item_id: int) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM work_orders WHERE item_id=? LIMIT 1", (item_id,)
            ).fetchone()
        return row is not None

    def list_work_orders(self, item_id: Optional[int] = None,
                          status: Optional[str] = None,
                          batch_no: Optional[str] = None,
                          historical: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM work_orders WHERE 1=1"
        params: list = []
        if item_id is not None:
            sql += " AND item_id=?"; params.append(item_id)
        if status is not None:
            sql += " AND status=?"; params.append(status)
        if batch_no is not None:
            sql += " AND batch_no=?"; params.append(batch_no)
        if historical is not None:
            sql += " AND is_historical=?"; params.append(historical)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def reconfirm_work_order(self, order_id: int, basis: Dict[str, Any]) -> tuple:
        """按当前版本重新确认排程依据。已开工的保留队伍和现场记录，不覆盖。"""
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM work_orders WHERE id=?", (order_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("工单不存在")
            order = dict(row)
            if order["status"] != "pending":
                return order, False
            if (order["basis_severity"] == basis["severity"]
                    and order["basis_open_records"] == basis["open_records"]):
                return order, False
            self.conn.execute(
                """UPDATE work_orders SET basis_severity=?, basis_open_records=?,
                   basis_priority=?, basis_version=?, revised_count=revised_count+1,
                   updated_at=? WHERE id=? AND status='pending'""",
                (basis["severity"], basis["open_records"], basis["priority"],
                 basis["version"], now, order_id),
            )
        return self.get_work_order(order_id), True

    def start_work_order(self, order_id: int, team: str, site_record: str,
                         actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM work_orders WHERE id=?", (order_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("工单不存在")
            if row["status"] != "pending":
                raise ConflictError("工单已开工，队伍和现场记录不可覆盖")
            self.conn.execute(
                """UPDATE work_orders SET status='in_progress', team=?, site_record=?,
                   updated_at=? WHERE id=? AND status='pending'""",
                (team, site_record, now, order_id),
            )
        return self.get_work_order(order_id)

    def reconcile_work_orders(self, item_id: int, basis: Dict[str, Any]) -> List[int]:
        """依据变化重排未开工工单；已开工的不动。返回被重排的工单id。"""
        now = utc_now()
        revised: List[int] = []
        with self._lock, self.conn:
            rows = self.conn.execute(
                "SELECT * FROM work_orders WHERE item_id=? AND status='pending' ORDER BY id",
                (item_id,),
            ).fetchall()
            for row in rows:
                order = dict(row)
                if (order["basis_severity"] == basis["severity"]
                        and order["basis_open_records"] == basis["open_records"]):
                    continue
                self.conn.execute(
                    """UPDATE work_orders SET basis_severity=?, basis_open_records=?,
                       basis_priority=?, basis_version=?, revised_count=revised_count+1,
                       updated_at=? WHERE id=? AND status='pending'""",
                    (basis["severity"], basis["open_records"], basis["priority"],
                     basis["version"], now, order["id"]),
                )
                revised.append(order["id"])
        return revised

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
