import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import CorpusDB, DomainError


class GuidelineChangeTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = CorpusDB(self.path)
        self.a1 = self.db.add_user("甲", "annotator")
        self.a2 = self.db.add_user("乙", "annotator")
        self.arb = self.db.add_user("仲裁", "arbitrator")
        self.mgr = self.db.add_user("管理", "manager")
        self.g1 = self.db.add_guideline("v1", "标签仅可为 正向/负向/中性")
        self.g2 = self.db.add_guideline("v2", "标签仅可为 好评/差评/中评")
        self.g3 = self.db.add_guideline("v3", "标签仅可为 推荐/不推荐")
        self.batch = self.db.create_batch("测试批次", self.g1)
        self.i1 = self.db.add_item(self.batch, 1, "这个版本很快。")
        self.i2 = self.db.add_item(self.batch, 2, "没有明显变化。")
        self.i3 = self.db.add_item(self.batch, 3, "界面设计很清爽。")
        for it in (self.i1, self.i2, self.i3):
            self.db.assign(it, self.a1)
            self.db.assign(it, self.a2)
        # v1 下的标注：i1 分歧（已仲裁），i2/i3 共识
        self.db.submit_annotation(self.i1, self.a1, "正向")
        self.db.submit_annotation(self.i1, self.a2, "中性")
        self.db.submit_annotation(self.i2, self.a1, "中性")
        self.db.submit_annotation(self.i2, self.a2, "中性")
        self.db.submit_annotation(self.i3, self.a1, "正向")
        self.db.submit_annotation(self.i3, self.a2, "正向")
        self.db.adjudicate(self.i1, "正向", "速度描述构成明确正向倾向", self.arb)

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def _ready_v1(self):
        for it in (self.i1, self.i2, self.i3):
            self.assertEqual(2, self.db._item_readiness(it, self.g1)["annotation_count"])

    def test_submit_change_generates_todos_and_keeps_out_of_scope(self):
        cid = self.db.submit_guideline_change(self.batch, self.mgr, self.g2, [self.i1, self.i2])
        self.assertGreater(cid, 0)
        info = self.db.get_guideline_change(self.batch)
        self.assertEqual("pending", info["status"])
        self.assertEqual(self.g1, info["from_guideline"]["id"])
        self.assertEqual(self.g2, info["to_guideline"]["id"])
        self.assertEqual([self.i1, self.i2], info["scope_item_ids"])
        # 待办只含范围内条目
        todo_ids = [t["item_id"] for t in info["todos"]]
        self.assertEqual([self.i1, self.i2], todo_ids)
        # 旧版标注不计入新版要求
        for t in info["todos"]:
            self.assertEqual(0, t["annotation_count"])
            self.assertFalse(t["ready"])
            self.assertTrue(any("缺少标注" in r for r in t["reasons"]))
        self.assertFalse(info["all_ready"])
        # 范围外条目不在待办中，且仍按 v1 就绪
        self.assertNotIn(self.i3, todo_ids)
        self._ready_v1()

    def test_confirm_rejected_until_supplemented_then_applied(self):
        self.db.submit_guideline_change(self.batch, self.mgr, self.g2, [self.i1, self.i2])
        # 未补齐即确认 → 拒绝
        with self.assertRaisesRegex(DomainError, "待补标未完成"):
            self.db.confirm_guideline_change(self.batch, self.mgr)
        # 冻结也被阻止
        with self.assertRaisesRegex(DomainError, "换版单"):
            self.db.freeze_batch(self.batch, self.mgr)
        # 按 v2 重新标注：i1 共识，i2 分歧
        self.db.submit_annotation(self.i1, self.a1, "好评")
        self.db.submit_annotation(self.i1, self.a2, "好评")
        self.db.submit_annotation(self.i2, self.a1, "好评")
        self.db.submit_annotation(self.i2, self.a2, "差评")
        info = self.db.get_guideline_change(self.batch)
        i2todo = next(t for t in info["todos"] if t["item_id"] == self.i2)
        self.assertFalse(i2todo["ready"])
        self.assertTrue(any("分歧" in r for r in i2todo["reasons"]))
        # 分歧未仲裁，仍不能生效
        with self.assertRaisesRegex(DomainError, "待补标未完成"):
            self.db.confirm_guideline_change(self.batch, self.mgr)
        # 仲裁按 v2 修改结果
        self.db.adjudicate(self.i2, "好评", "整体仍为正向评价", self.arb)
        info = self.db.get_guideline_change(self.batch)
        self.assertTrue(info["all_ready"])
        result = self.db.confirm_guideline_change(self.batch, self.mgr)
        self.assertTrue(result["ok"])
        self.assertFalse(result["already_applied"])
        self.assertEqual("applied", result["change"]["status"])
        # 条目当前指南已切换
        self.assertEqual(self.g2, self.db._effective_guidelines(self.batch)[self.i1])
        self.assertEqual(self.g2, self.db._effective_guidelines(self.batch)[self.i2])
        # 范围外条目保持 v1
        self.assertEqual(self.g1, self.db._effective_guidelines(self.batch)[self.i3])

    def test_confirm_is_idempotent_records_one_effective(self):
        self.db.submit_guideline_change(self.batch, self.mgr, self.g2, [self.i1])
        self.db.submit_annotation(self.i1, self.a1, "好评")
        self.db.submit_annotation(self.i1, self.a2, "好评")
        r1 = self.db.confirm_guideline_change(self.batch, self.mgr)
        r2 = self.db.confirm_guideline_change(self.batch, self.mgr)
        self.assertFalse(r1["already_applied"])
        self.assertTrue(r2["already_applied"])
        self.assertEqual(r1["change"]["id"], r2["change"]["id"])
        self.assertEqual(1, self.db.conn.execute(
            "SELECT COUNT(*) FROM guideline_changes WHERE batch_id=? AND status='applied'", (self.batch,)
        ).fetchone()[0])

    def test_dynamic_recompute_on_supplement_and_adjudication(self):
        self.db.submit_guideline_change(self.batch, self.mgr, self.g2, [self.i1, self.i2])
        rev0 = self.db.get_guideline_change(self.batch)["revision"]
        self.assertEqual(0, rev0)

        def rev():
            return self.db.get_guideline_change(self.batch)["revision"]

        # 补交一份 → 清单立即重算，修订号增加
        self.db.submit_annotation(self.i1, self.a1, "好评")
        self.assertEqual(rev0 + 1, rev())
        i1 = next(t for t in self.db.get_guideline_change(self.batch)["todos"] if t["item_id"] == self.i1)
        self.assertEqual(1, i1["annotation_count"])
        # 另一份补交 → 重算
        self.db.submit_annotation(self.i1, self.a2, "好评")
        self.assertEqual(rev0 + 2, rev())
        i1 = next(t for t in self.db.get_guideline_change(self.batch)["todos"] if t["item_id"] == self.i1)
        self.assertEqual(2, i1["annotation_count"])
        self.assertTrue(i1["ready"])
        # 仲裁修改结果 → 清单立即失效重算
        self.db.submit_annotation(self.i2, self.a1, "好评")
        self.db.submit_annotation(self.i2, self.a2, "差评")
        self.db.adjudicate(self.i2, "好评", "整体仍为正向评价", self.arb)
        self.assertEqual(rev0 + 5, rev())
        info = self.db.get_guideline_change(self.batch)
        i2 = next(t for t in info["todos"] if t["item_id"] == self.i2)
        self.assertTrue(i2["ready"])
        self.assertTrue(info["all_ready"])

    def test_old_conclusions_not_counted_and_export_consistent(self):
        # 换版前 i1/i2 在 v1 下均已就绪；换版到 v2 范围 i1/i2
        self.db.submit_guideline_change(self.batch, self.mgr, self.g2, [self.i1, self.i2])
        # 即使 v1 已就绪，v2 下仍需重新补齐
        for t in self.db.get_guideline_change(self.batch)["todos"]:
            self.assertFalse(t["ready"])
            self.assertEqual(0, t["annotation_count"])
        # 补齐 v2
        self.db.submit_annotation(self.i1, self.a1, "好评")
        self.db.submit_annotation(self.i1, self.a2, "好评")
        self.db.submit_annotation(self.i2, self.a1, "中评")
        self.db.submit_annotation(self.i2, self.a2, "中评")
        self.db.confirm_guideline_change(self.batch, self.mgr)
        # 冻结：i1/i2 按 v2，i3 按 v1
        result = self.db.freeze_batch(self.batch, self.mgr)
        self.assertIsNotNone(result["metrics"]["pairwise_agreement"])
        exported = self.db.export_gold(self.batch)
        by_item = {r["item_id"]: r for r in exported["records"]}
        self.assertEqual(self.g2, by_item[self.i1]["guideline_id"])
        self.assertEqual(self.g2, by_item[self.i2]["guideline_id"])
        self.assertEqual(self.g1, by_item[self.i3]["guideline_id"])
        self.assertEqual("好评", by_item[self.i1]["label"])
        self.assertEqual("中评", by_item[self.i2]["label"])
        self.assertEqual("正向", by_item[self.i3]["label"])

    def test_item_view_shows_effective_guideline_and_pending(self):
        self.db.submit_guideline_change(self.batch, self.mgr, self.g2, [self.i1])
        view = self.db.get_item_for_user(self.i1, self.a1)
        self.assertEqual(self.g2, view["guideline_id"])
        self.assertEqual("v2", view["guideline_version"])
        self.assertIsNotNone(view["pending_change"])
        # a1 在 setup 中已交过 v1 标注，故此处有 1 条 v1 记录
        self.assertEqual(1, len(view["own_annotations"]))
        self.assertEqual(self.g1, view["own_annotations"][0]["guideline_id"])
        # 范围外条目仍显示 v1
        view3 = self.db.get_item_for_user(self.i3, self.a1)
        self.assertEqual(self.g1, view3["guideline_id"])
        self.assertIsNone(view3["pending_change"])
        # 补交后 own_annotation 指向 v2
        self.db.submit_annotation(self.i1, self.a1, "好评")
        view = self.db.get_item_for_user(self.i1, self.a1)
        self.assertEqual("好评", view["own_annotation"]["label"])
        self.assertEqual(self.g2, view["own_annotation"]["guideline_id"])
        versions = {a["guideline_id"] for a in view["own_annotations"]}
        self.assertEqual({self.g1, self.g2}, versions)

    def test_cancel_then_resubmit(self):
        self.db.submit_guideline_change(self.batch, self.mgr, self.g2, [self.i1])
        self.db.cancel_guideline_change(self.batch, self.mgr)
        info = self.db.get_guideline_change(self.batch)
        self.assertEqual("cancelled", info["status"])
        # 取消后可重新提交
        cid = self.db.submit_guideline_change(self.batch, self.mgr, self.g3, [self.i1])
        self.assertGreater(cid, 0)
        info = self.db.get_guideline_change(self.batch)
        self.assertEqual("pending", info["status"])
        self.assertEqual(self.g3, info["to_guideline"]["id"])

    def test_scope_validation_and_frozen_batch_blocked(self):
        # 范围含不属于本批次的条目
        with self.assertRaisesRegex(DomainError, "范围"):
            self.db.submit_guideline_change(self.batch, self.mgr, self.g2, [9999])
        # 新指南与当前相同
        with self.assertRaisesRegex(DomainError, "相同"):
            self.db.submit_guideline_change(self.batch, self.mgr, self.g1, [self.i1])
        # 非管理员不能换版
        with self.assertRaisesRegex(DomainError, "管理员"):
            self.db.submit_guideline_change(self.batch, self.a1, self.g2, [self.i1])
        # 冻结后不能换版
        self.db.submit_guideline_change(self.batch, self.mgr, self.g2, [self.i1, self.i2, self.i3])
        self.db.submit_annotation(self.i1, self.a1, "好评")
        self.db.submit_annotation(self.i1, self.a2, "好评")
        self.db.submit_annotation(self.i2, self.a1, "中评")
        self.db.submit_annotation(self.i2, self.a2, "中评")
        self.db.submit_annotation(self.i3, self.a1, "好评")
        self.db.submit_annotation(self.i3, self.a2, "好评")
        self.db.confirm_guideline_change(self.batch, self.mgr)
        self.db.freeze_batch(self.batch, self.mgr)
        with self.assertRaisesRegex(DomainError, "冻结"):
            self.db.submit_guideline_change(self.batch, self.mgr, self.g3, [self.i1])

    def test_all_scope(self):
        self.db.submit_guideline_change(self.batch, self.mgr, self.g2, "all")
        info = self.db.get_guideline_change(self.batch)
        self.assertEqual(1, info["scope_all"])
        self.assertEqual([self.i1, self.i2, self.i3], info["scope_item_ids"])
        self.assertEqual(3, len(info["todos"]))

    def test_chained_changes_replay_per_item_versions(self):
        # 第一次换版：v1 -> v2，范围仅 i1
        self.db.submit_guideline_change(self.batch, self.mgr, self.g2, [self.i1])
        self.db.submit_annotation(self.i1, self.a1, "好评")
        self.db.submit_annotation(self.i1, self.a2, "好评")
        self.db.confirm_guideline_change(self.batch, self.mgr)
        # 第二次换版：v2 -> v3，范围仅 i2（i2 当前仍为 v1）
        self.db.submit_guideline_change(self.batch, self.mgr, self.g3, [self.i2])
        self.db.submit_annotation(self.i2, self.a1, "推荐")
        self.db.submit_annotation(self.i2, self.a2, "推荐")
        self.db.confirm_guideline_change(self.batch, self.mgr)
        eff = self.db._effective_guidelines(self.batch)
        self.assertEqual(self.g2, eff[self.i1])  # 第一次换版生效
        self.assertEqual(self.g3, eff[self.i2])  # 第二次换版生效
        self.assertEqual(self.g1, eff[self.i3])  # 从未换版，保持 v1
        # 冻结导出：每条按自身当前指南
        self.db.freeze_batch(self.batch, self.mgr)
        exported = self.db.export_gold(self.batch)
        by_item = {r["item_id"]: r for r in exported["records"]}
        self.assertEqual(self.g2, by_item[self.i1]["guideline_id"])
        self.assertEqual(self.g3, by_item[self.i2]["guideline_id"])
        self.assertEqual(self.g1, by_item[self.i3]["guideline_id"])

    def test_retry_after_failure_then_success(self):
        # 失败后重试接着处理：先因未补齐失败，补齐后重试成功
        self.db.submit_guideline_change(self.batch, self.mgr, self.g2, [self.i1, self.i2])
        with self.assertRaisesRegex(DomainError, "待补标未完成"):
            self.db.confirm_guideline_change(self.batch, self.mgr)
        # 补齐 i1（i2 仍缺）
        self.db.submit_annotation(self.i1, self.a1, "好评")
        self.db.submit_annotation(self.i1, self.a2, "好评")
        with self.assertRaisesRegex(DomainError, "待补标未完成"):
            self.db.confirm_guideline_change(self.batch, self.mgr)
        # 补齐 i2 后重试 → 成功
        self.db.submit_annotation(self.i2, self.a1, "中评")
        self.db.submit_annotation(self.i2, self.a2, "中评")
        result = self.db.confirm_guideline_change(self.batch, self.mgr)
        self.assertTrue(result["ok"])
        self.assertFalse(result["already_applied"])


if __name__ == "__main__":
    unittest.main()
