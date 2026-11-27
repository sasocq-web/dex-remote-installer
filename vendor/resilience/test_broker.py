import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import sasocq_control

sasocq_control.__path__.insert(0, str(Path(__file__).resolve().parent / "broker-candidate"))

from sasocq_control import bounded_protocol, broker_queue, outcome_journal


class VmResourceStatusTest(unittest.TestCase):
    def setUp(self):
        self.request = {"action": "vm", "params": {"operation": "resource-status", "name": "sasocq-server"}}
        self.answer = {"ok": True, "result": {"returncode": 0, "output": {
            "available": True, "name": "sasocq-server", "state": "running",
            "current": {"vcpus": 6}, "recommended": {"vcpus": 6}}}}

    def test_report_keeps_vm_state_and_closes_queue_and_worker_receipts(self):
        self.assertTrue(bounded_protocol.completion(self.answer, request=self.request))
        self.assertEqual(self.answer["result"]["output"]["state"], "running")
        self.assertEqual(outcome_journal.compact(self.answer, limit=1, request=self.request)["result"]["returncode"], 0)
        with TemporaryDirectory() as folder:
            root = Path(folder)
            store = bounded_protocol.Receipts(root, boot="test")
            queue = broker_queue.Queue(store, None)
            fields = dict.fromkeys(outcome_journal.FIELDS, "test")
            fields.update(uid=1000, request_id="test", fingerprint="a" * 64)
            work = broker_queue.Work(1000, self.request, fields, root / "work.json", 0)
            queue.active = work
            queue.completed(work, self.answer)
            self.assertEqual(json.loads(work.path.read_bytes())["state"], "completed")
            self.assertFalse(queue.hold)
            with patch.object(outcome_journal, "scope", return_value=fields):
                self.assertTrue(outcome_journal.record(self.request, self.answer, root)["validated_completion"])

    def test_uncertainty_checks_still_apply(self):
        self.assertFalse(bounded_protocol.completion(self.answer))
        self.assertFalse(bounded_protocol.completion(self.answer, request={"action": "vm", "params": {"operation": "start"}}))
        for field in ("pending", "timed_out", "execution_uncertain"):
            self.answer["result"]["output"][field] = True
            self.assertFalse(bounded_protocol.completion(self.answer, request=self.request))
            del self.answer["result"]["output"][field]
        self.answer["result"]["state"] = "running"
        self.assertFalse(bounded_protocol.completion(self.answer, request=self.request))


if __name__ == "__main__":
    unittest.main()
