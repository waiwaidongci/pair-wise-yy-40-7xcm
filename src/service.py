from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, BATCH_ENTITY, CREATE_ROLES, DISPATCH_ROLES,
                    ENTITY, ORDER_LOG_ROLES, REASSESS_ROLES, RECORD_ROLES,
                    TITLE, VIEW_ROLES, WORK_ORDER_ENTITY, WORK_ORDER_STATES,
                    completion_blockers, escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        self._reschedule_pending(item_id, actor, "record_added")
        return record

    def close_record(self, item_id: int, record_id: int, actor: str,
                     role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        record, changed = self.repository.close_record(item_id, record_id)
        if changed:
            self.repository.append_audit("record_closed", ENTITY, item_id, actor,
                                         {"record_id": record_id})
            self._reschedule_pending(item_id, actor, "record_closed")
        return record

    def reassess_item(self, item_id: int, payload: Dict[str, Any], actor: str,
                      role: str) -> Dict[str, Any]:
        ensure_role(role, REASSESS_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", item["quantity"]), "quantity")
        threshold = require_number(payload.get("threshold", item["threshold"]),
                                   "threshold", 0.000001)
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        updated = self.repository.update_item_assessment(
            item_id, severity, quantity, threshold, expected_version)
        self.repository.append_audit("reassess", ENTITY, item_id, actor, {
            "old_severity": item["severity"], "new_severity": severity,
            "quantity": quantity, "threshold": threshold,
        })
        result = self.enrich(updated)
        result["rescheduled_orders"] = self._reschedule_pending(
            item_id, actor, "reassess")
        return result

    def _reschedule_pending(self, item_id: int, actor: str, reason: str) -> int:
        changed, version = self.repository.reschedule_pending_orders(item_id)
        if changed:
            self.repository.append_audit("reschedule", ENTITY, item_id, actor, {
                "reason": reason, "orders": changed, "schedule_version": version,
            })
        return len(changed)

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def commit_batch(self, payload: Dict[str, Any], actor: str,
                     role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        batch_no = require_text(payload.get("batch_no"), "batch_no", 100)
        expected = payload.get("expected_schedule_version")
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
            raise ValueError("expected_schedule_version必须是非负整数")
        raw_lines = payload.get("lines")
        if not isinstance(raw_lines, list) or not raw_lines:
            raise ValidationError("lines必须是非空数组")
        if len(raw_lines) > 500:
            raise ValidationError("单批次行数不能超过500")
        lines, seen = [], set()
        for raw in raw_lines:
            if not isinstance(raw, dict):
                raise ValidationError("批次行必须是对象")
            line_no = raw.get("line_no")
            if not isinstance(line_no, int) or isinstance(line_no, bool) or line_no < 1:
                raise ValidationError("line_no必须是正整数")
            if line_no in seen:
                raise ValidationError("line_no重复")
            seen.add(line_no)
            team = require_text(raw.get("team"), "team", 100)
            order_id, item_id = raw.get("order_id"), raw.get("item_id")
            if (order_id is None) == (item_id is None):
                raise ValidationError("每行必须且只能提供item_id或order_id")
            if order_id is not None:
                if not isinstance(order_id, int) or isinstance(order_id, bool) or order_id < 1:
                    raise ValidationError("order_id必须是正整数")
                lines.append({"line_no": line_no, "action": "reassign",
                              "order_id": order_id, "team": team})
            else:
                if not isinstance(item_id, int) or isinstance(item_id, bool) or item_id < 1:
                    raise ValidationError("item_id必须是正整数")
                lines.append({"line_no": line_no, "action": "issue",
                              "item_id": item_id, "team": team})
        lines.sort(key=lambda line: line["line_no"])
        result = self.repository.commit_batch(batch_no, expected, lines, actor)
        if not result["replayed"]:
            self.repository.append_audit("batch_commit", BATCH_ENTITY,
                                         result["batch_id"], actor, {
                "batch_no": batch_no, "applied": len(result["applied"]),
                "skipped": len(result["skipped"]),
                "schedule_version": result["schedule_version"],
            })
        return result

    def schedule_status(self, role: str) -> Dict[str, Any]:
        self._view(role)
        return {"version": self.repository.schedule_version(),
                "pending": self.repository.count_work_orders("pending"),
                "started": self.repository.count_work_orders("started")}

    def list_work_orders(self, role: str, item_id: Optional[int] = None,
                         status: Optional[str] = None) -> list:
        self._view(role)
        if status is not None and status not in WORK_ORDER_STATES:
            raise ValidationError("未知工单状态")
        return [self.enrich_order(order)
                for order in self.repository.list_work_orders(item_id, status)]

    def get_work_order(self, order_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        order = self.enrich_order(self.repository.get_work_order(order_id))
        order["logs"] = self.repository.list_work_order_logs(order_id)
        return order

    def start_work_order(self, order_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        order = self.repository.start_work_order(order_id, actor)
        self.repository.append_audit("start_order", WORK_ORDER_ENTITY, order_id,
                                     actor, {"item_id": order["item_id"],
                                             "team": order["team"]})
        return self.enrich_order(order)

    def add_work_order_log(self, order_id: int, payload: Dict[str, Any],
                           actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, ORDER_LOG_ROLES)
        actor = require_text(actor, "actor", 100)
        note = require_text(payload.get("note"), "note")
        order = self.repository.get_work_order(order_id)
        log = self.repository.add_work_order_log(order_id, note, actor)
        self.repository.append_audit("order_log", WORK_ORDER_ENTITY, order_id,
                                     actor, {"log_id": log["id"],
                                             "item_id": order["item_id"]})
        return log

    def enrich_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        item = self.repository.get_item(order["item_id"])
        open_count = self.repository.open_record_count(order["item_id"])
        result = dict(order)
        result["item_title"] = item["title"]
        result["item_status"] = item["status"]
        result["basis_stale"] = (order["basis_severity"] != item["severity"]
                                 or order["basis_open_records"] != open_count)
        return result

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
