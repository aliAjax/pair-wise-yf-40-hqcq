import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class LabDeskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.lab_a = Actor("lab-a", "lab")
        self.lab_b = Actor("lab-b", "lab")
        self.quar = Actor("quar-1", "quarantine")

    def tearDown(self):
        self.tmp.cleanup()

    def _quarantined(self, code="C-100"):
        entity = self.service.create(
            self.admin, "consignment", {"code": code, "origin": "Port-A", "destination": "Farm-B"}
        )
        self.service.transition(
            self.admin, entity["id"], "inspect", {"inspector": "I-1", "inspection_result": "suspected"}
        )
        self.service.transition(
            self.quar, entity["id"], "quarantine", {"pest_found": True, "sample_id": code + "-S"}
        )
        return self.service.get(entity["id"])

    def _submit(self, consignment_id, sample_id="S-1", result="positive", actor=None):
        return self.service.create(
            actor or self.lab_a,
            "lab_report",
            {"consignment_id": consignment_id, "sample_id": sample_id, "result": result},
        )

    def test_submit_then_review_by_another_lab_member(self):
        consignment = self._quarantined()
        report = self._submit(consignment["id"])
        self.assertEqual(report["status"], "submitted")
        self.assertEqual(report["data"]["tested_by"], "lab-a")
        reviewed = self.service.transition(self.lab_b, report["id"], "review", {})
        self.assertEqual(reviewed["status"], "reviewed")
        self.assertEqual(reviewed["data"]["reviewed_by"], "lab-b")

    def test_duplicate_sample_id_is_returned_with_reason(self):
        consignment = self._quarantined()
        first = self._submit(consignment["id"])
        self.assertEqual(first["status"], "submitted")
        duplicate = self._submit(consignment["id"])
        self.assertEqual(duplicate["status"], "returned")
        self.assertIn("duplicate", duplicate["data"]["return_reason"])
        other = self._quarantined("C-200")
        ok = self._submit(other["id"])
        self.assertEqual(ok["status"], "submitted")

    def test_same_person_review_is_returned_with_reason(self):
        consignment = self._quarantined()
        report = self._submit(consignment["id"])
        returned = self.service.transition(self.lab_a, report["id"], "review", {})
        self.assertEqual(returned["status"], "returned")
        self.assertIn("differ", returned["data"]["return_reason"])
        resubmitted = self.service.transition(self.lab_a, report["id"], "resubmit", {})
        self.assertEqual(resubmitted["status"], "submitted")
        reviewed = self.service.transition(self.lab_b, report["id"], "review", {})
        self.assertEqual(reviewed["status"], "reviewed")

    def test_release_and_destroy_follow_reviewed_conclusion(self):
        consignment = self._quarantined()
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.quar, consignment["id"], "destroy", {"method": "incineration", "witnessed_by": "W-1"}
            )
        with self.assertRaises(ValidationError):
            self.service.transition(self.quar, consignment["id"], "release", {"treatment": "none"})
        report = self._submit(consignment["id"], result="positive")
        self.service.transition(self.lab_b, report["id"], "review", {})
        with self.assertRaises(ValidationError):
            self.service.transition(self.quar, consignment["id"], "release", {"treatment": "none"})
        destroyed = self.service.transition(
            self.quar, consignment["id"], "destroy", {"method": "incineration", "witnessed_by": "W-1"}
        )
        self.assertEqual(destroyed["status"], "destroyed")
        self.assertEqual(destroyed["data"]["lab_report_id"], report["id"])

    def test_release_allowed_with_reviewed_negative(self):
        consignment = self._quarantined()
        report = self._submit(consignment["id"], result="negative")
        self.service.transition(self.lab_b, report["id"], "review", {})
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.quar, consignment["id"], "destroy", {"method": "incineration", "witnessed_by": "W-1"}
            )
        released = self.service.transition(self.quar, consignment["id"], "release", {"treatment": "none"})
        self.assertEqual(released["status"], "released")

    def test_amending_result_invalidates_review(self):
        consignment = self._quarantined()
        report = self._submit(consignment["id"], result="positive")
        self.service.transition(self.lab_b, report["id"], "review", {})
        amended = self.service.transition(self.lab_a, report["id"], "amend", {"result": "negative"})
        self.assertEqual(amended["status"], "submitted")
        self.assertTrue(amended["data"]["review_invalidated"])
        self.assertIsNone(amended["data"]["reviewed_by"])
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.quar, consignment["id"], "destroy", {"method": "incineration", "witnessed_by": "W-1"}
            )
        self.service.transition(self.lab_b, report["id"], "review", {})
        released = self.service.transition(self.quar, consignment["id"], "release", {"treatment": "none"})
        self.assertEqual(released["status"], "released")

    def test_history_and_audit_keep_every_submission(self):
        consignment = self._quarantined()
        report = self._submit(consignment["id"])
        self.service.transition(self.lab_a, report["id"], "review", {})
        self.service.transition(self.lab_a, report["id"], "resubmit", {})
        self.service.transition(self.lab_b, report["id"], "review", {})
        events = [item["event"] for item in self.service.get(report["id"])["data"]["history"]]
        self.assertEqual(events, ["submitted", "returned", "resubmitted", "reviewed"])
        actions = [row["action"] for row in self.service.audit_log(entity_id=report["id"])]
        self.assertEqual(actions, ["create", "review", "resubmit", "review"])


if __name__ == "__main__":
    unittest.main()
