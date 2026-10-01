import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class WorkOrderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _item(self, title="wo item", severity="high", quantity=5, threshold=10, ref=None):
        return self.service.create_item(
            {"title": title, "description": "d", "severity": severity,
             "quantity": quantity, "threshold": threshold, "external_ref": ref},
            "creator", 'assessor')

    def _dispatch(self, batch_no, item_ids, actor="dispatcher", role="structural_engineer"):
        return self.service.submit_batch(
            {"batch_no": batch_no,
             "orders": [{"item_id": i} for i in item_ids]}, actor, role)

    def test_batch_submit_creates_orders_with_basis(self):
        item = self._item()
        out = self._dispatch("B1", [item["id"]])
        self.assertTrue(all(r["ok"] for r in out["results"]))
        order = out["results"][0]["order"]
        self.assertEqual(order["status"], "pending")
        self.assertEqual(order["batch_no"], "B1")
        self.assertEqual(order["basis_severity"], "high")
        self.assertEqual(order["basis_open_records"], 0)
        self.assertGreater(order["basis_priority"], 0)
        self.assertFalse(order["stale"])
        # list endpoint visibility
        listed = self.service.list_work_orders("viewer", item_id=item["id"])
        self.assertEqual(len(listed), 1)

    def test_severity_change_reschedules_pending_keeps_inprogress(self):
        item = self._item(severity="medium")
        out = self._dispatch("B1", [item["id"]])
        pending = out["results"][0]["order"]
        # start the order: team and site recorded
        started = self.service.start_work_order(
            pending["id"], {"team": "team-a", "site_record": "site-1"},
            "dispatcher", 'structural_engineer')
        self.assertEqual(started["status"], "in_progress")
        # a second, still-pending order for the same item
        out2 = self._dispatch("B2", [item["id"]])
        pending2 = out2["results"][0]["order"]
        # severity changes -> version bumps -> basis invalid
        self.service.update_item(item["id"], {"severity": "severe"},
                                 item["version"], "assessor", 'assessor')
        # in-progress order keeps team and site record, basis untouched
        kept = self.service.list_work_orders("viewer", item_id=item["id"],
                                              status="in_progress")[0]
        self.assertEqual(kept["team"], "team-a")
        self.assertEqual(kept["site_record"], "site-1")
        self.assertEqual(kept["basis_severity"], "medium")
        # pending order re-scheduled with new basis
        rescheduled = self.service.list_work_orders("viewer", item_id=item["id"],
                                                    batch_no="B2")[0]
        self.assertEqual(rescheduled["basis_severity"], "severe")
        self.assertEqual(rescheduled["revised_count"], 1)
        self.assertFalse(rescheduled["stale"])

    def test_open_record_change_reschedules_pending(self):
        item = self._item(severity="high")
        out = self._dispatch("B1", [item["id"]])
        order = out["results"][0]["order"]
        # adding an open record changes open-record count -> basis invalid
        self.service.add_record(item["id"], {"kind": "damage", "detail": "crack",
                                             "status": "open"}, "recorder", 'assessor')
        refreshed = self.service.list_work_orders("viewer", item_id=item["id"])[0]
        self.assertEqual(refreshed["basis_open_records"], 1)
        self.assertEqual(refreshed["revised_count"], 1)
        self.assertFalse(refreshed["stale"])
        # closing the record changes count again -> re-schedule once more
        rec = self.service.list_records(item["id"], "viewer")[0]
        self.service.close_record(item["id"], rec["id"], "recorder", 'assessor')
        refreshed = self.service.list_work_orders("viewer", item_id=item["id"])[0]
        self.assertEqual(refreshed["basis_open_records"], 0)
        self.assertEqual(refreshed["revised_count"], 2)

    def test_duplicate_batch_does_not_overwrite_started(self):
        item = self._item()
        self._dispatch("B1", [item["id"]])
        order = self.service.list_work_orders("viewer", item_id=item["id"])[0]
        self.service.start_work_order(
            order["id"], {"team": "team-a", "site_record": "site-1"},
            "dispatcher", 'structural_engineer')
        # two dispatchers submit the same batch: later one re-confirms, no overwrite
        again = self._dispatch("B1", [item["id"]], actor="dispatcher2")
        self.assertEqual(len(again["results"]), 1)
        self.assertTrue(again["results"][0]["ok"])
        self.assertEqual(again["results"][0]["state"], "kept")
        orders = self.service.list_work_orders("viewer", item_id=item["id"])
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["team"], "team-a")
        self.assertEqual(orders[0]["site_record"], "site-1")
        # re-starting is rejected
        with self.assertRaises(ConflictError):
            self.service.start_work_order(
                order["id"], {"team": "team-b", "site_record": "site-2"},
                "dispatcher", 'structural_engineer')

    def test_retry_after_partial_failure_is_idempotent(self):
        good = self._item(ref="GOOD-1")
        missing = 999999
        out = self.service.submit_batch(
            {"batch_no": "B9",
             "orders": [{"item_id": good["id"]}, {"item_id": missing}]},
            "dispatcher", 'structural_engineer')
        states = {r["item_id"]: r for r in out["results"]}
        self.assertTrue(states[good["id"]]["ok"])
        self.assertFalse(states[missing]["ok"])
        self.assertEqual(states[missing]["kind"], "not_found")
        # retry with the original batch number: already-issued order not repeated
        retry = self.service.submit_batch(
            {"batch_no": "B9",
             "orders": [{"item_id": good["id"]}, {"item_id": missing}]},
            "dispatcher", 'structural_engineer')
        good_state = next(r for r in retry["results"] if r["item_id"] == good["id"])
        self.assertTrue(good_state["ok"])
        self.assertEqual(good_state["state"], "current")
        orders = self.service.list_work_orders("viewer", batch_no="B9")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["item_id"], good["id"])

    def test_backfill_marks_historical_and_is_idempotent(self):
        item = self._item()
        out = self.service.backfill_work_orders("clerk", 'assessor')
        self.assertEqual(out["batch_no"], "BACKFILL")
        self.assertEqual(len(out["created"]), 1)
        hist = out["created"][0]
        self.assertEqual(hist["is_historical"], 1)
        self.assertEqual(hist["basis_severity"], "high")
        self.assertFalse(hist["stale"])
        # re-running backfill does not duplicate
        again = self.service.backfill_work_orders("clerk", 'assessor')
        self.assertEqual(len(again["created"]), 0)
        self.assertEqual(
            len(self.service.list_work_orders("viewer", item_id=item["id"])), 1)

    def test_backfill_skips_items_with_orders(self):
        item = self._item()
        self._dispatch("B1", [item["id"]])
        out = self.service.backfill_work_orders("clerk", 'assessor')
        self.assertEqual(len(out["created"]), 0)

    def test_audit_chain_and_transition_still_work(self):
        item = self._item()
        self._dispatch("B1", [item["id"]])
        current = item
        for target in STATES[1:]:
            current = self.service.transition(
                current["id"], target, current["version"],
                "reviewer", TRANSITION_ROLES[target][0])
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
