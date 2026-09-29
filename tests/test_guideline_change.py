import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import CorpusDB, DomainError


class GuidelineChangeTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = CorpusDB(self.path)
        self.a1 = self.db.add_user("甲", "annotator")
        self.a2 = self.db.add_user("乙", "annotator")
        self.a3 = self.db.add_user("丙", "annotator")
        self.arb = self.db.add_user("仲裁", "arbitrator")
        self.mgr1 = self.db.add_user("管理甲", "manager")
        self.mgr2 = self.db.add_user("管理乙", "manager")
        self.v1 = self.db.add_guideline("v1", "标签仅可为 正向/负向/中性。")
        self.v2 = self.db.add_guideline("v2", "标签仅可为 正向/负向/中性/待定；流行语默认待定。")
        self.batch = self.db.create_batch("测试批次", self.v1)
        self.item1 = self.db.add_item(self.batch, 1, "这个版本很快。")
        self.item2 = self.db.add_item(self.batch, 2, "没有明显变化。")
        for item in (self.item1, self.item2):
            self.db.assign(item, self.a1)
            self.db.assign(item, self.a2)
        self.db.submit_annotation(self.item1, self.a1, "正向")
        self.db.submit_annotation(self.item1, self.a2, "中性")
        self.db.submit_annotation(self.item2, self.a1, "中性")
        self.db.submit_annotation(self.item2, self.a2, "中性")

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def _complete_new(self, item, labels):
        for annotator, label in zip((self.a1, self.a2), labels):
            self.db.submit_annotation(item, annotator, label)

    # ------------------------------------------------------------- 主流程

    def test_pending_todos_generated_from_old_guidelines(self):
        status = self.db.create_guideline_change(self.batch, self.mgr1, self.v2)
        change = status["change"]
        self.assertEqual("pending", change["status"])
        self.assertEqual(2, status["todo_total"])
        self.assertEqual(0, status["todo_done"])
        # 版本差异可见
        self.assertTrue(any("--- 原指南 v1" in line for line in status["diff_lines"]))
        self.assertTrue(any("+++ 新指南 v2" in line for line in status["diff_lines"]))
        self.assertTrue(any(line.startswith("+") and "待定" in line for line in status["diff_lines"]))
        # 待办原因：item1 原指南有未仲裁分歧，item2 一致；两者都未按新指南补标
        by_item = {t["item_id"]: t for t in status["todos"]}
        reasons1 = " ".join(by_item[self.item1]["reasons"])
        self.assertIn("未仲裁分歧", reasons1)
        self.assertIn("正向/中性", reasons1)
        reasons2 = " ".join(by_item[self.item2]["reasons"])
        self.assertIn("标注一致", reasons2)
        self.assertTrue(any("尚未按新指南" in r for r in by_item[self.item1]["reasons"]))
        # 执行中不能冻结
        with self.assertRaisesRegex(DomainError, "换版执行中"):
            self.db.freeze_batch(self.batch, self.mgr1)
        # 执行中不能新增条目（范围在发起时确定）
        with self.assertRaisesRegex(DomainError, "换版执行中不能新增"):
            self.db.add_item(self.batch, 3, "新句子")
        # 未补齐不能确认
        with self.assertRaisesRegex(DomainError, "待补标"):
            self.db.confirm_guideline_change(self.batch, self.mgr1)

    def test_complete_supplement_then_confirm_takes_effect(self):
        self.db.create_guideline_change(self.batch, self.mgr1, self.v2)
        # 范围内提交默认落在新指南
        ann_id = self.db.submit_annotation(self.item1, self.a1, "正向")
        row = self.db.conn.execute(
            "SELECT guideline_id FROM annotations WHERE id=?", (ann_id,)
        ).fetchone()
        self.assertEqual(self.v2, row["guideline_id"])
        self.db.submit_annotation(self.item1, self.a2, "正向")
        self.db.submit_annotation(self.item2, self.a1, "中性")
        self.db.submit_annotation(self.item2, self.a2, "待定")
        status = self.db.guideline_change_status(self.batch)
        self.assertEqual(2, status["todo_total"])
        self.assertEqual(1, status["todo_done"])  # item1 一致完成；item2 新分歧
        with self.assertRaisesRegex(DomainError, "待补标"):
            self.db.confirm_guideline_change(self.batch, self.mgr1)
        # 旧指南的仲裁不能算进新指南：先给 v1 分歧仲裁
        self.db.adjudicate(self.item1, "正向", "v1规则下明确表达积极", self.arb, self.v1)
        status = self.db.guideline_change_status(self.batch)
        item2 = next(t for t in status["todos"] if t["item_id"] == self.item2)
        self.assertFalse(item2["done"])
        self.assertIn("需按新指南仲裁", " ".join(item2["reasons"]))
        # 范围内仲裁默认按新指南
        self.db.adjudicate(self.item2, "待定", "v2新增待定标签适用", self.arb)
        result = self.db.confirm_guideline_change(self.batch, self.mgr1)
        self.assertEqual("effective", result["change"]["status"])
        self.assertFalse(result["idempotent"])
        # 条目与批次都指向新指南
        self.assertEqual(
            self.v2,
            self.db.conn.execute("SELECT guideline_id FROM items WHERE id=?", (self.item1,)).fetchone()[0],
        )
        self.assertEqual(
            self.v2,
            self.db.conn.execute("SELECT guideline_id FROM batches WHERE id=?", (self.batch,)).fetchone()[0],
        )
        # 生效后不能再按旧指南补交
        with self.assertRaisesRegex(DomainError, "只能按当前指南"):
            self.db.submit_annotation(self.item1, self.a1, "负向", guideline_id=self.v1)
        # 冻结与导出版本一致
        frozen = self.db.freeze_batch(self.batch, self.mgr1)
        self.assertIsNotNone(frozen["metrics"]["pairwise_agreement"])
        records = {r["item_id"]: r for r in self.db.export_gold(self.batch)["records"]}
        self.assertEqual("v2", records[self.item1]["guideline_version"])
        self.assertEqual("v2", records[self.item2]["guideline_version"])
        self.assertEqual("adjudication", records[self.item2]["source"])

    # ------------------------------------------------- 失效与实时重算

    def test_late_old_annotation_invalidates_list_and_bumps_revision(self):
        self.db.create_guideline_change(self.batch, self.mgr1, self.v2)
        rev0 = self.db.guideline_change_status(self.batch)["change"]["revision"]
        self.db.assign(self.item1, self.a3)
        # 执行中有人按旧指南补交
        self.db.submit_annotation(self.item1, self.a3, "正向", guideline_id=self.v1)
        status = self.db.guideline_change_status(self.batch)
        self.assertEqual(rev0 + 1, status["change"]["revision"])
        todo = next(t for t in status["todos"] if t["item_id"] == self.item1)
        self.assertEqual(3, todo["old"]["annotations"])
        self.assertFalse(todo["done"])

    def test_adjudication_change_invalidates_and_old_conclusion_not_counted(self):
        self.db.create_guideline_change(self.batch, self.mgr1, self.v2)
        rev0 = self.db.guideline_change_status(self.batch)["change"]["revision"]
        # 先按旧指南仲裁掉 item1 的分歧
        self.db.adjudicate(self.item1, "正向", "旧规则下的仲裁结论", self.arb, self.v1)
        rev1 = self.db.guideline_change_status(self.batch)["change"]["revision"]
        self.assertEqual(rev0 + 1, rev1)
        # 新补标产生分歧
        self.db.submit_annotation(self.item1, self.a1, "中性")
        self.db.submit_annotation(self.item1, self.a2, "待定")
        todo = next(
            t for t in self.db.guideline_change_status(self.batch)["todos"]
            if t["item_id"] == self.item1
        )
        # 旧仲裁不能当作新指南结论
        self.assertTrue(todo["new"]["disagreement"])
        self.assertFalse(todo["new"]["adjudicated"])
        self.assertFalse(todo["done"])
        # 补交新标注会使该指南旧仲裁失效（此处本来就没有）
        self.db.adjudicate(self.item1, "待定", "按 v2 流行语规则应判待定", self.arb, self.v2)
        todo = next(
            t for t in self.db.guideline_change_status(self.batch)["todos"]
            if t["item_id"] == self.item1
        )
        self.assertTrue(todo["done"])
        self.assertEqual("待定", todo["new"]["final_label"])

    def test_supplement_annotation_wipes_same_guideline_adjudication(self):
        self.db.create_guideline_change(self.batch, self.mgr1, self.v2)
        self.db.submit_annotation(self.item1, self.a1, "待定")
        self.db.submit_annotation(self.item1, self.a2, "待定")
        self.db.adjudicate(self.item1, "待定", "v2 下一致后仲裁留痕", self.arb, self.v2)
        self.assertTrue(
            next(t for t in self.db.guideline_change_status(self.batch)["todos"]
                 if t["item_id"] == self.item1)["done"]
        )
        # 有人修改新指南标注 -> 该指南仲裁结论过期，需重算
        self.db.submit_annotation(self.item1, self.a1, "中性")
        todo = next(
            t for t in self.db.guideline_change_status(self.batch)["todos"]
            if t["item_id"] == self.item1
        )
        self.assertFalse(todo["new"]["adjudicated"])
        self.assertFalse(todo["done"])
        # 旧指南仲裁不受新指南补交影响
        self.db.adjudicate(self.item1, "正向", "旧规则下的仲裁结论", self.arb, self.v1)
        self.db.submit_annotation(self.item1, self.a1, "待定")
        adj = self.db.conn.execute(
            "SELECT COUNT(*) FROM adjudications WHERE item_id=? AND guideline_id=?",
            (self.item1, self.v1),
        ).fetchone()[0]
        self.assertEqual(1, adj)

    # ------------------------------------------------------- 范围与中断

    def test_out_of_scope_items_keep_old_guideline(self):
        # 只把 item1 纳入换版范围
        self.db.create_guideline_change(self.batch, self.mgr1, self.v2, [self.item1])
        self.db.submit_annotation(self.item1, self.a1, "正向")
        self.db.submit_annotation(self.item1, self.a2, "正向")
        result = self.db.confirm_guideline_change(self.batch, self.mgr1)
        self.assertEqual([self.item1], result["change"]["item_ids"])
        self.assertEqual(
            self.v2,
            self.db.conn.execute("SELECT guideline_id FROM items WHERE id=?", (self.item1,)).fetchone()[0],
        )
        self.assertEqual(
            self.v1,
            self.db.conn.execute("SELECT guideline_id FROM items WHERE id=?", (self.item2,)).fetchone()[0],
        )
        # 部分换版时批次默认指南保持不变
        self.assertEqual(
            self.v1,
            self.db.conn.execute("SELECT guideline_id FROM batches WHERE id=?", (self.batch,)).fetchone()[0],
        )
        # 冻结/导出按条目记录各自版本
        self.db.freeze_batch(self.batch, self.mgr1)
        records = {r["item_id"]: r for r in self.db.export_gold(self.batch)["records"]}
        self.assertEqual("v2", records[self.item1]["guideline_version"])
        self.assertEqual("v1", records[self.item2]["guideline_version"])

    def test_cancel_interrupts_and_resubmit_starts_fresh(self):
        first = self.db.create_guideline_change(self.batch, self.mgr1, self.v2)["change"]["id"]
        self.db.cancel_guideline_change(self.batch, self.mgr1)
        self.assertEqual("cancelled", self.db.guideline_change_status(self.batch)["change"]["status"])
        # 撤销后新补标不落在新指南
        ann_id = self.db.submit_annotation(self.item1, self.a1, "正向")
        self.assertEqual(
            self.v1,
            self.db.conn.execute("SELECT guideline_id FROM annotations WHERE id=?", (ann_id,)).fetchone()[0],
        )
        second = self.db.create_guideline_change(self.batch, self.mgr1, self.v2)["change"]
        self.assertNotEqual(first, second["id"])
        self.assertEqual(1, second["revision"])

    def test_duplicate_submit_and_invalid_scope_rejected(self):
        self.db.create_guideline_change(self.batch, self.mgr1, self.v2, [self.item1])
        with self.assertRaisesRegex(DomainError, "执行中的换版"):
            self.db.create_guideline_change(self.batch, self.mgr1, self.v2)
        self.db.cancel_guideline_change(self.batch, self.mgr1)
        with self.assertRaisesRegex(DomainError, "不属于该批次"):
            self.db.create_guideline_change(self.batch, self.mgr1, self.v2, [self.item1, 99999])
        with self.assertRaisesRegex(DomainError, "无需换版"):
            self.db.create_guideline_change(self.batch, self.mgr1, self.v1)
        self.db.adjudicate(self.item1, "正向", "冻结前先按 v1 仲裁分歧", self.arb, self.v1)
        self.db.freeze_batch(self.batch, self.mgr1)
        with self.assertRaisesRegex(DomainError, "已冻结"):
            self.db.create_guideline_change(self.batch, self.mgr1, self.v2)

    # ------------------------------------------------------- 并发与重试

    def test_double_confirm_is_idempotent_and_recorded_once(self):
        self.db.create_guideline_change(self.batch, self.mgr1, self.v2)
        self._complete_new(self.item1, ("正向", "正向"))
        self._complete_new(self.item2, ("中性", "中性"))
        first = self.db.confirm_guideline_change(self.batch, self.mgr1)
        self.assertFalse(first["idempotent"])
        # 失败后重试语义：已生效则回放同一次结果
        second = self.db.confirm_guideline_change(self.batch, self.mgr2)
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["change"]["id"], second["change"]["id"])
        rows = self.db.conn.execute(
            "SELECT COUNT(*) FROM guideline_changes WHERE batch_id=? AND status='effective'",
            (self.batch,),
        ).fetchone()[0]
        self.assertEqual(1, rows)
        self.assertEqual(self.mgr1, self.db.conn.execute(
            "SELECT effective_by FROM guideline_changes WHERE batch_id=?", (self.batch,)
        ).fetchone()["effective_by"])

    def test_concurrent_confirms_activate_exactly_once(self):
        self.db.create_guideline_change(self.batch, self.mgr1, self.v2)
        self._complete_new(self.item1, ("正向", "正向"))
        self._complete_new(self.item2, ("中性", "待定"))
        self.db.adjudicate(self.item2, "待定", "并发场景下按 v2 仲裁", self.arb, self.v2)
        barrier = threading.Barrier(2)
        results: dict[str, object] = {}
        errors: dict[str, BaseException] = {}

        def confirm(name: str, manager_id: int):
            db = CorpusDB(self.path)
            try:
                barrier.wait()
                results[name] = db.confirm_guideline_change(self.batch, manager_id)
            except BaseException as exc:  # noqa: BLE001 - surface any failure
                errors[name] = exc
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(confirm, "甲", self.mgr1)
            f2 = pool.submit(confirm, "乙", self.mgr2)
            f1.result(); f2.result()
        self.assertEqual({}, errors)
        statuses = [r["change"]["status"] for r in results.values()]
        self.assertEqual(["effective", "effective"], sorted(statuses))
        self.assertEqual(
            1,
            self.db.conn.execute(
                "SELECT COUNT(*) FROM guideline_changes WHERE batch_id=? AND status='effective'",
                (self.batch,),
            ).fetchone()[0],
        )

    def test_failed_confirm_can_resume_after_supplement(self):
        self.db.create_guideline_change(self.batch, self.mgr1, self.v2)
        self._complete_new(self.item1, ("正向", "正向"))
        # item2 未补标，确认失败后执行单保留，可继续处理
        with self.assertRaises(DomainError):
            self.db.confirm_guideline_change(self.batch, self.mgr1)
        self.assertEqual("pending", self.db.guideline_change_status(self.batch)["change"]["status"])
        self._complete_new(self.item2, ("中性", "中性"))
        result = self.db.confirm_guideline_change(self.batch, self.mgr1)
        self.assertEqual("effective", result["change"]["status"])

    # ----------------------------------------------------------- 页面数据

    def test_item_view_shows_pending_guidelines_and_reasons(self):
        self.db.create_guideline_change(self.batch, self.mgr1, self.v2)
        view = self.db.get_item_for_user(self.item1, self.a1)
        self.assertEqual("v1", view["guideline_version"])  # 当前指南仍是 v1
        pending = view["pending_change"]
        self.assertIsNotNone(pending)
        self.assertEqual(self.v2, pending["new_guideline"]["id"])
        self.assertIn("尚未按新指南", " ".join(pending["todo"]["reasons"]))
        self.db.submit_annotation(self.item1, self.a1, "正向")
        view = self.db.get_item_for_user(self.item1, self.a1)
        self.assertEqual("正向", view["pending_change"]["own_pending_annotation"]["label"])


if __name__ == "__main__":
    unittest.main()
