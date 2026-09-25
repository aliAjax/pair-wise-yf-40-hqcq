import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class LabConclusionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.lab1 = Actor("LAB-1", "lab")
        self.lab2 = Actor("LAB-2", "lab")
        self.quar = Actor("Q-1", "quarantine")

    def tearDown(self):
        self.tmp.cleanup()

    def _quarantined(self, code="C-1"):
        consignment = self.service.create(
            self.admin,
            "consignment",
            {"code": code, "origin": "A", "destination": "B"},
        )
        self.service.transition(
            self.admin,
            consignment["id"],
            "inspect",
            {"inspector": "I-1", "inspection_result": "suspected"},
        )
        self.service.transition(
            self.admin,
            consignment["id"],
            "quarantine",
            {"pest_found": True, "sample_id": "FIELD-" + code},
        )
        return self.service.get(consignment["id"])

    def _register(self, consignment_id, sample="S-1", tester="LAB-1", result="negative"):
        return self.service.create(
            self.lab1,
            "lab_conclusion",
            {
                "consignment_id": consignment_id,
                "sample_id": sample,
                "tester": tester,
                "result": result,
            },
        )

    def test_register_then_review_by_another_experimenter(self):
        consignment = self._quarantined()
        conclusion = self._register(consignment["id"])
        self.assertEqual(conclusion["status"], "pending")
        self.assertIsNone(conclusion["data"]["reviewed_by"])
        self.assertEqual(conclusion["data"]["registered_by"], "LAB-1")

        approved = self.service.transition(self.lab2, conclusion["id"], "review", {})
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["data"]["reviewed_by"], "LAB-2")
        self.assertEqual(approved["data"]["effective_revision"], 1)

    def test_same_person_review_is_returned(self):
        consignment = self._quarantined()
        conclusion = self._register(consignment["id"])
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("LAB-1", "lab"), conclusion["id"], "review", {}
            )

    def test_duplicate_sample_id_is_returned_with_explanation(self):
        first_batch = self._quarantined("C-1")
        self._register(first_batch["id"], sample="S-DUP")
        second_batch = self._quarantined("C-2")
        with self.assertRaises(ConflictError) as ctx:
            self._register(second_batch["id"], sample="S-DUP")
        self.assertIn("S-DUP", str(ctx.exception))

    def test_two_active_conclusions_for_one_batch_are_rejected(self):
        consignment = self._quarantined()
        self._register(consignment["id"], sample="S-1")
        with self.assertRaises(ConflictError):
            self._register(consignment["id"], sample="S-2")

    def test_register_requires_quarantined_batch(self):
        consignment = self.service.create(
            self.admin, "consignment",
            {"code": "C-0", "origin": "A", "destination": "B"},
        )
        with self.assertRaises(ValidationError):
            self._register(consignment["id"])

    def test_release_and_destroy_follow_effective_conclusion(self):
        batch = self._quarantined()
        conclusion = self._register(batch["id"], result="negative")
        # 待复核（无生效结论）时，放行和销毁都不允许。
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.quar, batch["id"], "release",
                {"pest_found": False, "treatment": "completed"},
            )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.quar, batch["id"], "destroy",
                {"method": "incineration", "witnessed_by": "W-1"},
            )
        self.service.transition(self.lab2, conclusion["id"], "review", {})
        released = self.service.transition(
            self.quar, batch["id"], "release",
            {"pest_found": False, "treatment": "completed"},
        )
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["data"]["basis_conclusion_id"], conclusion["id"])
        self.assertEqual(released["data"]["basis_sample_id"], "S-1")
        self.assertEqual(released["data"]["basis_result"], "negative")

    def test_negative_conclusion_cannot_be_destroyed(self):
        batch = self._quarantined()
        conclusion = self._register(batch["id"], result="negative")
        self.service.transition(self.lab2, conclusion["id"], "review", {})
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.quar, batch["id"], "destroy",
                {"method": "incineration", "witnessed_by": "W-1"},
            )

    def test_positive_conclusion_cannot_be_released_but_can_be_destroyed(self):
        batch = self._quarantined()
        conclusion = self._register(batch["id"], result="positive")
        self.service.transition(self.lab2, conclusion["id"], "review", {})
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.quar, batch["id"], "release",
                {"pest_found": False, "treatment": "completed"},
            )
        destroyed = self.service.transition(
            self.quar, batch["id"], "destroy",
            {"method": "incineration", "witnessed_by": "W-1"},
        )
        self.assertEqual(destroyed["status"], "destroyed")
        self.assertEqual(destroyed["data"]["basis_result"], "positive")

    def test_amending_approved_result_invalidates_review(self):
        batch = self._quarantined()
        conclusion = self.service.get(
            self._register(batch["id"], result="negative")["id"]
        )
        self.service.transition(self.lab2, conclusion["id"], "review", {})
        amended = self.service.transition(
            self.lab1,
            conclusion["id"],
            "amend_result",
            {"result": "positive", "note": "复查镜检发现孢子"},
        )
        self.assertEqual(amended["status"], "pending")
        self.assertIsNone(amended["data"]["reviewed_by"])
        self.assertIsNone(amended["data"]["effective_revision"])
        self.assertEqual(amended["data"]["revision"], 2)
        self.assertEqual(amended["data"]["result"], "positive")
        self.assertEqual(len(amended["data"]["review_history"]), 1)
        archived = amended["data"]["review_history"][0]
        self.assertEqual(archived["reviewed_by"], "LAB-2")
        self.assertEqual(archived["result"], "negative")

        # 原复核失效期间不得处置批次。
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.quar, batch["id"], "destroy",
                {"method": "incineration", "witnessed_by": "W-1"},
            )
        # 由另一名实验员重新复核后，新结论才重新生效。
        reapproved = self.service.transition(
            self.lab2, amended["id"], "review", {}
        )
        self.assertEqual(reapproved["status"], "approved")
        self.assertEqual(reapproved["data"]["effective_revision"], 2)
        destroyed = self.service.transition(
            self.quar, batch["id"], "destroy",
            {"method": "incineration", "witnessed_by": "W-1"},
        )
        self.assertEqual(destroyed["data"]["basis_result"], "positive")

    def test_amending_pending_result_does_not_change_status(self):
        batch = self._quarantined()
        conclusion = self._register(batch["id"], result="negative")
        amended = self.service.transition(
            self.lab1, conclusion["id"], "amend_result", {"result": "positive"}
        )
        self.assertEqual(amended["status"], "pending")
        self.assertEqual(amended["data"]["revision"], 1)
        self.assertEqual(amended["data"]["review_history"], [])

    def test_amending_after_disposal_is_blocked(self):
        batch = self._quarantined()
        conclusion = self._register(batch["id"], result="negative")
        self.service.transition(self.lab2, conclusion["id"], "review", {})
        self.service.transition(
            self.quar, batch["id"], "release",
            {"pest_found": False, "treatment": "completed"},
        )
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.lab1, conclusion["id"], "amend_result",
                {"result": "positive", "note": "太晚了"},
            )

    def test_every_submission_and_processing_is_audited(self):
        batch = self._quarantined()
        conclusion = self._register(batch["id"], result="negative")
        self.service.transition(self.lab2, conclusion["id"], "review", {})
        self.service.transition(
            self.lab1, conclusion["id"], "amend_result",
            {"result": "positive", "note": "镜检修正"},
        )
        actions = [
            row["action"]
            for row in self.service.audit_log(conclusion["id"])
        ]
        self.assertEqual(actions, ["create", "review", "amend_result"])
        amend_row = self.service.audit_log(conclusion["id"])[-1]
        self.assertEqual(amend_row["from_status"], "approved")
        self.assertEqual(amend_row["to_status"], "pending")


if __name__ == "__main__":
    unittest.main()
