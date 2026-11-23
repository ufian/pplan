from unittest.mock import patch

from pplan.core import PlanError
from pplan.feedback import enqueue_improvement, list_improvements, update_improvement
from tests.test_plan import Fixture


class FeedbackTests(Fixture):
    def test_claim_complete_and_retry_notifies_once(self):
        request_id = enqueue_improvement(self.plan, 42, "Improve calendar labels")
        update_improvement(self.plan, 42, request_id, "in_progress")
        with self.assertRaises(PlanError):
            update_improvement(self.plan, 42, request_id, "in_progress")
        update_improvement(self.plan, 42, request_id, "completed", "Deployed and verified.")
        update_improvement(self.plan, 42, request_id, "completed", "Deployed and verified.")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outbox").fetchone()[0], 1)
        self.assertEqual(list_improvements(self.plan), [])
        self.assertEqual(list_improvements(self.plan, True)[0]["status"], "completed")

    def test_completion_and_notification_roll_back_together(self):
        request_id = enqueue_improvement(self.plan, 42, "Improve calendar labels")
        update_improvement(self.plan, 42, request_id, "in_progress")
        with patch.object(self.store, "enqueue", side_effect=RuntimeError("storage unavailable")):
            with self.assertRaises(RuntimeError):
                update_improvement(self.plan, 42, request_id, "completed", "Deployed and verified.")
        self.assertEqual(list_improvements(self.plan)[0]["status"], "in_progress")

    def test_only_owner_can_change_improvement_status(self):
        request_id = enqueue_improvement(self.plan, 42, "Improve calendar labels")
        with self.assertRaises(PlanError):
            update_improvement(self.plan, 99, request_id, "in_progress")
        self.assertEqual(list_improvements(self.plan)[0]["status"], "queued")

    def test_needs_input_can_be_requeued(self):
        request_id = enqueue_improvement(self.plan, 42, "Improve calendar labels")
        update_improvement(self.plan, 42, request_id, "in_progress")
        update_improvement(self.plan, 42, request_id, "needs_input", "Which labels?")
        update_improvement(self.plan, 42, request_id, "queued")
        self.assertEqual(list_improvements(self.plan)[0]["status"], "queued")
        self.assertIsNone(list_improvements(self.plan)[0]["result"])
