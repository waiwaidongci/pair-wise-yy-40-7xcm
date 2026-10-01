from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ValidationError, ensure_role, normalize_severity,
                     require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, BACKFILL_BATCH_NO, CREATE_ROLES, DISPATCH_ROLES,
                    ENTITY, RECORD_ROLES, TITLE, VIEW_ROLES, basis_is_stale,
                    completion_blockers, escalation_required, priority_score,
                    response_deadline_hours, role_for_transition, scheduling_basis,
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
        self._reconcile_item(item_id)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

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
            from .domain import ConflictError
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

    def _basis_for(self, item: Dict[str, Any]) -> Dict[str, Any]:
        open_count = self.repository.open_record_count(item["id"])
        return scheduling_basis(item["severity"], float(item["quantity"]),
                                float(item["threshold"]), open_count, item["version"])

    def _reconcile_item(self, item_id: int) -> list:
        """项目严重程度或未关闭记录变化后，重排未开工工单。"""
        item = self.repository.get_item(item_id)
        basis = self._basis_for(item)
        return self.repository.reconcile_work_orders(item_id, basis)

    def update_item(self, item_id: int, payload: Dict[str, Any],
                    expected_version: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        severity = normalize_severity(payload.get("severity", item["severity"]))
        quantity = require_number(payload.get("quantity", item["quantity"]), "quantity")
        threshold = require_number(payload.get("threshold", item["threshold"]),
                                   "threshold", 0.000001)
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        updated = self.repository.update_item(item_id, severity, quantity,
                                              threshold, expected_version, actor)
        revised = self._reconcile_item(item_id)
        self.repository.append_audit("update", ENTITY, item_id, actor, {
            "severity": severity, "quantity": quantity, "threshold": threshold,
            "revised_orders": len(revised),
        })
        return self.enrich(updated)

    def close_record(self, item_id: int, record_id: int, actor: str,
                     role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        self.repository.get_item(item_id)
        record = self.repository.close_record(record_id)
        revised = self._reconcile_item(item_id)
        self.repository.append_audit("record_closed", ENTITY, item_id, actor, {
            "record_id": record_id, "revised_orders": len(revised),
        })
        return record

    def _submit_one(self, batch_no: str, item_id: int, actor: str) -> tuple:
        item = self.repository.get_item(item_id)
        basis = self._basis_for(item)
        existing = self.repository.find_work_order(batch_no, item_id)
        if existing is None:
            try:
                order = self.repository.create_work_order(batch_no, item_id, basis, actor)
                return order, "created"
            except ConflictError:
                existing = self.repository.find_work_order(batch_no, item_id)
                if existing is None:
                    raise
        order, changed = self.repository.reconfirm_work_order(existing["id"], basis)
        if existing["status"] == "in_progress":
            return order, "kept"
        return order, "reconfirmed" if changed else "current"

    def submit_batch(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        batch_no = require_text(payload.get("batch_no"), "batch_no", 100)
        entries = payload.get("orders")
        if not isinstance(entries, list) or not entries:
            raise ValidationError("orders必须是非空列表")
        results = []
        for entry in entries:
            if not isinstance(entry, dict):
                results.append({"item_id": None, "ok": False,
                                "error": "每条工单必须是对象"})
                continue
            raw_id = entry.get("item_id")
            try:
                item_id = int(raw_id)
            except (TypeError, ValueError):
                results.append({"item_id": raw_id, "ok": False,
                                "error": "item_id必须是整数"})
                continue
            try:
                order, state = self._submit_one(batch_no, item_id, actor)
                results.append({"item_id": item_id, "ok": True, "state": state,
                                "order": self._enrich_order(order)})
            except ConflictError as exc:
                results.append({"item_id": item_id, "ok": False,
                                "error": exc.message, "kind": exc.kind})
            except Exception as exc:
                from .domain import DomainError
                if isinstance(exc, DomainError):
                    results.append({"item_id": item_id, "ok": False,
                                    "error": exc.message, "kind": exc.kind})
                else:
                    raise
        return {"batch_no": batch_no, "results": results}

    def start_work_order(self, order_id: int, payload: Dict[str, Any],
                          actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        team = require_text(payload.get("team"), "team", 100)
        site_record = require_text(payload.get("site_record"), "site_record")
        order = self.repository.start_work_order(order_id, team, site_record, actor)
        self.repository.append_audit("work_order_started", "work_order", order_id,
                                     actor, {"item_id": order["item_id"], "team": team})
        return self._enrich_order(order)

    def list_work_orders(self, role: str, item_id: Optional[int] = None,
                         status: Optional[str] = None, batch_no: Optional[str] = None,
                         historical: Optional[int] = None) -> list:
        self._view(role)
        if item_id is not None:
            try:
                item_id = int(item_id)
            except (TypeError, ValueError):
                raise ValidationError("item_id必须是整数")
        if historical is not None:
            try:
                historical = int(historical)
            except (TypeError, ValueError):
                raise ValidationError("historical必须是0或1")
        rows = self.repository.list_work_orders(item_id, status, batch_no, historical)
        return [self._enrich_order(row) for row in rows]

    def backfill_work_orders(self, actor: str, role: str) -> Dict[str, Any]:
        """旧数据升级：按现有项目回填排程依据并标成历史。"""
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        created = []
        for item in self.repository.list_items():
            if self.repository.has_work_orders(item["id"]):
                continue
            basis = self._basis_for(item)
            order = self.repository.create_work_order(BACKFILL_BATCH_NO, item["id"],
                                                      basis, actor, historical=1)
            created.append(self._enrich_order(order))
        self.repository.append_audit("work_order_backfill", "work_order", 0, actor,
                                     {"batch_no": BACKFILL_BATCH_NO,
                                      "created": len(created)})
        return {"batch_no": BACKFILL_BATCH_NO, "created": created}

    def _enrich_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(order)
        item = self.repository.get_item(order["item_id"])
        basis = self._basis_for(item)
        result["current_priority"] = basis["priority"]
        result["stale"] = basis_is_stale(order, basis)
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
