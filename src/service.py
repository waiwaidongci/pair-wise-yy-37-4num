from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_equipment_kind, normalize_severity, parse_iso,
                     parse_window, require_code, require_int, require_number,
                     require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, EQUIPMENT_ENTITY,
                    EQUIPMENT_ROLES, OUTAGE_CREATE_ROLES, OUTAGE_ENTITY,
                    OUTAGE_REVIEW_ROLES, RECORD_ROLES, TITLE, VIEW_ROLES,
                    capacity_margin, completion_blockers, escalation_required,
                    outage_conflict, priority_score, response_deadline_hours,
                    role_for_transition, unconfirmed_outage_blockers,
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
            gate = unconfirmed_outage_blockers(
                self.repository.unconfirmed_outage_count(item_id))
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

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---- 治理设备 / 替代装置台账（报备数据承担方） ----

    def register_equipment(self, payload: Dict[str, Any], actor: str,
                           role: str) -> Dict[str, Any]:
        ensure_role(role, EQUIPMENT_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 200)
        kind = normalize_equipment_kind(payload.get("kind", "control"))
        rated_capacity = require_number(payload.get("rated_capacity", 0), "rated_capacity")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        equipment = self.repository.create_equipment(
            name, kind, rated_capacity, external_ref, actor)
        self.repository.append_audit("equipment_register", EQUIPMENT_ENTITY,
                                     equipment["id"], actor,
                                     {"name": name, "kind": kind,
                                      "rated_capacity": rated_capacity})
        return equipment

    def list_equipment(self, role: str, kind: Optional[str] = None) -> list:
        self._view(role)
        if kind is not None:
            kind = normalize_equipment_kind(kind)
        return self.repository.list_equipment(kind)

    def get_equipment(self, equipment_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.repository.get_equipment(equipment_id)

    # ---- 停运报备（登记、审核、延期/复产/量变） ----

    @staticmethod
    def _effective_end(report: Dict[str, Any]):
        if report.get("resumed_at"):
            return parse_iso(report["resumed_at"], "resumed_at")
        return parse_iso(report["end_time"], "end_time")

    @classmethod
    def _report_window(cls, report: Dict[str, Any]) -> Tuple[Any, Any]:
        return parse_iso(report["start_time"], "start_time"), cls._effective_end(report)

    def _load_substitute_inputs(self, value: Any) -> List[Dict[str, Any]]:
        if not isinstance(value, list) or not value:
            raise ValidationError("substitutes至少要登记一台替代装置")
        if len(value) > 20:
            raise ValidationError("substitutes不能超过20台")
        result = []
        seen = set()
        for entry in value:
            if not isinstance(entry, dict):
                raise ValidationError("substitutes条目必须是对象")
            equipment_id = require_int(entry.get("equipment_id"), "substitutes.equipment_id")
            if equipment_id in seen:
                raise ValidationError("替代装置不能重复登记")
            seen.add(equipment_id)
            equipment = self.repository.get_equipment(equipment_id)
            rated = entry.get("rated_capacity", equipment["rated_capacity"])
            result.append({"equipment_id": equipment_id,
                           "rated_capacity": require_number(rated, "substitutes.rated_capacity"),
                           "name": equipment["name"]})
        return result

    @staticmethod
    def _load_outlets(value: Any) -> List[str]:
        if not isinstance(value, list) or not value:
            raise ValidationError("affected_outlets至少要登记一个排放口")
        if len(value) > 20:
            raise ValidationError("affected_outlets不能超过20个")
        codes, seen = [], set()
        for entry in value:
            code = require_code(entry, "affected_outlets")
            if code in seen:
                raise ValidationError("受影响排放口不能重复登记")
            seen.add(code)
            codes.append(code)
        return codes

    def _pending_conflicts(self, equipment_id: int, substitute_ids: List[int],
                           start, end, exclude_id: Optional[int] = None):
        candidates = []
        for report in self.repository.pending_outages_touching(equipment_id, substitute_ids):
            if exclude_id is not None and report["id"] == exclude_id:
                continue
            cand_start, cand_end = self._report_window(report)
            candidates.append({
                "id": report["id"],
                "equipment_id": report["equipment_id"],
                "substitute_ids": [s["equipment_id"] for s in report["substitutes"]],
                "window_start": cand_start,
                "window_end": cand_end,
            })
        return outage_conflict(equipment_id, substitute_ids, start, end, candidates)

    def create_outage(self, payload: Dict[str, Any], actor: str,
                      role: str) -> Dict[str, Any]:
        ensure_role(role, OUTAGE_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        equipment_id = require_int(payload.get("equipment_id"), "equipment_id")
        equipment = self.repository.get_equipment(equipment_id)
        if equipment["kind"] != "control":
            raise ValidationError("停运报备的设备必须是治理设备")
        item_id = require_int(payload.get("item_id"), "item_id")
        self.repository.get_item(item_id)
        start, end = parse_window(payload.get("start_time"), payload.get("end_time"))
        affected = require_number(payload.get("affected_quantity"),
                                  "affected_quantity", 0.000001)
        reason = require_text(payload.get("reason"), "reason")
        outlets = self._load_outlets(payload.get("affected_outlets"))
        substitutes = self._load_substitute_inputs(payload.get("substitutes"))
        substitute_ids = [s["equipment_id"] for s in substitutes]
        if equipment_id in substitute_ids:
            raise ValidationError("停运设备不能同时作为自身的替代装置")
        provided, margin = capacity_margin(affected, substitutes)
        conflicts = self._pending_conflicts(equipment_id, substitute_ids, start, end)
        if conflicts:
            raise ConflictError("同一设备或替代装置在重叠时段只允许一笔待审报备："
                                + str(conflicts))
        report = self.repository.create_outage(
            equipment_id, item_id, start.isoformat(), end.isoformat(),
            affected, provided, margin, reason, outlets,
            [(s["equipment_id"], s["rated_capacity"]) for s in substitutes], actor)
        self.repository.append_audit("outage_register", OUTAGE_ENTITY, report["id"],
                                     actor, {"equipment_id": equipment_id,
                                             "item_id": item_id,
                                             "start_time": start.isoformat(),
                                             "end_time": end.isoformat(),
                                             "affected_quantity": affected,
                                             "outlets": outlets,
                                             "substitute_ids": substitute_ids,
                                             "margin": margin, "status": "pending"})
        return self.enrich_outage(report)

    def review_outage(self, outage_id: int, payload: Dict[str, Any], actor: str,
                      role: str) -> Dict[str, Any]:
        ensure_role(role, OUTAGE_REVIEW_ROLES)
        actor = require_text(actor, "actor", 100)
        decision = payload.get("decision")
        if decision not in ("confirm", "return"):
            raise ValidationError("decision必须是confirm或return")
        note = payload.get("review_note")
        if note is not None:
            note = require_text(note, "review_note", 2000)
        expected_version = require_int(payload.get("expected_version"),
                                       "expected_version")
        report = self.repository.get_outage(outage_id)
        if report["status"] != "pending":
            raise ConflictError(f"当前状态{report['status']}不可审核，仅待审报备可审核")
        provided, margin = capacity_margin(report["affected_quantity"],
                                           report["substitutes"])
        if decision == "confirm" and margin < 0:
            raise ConflictError(f"替代能力余量{margin:g}不足，不能确认，请退回")
        status = "confirmed" if decision == "confirm" else "returned"
        updated = self.repository.review_outage(
            outage_id, status, note, margin, provided, expected_version)
        self.repository.append_audit("outage_review", OUTAGE_ENTITY, outage_id, actor,
                                     {"decision": decision, "status": status,
                                      "affected_quantity": updated["affected_quantity"],
                                      "capacity_provided": provided, "margin": margin,
                                      "review_note": note})
        return self.enrich_outage(updated)

    def _amend_window_check(self, report: Dict[str, Any], substitute_ids: List[int],
                            start, end, exclude_id: int) -> None:
        conflicts = self._pending_conflicts(
            report["equipment_id"], substitute_ids, start, end, exclude_id)
        if conflicts:
            raise ConflictError("修改后与其它待审报备在设备或替代装置上时段重叠："
                                + str(conflicts))

    @staticmethod
    def _require_version(payload: Dict[str, Any]) -> int:
        return require_int(payload.get("expected_version"), "expected_version")

    def _recalculate(self, report: Dict[str, Any], affected: Optional[float] = None):
        quantity = affected if affected is not None else report["affected_quantity"]
        return capacity_margin(quantity, report["substitutes"]) + (quantity,)

    def extend_outage(self, outage_id: int, payload: Dict[str, Any], actor: str,
                      role: str) -> Dict[str, Any]:
        ensure_role(role, OUTAGE_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        expected_version = self._require_version(payload)
        report = self.repository.get_outage(outage_id)
        if report["resumed_at"]:
            raise ConflictError("已提前复产的报备不能延期")
        current_end = parse_iso(report["end_time"], "end_time")
        new_end = parse_iso(payload.get("new_end_time"), "new_end_time")
        if new_end <= current_end:
            raise ValidationError("延期后的end_time必须晚于当前end_time")
        start = parse_iso(report["start_time"], "start_time")
        substitute_ids = [s["equipment_id"] for s in report["substitutes"]]
        self._amend_window_check(report, substitute_ids, start, new_end, outage_id)
        provided, margin, _ = self._recalculate(report)
        updated = self.repository.amend_outage(
            outage_id, expected_version, end_time=new_end.isoformat(),
            capacity_provided=provided, margin=margin)
        self.repository.append_audit("outage_extend", OUTAGE_ENTITY, outage_id, actor,
                                     {"old_end_time": current_end.isoformat(),
                                      "new_end_time": new_end.isoformat(),
                                      "margin": margin, "status": "pending"})
        return self.enrich_outage(updated)

    def resume_outage(self, outage_id: int, payload: Dict[str, Any], actor: str,
                      role: str) -> Dict[str, Any]:
        ensure_role(role, OUTAGE_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        expected_version = self._require_version(payload)
        report = self.repository.get_outage(outage_id)
        if report["resumed_at"]:
            raise ConflictError("该报备已登记提前复产")
        start = parse_iso(report["start_time"], "start_time")
        end = parse_iso(report["end_time"], "end_time")
        resumed = parse_iso(payload.get("resumed_at"), "resumed_at")
        if not (start <= resumed < end):
            raise ValidationError("复产时间必须落在停运时段[start_time,end_time)内")
        substitute_ids = [s["equipment_id"] for s in report["substitutes"]]
        self._amend_window_check(report, substitute_ids, start, resumed, outage_id)
        provided, margin, _ = self._recalculate(report)
        updated = self.repository.amend_outage(
            outage_id, expected_version, resumed_at=resumed.isoformat(),
            capacity_provided=provided, margin=margin)
        self.repository.append_audit("outage_resume", OUTAGE_ENTITY, outage_id, actor,
                                     {"resumed_at": resumed.isoformat(),
                                      "planned_end_time": end.isoformat(),
                                      "margin": margin, "status": "pending"})
        return self.enrich_outage(updated)

    def change_outage_quantity(self, outage_id: int, payload: Dict[str, Any],
                               actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, OUTAGE_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        expected_version = self._require_version(payload)
        report = self.repository.get_outage(outage_id)
        if report["resumed_at"]:
            raise ConflictError("已提前复产的报备不能再变更申报量")
        new_quantity = require_number(payload.get("affected_quantity"),
                                      "affected_quantity", 0.000001)
        if new_quantity == report["affected_quantity"]:
            raise ValidationError("申报量没有变化")
        start, end = self._report_window(report)
        substitute_ids = [s["equipment_id"] for s in report["substitutes"]]
        self._amend_window_check(report, substitute_ids, start, end, outage_id)
        provided, margin, quantity = self._recalculate(report, new_quantity)
        updated = self.repository.amend_outage(
            outage_id, expected_version, affected_quantity=quantity,
            capacity_provided=provided, margin=margin)
        self.repository.append_audit("outage_quantity_change", OUTAGE_ENTITY,
                                     outage_id, actor,
                                     {"old_quantity": report["affected_quantity"],
                                      "new_quantity": quantity,
                                      "capacity_provided": provided,
                                      "margin": margin, "status": "pending"})
        return self.enrich_outage(updated)

    def get_outage(self, outage_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich_outage(self.repository.get_outage(outage_id))

    def list_outages(self, role: str, status: Optional[str] = None,
                     item_id: Optional[int] = None,
                     equipment_id: Optional[int] = None) -> list:
        self._view(role)
        return [self.enrich_outage(report)
                for report in self.repository.list_outages(status, item_id, equipment_id)]

    @staticmethod
    def enrich_outage(report: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(report)
        provided = sum(float(s["rated_capacity"]) for s in report["substitutes"])
        result["capacity_provided"] = provided
        result["margin"] = provided - float(report["affected_quantity"])
        result["capacity_sufficient"] = result["margin"] >= 0
        result["effective_end_time"] = (report["resumed_at"]
                                        if report.get("resumed_at")
                                        else report["end_time"])
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
