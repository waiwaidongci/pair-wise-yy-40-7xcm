from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, LEGACY_BATCH_NO, SCHEDULE_ENTITY, STATES, schedule_basis


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
        migrated = self._migrate_legacy_work_orders()
        if migrated:
            self.append_audit("migrate", SCHEDULE_ENTITY, 0, "migration", {
                "batch_no": LEGACY_BATCH_NO, "orders": migrated, "status": "historical",
            })

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
                CREATE TABLE IF NOT EXISTS schedule_state (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    version INTEGER NOT NULL DEFAULT 0,
                    legacy_migrated INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS work_order_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    actor TEXT NOT NULL,
                    lines TEXT NOT NULL,
                    result TEXT NOT NULL,
                    expected_version INTEGER NOT NULL,
                    schedule_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS work_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    batch_no TEXT NOT NULL,
                    line_no INTEGER NOT NULL,
                    team TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','started','historical')),
                    basis_severity TEXT NOT NULL,
                    basis_open_records INTEGER NOT NULL,
                    basis_priority INTEGER NOT NULL,
                    basis_deadline_hours INTEGER NOT NULL,
                    basis_version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(batch_no, line_no)
                );
                CREATE INDEX IF NOT EXISTS ix_work_orders_item
                    ON work_orders(item_id, status);
                CREATE TABLE IF NOT EXISTS work_order_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL REFERENCES work_orders(id) ON DELETE CASCADE,
                    note TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

    def _ensure_schedule_row(self) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO schedule_state(id, version, legacy_migrated) VALUES(1,0,0)")

    def _migrate_legacy_work_orders(self) -> int:
        """旧数据升级：按现有项目回填排程依据，生成标记为historical的工单。幂等。"""
        now = utc_now()
        with self._lock, self.conn:
            self._ensure_schedule_row()
            row = self.conn.execute(
                "SELECT legacy_migrated FROM schedule_state WHERE id=1").fetchone()
            if row["legacy_migrated"]:
                return 0
            items = self.conn.execute("SELECT * FROM items ORDER BY id").fetchall()
            for index, item in enumerate(items, 1):
                open_count = int(self.conn.execute(
                    "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                    (item["id"],)).fetchone()["n"])
                basis = schedule_basis(item["severity"], item["quantity"],
                                       item["threshold"], open_count)
                self.conn.execute(
                    """INSERT INTO work_orders(item_id, batch_no, line_no, team, status,
                       basis_severity, basis_open_records, basis_priority,
                       basis_deadline_hours, basis_version, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (item["id"], LEGACY_BATCH_NO, index, "历史回填", "historical",
                     basis["severity"], basis["open_records"], basis["priority"],
                     basis["deadline_hours"], 1, "migration", now, now))
            self.conn.execute("UPDATE schedule_state SET legacy_migrated=1 WHERE id=1")
            return len(items)

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

    def schedule_version(self) -> int:
        with self._lock, self.conn:
            self._ensure_schedule_row()
            row = self.conn.execute(
                "SELECT version FROM schedule_state WHERE id=1").fetchone()
            return int(row["version"])

    def _apply_batch_lines(self, batch_no: str, lines: list, actor: str,
                           now: str) -> tuple:
        applied, skipped = [], []
        for line in lines:
            line_no = line["line_no"]
            if line["action"] == "reassign":
                order = self.conn.execute(
                    "SELECT * FROM work_orders WHERE id=?",
                    (line["order_id"],)).fetchone()
                if order is None:
                    raise NotFoundError(f"工单{line['order_id']}不存在")
                if order["status"] != "pending":
                    skipped.append({"line_no": line_no, "order_id": order["id"],
                                    "reason": "工单已开工或为历史工单，保留原队伍与现场记录"})
                    continue
                self.conn.execute(
                    "UPDATE work_orders SET team=?, updated_at=? WHERE id=? AND status='pending'",
                    (line["team"], now, order["id"]))
                applied.append({"line_no": line_no, "action": "reassign",
                                "order_id": order["id"], "team": line["team"]})
                continue
            existing = self.conn.execute(
                "SELECT id FROM work_orders WHERE batch_no=? AND line_no=?",
                (batch_no, line_no)).fetchone()
            if existing is not None:
                # 重试时已落下的工单不再重复
                applied.append({"line_no": line_no, "action": "issue",
                                "order_id": int(existing["id"]), "team": line["team"],
                                "already_exists": True})
                continue
            item = self.conn.execute("SELECT * FROM items WHERE id=?",
                                     (line["item_id"],)).fetchone()
            if item is None:
                raise NotFoundError(f"项目{line['item_id']}不存在")
            open_count = int(self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (line["item_id"],)).fetchone()["n"])
            basis = schedule_basis(item["severity"], item["quantity"],
                                   item["threshold"], open_count)
            cur = self.conn.execute(
                """INSERT INTO work_orders(item_id, batch_no, line_no, team, status,
                   basis_severity, basis_open_records, basis_priority,
                   basis_deadline_hours, basis_version, created_by, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (line["item_id"], batch_no, line_no, line["team"], "pending",
                 basis["severity"], basis["open_records"], basis["priority"],
                 basis["deadline_hours"], 1, actor, now, now))
            applied.append({"line_no": line_no, "action": "issue",
                            "order_id": int(cur.lastrowid), "team": line["team"],
                            "basis": basis})
        return applied, skipped

    def commit_batch(self, batch_no: str, expected_version: int, lines: list,
                     actor: str) -> Dict[str, Any]:
        """批次提交：版本一致才写入；同批次号同内容重放返回原结果，不重复落单。"""
        now = utc_now()
        with self._lock, self.conn:
            self._ensure_schedule_row()
            existing = self.conn.execute(
                "SELECT * FROM work_order_batches WHERE batch_no=?",
                (batch_no,)).fetchone()
            if existing is not None:
                if json.loads(existing["lines"]) != lines:
                    raise ConflictError("批次号已存在且内容不一致，请按当前排程版本重新确认")
                result = json.loads(existing["result"])
                result.update({"batch_id": existing["id"], "batch_no": batch_no,
                               "replayed": True,
                               "schedule_version": int(existing["schedule_version"])})
                return result
            current = int(self.conn.execute(
                "SELECT version FROM schedule_state WHERE id=1").fetchone()["version"])
            if expected_version != current:
                raise ConflictError(
                    f"排程版本已变化，当前版本为{current}，请按当前版本重新确认")
            applied, skipped = self._apply_batch_lines(batch_no, lines, actor, now)
            result = {"applied": applied, "skipped": skipped}
            cur = self.conn.execute(
                """INSERT INTO work_order_batches(batch_no, actor, lines, result,
                   expected_version, schedule_version, created_at) VALUES(?,?,?,?,?,?,?)""",
                (batch_no, actor, json.dumps(lines, ensure_ascii=False, sort_keys=True),
                 json.dumps(result, ensure_ascii=False, sort_keys=True),
                 expected_version, current + 1, now))
            self.conn.execute("UPDATE schedule_state SET version=version+1 WHERE id=1")
            result.update({"batch_id": int(cur.lastrowid), "batch_no": batch_no,
                           "replayed": False, "schedule_version": current + 1})
            return result

    def get_work_order(self, order_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM work_orders WHERE id=?",
                                    (order_id,)).fetchone()
        if row is None:
            raise NotFoundError("工单不存在")
        return dict(row)

    def list_work_orders(self, item_id: Optional[int] = None,
                         status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM work_orders"
        clauses, params = [], []
        if item_id is not None:
            clauses.append("item_id=?")
            params.append(item_id)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def count_work_orders(self, status: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM work_orders WHERE status=?",
                (status,)).fetchone()
        return int(row["n"])

    def start_work_order(self, order_id: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE work_orders SET status='started', updated_at=?"
                " WHERE id=? AND status='pending'", (now, order_id))
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM work_orders WHERE id=?",
                                           (order_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("工单不存在")
                raise ConflictError("工单已开工或为历史工单，不能重复开工")
        return self.get_work_order(order_id)

    def add_work_order_log(self, order_id: int, note: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        order = self.get_work_order(order_id)
        if order["status"] != "started":
            raise ConflictError("工单未开工，不能登记现场记录")
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO work_order_logs(order_id, note, created_by, created_at)"
                " VALUES(?,?,?,?)", (order_id, note, actor, now))
            log_id = int(cur.lastrowid)
        with self._lock:
            row = self.conn.execute("SELECT * FROM work_order_logs WHERE id=?",
                                    (log_id,)).fetchone()
        return dict(row)

    def list_work_order_logs(self, order_id: int) -> List[Dict[str, Any]]:
        self.get_work_order(order_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM work_order_logs WHERE order_id=? ORDER BY id",
                (order_id,)).fetchall()
        return [dict(row) for row in rows]

    def update_item_assessment(self, item_id: int, severity: str, quantity: float,
                               threshold: float, expected_version: int) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET severity=?, quantity=?, threshold=?,
                   version=version+1, updated_at=? WHERE id=? AND version=?""",
                (severity, quantity, threshold, now, item_id, expected_version))
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?",
                                           (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def close_record(self, item_id: int, record_id: int) -> tuple:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM records WHERE id=? AND item_id=?",
                (record_id, item_id)).fetchone()
            if row is None:
                raise NotFoundError("记录不存在")
            if row["status"] == "closed":
                return dict(row), False
            self.conn.execute("UPDATE records SET status='closed' WHERE id=?",
                              (record_id,))
            row = self.conn.execute("SELECT * FROM records WHERE id=?",
                                    (record_id,)).fetchone()
            return dict(row), True

    def reschedule_pending_orders(self, item_id: int) -> tuple:
        """风险变化后重排：未开工工单依据失效并按当前风险重算，已开工不动。"""
        now = utc_now()
        with self._lock, self.conn:
            self._ensure_schedule_row()
            item = self.conn.execute("SELECT * FROM items WHERE id=?",
                                     (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("项目不存在")
            open_count = int(self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,)).fetchone()["n"])
            basis = schedule_basis(item["severity"], item["quantity"],
                                   item["threshold"], open_count)
            rows = self.conn.execute(
                "SELECT * FROM work_orders WHERE item_id=? AND status='pending'",
                (item_id,)).fetchall()
            changed = []
            for row in rows:
                old_basis = {"severity": row["basis_severity"],
                             "open_records": row["basis_open_records"],
                             "priority": row["basis_priority"],
                             "deadline_hours": row["basis_deadline_hours"],
                             "basis_version": row["basis_version"]}
                if (old_basis["severity"] == basis["severity"]
                        and old_basis["open_records"] == basis["open_records"]
                        and old_basis["priority"] == basis["priority"]
                        and old_basis["deadline_hours"] == basis["deadline_hours"]):
                    continue
                self.conn.execute(
                    """UPDATE work_orders SET basis_severity=?, basis_open_records=?,
                       basis_priority=?, basis_deadline_hours=?,
                       basis_version=basis_version+1, updated_at=? WHERE id=?""",
                    (basis["severity"], basis["open_records"], basis["priority"],
                     basis["deadline_hours"], now, row["id"]))
                changed.append({"order_id": row["id"], "team": row["team"],
                                "old_basis": old_basis,
                                "new_basis": dict(basis,
                                                  basis_version=row["basis_version"] + 1)})
            version = None
            if changed:
                self.conn.execute(
                    "UPDATE schedule_state SET version=version+1 WHERE id=1")
                version = int(self.conn.execute(
                    "SELECT version FROM schedule_state WHERE id=1").fetchone()["version"])
            return changed, version

    def close(self) -> None:
        with self._lock:
            self.conn.close()
