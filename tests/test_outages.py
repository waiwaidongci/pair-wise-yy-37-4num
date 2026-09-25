import tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import capacity_margin, outage_conflict, windows_overlap


def iso(dt):
    return dt.replace(microsecond=0).isoformat()


class OutageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "permit A", "description": "outage flow", "severity": "medium",
             "quantity": 5, "threshold": 10, "external_ref": "P-1"},
            "creator", "applicant")
        self.control = self.service.register_equipment(
            {"name": "喷淋塔", "kind": "control", "rated_capacity": 100,
             "external_ref": "EQ-C1"}, "ops", "applicant")
        self.backup = self.service.register_equipment(
            {"name": "备用活性炭", "kind": "substitute", "rated_capacity": 80,
             "external_ref": "EQ-S1"}, "ops", "applicant")
        self.start = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
        self.end = self.start + timedelta(hours=8)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _payload(self, quantity=60, **overrides):
        payload = {
            "equipment_id": self.control["id"],
            "item_id": self.item["id"],
            "start_time": iso(self.start),
            "end_time": iso(self.end),
            "affected_quantity": quantity,
            "affected_outlets": ["DA-01", "DA-02"],
            "substitutes": [{"equipment_id": self.backup["id"],
                             "rated_capacity": 80}],
            "reason": "年度检修",
        }
        payload.update(overrides)
        return payload

    def _register(self, **overrides):
        return self.service.create_outage(self._payload(**overrides), "ops", "applicant")

    def _confirm(self, report):
        return self.service.review_outage(
            report["id"], {"decision": "confirm",
                           "expected_version": report["version"]},
            "mgr", "compliance_manager")

    def test_register_captures_all_fields_and_margin(self):
        report = self._register()
        self.assertEqual(report["status"], "pending")
        self.assertEqual(report["equipment_id"], self.control["id"])
        self.assertEqual(report["outlets"], ["DA-01", "DA-02"])
        self.assertEqual(len(report["substitutes"]), 1)
        self.assertEqual(report["substitutes"][0]["rated_capacity"], 80)
        self.assertEqual(report["margin"], 20)
        self.assertTrue(report["capacity_sufficient"])
        self.assertEqual(report["effective_end_time"], iso(self.end))

    def test_overlap_and_margin_rules_are_pure(self):
        a0, a1 = self.start, self.end
        self.assertTrue(windows_overlap(a0, a1, self.start + timedelta(hours=4),
                                        self.end + timedelta(hours=2)))
        self.assertFalse(windows_overlap(a0, a1, self.end,
                                         self.end + timedelta(hours=2)))
        provided, margin = capacity_margin(60, [{"rated_capacity": 80}])
        self.assertEqual((provided, margin), (80, 20))
        provided, margin = capacity_margin(90, [{"rated_capacity": 80}])
        self.assertEqual(margin, -10)
        candidates = [{"id": 7, "equipment_id": self.control["id"],
                       "substitute_ids": [self.backup["id"]],
                       "window_start": a0, "window_end": a1}]
        conflicts = outage_conflict(self.control["id"], [self.backup["id"]],
                                    self.start + timedelta(hours=1),
                                    self.end, candidates)
        self.assertEqual(conflicts[0]["outage_id"], 7)
        self.assertEqual(len(conflicts[0]["resources"]), 2)

    def test_same_equipment_overlapping_window_keeps_single_pending(self):
        self._register()
        other_backup = self.service.register_equipment(
            {"name": "移动治理车", "kind": "substitute", "rated_capacity": 200},
            "ops", "applicant")
        with self.assertRaises(ConflictError):
            self._register(substitutes=[{"equipment_id": other_backup["id"]}])
        # 首尾相接不算重叠，可以报备
        self._register(start_time=iso(self.end),
                       end_time=iso(self.end + timedelta(hours=2)),
                       substitutes=[{"equipment_id": other_backup["id"]}])

    def test_same_substitute_overlapping_window_keeps_single_pending(self):
        self._register()
        other_control = self.service.register_equipment(
            {"name": "第二套治理设备", "kind": "control", "rated_capacity": 100},
            "ops", "applicant")
        with self.assertRaises(ConflictError):
            self._register(equipment_id=other_control["id"])
        # 不同时段可复用同一替代装置
        self._register(equipment_id=other_control["id"],
                       start_time=iso(self.end),
                       end_time=iso(self.end + timedelta(hours=1)))

    def test_manager_confirm_and_inspection_gate(self):
        report = self._register()
        # 未确认报备时，许可单不能进入复查
        submitted = self.service.transition(
            self.item["id"], "submitted", self.item["version"], "a", "applicant")
        with self.assertRaises(ConflictError):
            self.service.transition(submitted["id"], "inspection",
                                    submitted["version"], "i", "inspector")
        confirmed = self._confirm(report)
        self.assertEqual(confirmed["status"], "confirmed")
        inspection = self.service.transition(
            submitted["id"], "inspection", submitted["version"], "i", "inspector")
        self.assertEqual(inspection["status"], "inspection")

    def test_insufficient_capacity_is_returned_not_confirmed(self):
        report = self._register(quantity=90)
        self.assertFalse(report["capacity_sufficient"])
        with self.assertRaises(ConflictError):
            self._confirm(report)
        returned = self.service.review_outage(
            report["id"], {"decision": "return", "review_note": "余量不足",
                           "expected_version": report["version"]},
            "mgr", "compliance_manager")
        self.assertEqual(returned["status"], "returned")
        # 退回后许可单仍然不能进入复查
        item = self.service.get_item(self.item["id"], "viewer")
        submitted = self.service.transition(
            item["id"], "submitted", item["version"], "a", "applicant")
        with self.assertRaises(ConflictError):
            self.service.transition(submitted["id"], "inspection",
                                    submitted["version"], "i", "inspector")

    def test_extension_recalculates_and_reopens_review(self):
        confirmed = self._confirm(self._register())
        self.assertEqual(confirmed["version"], 2)
        extended = self.service.extend_outage(
            confirmed["id"],
            {"new_end_time": iso(self.end + timedelta(hours=4)),
             "expected_version": confirmed["version"]},
            "ops", "applicant")
        self.assertEqual(extended["status"], "pending")
        self.assertEqual(extended["end_time"], iso(self.end + timedelta(hours=4)))
        # 重算后余量不变但必须重新确认
        self.assertEqual(extended["margin"], 20)
        reconfirmed = self._confirm(extended)
        self.assertEqual(reconfirmed["status"], "confirmed")

    def test_early_resume_shortens_effective_window(self):
        confirmed = self._confirm(self._register())
        resumed = self.service.resume_outage(
            confirmed["id"],
            {"resumed_at": iso(self.end - timedelta(hours=2)),
             "expected_version": confirmed["version"]},
            "ops", "applicant")
        self.assertEqual(resumed["status"], "pending")
        self.assertEqual(resumed["effective_end_time"],
                         iso(self.end - timedelta(hours=2)))
        self.assertTrue(self.repo.verify_audit_chain())
        events = self.service.audit("viewer")
        actions = [e["action"] for e in events]
        self.assertIn("outage_resume", actions)

    def test_quantity_change_recalculates_margin_and_blocks_confirm(self):
        confirmed = self._confirm(self._register(quantity=60))
        changed = self.service.change_outage_quantity(
            confirmed["id"], {"affected_quantity": 90,
                              "expected_version": confirmed["version"]},
            "ops", "applicant")
        self.assertEqual(changed["status"], "pending")
        self.assertEqual(changed["margin"], -10)
        with self.assertRaises(ConflictError):
            self._confirm(changed)
        fixed = self.service.change_outage_quantity(
            changed["id"], {"affected_quantity": 50,
                            "expected_version": changed["version"]},
            "ops", "applicant")
        self.assertEqual(fixed["margin"], 30)
        self.assertEqual(self._confirm(fixed)["status"], "confirmed")

    def test_amendments_conflict_against_other_pending(self):
        first = self._register()
        # 第二笔紧邻第一笔之后，共用同一替代装置（首尾相接不冲突）
        second = self._register(
            start_time=iso(self.end),
            end_time=iso(self.end + timedelta(hours=2)))
        # 第三笔在第二笔之后，也共用同一替代装置
        third_control = self.service.register_equipment(
            {"name": "炉窑治理设施", "kind": "control", "rated_capacity": 100},
            "ops", "applicant")
        self._register(
            equipment_id=third_control["id"],
            start_time=iso(self.end + timedelta(hours=3)),
            end_time=iso(self.end + timedelta(hours=5)))
        # 第二笔延期到与第三笔重叠（共用替代装置）应被拒绝
        with self.assertRaises(ConflictError):
            self.service.extend_outage(
                second["id"],
                {"new_end_time": iso(self.end + timedelta(hours=4)),
                 "expected_version": second["version"]},
                "ops", "applicant")
        self.assertEqual(self.service.get_outage(first["id"], "viewer")["status"],
                         "pending")

    def test_validation_and_permission_guards(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_equipment(
                {"name": "x", "kind": "control"}, "ops", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.review_outage(1, {"decision": "confirm",
                                           "expected_version": 1},
                                       "mgr", "applicant")
        with self.assertRaises(ValidationError):
            self._register(substitutes=[])
        with self.assertRaises(ValidationError):
            self._register(affected_outlets=[])
        with self.assertRaises(ValidationError):
            self._register(start_time=iso(self.end), end_time=iso(self.start))
        report = self._register()
        with self.assertRaises(ValidationError):
            self.service.resume_outage(
                report["id"], {"resumed_at": iso(self.end),
                               "expected_version": report["version"]},
                "ops", "applicant")
        with self.assertRaises(ValidationError):
            self.service.extend_outage(
                report["id"], {"new_end_time": iso(self.end - timedelta(hours=1)),
                               "expected_version": report["version"]},
                "ops", "applicant")
        # 只有待审状态可审核
        self._confirm(report)
        with self.assertRaises(ConflictError):
            self._confirm(report)

    def test_version_conflict_on_review(self):
        report = self._register()
        with self.assertRaises(ConflictError):
            self.service.review_outage(
                report["id"], {"decision": "confirm",
                               "expected_version": report["version"] + 1},
                "mgr", "compliance_manager")

    def test_audit_trail_recalculations_archived(self):
        report = self._register()
        self._confirm(report)
        events = self.service.audit("viewer")
        actions = [e["action"] for e in events]
        self.assertIn("outage_register", actions)
        self.assertIn("outage_review", actions)
        register = next(e for e in events if e["action"] == "outage_register")
        self.assertEqual(register["entity_type"], "停运报备")
        self.assertEqual(register["detail"]["outlets"], ["DA-01", "DA-02"])
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
