from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class DomainError(ValueError):
    """Business rule violation."""


class CorpusDB:
    """A small multi-annotator corpus governance service."""

    def __init__(self, path: str = "corpus.db") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self._schema()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              role TEXT NOT NULL CHECK(role IN ('annotator','arbitrator','manager'))
            );
            CREATE TABLE IF NOT EXISTS guidelines (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              version TEXT NOT NULL UNIQUE,
              rules TEXT NOT NULL,
              active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
            );
            CREATE TABLE IF NOT EXISTS batches (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL,
              guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','annotating','frozen')),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS items (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
              ordinal INTEGER NOT NULL,
              text TEXT NOT NULL,
              UNIQUE(batch_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS assignments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
              item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
              annotator_id INTEGER NOT NULL REFERENCES users(id),
              status TEXT NOT NULL DEFAULT 'assigned' CHECK(status IN ('assigned','submitted')),
              UNIQUE(item_id, annotator_id)
            );
            CREATE TABLE IF NOT EXISTS annotations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
              annotator_id INTEGER NOT NULL REFERENCES users(id),
              guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              label TEXT NOT NULL,
              comment TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              UNIQUE(item_id, annotator_id, guideline_id)
            );
            CREATE TABLE IF NOT EXISTS adjudications (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
              guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              final_label TEXT NOT NULL,
              reason TEXT NOT NULL,
              arbitrator_id INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              UNIQUE(item_id, guideline_id)
            );
            CREATE TABLE IF NOT EXISTS discussions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
              author_id INTEGER NOT NULL REFERENCES users(id),
              body TEXT NOT NULL,
              contains_answer INTEGER NOT NULL DEFAULT 0 CHECK(contains_answer IN (0,1)),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gold_records (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
              item_id INTEGER NOT NULL UNIQUE REFERENCES items(id),
              guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              label TEXT NOT NULL,
              source TEXT NOT NULL CHECK(source IN ('consensus','adjudication')),
              adjudication_id INTEGER REFERENCES adjudications(id),
              frozen_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS batch_freezes (
              batch_id INTEGER PRIMARY KEY REFERENCES batches(id),
              metrics_json TEXT NOT NULL,
              frozen_by INTEGER NOT NULL REFERENCES users(id),
              frozen_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS guideline_changes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
              from_guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              to_guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              scope_all INTEGER NOT NULL DEFAULT 0 CHECK(scope_all IN (0,1)),
              scope_item_ids TEXT,
              status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','applied','cancelled')),
              revision INTEGER NOT NULL DEFAULT 0,
              version INTEGER NOT NULL DEFAULT 0,
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              applied_by INTEGER REFERENCES users(id),
              applied_at TEXT,
              cancelled_by INTEGER REFERENCES users(id),
              cancelled_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_guideline_changes_batch_status
              ON guideline_changes(batch_id, status);
            """
        )
        self.conn.commit()
        self._migrate_adjudications()

    def _migrate_adjudications(self) -> None:
        """Rebuild adjudications if an old single-column UNIQUE(item_id) table exists."""
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='adjudications'"
        ).fetchone()
        if not row or "UNIQUE(item_id, guideline_id)" in (row[0] or ""):
            return
        self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self.conn.execute(
                """
                CREATE TABLE adjudications_new (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                  guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
                  final_label TEXT NOT NULL,
                  reason TEXT NOT NULL,
                  arbitrator_id INTEGER NOT NULL REFERENCES users(id),
                  created_at TEXT NOT NULL,
                  UNIQUE(item_id, guideline_id)
                )
                """
            )
            self.conn.execute(
                "INSERT INTO adjudications_new(id,item_id,guideline_id,final_label,reason,arbitrator_id,created_at) "
                "SELECT id,item_id,guideline_id,final_label,reason,arbitrator_id,created_at FROM adjudications"
            )
            self.conn.execute("DROP TABLE adjudications")
            self.conn.execute("ALTER TABLE adjudications_new RENAME TO adjudications")
            self.conn.commit()
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")

    def seed_demo(self) -> None:
        if self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        a1 = self.add_user("标注员甲", "annotator")
        a2 = self.add_user("标注员乙", "annotator")
        arb = self.add_user("仲裁员", "arbitrator")
        guideline = self.add_guideline("v1", "标签仅可为 正向/负向/中性；先独立标注，不得查看他人答案。")
        batch = self.create_batch("情感标注示例", guideline)
        item1 = self.add_item(batch, 1, "这个更新让工作流畅了很多。")
        item2 = self.add_item(batch, 2, "功能没有变化，但也没有明显问题。")
        self.assign(item1, a1)
        self.assign(item1, a2)
        self.assign(item2, a1)
        self.assign(item2, a2)
        self.submit_annotation(item1, a1, "正向", "整体表达积极")
        self.submit_annotation(item1, a2, "中性", "描述较克制")
        self.submit_annotation(item2, a1, "中性")
        self.submit_annotation(item2, a2, "中性")

    def add_user(self, name: str, role: str) -> int:
        if not name.strip() or role not in {"annotator", "arbitrator", "manager"}:
            raise DomainError("用户名或角色无效")
        with self.transaction():
            try:
                cur = self.conn.execute("INSERT INTO users(name,role) VALUES(?,?)", (name.strip(), role))
            except sqlite3.IntegrityError as exc:
                raise DomainError("用户名已存在") from exc
        return int(cur.lastrowid)

    def add_guideline(self, version: str, rules: str) -> int:
        if not version.strip() or not rules.strip():
            raise DomainError("指南版本和规则不能为空")
        with self.transaction():
            cur = self.conn.execute("INSERT INTO guidelines(version,rules) VALUES(?,?)", (version.strip(), rules.strip()))
        return int(cur.lastrowid)

    def create_batch(self, name: str, guideline_id: int) -> int:
        if not name.strip() or not self.conn.execute("SELECT 1 FROM guidelines WHERE id=? AND active=1", (guideline_id,)).fetchone():
            raise DomainError("批次名称或指南无效")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO batches(name,guideline_id,created_at) VALUES(?,?,?)",
                (name.strip(), guideline_id, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def add_item(self, batch_id: int, ordinal: int, text: str) -> int:
        batch = self.conn.execute("SELECT status FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch or batch["status"] == "frozen":
            raise DomainError("批次不存在或已经冻结")
        if ordinal <= 0 or not text.strip():
            raise DomainError("序号必须大于0且文本不能为空")
        with self.transaction():
            try:
                cur = self.conn.execute("INSERT INTO items(batch_id,ordinal,text) VALUES(?,?,?)", (batch_id, ordinal, text.strip()))
            except sqlite3.IntegrityError as exc:
                raise DomainError("该批次序号已存在") from exc
        return int(cur.lastrowid)

    def assign(self, item_id: int, annotator_id: int) -> int:
        item = self.conn.execute("SELECT batch_id FROM items WHERE id=?", (item_id,)).fetchone()
        user = self.conn.execute("SELECT role FROM users WHERE id=?", (annotator_id,)).fetchone()
        if not item or not user or user["role"] != "annotator":
            raise DomainError("条目不存在或用户不是标注员")
        with self.transaction():
            self.conn.execute("UPDATE batches SET status='annotating' WHERE id=? AND status='draft'", (item["batch_id"],))
            try:
                cur = self.conn.execute(
                    "INSERT INTO assignments(batch_id,item_id,annotator_id) VALUES(?,?,?)",
                    (item["batch_id"], item_id, annotator_id),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("同一标注员不能重复领取同一条目") from exc
        return int(cur.lastrowid)

    def submit_annotation(self, item_id: int, annotator_id: int, label: str, comment: str = "") -> int:
        if not label.strip():
            raise DomainError("标签不能为空")
        item = self.conn.execute(
            "SELECT i.*, b.guideline_id AS batch_guideline_id, b.status FROM items i JOIN batches b ON b.id=i.batch_id WHERE i.id=?", (item_id,)
        ).fetchone()
        assignment = self.conn.execute(
            "SELECT * FROM assignments WHERE item_id=? AND annotator_id=?", (item_id, annotator_id)
        ).fetchone()
        if not item or not assignment:
            raise DomainError("只能提交已分配条目的标注")
        if item["status"] == "frozen":
            raise DomainError("冻结批次不能修改标注")
        ann_gid = self._annotation_guideline_for_item(item["batch_id"], item_id)
        with self.transaction():
            self.conn.execute("DELETE FROM adjudications WHERE item_id=? AND guideline_id=?", (item_id, ann_gid))
            try:
                cur = self.conn.execute(
                    "INSERT INTO annotations(item_id,annotator_id,guideline_id,label,comment,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, annotator_id, ann_gid, label.strip(), comment.strip(), datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError:
                self.conn.execute(
                    "UPDATE annotations SET label=?,comment=?,created_at=? WHERE item_id=? AND annotator_id=? AND guideline_id=?",
                    (label.strip(), comment.strip(), datetime.now().isoformat(), item_id, annotator_id, ann_gid),
                )
                annotation_id = self.conn.execute(
                    "SELECT id FROM annotations WHERE item_id=? AND annotator_id=? AND guideline_id=?",
                    (item_id, annotator_id, ann_gid),
                ).fetchone()["id"]
            else:
                annotation_id = int(cur.lastrowid)
            self.conn.execute("UPDATE assignments SET status='submitted' WHERE id=?", (assignment["id"],))
            self._bump_change_revision(item["batch_id"], item_id)
        return int(annotation_id)

    def add_discussion(self, item_id: int, author_id: int, body: str, contains_answer: bool = False) -> int:
        if not body.strip():
            raise DomainError("讨论内容不能为空")
        if not self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
            raise DomainError("条目不存在")
        if not self.conn.execute("SELECT 1 FROM users WHERE id=?", (author_id,)).fetchone():
            raise DomainError("用户不存在")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO discussions(item_id,author_id,body,contains_answer,created_at) VALUES(?,?,?,?,?)",
                (item_id, author_id, body.strip(), int(contains_answer), datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def get_item_for_user(self, item_id: int, user_id: int) -> dict:
        item = self.conn.execute(
            "SELECT i.id,i.batch_id,i.ordinal,i.text FROM items i WHERE i.id=?", (item_id,)
        ).fetchone()
        if not item:
            raise DomainError("条目不存在")
        eff_gid = self._annotation_guideline_for_item(item["batch_id"], item_id)
        g = self.conn.execute("SELECT version,rules FROM guidelines WHERE id=?", (eff_gid,)).fetchone()
        payload = {
            "id": item["id"], "batch_id": item["batch_id"], "ordinal": item["ordinal"], "text": item["text"],
            "guideline_id": eff_gid, "guideline_version": g["version"], "rules": g["rules"],
        }
        pending = self._pending_change(item["batch_id"])
        if pending and item["id"] in self._change_scope_ids(pending):
            payload["pending_change"] = {
                "id": pending["id"], "status": pending["status"],
                "to_guideline_id": pending["to_guideline_id"], "revision": pending["revision"],
            }
        else:
            payload["pending_change"] = None
        own = self.conn.execute(
            "SELECT id,label,comment,created_at,guideline_id FROM annotations "
            "WHERE item_id=? AND annotator_id=? AND guideline_id=?",
            (item_id, user_id, eff_gid),
        ).fetchone()
        revealed = own is not None
        discussions = []
        for row in self.conn.execute(
            "SELECT d.*,u.name FROM discussions d JOIN users u ON u.id=d.author_id WHERE d.item_id=? ORDER BY d.id", (item_id,)
        ).fetchall():
            if row["contains_answer"] and not revealed:
                discussions.append({"id": row["id"], "author": row["name"], "body": "提交自己的标注后才能查看此讨论", "hidden": True})
            else:
                discussions.append(dict(row))
        payload["own_annotation"] = dict(own) if own else None
        payload["own_annotations"] = [
            dict(r) for r in self.conn.execute(
                "SELECT a.id,a.label,a.comment,a.created_at,a.guideline_id,g.version AS guideline_version "
                "FROM annotations a JOIN guidelines g ON g.id=a.guideline_id "
                "WHERE a.item_id=? AND a.annotator_id=? ORDER BY a.id", (item_id, user_id),
            ).fetchall()
        ]
        payload["discussions"] = discussions
        return payload

    def disagreements(self, batch_id: int) -> list[dict]:
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise DomainError("批次不存在")
        effective = self._effective_guidelines(batch_id)
        result = []
        for item in self.conn.execute("SELECT * FROM items WHERE batch_id=? ORDER BY ordinal", (batch_id,)).fetchall():
            eff_gid = effective.get(item["id"], batch["guideline_id"])
            rows = self.conn.execute(
                "SELECT a.*,u.name FROM annotations a JOIN users u ON u.id=a.annotator_id "
                "WHERE a.item_id=? AND a.guideline_id=? ORDER BY a.id",
                (item["id"], eff_gid),
            ).fetchall()
            labels = {row["label"] for row in rows}
            adj = self.conn.execute(
                "SELECT * FROM adjudications WHERE item_id=? AND guideline_id=?", (item["id"], eff_gid)
            ).fetchone()
            if len(rows) >= 2 and len(labels) > 1 and not adj:
                result.append({
                    "item_id": item["id"], "ordinal": item["ordinal"], "text": item["text"],
                    "guideline_id": eff_gid,
                    "labels": [dict(row) for row in rows],
                })
        return result

    def adjudicate(self, item_id: int, final_label: str, reason: str, arbitrator_id: int) -> int:
        user = self.conn.execute("SELECT role FROM users WHERE id=?", (arbitrator_id,)).fetchone()
        item = self.conn.execute(
            "SELECT i.*,b.guideline_id AS batch_guideline_id,b.status FROM items i JOIN batches b ON b.id=i.batch_id WHERE i.id=?", (item_id,)
        ).fetchone()
        if not item or not user or user["role"] != "arbitrator":
            raise DomainError("条目或仲裁员无效")
        if item["status"] == "frozen":
            raise DomainError("冻结批次不能重新仲裁")
        ann_gid = self._annotation_guideline_for_item(item["batch_id"], item_id)
        rows = self.conn.execute(
            "SELECT label FROM annotations WHERE item_id=? AND guideline_id=?", (item_id, ann_gid)
        ).fetchall()
        if len(rows) < 2:
            raise DomainError("至少需要两份标注才能仲裁")
        if not final_label.strip() or len(reason.strip()) < 5:
            raise DomainError("最终标签必填，仲裁理由至少5个字符")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO adjudications(item_id,guideline_id,final_label,reason,arbitrator_id,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, ann_gid, final_label.strip(), reason.strip(), arbitrator_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError:
                self.conn.execute(
                    "UPDATE adjudications SET final_label=?,reason=?,arbitrator_id=?,created_at=? WHERE item_id=? AND guideline_id=?",
                    (final_label.strip(), reason.strip(), arbitrator_id, datetime.now().isoformat(), item_id, ann_gid),
                )
                adjudication_id = self.conn.execute(
                    "SELECT id FROM adjudications WHERE item_id=? AND guideline_id=?", (item_id, ann_gid)
                ).fetchone()["id"]
            else:
                adjudication_id = int(cur.lastrowid)
            self._bump_change_revision(item["batch_id"], item_id)
        return int(adjudication_id)

    def consistency(self, batch_id: int) -> dict:
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise DomainError("批次不存在")
        effective = self._effective_guidelines(batch_id)
        items = self.conn.execute("SELECT id,ordinal FROM items WHERE batch_id=? ORDER BY ordinal", (batch_id,)).fetchall()
        per_item, agreement_pairs, total_pairs = [], 0, 0
        label_totals: Counter[str] = Counter()
        all_annotation_count = 0
        for item in items:
            eff_gid = effective.get(item["id"], batch["guideline_id"])
            labels = [r["label"] for r in self.conn.execute(
                "SELECT label FROM annotations WHERE item_id=? AND guideline_id=?", (item["id"], eff_gid)
            ).fetchall()]
            if len(labels) < 2:
                per_item.append({"item_id": item["id"], "ordinal": item["ordinal"], "agreement": None, "annotations": len(labels)})
                continue
            pairs = total = 0
            for i in range(len(labels)):
                for j in range(i + 1, len(labels)):
                    total += 1
                    pairs += labels[i] == labels[j]
            agreement = pairs / total
            agreement_pairs += pairs
            total_pairs += total
            all_annotation_count += len(labels)
            label_totals.update(labels)
            per_item.append({"item_id": item["id"], "ordinal": item["ordinal"], "agreement": round(agreement, 4), "annotations": len(labels)})
        pairwise = agreement_pairs / total_pairs if total_pairs else None
        expected = sum((count / all_annotation_count) ** 2 for count in label_totals.values()) if all_annotation_count else None
        kappa = None
        if pairwise is not None and expected is not None and expected < 1:
            kappa = (pairwise - expected) / (1 - expected)
        return {
            "batch_id": batch_id,
            "items_with_multiple_annotations": total_pairs and sum(1 for row in per_item if row["agreement"] is not None),
            "pairwise_agreement": round(pairwise, 4) if pairwise is not None else None,
            "fleiss_kappa": round(kappa, 4) if kappa is not None else None,
            "items": per_item,
        }

    def freeze_batch(self, batch_id: int, manager_id: int) -> dict:
        manager = self.conn.execute("SELECT role FROM users WHERE id=?", (manager_id,)).fetchone()
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch or not manager or manager["role"] != "manager":
            raise DomainError("批次或管理员无效")
        if batch["status"] == "frozen":
            raise DomainError("批次已冻结")
        if self._pending_change(batch_id):
            raise DomainError("批次有进行中的换版单，请先确认生效或取消后再冻结")
        items = self.conn.execute("SELECT id FROM items WHERE batch_id=? ORDER BY ordinal", (batch_id,)).fetchall()
        if not items:
            raise DomainError("空批次不能冻结")
        disagreements = self.disagreements(batch_id)
        if disagreements:
            raise DomainError(f"仍有 {len(disagreements)} 条分歧未仲裁")
        effective = self._effective_guidelines(batch_id)
        missing = []
        for item in items:
            eff_gid = effective.get(item["id"], batch["guideline_id"])
            count = self.conn.execute(
                "SELECT COUNT(*) FROM annotations WHERE item_id=? AND guideline_id=?", (item["id"], eff_gid)
            ).fetchone()[0]
            if count < 2:
                missing.append(item["id"])
        if missing:
            raise DomainError(f"条目缺少至少两份标注: {missing}")
        metrics = self.consistency(batch_id)
        frozen_at = datetime.now().isoformat()
        with self.transaction():
            for item in items:
                eff_gid = effective.get(item["id"], batch["guideline_id"])
                labels = [r["label"] for r in self.conn.execute(
                    "SELECT label FROM annotations WHERE item_id=? AND guideline_id=?", (item["id"], eff_gid)
                ).fetchall()]
                adj = self.conn.execute(
                    "SELECT * FROM adjudications WHERE item_id=? AND guideline_id=?", (item["id"], eff_gid)
                ).fetchone()
                if adj:
                    label, source, adj_id = adj["final_label"], "adjudication", adj["id"]
                else:
                    label, source, adj_id = labels[0], "consensus", None
                self.conn.execute(
                    "INSERT INTO gold_records(batch_id,item_id,guideline_id,label,source,adjudication_id,frozen_at) VALUES(?,?,?,?,?,?,?)",
                    (batch_id, item["id"], eff_gid, label, source, adj_id, frozen_at),
                )
            self.conn.execute(
                "INSERT INTO batch_freezes(batch_id,metrics_json,frozen_by,frozen_at) VALUES(?,?,?,?)",
                (batch_id, json.dumps(metrics, ensure_ascii=False), manager_id, frozen_at),
            )
            self.conn.execute("UPDATE batches SET status='frozen' WHERE id=?", (batch_id,))
        return {"batch_id": batch_id, "metrics": metrics, "frozen_at": frozen_at}

    def export_gold(self, batch_id: int) -> dict:
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch or batch["status"] != "frozen":
            raise DomainError("只有已冻结批次可以导出金标准")
        freeze = self.conn.execute("SELECT * FROM batch_freezes WHERE batch_id=?", (batch_id,)).fetchone()
        rows = self.conn.execute(
            "SELECT g.item_id,i.ordinal,i.text,g.guideline_id,g.label,g.source,g.frozen_at "
            "FROM gold_records g JOIN items i ON i.id=g.item_id "
            "WHERE g.batch_id=? ORDER BY i.ordinal", (batch_id,)
        ).fetchall()
        return {
            "batch_id": batch_id, "batch_name": batch["name"], "frozen_at": freeze["frozen_at"],
            "metrics": json.loads(freeze["metrics_json"]), "records": [dict(row) for row in rows],
        }

    # ------------------------------------------------------------------
    # 指南换版（可中断、可恢复的执行过程）
    # ------------------------------------------------------------------

    def _pending_change(self, batch_id: int):
        return self.conn.execute(
            "SELECT * FROM guideline_changes WHERE batch_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
            (batch_id,),
        ).fetchone()

    def _change_scope_ids(self, change) -> list[int]:
        if not change:
            return []
        return [int(x) for x in json.loads(change["scope_item_ids"] or "[]")]

    def _effective_guidelines(self, batch_id: int) -> dict[int, int]:
        """Return {item_id: authoritative guideline_id} for every item in a batch."""
        batch = self.conn.execute("SELECT guideline_id FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            return {}
        eff = {
            r["id"]: batch["guideline_id"]
            for r in self.conn.execute("SELECT id FROM items WHERE batch_id=?", (batch_id,)).fetchall()
        }
        for ch in self.conn.execute(
            "SELECT to_guideline_id, scope_item_ids FROM guideline_changes "
            "WHERE batch_id=? AND status='applied' ORDER BY id",
            (batch_id,),
        ).fetchall():
            for iid in json.loads(ch["scope_item_ids"] or "[]"):
                if iid in eff:
                    eff[iid] = ch["to_guideline_id"]
        return eff

    def _annotation_guideline_for_item(self, batch_id: int, item_id: int) -> int:
        """Guideline version under which a new annotation/adjudication for this item is recorded."""
        pending = self._pending_change(batch_id)
        if pending and item_id in self._change_scope_ids(pending):
            return pending["to_guideline_id"]
        return self._effective_guidelines(batch_id).get(item_id) or self.conn.execute(
            "SELECT guideline_id FROM batches WHERE id=?", (batch_id,)
        ).fetchone()["guideline_id"]

    def _bump_change_revision(self, batch_id: int, item_id: int) -> None:
        """Invalidate any pending to-do list covering this item; it is recomputed on read."""
        pending = self._pending_change(batch_id)
        if pending and item_id in self._change_scope_ids(pending):
            self.conn.execute(
                "UPDATE guideline_changes SET revision=revision+1 WHERE id=? AND status='pending'",
                (pending["id"],),
            )

    def _item_readiness(self, item_id: int, guideline_id: int) -> dict:
        anns = self.conn.execute(
            "SELECT annotator_id, label FROM annotations WHERE item_id=? AND guideline_id=? ORDER BY id",
            (item_id, guideline_id),
        ).fetchall()
        labels = [r["label"] for r in anns]
        adj = self.conn.execute(
            "SELECT id FROM adjudications WHERE item_id=? AND guideline_id=?",
            (item_id, guideline_id),
        ).fetchone()
        reasons = []
        if len(anns) < 2:
            reasons.append(f"缺少标注：已交 {len(anns)} 份，需至少 2 份")
        if len(set(labels)) > 1 and not adj:
            reasons.append("存在分歧未仲裁")
        return {
            "annotation_count": len(anns),
            "labels": labels,
            "has_adjudication": adj is not None,
            "ready": not reasons,
            "reasons": reasons,
        }

    def _change_todos(self, change) -> tuple[list[dict], bool]:
        """Recompute the to-be-supplemented list from the latest annotations/adjudications."""
        todos, all_ready = [], True
        for iid in self._change_scope_ids(change):
            item = self.conn.execute("SELECT id,ordinal,text FROM items WHERE id=?", (iid,)).fetchone()
            if not item:
                continue
            state = self._item_readiness(iid, change["to_guideline_id"])
            prev = self.conn.execute(
                "SELECT COUNT(*) FROM annotations WHERE item_id=? AND guideline_id=?",
                (iid, change["from_guideline_id"]),
            ).fetchone()[0]
            todos.append({
                "item_id": iid, "ordinal": item["ordinal"], "text": item["text"],
                "previous_annotation_count": prev,
                **state,
            })
            if not state["ready"]:
                all_ready = False
        return todos, all_ready

    def _guideline_brief(self, guideline_id: int) -> dict:
        row = self.conn.execute("SELECT id,version,rules FROM guidelines WHERE id=?", (guideline_id,)).fetchone()
        return dict(row) if row else {"id": guideline_id, "version": None, "rules": None}

    def submit_guideline_change(
        self, batch_id: int, manager_id: int, to_guideline_id: int, scope_item_ids=None
    ) -> int:
        manager = self.conn.execute("SELECT role FROM users WHERE id=?", (manager_id,)).fetchone()
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        to_guideline = self.conn.execute(
            "SELECT * FROM guidelines WHERE id=? AND active=1", (to_guideline_id,)
        ).fetchone()
        if not manager or manager["role"] != "manager":
            raise DomainError("管理员无效")
        if not batch:
            raise DomainError("批次不存在")
        if batch["status"] == "frozen":
            raise DomainError("冻结批次不能换版")
        if not to_guideline:
            raise DomainError("新指南不存在或未启用")
        if self._pending_change(batch_id):
            raise DomainError("该批次已有进行中的换版单，请先确认生效或取消")
        items = self.conn.execute(
            "SELECT id FROM items WHERE batch_id=? ORDER BY ordinal", (batch_id,)
        ).fetchall()
        item_ids = [r["id"] for r in items]
        if scope_item_ids is None or scope_item_ids == [] or scope_item_ids == "all":
            scope_ids, scope_all = item_ids, 1
        else:
            try:
                scope_ids = [int(x) for x in scope_item_ids]
            except (TypeError, ValueError) as exc:
                raise DomainError("适用条目范围无效") from exc
            scope_all = 0
            if any(i not in item_ids for i in scope_ids):
                raise DomainError("适用范围包含不属于本批次的条目")
        if not scope_ids:
            raise DomainError("适用条目范围不能为空")
        effective = self._effective_guidelines(batch_id)
        from_ids = {effective.get(i, batch["guideline_id"]) for i in scope_ids}
        if len(from_ids) != 1:
            raise DomainError("范围内条目当前指南不一致，无法统一换版")
        from_gid = next(iter(from_ids))
        if from_gid == to_guideline_id:
            raise DomainError("新指南与范围内条目当前指南相同，无需换版")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO guideline_changes"
                "(batch_id,from_guideline_id,to_guideline_id,scope_all,scope_item_ids,status,created_by,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    batch_id, from_gid, to_guideline_id, scope_all,
                    json.dumps(scope_ids), "pending", manager_id, datetime.now().isoformat(),
                ),
            )
        return int(cur.lastrowid)

    def get_guideline_change(self, batch_id: int) -> dict:
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise DomainError("批次不存在")
        change = self._pending_change(batch_id) or self.conn.execute(
            "SELECT * FROM guideline_changes WHERE batch_id=? ORDER BY id DESC LIMIT 1", (batch_id,)
        ).fetchone()
        if not change:
            return {"batch_id": batch_id, "change": None}
        todos, all_ready = self._change_todos(change) if change["status"] == "pending" else ([], True)
        return {
            "batch_id": batch_id,
            "batch_name": batch["name"],
            "status": change["status"],
            "revision": change["revision"],
            "change": dict(change),
            "from_guideline": self._guideline_brief(change["from_guideline_id"]),
            "to_guideline": self._guideline_brief(change["to_guideline_id"]),
            "scope_all": change["scope_all"],
            "scope_item_ids": self._change_scope_ids(change),
            "todos": todos,
            "all_ready": all_ready,
            "computed_at": datetime.now().isoformat(),
        }

    def confirm_guideline_change(self, batch_id: int, manager_id: int) -> dict:
        """Apply a pending change. Idempotent: concurrent/retried confirmations record only one effective event."""
        manager = self.conn.execute("SELECT role FROM users WHERE id=?", (manager_id,)).fetchone()
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not manager or manager["role"] != "manager":
            raise DomainError("管理员无效")
        if not batch:
            raise DomainError("批次不存在")
        with self.transaction():
            pending = self.conn.execute(
                "SELECT * FROM guideline_changes WHERE batch_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
                (batch_id,),
            ).fetchone()
            if not pending:
                applied = self.conn.execute(
                    "SELECT * FROM guideline_changes WHERE batch_id=? AND status='applied' ORDER BY id DESC LIMIT 1",
                    (batch_id,),
                ).fetchone()
                if applied:
                    return {"ok": True, "already_applied": True, "change": dict(applied)}
                raise DomainError("没有进行中的换版单")
            todos, all_ready = self._change_todos(pending)
            if not all_ready:
                outstanding = [t for t in todos if not t["ready"]]
                detail = "；".join(f"条目 {t['ordinal']}: {', '.join(t['reasons'])}" for t in outstanding)
                raise DomainError(f"待补标未完成，不能生效（{len(outstanding)} 条）：{detail}")
            cur = self.conn.execute(
                "UPDATE guideline_changes SET status='applied', applied_by=?, applied_at=?, version=version+1 "
                "WHERE id=? AND status='pending' AND version=?",
                (manager_id, datetime.now().isoformat(), pending["id"], pending["version"]),
            )
            if cur.rowcount == 0:
                applied = self.conn.execute(
                    "SELECT * FROM guideline_changes WHERE id=?", (pending["id"],)
                ).fetchone()
                return {"ok": True, "already_applied": True, "change": dict(applied)}
            applied = self.conn.execute(
                "SELECT * FROM guideline_changes WHERE id=?", (pending["id"],)
            ).fetchone()
        return {"ok": True, "already_applied": False, "change": dict(applied)}

    def cancel_guideline_change(self, batch_id: int, manager_id: int) -> dict:
        manager = self.conn.execute("SELECT role FROM users WHERE id=?", (manager_id,)).fetchone()
        if not manager or manager["role"] != "manager":
            raise DomainError("管理员无效")
        with self.transaction():
            pending = self.conn.execute(
                "SELECT * FROM guideline_changes WHERE batch_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
                (batch_id,),
            ).fetchone()
            if not pending:
                raise DomainError("没有进行中的换版单")
            self.conn.execute(
                "UPDATE guideline_changes SET status='cancelled', cancelled_by=?, cancelled_at=?, version=version+1 "
                "WHERE id=? AND status='pending'",
                (manager_id, datetime.now().isoformat(), pending["id"]),
            )
        return {"ok": True, "cancelled": pending["id"]}

    def snapshot(self) -> dict:
        return {
            "users": [dict(r) for r in self.conn.execute("SELECT id,name,role FROM users ORDER BY id")],
            "guidelines": [dict(r) for r in self.conn.execute("SELECT * FROM guidelines ORDER BY id")],
            "batches": [dict(r) for r in self.conn.execute("SELECT * FROM batches ORDER BY id")],
            "items": [dict(r) for r in self.conn.execute("SELECT * FROM items ORDER BY batch_id,ordinal")],
            "guideline_changes": [
                dict(r) for r in self.conn.execute("SELECT * FROM guideline_changes ORDER BY id")
            ],
        }
