from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ensure_role,
                     normalize_severity, require_number, require_period, require_text)
from .repository import Repository
from .rules import (ACTIVE_SHUTDOWN_STATUSES, AUDIT_ROLES, CREATE_ROLES, ENTITY,
                    RECORD_ROLES, SHUTDOWN_AMEND_ROLES, SHUTDOWN_CREATE_ROLES,
                    SHUTDOWN_ENTITY, SHUTDOWN_REVIEW_ROLES, SHUTDOWN_VIEW_ROLES,
                    TITLE, VIEW_ROLES, capacity_margin, capacity_sufficient,
                    completion_blockers, escalation_required, inspection_blockers,
                    priority_score, response_deadline_hours, role_for_transition,
                    shutdown_conflicts, validate_transition)


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
            raise ConflictError("；".join(blockers))
        if target == "inspection":
            gate = inspection_blockers(
                item_id, self.repository.unconfirmed_shutdown_report_ids(item_id))
            if gate:
                raise ConflictError("；".join(gate))
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

    @staticmethod
    def _require_positive_int(value, field):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            from .domain import ValidationError
            raise ValidationError(f"{field}必须是正整数")
        return value

    def _parse_outlets(self, payload, permit_id=None):
        from .domain import ValidationError
        outlets = payload.get("affected_outlets")
        if not isinstance(outlets, list) or not outlets:
            raise ValidationError("affected_outlets不能为空，至少登记一个受影响排放口")
        result = []
        seen = set()
        for entry in outlets:
            if not isinstance(entry, dict):
                raise ValidationError("受影响排放口格式不正确")
            outlet_code = require_text(entry.get("outlet_code"), "outlet_code", 100)
            if outlet_code in seen:
                raise ValidationError(f"排放口{outlet_code}重复登记")
            seen.add(outlet_code)
            quantity = require_number(entry.get("declared_quantity", 0), "declared_quantity")
            item_id = entry.get("item_id", permit_id)
            item_id = self._require_positive_int(item_id, "item_id")
            self.repository.get_item(item_id)
            result.append({"item_id": item_id, "outlet_code": outlet_code,
                           "declared_quantity": quantity})
        return result

    def _enrich_shutdown(self, report):
        result = dict(report)
        result["capacity_margin"] = capacity_margin(
            report["backup_capacity"], report["affected_quantity"])
        result["capacity_sufficient"] = capacity_sufficient(
            report["backup_capacity"], report["affected_quantity"])
        return result

    def create_shutdown_report(self, payload, actor, role):
        ensure_role(role, SHUTDOWN_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        item_id = self._require_positive_int(payload.get("item_id"), "item_id")
        self.repository.get_item(item_id)
        equipment_id = require_text(payload.get("equipment_id"), "equipment_id", 100)
        equipment_name = require_text(payload.get("equipment_name"), "equipment_name", 200)
        period_start, period_end = require_period(
            payload.get("period_start"), payload.get("period_end"))
        backup_device_id = payload.get("backup_device_id")
        if backup_device_id is not None:
            backup_device_id = require_text(backup_device_id, "backup_device_id", 100)
        backup_device_name = payload.get("backup_device_name")
        if backup_device_name is not None:
            backup_device_name = require_text(backup_device_name, "backup_device_name", 200)
        backup_capacity = require_number(
            payload.get("backup_capacity", 0), "backup_capacity")
        outlets = self._parse_outlets(payload, permit_id=item_id)
        affected_quantity = round(sum(o["declared_quantity"] for o in outlets), 6)
        active = self.repository.list_shutdown_reports()
        conflicts = [c for c in shutdown_conflicts(
            active, equipment_id, backup_device_id, period_start, period_end)
            if c["status"] in ACTIVE_SHUTDOWN_STATUSES]
        if conflicts:
            raise ConflictError(
                "存在重叠时段的待审/已确认报备：" + ",".join(str(c["id"]) for c in conflicts))
        report = self.repository.create_shutdown_report(
            item_id, equipment_id, equipment_name, period_start, period_end,
            backup_device_id, backup_device_name, backup_capacity, affected_quantity,
            outlets, actor)
        margin = capacity_margin(backup_capacity, affected_quantity)
        self.repository.append_audit("shutdown_create", SHUTDOWN_ENTITY, report["id"], actor, {
            "item_id": item_id, "equipment_id": equipment_id,
            "period_start": period_start, "period_end": period_end,
            "backup_device_id": backup_device_id, "backup_capacity": backup_capacity,
            "affected_quantity": affected_quantity, "capacity_margin": margin,
            "outlet_count": len(outlets),
        })
        return self._enrich_shutdown(report)

    def review_shutdown_report(self, report_id, payload, actor, role):
        ensure_role(role, SHUTDOWN_REVIEW_ROLES)
        actor = require_text(actor, "actor", 100)
        expected_version = self._require_positive_int(
            payload.get("expected_version"), "expected_version")
        report = self.repository.get_shutdown_report(report_id)
        if report["status"] != "pending":
            raise ConflictError("只有待审报备可以审核")
        margin = capacity_margin(
            report["backup_capacity"], report["affected_quantity"])
        note = payload.get("note")
        if note is not None:
            note = require_text(note, "note", 2000)
        if margin < 0:
            status = "returned"
            if not note:
                note = (f"替代装置能力{report['backup_capacity']}低于受影响申报量"
                        f"{report['affected_quantity']}，余量{margin}不足，退回整改")
        else:
            status = "confirmed"
        updated = self.repository.review_shutdown_report(
            report_id, status, note, margin, expected_version, actor)
        self.repository.append_audit(
            "shutdown_return" if status == "returned" else "shutdown_confirm",
            SHUTDOWN_ENTITY, report_id, actor, {
                "from": report["status"], "to": status, "note": note,
                "affected_quantity": report["affected_quantity"],
                "backup_capacity": report["backup_capacity"],
                "capacity_margin": margin,
                "period_start": report["period_start"], "period_end": report["period_end"],
            })
        return self._enrich_shutdown(updated)

    def amend_shutdown_report(self, report_id, payload, actor, role):
        ensure_role(role, SHUTDOWN_AMEND_ROLES)
        actor = require_text(actor, "actor", 100)
        expected_version = self._require_positive_int(
            payload.get("expected_version"), "expected_version")
        report = self.repository.get_shutdown_report(report_id)
        values = {}
        if "equipment_id" in payload:
            values["equipment_id"] = require_text(payload["equipment_id"], "equipment_id", 100)
        if "equipment_name" in payload:
            values["equipment_name"] = require_text(
                payload["equipment_name"], "equipment_name", 200)
        period_start = report["period_start"]
        period_end = report["period_end"]
        if "period_start" in payload or "period_end" in payload:
            period_start, period_end = require_period(
                payload.get("period_start", report["period_start"]),
                payload.get("period_end", report["period_end"]))
            values["period_start"] = period_start
            values["period_end"] = period_end
        backup_device_id = report["backup_device_id"]
        if "backup_device_id" in payload:
            backup_device_id = payload["backup_device_id"]
            if backup_device_id is not None:
                backup_device_id = require_text(
                    backup_device_id, "backup_device_id", 100)
            values["backup_device_id"] = backup_device_id
        if "backup_device_name" in payload:
            backup_device_name = payload["backup_device_name"]
            if backup_device_name is not None:
                backup_device_name = require_text(
                    backup_device_name, "backup_device_name", 200)
            values["backup_device_name"] = backup_device_name
        backup_capacity = report["backup_capacity"]
        if "backup_capacity" in payload:
            backup_capacity = require_number(
                payload["backup_capacity"], "backup_capacity")
            values["backup_capacity"] = backup_capacity
        outlets = None
        affected_quantity = report["affected_quantity"]
        if "affected_outlets" in payload:
            outlets = self._parse_outlets(payload, permit_id=report["item_id"])
            affected_quantity = round(
                sum(o["declared_quantity"] for o in outlets), 6)
            values["affected_quantity"] = affected_quantity
        values["backup_capacity"] = backup_capacity
        values["affected_quantity"] = affected_quantity
        equipment_id = values.get("equipment_id", report["equipment_id"])
        conflicts = [c for c in shutdown_conflicts(
            self.repository.list_shutdown_reports(),
            equipment_id, backup_device_id, period_start, period_end,
            exclude_id=report_id) if c["status"] in ACTIVE_SHUTDOWN_STATUSES]
        if conflicts:
            raise ConflictError(
                "存在重叠时段的待审/已确认报备：" + ",".join(str(c["id"]) for c in conflicts))
        before = {"status": report["status"], "period_start": report["period_start"],
                  "period_end": report["period_end"],
                  "backup_capacity": report["backup_capacity"],
                  "affected_quantity": report["affected_quantity"]}
        updated = self.repository.amend_shutdown_report(
            report_id, values, outlets, expected_version, actor)
        margin = capacity_margin(updated["backup_capacity"], updated["affected_quantity"])
        self.repository.append_audit("shutdown_amend", SHUTDOWN_ENTITY, report_id, actor, {
            "from": before,
            "to": {"period_start": updated["period_start"],
                   "period_end": updated["period_end"],
                   "backup_capacity": updated["backup_capacity"],
                   "affected_quantity": updated["affected_quantity"],
                   "outlet_count": len(updated["outlets"])},
            "capacity_margin": margin, "reason": payload.get("reason"),
            "reset_to": "pending",
        })
        return self._enrich_shutdown(updated)

    def get_shutdown_report(self, report_id, role):
        ensure_role(role, SHUTDOWN_VIEW_ROLES)
        return self._enrich_shutdown(self.repository.get_shutdown_report(report_id))

    def list_shutdown_reports(self, role, status=None, item_id=None):
        ensure_role(role, SHUTDOWN_VIEW_ROLES)
        if status is not None and status not in ("pending", "confirmed", "returned"):
            from .domain import ValidationError
            raise ValidationError("status不在允许范围内")
        return [self._enrich_shutdown(report)
                for report in self.repository.list_shutdown_reports(status, item_id)]

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

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
