import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import (capacity_margin, capacity_sufficient, periods_overlap,
                       shutdown_conflicts, STATES, TRANSITION_ROLES)


def report_payload(item_id, start="2026-10-01T00:00:00Z", end="2026-10-03T00:00:00Z",
                   equipment="EQ-1", backup="BAK-1", capacity=100.0, outlets=None):
    return {
        "item_id": item_id, "equipment_id": equipment,
        "equipment_name": "一号除尘器", "period_start": start, "period_end": end,
        "backup_device_id": backup, "backup_device_name": "备用布袋",
        "backup_capacity": capacity,
        "affected_outlets": outlets if outlets is not None else [
            {"outlet_code": "OP-1", "declared_quantity": 80.0}],
    }


class ShutdownRulesTest(unittest.TestCase):
    def test_overlap_half_open(self):
        self.assertFalse(periods_overlap(
            "2026-10-01T00:00:00+00:00", "2026-10-02T00:00:00+00:00",
            "2026-10-02T00:00:00+00:00", "2026-10-03T00:00:00+00:00"))
        self.assertTrue(periods_overlap(
            "2026-10-01T00:00:00+00:00", "2026-10-02T01:00:00+00:00",
            "2026-10-02T00:00:00+00:00", "2026-10-03T00:00:00+00:00"))

    def test_margin_and_sufficient(self):
        self.assertEqual(capacity_margin(100, 80), 20)
        self.assertTrue(capacity_sufficient(100, 100))
        self.assertFalse(capacity_sufficient(79.9, 80))

    def test_conflicts_match_equipment_or_backup_only(self):
        existing = [{"id": 1, "status": "pending", "equipment_id": "EQ-1",
                     "backup_device_id": "BAK-1",
                     "period_start": "2026-10-01T00:00:00+00:00",
                     "period_end": "2026-10-03T00:00:00+00:00"}]
        clash = shutdown_conflicts(existing, "EQ-2", "BAK-1",
                                   "2026-10-02T00:00:00+00:00",
                                   "2026-10-04T00:00:00+00:00")
        self.assertEqual(len(clash), 1)
        self.assertEqual(clash[0]["reason"], "backup_device")
        no_clash = shutdown_conflicts(existing, "EQ-2", "BAK-2",
                                      "2026-10-04T00:00:00+00:00",
                                      "2026-10-05T00:00:00+00:00")
        self.assertEqual(no_clash, [])
        returned = [dict(existing[0], status="returned")]
        self.assertEqual(shutdown_conflicts(
            returned, "EQ-1", "BAK-1", "2026-10-02T00:00:00+00:00",
            "2026-10-02T12:00:00+00:00"), [])


class ShutdownServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "停运报备许可", "description": "shutdown flow",
             "severity": "high", "quantity": 80, "threshold": 100,
             "external_ref": "SD-1"}, "creator", "applicant")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _to_inspection(self):
        item = self.service.get_item(self.item["id"], "viewer")
        return self.service.transition(
            item["id"], "submitted", item["version"], "applicant-a", "applicant")

    def test_register_aggregates_outlets_and_enriches(self):
        payload = report_payload(self.item["id"], outlets=[
            {"outlet_code": "OP-1", "declared_quantity": 30.0},
            {"outlet_code": "OP-2", "declared_quantity": 45.0}])
        report = self.service.create_shutdown_report(payload, "ops", "applicant")
        self.assertEqual(report["status"], "pending")
        self.assertEqual(report["affected_quantity"], 75.0)
        self.assertEqual(report["capacity_margin"], 25.0)
        self.assertTrue(report["capacity_sufficient"])
        self.assertEqual(len(report["outlets"]), 2)

    def test_overlapping_same_equipment_or_backup_single_pending(self):
        self.service.create_shutdown_report(
            report_payload(self.item["id"]), "ops", "applicant")
        with self.assertRaises(ConflictError):
            self.service.create_shutdown_report(
                report_payload(self.item["id"], equipment="EQ-1", backup="BAK-9"),
                "ops", "applicant")
        with self.assertRaises(ConflictError):
            self.service.create_shutdown_report(
                report_payload(self.item["id"], equipment="EQ-9", backup="BAK-1"),
                "ops", "applicant")
        ok = self.service.create_shutdown_report(
            report_payload(self.item["id"], start="2026-10-03T00:00:00Z",
                           end="2026-10-04T00:00:00Z", equipment="EQ-9",
                           backup="BAK-9"), "ops", "applicant")
        self.assertEqual(ok["status"], "pending")

    def test_inspection_blocked_until_confirmed(self):
        self.service.create_shutdown_report(
            report_payload(self.item["id"]), "ops", "applicant")
        submitted = self._to_inspection()
        with self.assertRaises(ConflictError):
            self.service.transition(
                submitted["id"], "inspection", submitted["version"],
                "inspector-a", "inspector")

    def test_review_confirm_then_inspection_allowed(self):
        report = self.service.create_shutdown_report(
            report_payload(self.item["id"]), "ops", "applicant")
        reviewed = self.service.review_shutdown_report(
            report["id"], {"expected_version": report["version"]},
            "cm", "compliance_manager")
        self.assertEqual(reviewed["status"], "confirmed")
        self.assertEqual(reviewed["capacity_margin"], 20.0)
        submitted = self._to_inspection()
        inspection = self.service.transition(
            submitted["id"], "inspection", submitted["version"],
            "inspector-a", "inspector")
        self.assertEqual(inspection["status"], "inspection")

    def test_review_returned_when_margin_short(self):
        report = self.service.create_shutdown_report(
            report_payload(self.item["id"], capacity=50.0), "ops", "applicant")
        reviewed = self.service.review_shutdown_report(
            report["id"], {"expected_version": report["version"]},
            "cm", "compliance_manager")
        self.assertEqual(reviewed["status"], "returned")
        self.assertIsNotNone(reviewed["decision_note"])
        self.assertFalse(reviewed["capacity_sufficient"])
        with self.assertRaises(PermissionDenied):
            self.service.review_shutdown_report(
                report["id"], {"expected_version": reviewed["version"]},
                "ops", "applicant")

    def test_returned_frees_slot_amend_reenters_pending(self):
        first = self.service.create_shutdown_report(
            report_payload(self.item["id"], capacity=10.0), "ops", "applicant")
        returned = self.service.review_shutdown_report(
            first["id"], {"expected_version": first["version"]},
            "cm", "compliance_manager")
        self.service.create_shutdown_report(
            report_payload(self.item["id"], backup="BAK-2"), "ops", "applicant")
        amended = self.service.amend_shutdown_report(
            returned["id"], {"expected_version": returned["version"],
                             "backup_capacity": 120.0,
                             "period_start": "2026-10-03T00:00:00Z",
                             "period_end": "2026-10-04T00:00:00Z"}, "ops", "applicant")
        self.assertEqual(amended["status"], "pending")
        self.assertEqual(amended["capacity_margin"], 40.0)
        with self.assertRaises(ConflictError):
            self.service.amend_shutdown_report(
                amended["id"], {"expected_version": amended["version"],
                                "period_start": "2026-10-01T00:00:00Z",
                                "period_end": "2026-10-03T00:00:00Z",
                                "backup_device_id": "BAK-2"}, "ops", "applicant")

    def test_extend_early_resume_and_quantity_change_recalculate_and_audit(self):
        report = self.service.create_shutdown_report(
            report_payload(self.item["id"]), "ops", "applicant")
        confirmed = self.service.review_shutdown_report(
            report["id"], {"expected_version": report["version"]},
            "cm", "compliance_manager")
        extended = self.service.amend_shutdown_report(
            confirmed["id"], {"expected_version": confirmed["version"],
                              "period_end": "2026-10-05T00:00:00Z",
                              "reason": "停运延期"}, "ops", "applicant")
        self.assertEqual(extended["status"], "pending")
        self.assertEqual(extended["period_end"], "2026-10-05T00:00:00+00:00")
        changed = self.service.amend_shutdown_report(
            extended["id"], {"expected_version": extended["version"],
                             "affected_outlets": [
                                 {"outlet_code": "OP-1", "declared_quantity": 95.0}],
                             "reason": "申报量变化"}, "ops", "applicant")
        self.assertEqual(changed["affected_quantity"], 95.0)
        self.assertEqual(changed["capacity_margin"], 5.0)
        self.assertTrue(self.repo.verify_audit_chain())
        events = [e for e in self.repo.list_audit()
                  if e["entity_type"] == "停运报备" and e["entity_id"] == changed["id"]]
        actions = [e["action"] for e in events]
        self.assertEqual(
            actions, ["shutdown_create", "shutdown_confirm",
                      "shutdown_amend", "shutdown_amend"])
        early = self.service.amend_shutdown_report(
            changed["id"], {"expected_version": changed["version"],
                            "period_end": "2026-10-02T12:00:00Z",
                            "reason": "提前复产"}, "ops", "applicant")
        self.assertEqual(early["period_end"], "2026-10-02T12:00:00+00:00")

    def test_permissions_and_validation(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_shutdown_report(
                report_payload(self.item["id"]), "viewer-x", "viewer")
        with self.assertRaises(ValidationError):
            payload = report_payload(self.item["id"])
            payload["period_end"] = "2026-09-30T00:00:00Z"
            self.service.create_shutdown_report(payload, "ops", "applicant")
        with self.assertRaises(ValidationError):
            payload = report_payload(self.item["id"])
            payload["affected_outlets"] = []
            self.service.create_shutdown_report(payload, "ops", "applicant")
        with self.assertRaises(ValidationError):
            payload = report_payload(self.item["id"])
            payload["affected_outlets"] = [
                {"outlet_code": "OP-1", "declared_quantity": 80},
                {"outlet_code": "OP-1", "declared_quantity": 10}]
            self.service.create_shutdown_report(payload, "ops", "applicant")

    def test_version_conflict_on_review(self):
        report = self.service.create_shutdown_report(
            report_payload(self.item["id"]), "ops", "applicant")
        with self.assertRaises(ConflictError):
            self.service.review_shutdown_report(
                report["id"], {"expected_version": report["version"] + 1},
                "cm", "compliance_manager")


if __name__ == "__main__":
    unittest.main()
