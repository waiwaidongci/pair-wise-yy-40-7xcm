import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, NotFoundError, PermissionDenied
from src.repository import Repository
from src.rules import LEGACY_BATCH_NO
from src.service import Service


class WorkOrderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "教学楼", "description": "三层框架结构", "severity": "high",
             "quantity": 12, "threshold": 6, "external_ref": "WO-ITEM-1"},
            "creator", "assessor")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _commit(self, batch_no, version, lines, actor="dispatcher-a"):
        return self.service.commit_batch(
            {"batch_no": batch_no, "expected_schedule_version": version,
             "lines": lines}, actor, "dispatcher")

    def test_batch_issue_and_version_conflict(self):
        self.assertEqual(self.service.schedule_status("dispatcher")["version"], 0)
        result = self._commit("B-1", 0, [
            {"line_no": 1, "item_id": self.item["id"], "team": "甲队"}])
        self.assertFalse(result["replayed"])
        self.assertEqual(result["schedule_version"], 1)
        order = self.service.get_work_order(result["applied"][0]["order_id"], "viewer")
        self.assertEqual(order["status"], "pending")
        self.assertEqual(order["team"], "甲队")
        self.assertEqual(order["basis_severity"], "high")
        self.assertEqual(order["basis_open_records"], 0)
        self.assertFalse(order["basis_stale"])
        # 版本过期：后到者必须按当前版本重新确认
        with self.assertRaises(ConflictError):
            self._commit("B-2", 0, [
                {"line_no": 1, "item_id": self.item["id"], "team": "乙队"}])
        with self.assertRaises(PermissionDenied):
            self.service.commit_batch(
                {"batch_no": "B-3", "expected_schedule_version": 1,
                 "lines": [{"line_no": 1, "item_id": self.item["id"], "team": "丙队"}]},
                "attacker", "viewer")

    def test_reassess_reschedules_pending_and_keeps_started(self):
        result = self._commit("B-10", 0, [
            {"line_no": 1, "item_id": self.item["id"], "team": "甲队"},
            {"line_no": 2, "item_id": self.item["id"], "team": "乙队"}])
        started_id = result["applied"][0]["order_id"]
        pending_id = result["applied"][1]["order_id"]
        self.service.start_work_order(started_id, "dispatcher-a", "dispatcher")
        self.service.add_work_order_log(started_id, {"note": "现场围挡已搭设"},
                                        "dispatcher-a", "dispatcher")
        item = self.service.get_item(self.item["id"], "viewer")
        updated = self.service.reassess_item(
            self.item["id"], {"severity": "severe",
                              "expected_version": item["version"]},
            "assessor-1", "assessor")
        self.assertEqual(updated["rescheduled_orders"], 1)
        # 已开工：保留队伍、原依据和现场记录
        started = self.service.get_work_order(started_id, "viewer")
        self.assertEqual(started["team"], "甲队")
        self.assertEqual(started["basis_severity"], "high")
        self.assertEqual(len(started["logs"]), 1)
        # 未开工：依据失效重排
        pending = self.service.get_work_order(pending_id, "viewer")
        self.assertEqual(pending["basis_severity"], "severe")
        self.assertEqual(pending["basis_version"], 2)
        self.assertFalse(pending["basis_stale"])
        self.assertEqual(self.service.schedule_status("dispatcher")["version"], 2)

    def test_record_backfill_and_close_trigger_reschedule(self):
        result = self._commit("B-20", 0, [
            {"line_no": 1, "item_id": self.item["id"], "team": "甲队"}])
        order_id = result["applied"][0]["order_id"]
        # 评估员补录受损记录 → 未关闭记录变化 → 未开工工单重排
        record = self.service.add_record(
            self.item["id"],
            {"kind": "damage", "detail": "补录：填充墙开裂", "status": "open",
             "external_ref": "DMG-1"}, "assessor-1", "assessor")
        order = self.service.get_work_order(order_id, "viewer")
        self.assertEqual(order["basis_open_records"], 1)
        self.assertEqual(order["basis_version"], 2)
        # 记录关闭 → 未关闭记录减少 → 再次重排
        self.service.close_record(self.item["id"], record["id"],
                                  "assessor-1", "assessor")
        order = self.service.get_work_order(order_id, "viewer")
        self.assertEqual(order["basis_open_records"], 0)
        self.assertEqual(order["basis_version"], 3)

    def test_concurrent_batch_reconfirm_never_overwrites_started(self):
        first = self._commit("B-30", 0, [
            {"line_no": 1, "item_id": self.item["id"], "team": "甲队"}])
        order_id = first["applied"][0]["order_id"]
        self.service.start_work_order(order_id, "dispatcher-a", "dispatcher")
        self.service.add_work_order_log(order_id, {"note": "已进场"},
                                        "dispatcher-a", "dispatcher")
        # 调度员B基于旧版本提交同一批次的改派 → 冲突，需按当前版本重新确认
        with self.assertRaises(ConflictError):
            self._commit("B-31", 0, [
                {"line_no": 1, "order_id": order_id, "team": "乙队"}],
                actor="dispatcher-b")
        current = self.service.schedule_status("dispatcher")["version"]
        result = self._commit("B-31", current, [
            {"line_no": 1, "order_id": order_id, "team": "乙队"}],
            actor="dispatcher-b")
        # 已开工内容不被覆盖：改派被跳过
        self.assertEqual(len(result["applied"]), 0)
        self.assertEqual(len(result["skipped"]), 1)
        order = self.service.get_work_order(order_id, "viewer")
        self.assertEqual(order["team"], "甲队")
        self.assertEqual(len(order["logs"]), 1)

    def test_retry_same_batch_after_failure_not_duplicated(self):
        lines = [{"line_no": 1, "item_id": self.item["id"], "team": "甲队"},
                 {"line_no": 2, "item_id": self.item["id"], "team": "乙队"}]
        result = self._commit("B-40", 0, lines)
        # 客户端未收到响应，按原批次号重试 → 重放原结果，不重复落单
        replay = self._commit("B-40", 0, lines)
        self.assertTrue(replay["replayed"])
        self.assertEqual([line["order_id"] for line in replay["applied"]],
                         [line["order_id"] for line in result["applied"]])
        self.assertEqual(
            len(self.service.list_work_orders("viewer", self.item["id"])), 2)
        # 同批次号不同内容 → 拒绝，要求重新确认
        with self.assertRaises(ConflictError):
            self._commit("B-40", 1, [
                {"line_no": 1, "item_id": self.item["id"], "team": "丙队"}])
        # 批次写入失败（项目不存在）→ 整体回滚，按原批次号修正后重试成功
        with self.assertRaises(NotFoundError):
            self._commit("B-41", 1, [{"line_no": 1, "item_id": 99999, "team": "甲队"}])
        fixed = self._commit("B-41", 1, [
            {"line_no": 1, "item_id": self.item["id"], "team": "甲队"}])
        self.assertFalse(fixed["replayed"])
        self.assertEqual(
            len(self.service.list_work_orders("viewer", self.item["id"])), 3)

    def test_start_and_log_guards(self):
        result = self._commit("B-50", 0, [
            {"line_no": 1, "item_id": self.item["id"], "team": "甲队"}])
        order_id = result["applied"][0]["order_id"]
        with self.assertRaises(ConflictError):
            self.service.add_work_order_log(order_id, {"note": "未开工登记"},
                                            "dispatcher-a", "dispatcher")
        with self.assertRaises(PermissionDenied):
            self.service.start_work_order(order_id, "attacker", "viewer")
        self.service.start_work_order(order_id, "dispatcher-a", "dispatcher")
        with self.assertRaises(ConflictError):
            self.service.start_work_order(order_id, "dispatcher-a", "dispatcher")


class MigrationTest(unittest.TestCase):
    def test_legacy_items_backfilled_as_historical(self):
        tmp = tempfile.TemporaryDirectory()
        db = str(Path(tmp.name) / "legacy.db")
        repo = Repository(db)
        service = Service(repo)
        item = service.create_item(
            {"title": "旧楼", "description": "升级前已存在的项目", "severity": "medium",
             "quantity": 3, "threshold": 3, "external_ref": "OLD-1"},
            "creator", "assessor")
        service.add_record(item["id"], {"kind": "damage", "detail": "旧记录",
                                        "status": "open"}, "creator", "assessor")
        repo.close()
        # 模拟升级前的旧库：迁移标记回退，重新打开触发回填
        conn = sqlite3.connect(db)
        conn.execute("UPDATE schedule_state SET legacy_migrated=0")
        conn.execute("DELETE FROM work_orders")
        conn.commit()
        conn.close()
        repo2 = Repository(db)
        service2 = Service(repo2)
        orders = service2.list_work_orders("viewer", item["id"])
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["status"], "historical")
        self.assertEqual(orders[0]["batch_no"], LEGACY_BATCH_NO)
        self.assertEqual(orders[0]["basis_severity"], "medium")
        self.assertEqual(orders[0]["basis_open_records"], 1)
        # 历史工单不参与重排
        current = service2.get_item(item["id"], "viewer")
        service2.reassess_item(item["id"], {"severity": "high",
                                            "expected_version": current["version"]},
                               "assessor-1", "assessor")
        orders = service2.list_work_orders("viewer", item["id"])
        self.assertEqual(orders[0]["basis_severity"], "medium")
        # 迁移幂等：再次打开不重复回填
        repo2.close()
        repo3 = Repository(db)
        self.assertEqual(
            len(Service(repo3).list_work_orders("viewer", item["id"])), 1)
        repo3.close()
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
