from __future__ import annotations

import difflib
import json
import sqlite3
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class DomainError(ValueError):
    """Business rule violation."""


def _now() -> str:
    return datetime.now().isoformat()


class CorpusDB:
    """A small multi-annotator corpus governance service.

    指南换版被建模为一个可中断的执行过程（guideline_changes）：
    管理员提交新指南与适用条目范围后，系统按原指南结果生成待补标清单；
    标注员补齐、必要时仲裁，管理员确认后新指南才对范围内条目生效。
    """

    def __init__(self, path: str = "corpus.db") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
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

    # ---------------------------------------------------------------- schema

    def _migrate(self) -> None:
        """Upgrade databases created before per-item guidelines existed."""
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(items)")]
        if cols and "guideline_id" not in cols:
            self.conn.execute("ALTER TABLE items ADD COLUMN guideline_id INTEGER REFERENCES guidelines(id)")
            self.conn.execute(
                "UPDATE items SET guideline_id=(SELECT guideline_id FROM batches WHERE batches.id=items.batch_id)"
            )
            self.conn.commit()
        # Legacy schema kept one adjudication per item; re-key by (item, guideline).
        ddl = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='adjudications'"
        ).fetchone()
        if ddl and "UNIQUE" in ddl["sql"].upper() and "ITEM_ID INTEGER NOT NULL UNIQUE" in ddl["sql"].upper().replace("\n", " "):
            self.conn.execute("PRAGMA foreign_keys=OFF")
            self.conn.executescript(
                """
                BEGIN;
                CREATE TABLE adjudications_new (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                  guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
                  final_label TEXT NOT NULL,
                  reason TEXT NOT NULL,
                  arbitrator_id INTEGER NOT NULL REFERENCES users(id),
                  created_at TEXT NOT NULL,
                  UNIQUE(item_id, guideline_id)
                );
                INSERT INTO adjudications_new(id,item_id,guideline_id,final_label,reason,arbitrator_id,created_at)
                SELECT id,item_id,guideline_id,final_label,reason,arbitrator_id,created_at FROM adjudications;
                DROP TABLE adjudications;
                ALTER TABLE adjudications_new RENAME TO adjudications;
                COMMIT;
                """
            )
            self.conn.execute("PRAGMA foreign_keys=ON")

    def _schema(self) -> None:
        self._migrate()
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
              guideline_id INTEGER REFERENCES guidelines(id),
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
              old_guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              new_guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','effective','cancelled')),
              revision INTEGER NOT NULL DEFAULT 1,
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              recomputed_at TEXT NOT NULL,
              effective_by INTEGER REFERENCES users(id),
              effective_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_change_pending_batch
              ON guideline_changes(batch_id) WHERE status='pending';
            CREATE TABLE IF NOT EXISTS guideline_change_items (
              change_id INTEGER NOT NULL REFERENCES guideline_changes(id) ON DELETE CASCADE,
              item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
              PRIMARY KEY(change_id, item_id)
            );
            """
        )
        self.conn.commit()

    # ------------------------------------------------------------- demo data

    def seed_demo(self) -> None:
        if self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        a1 = self.add_user("标注员甲", "annotator")
        a2 = self.add_user("标注员乙", "annotator")
        arb = self.add_user("仲裁员", "arbitrator")
        mgr = self.add_user("管理员", "manager")
        guideline = self.add_guideline("v1", "标签仅可为 正向/负向/中性；先独立标注，不得查看他人答案。")
        self.add_guideline(
            "v2",
            "标签仅可为 正向/负向/中性/待定；网络流行语默认 待定，需在备注给出依据。",
        )
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
        _ = mgr

    # ------------------------------------------------------- basic mutations

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
            try:
                cur = self.conn.execute("INSERT INTO guidelines(version,rules) VALUES(?,?)", (version.strip(), rules.strip()))
            except sqlite3.IntegrityError as exc:
                raise DomainError("指南版本已存在") from exc
        return int(cur.lastrowid)

    def create_batch(self, name: str, guideline_id: int) -> int:
        if not name.strip() or not self.conn.execute("SELECT 1 FROM guidelines WHERE id=? AND active=1", (guideline_id,)).fetchone():
            raise DomainError("批次名称或指南无效")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO batches(name,guideline_id,created_at) VALUES(?,?,?)",
                (name.strip(), guideline_id, _now()),
            )
        return int(cur.lastrowid)

    def add_item(self, batch_id: int, ordinal: int, text: str) -> int:
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch or batch["status"] == "frozen":
            raise DomainError("批次不存在或已经冻结")
        if ordinal <= 0 or not text.strip():
            raise DomainError("序号必须大于0且文本不能为空")
        if self._pending_change(batch_id) is not None:
            raise DomainError("换版执行中不能新增条目，请先确认或撤销换版")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO items(batch_id,ordinal,text,guideline_id) VALUES(?,?,?,?)",
                    (batch_id, ordinal, text.strip(), batch["guideline_id"]),
                )
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

    # ----------------------------------------------------- guideline changes

    def _pending_change(self, batch_id: int):
        return self.conn.execute(
            "SELECT * FROM guideline_changes WHERE batch_id=? AND status='pending'", (batch_id,)
        ).fetchone()

    def _change_scope(self, change_id: int) -> set[int]:
        return {
            r["item_id"]
            for r in self.conn.execute(
                "SELECT item_id FROM guideline_change_items WHERE change_id=?", (change_id,)
            )
        }

    def _resolve_guideline(self, item, explicit_id: int = 0):
        """Pick the guideline an annotation/adjudication may be written under.

        范围内部队默认按待生效新指南补标；显式指定时只允许条目当前指南，
        或执行中单的新指南（补交旧指南）。返回 (guideline_id, change, in_scope)。
        """
        change = self._pending_change(item["batch_id"])
        in_scope = False
        if change is not None:
            in_scope = self.conn.execute(
                "SELECT 1 FROM guideline_change_items WHERE change_id=? AND item_id=?",
                (change["id"], item["id"]),
            ).fetchone() is not None
        allowed = {item["guideline_id"]}
        if in_scope:
            allowed.add(change["new_guideline_id"])
        if explicit_id:
            if explicit_id not in allowed:
                raise DomainError("该条目只能按当前指南" + ("或待生效的新指南" if in_scope else "") + "提交")
            return explicit_id, change, in_scope
        if in_scope:
            return change["new_guideline_id"], change, True
        return item["guideline_id"], change, False

    def _bump_change(self, change_id: int) -> None:
        self.conn.execute(
            "UPDATE guideline_changes SET revision=revision+1, recomputed_at=? WHERE id=?",
            (_now(), change_id),
        )

    def _guideline_state(self, item_id: int, guideline_id: int) -> dict:
        rows = self.conn.execute(
            "SELECT label FROM annotations WHERE item_id=? AND guideline_id=? ORDER BY id",
            (item_id, guideline_id),
        ).fetchall()
        labels = [r["label"] for r in rows]
        adj = self.conn.execute(
            "SELECT final_label,reason FROM adjudications WHERE item_id=? AND guideline_id=?",
            (item_id, guideline_id),
        ).fetchone()
        return {
            "annotations": len(labels),
            "labels": labels,
            "disagreement": len(labels) >= 2 and len(set(labels)) > 1,
            "adjudicated": adj is not None,
            "final_label": adj["final_label"] if adj else None,
        }

    def _todo_for(self, change, item, versions: dict[int, str]) -> dict:
        old = self._guideline_state(item["id"], change["old_guideline_id"])
        new = self._guideline_state(item["id"], change["new_guideline_id"])
        old_v, new_v = versions[change["old_guideline_id"]], versions[change["new_guideline_id"]]
        reasons: list[str] = []
        if old["annotations"] < 2:
            reasons.append(f"原指南（{old_v}）下标注不足（{old['annotations']}/2 份）")
        elif old["disagreement"] and not old["adjudicated"]:
            reasons.append(f"原指南（{old_v}）下存在未仲裁分歧：{'/'.join(old['labels'])}")
        elif old["adjudicated"]:
            reasons.append(f"原指南（{old_v}）下分歧已有仲裁结论：{old['final_label']}")
        else:
            reasons.append(f"原指南（{old_v}）下标注一致")
        done = False
        if new["annotations"] == 0:
            reasons.append(f"尚未按新指南（{new_v}）补标")
        elif new["annotations"] < 2:
            reasons.append(f"新指南（{new_v}）下补标不足（{new['annotations']}/2 份）")
        elif new["disagreement"] and not new["adjudicated"]:
            reasons.append(f"新指南（{new_v}）下补标结果存在分歧（{'/'.join(new['labels'])}），需按新指南仲裁")
        else:
            done = True
            if new["adjudicated"]:
                reasons.append(f"新指南（{new_v}）下分歧已仲裁：{new['final_label']}")
            else:
                reasons.append(f"新指南（{new_v}）下补标一致：{'/'.join(new['labels'])}")
        return {
            "item_id": item["id"],
            "ordinal": item["ordinal"],
            "text": item["text"],
            "old": old,
            "new": new,
            "reasons": reasons,
            "done": done,
        }

    def _versions(self) -> dict[int, str]:
        return {r["id"]: r["version"] for r in self.conn.execute("SELECT id,version FROM guidelines")}

    def change_todos(self, change_id: int) -> list[dict]:
        change = self.conn.execute("SELECT * FROM guideline_changes WHERE id=?", (change_id,)).fetchone()
        if change is None:
            raise DomainError("换版执行单不存在")
        versions = self._versions()
        rows = self.conn.execute(
            "SELECT i.* FROM guideline_change_items c JOIN items i ON i.id=c.item_id "
            "WHERE c.change_id=? ORDER BY i.ordinal",
            (change_id,),
        ).fetchall()
        return [self._todo_for(change, item, versions) for item in rows]

    def create_guideline_change(
        self, batch_id: int, manager_id: int, new_guideline_id: int, item_ids: list[int] | None = None
    ) -> dict:
        manager = self.conn.execute("SELECT role FROM users WHERE id=?", (manager_id,)).fetchone()
        if not manager or manager["role"] != "manager":
            raise DomainError("需要管理员权限")
        if not isinstance(new_guideline_id, int) or new_guideline_id <= 0:
            raise DomainError("新指南无效")
        if item_ids is not None and not all(isinstance(x, int) and x > 0 for x in item_ids):
            raise DomainError("适用条目范围无效")
        with self.transaction():
            batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if not batch:
                raise DomainError("批次不存在")
            if batch["status"] == "frozen":
                raise DomainError("已冻结批次不能换版")
            if self._pending_change(batch_id) is not None:
                raise DomainError("该批次已有执行中的换版，请先完成、确认或撤销")
            new_guide = self.conn.execute(
                "SELECT * FROM guidelines WHERE id=? AND active=1", (new_guideline_id,)
            ).fetchone()
            if not new_guide:
                raise DomainError("新指南不存在或已停用")
            if new_guideline_id == batch["guideline_id"]:
                raise DomainError("新指南与批次当前指南相同，无需换版")
            all_items = self.conn.execute(
                "SELECT * FROM items WHERE batch_id=? ORDER BY ordinal", (batch_id,)
            ).fetchall()
            if not all_items:
                raise DomainError("空批次不能换版")
            if not item_ids:
                scope = all_items
            else:
                by_id = {item["id"]: item for item in all_items}
                unknown = sorted(set(item_ids) - set(by_id))
                if unknown:
                    raise DomainError(f"条目不属于该批次: {unknown}")
                if len(set(item_ids)) != len(item_ids):
                    raise DomainError("适用条目范围存在重复")
                scope = [by_id[i] for i in dict.fromkeys(item_ids)]
            mixed = [item["ordinal"] for item in scope if item["guideline_id"] != batch["guideline_id"]]
            if mixed:
                raise DomainError(f"范围内条目当前指南不统一，条目序号: {mixed}")
            ts = _now()
            try:
                cur = self.conn.execute(
                    "INSERT INTO guideline_changes(batch_id,old_guideline_id,new_guideline_id,status,"
                    "revision,created_by,created_at,recomputed_at) VALUES(?,?,?,'pending',1,?,?,?)",
                    (batch_id, batch["guideline_id"], new_guideline_id, manager_id, ts, ts),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("该批次已有执行中的换版") from exc
            change_id = int(cur.lastrowid)
            self.conn.executemany(
                "INSERT INTO guideline_change_items(change_id,item_id) VALUES(?,?)",
                [(change_id, item["id"]) for item in scope],
            )
        return self.guideline_change_status(batch_id)

    def confirm_guideline_change(self, batch_id: int, manager_id: int) -> dict:
        manager = self.conn.execute("SELECT role FROM users WHERE id=?", (manager_id,)).fetchone()
        if not manager or manager["role"] != "manager":
            raise DomainError("需要管理员权限")
        with self.transaction():
            change = self._pending_change(batch_id)
            if change is None:
                latest = self.conn.execute(
                    "SELECT * FROM guideline_changes WHERE batch_id=? ORDER BY id DESC LIMIT 1", (batch_id,)
                ).fetchone()
                if latest is not None and latest["status"] == "effective":
                    # 并发/重试：生效只记录一次，重复确认直接回放同一次结果。
                    payload = self.guideline_change_status(batch_id)
                    payload["idempotent"] = True
                    return payload
                raise DomainError("该批次没有执行中的换版")
            pending = [t for t in self.change_todos(change["id"]) if not t["done"]]
            if pending:
                ordinals = [t["ordinal"] for t in pending]
                raise DomainError(f"还有 {len(pending)} 条待补标未完成，条目序号: {ordinals}")
            ts = _now()
            scope = self._change_scope(change["id"])
            self.conn.execute(
                "UPDATE items SET guideline_id=? WHERE id IN (%s)" % ",".join("?" * len(scope)),
                (change["new_guideline_id"], *sorted(scope)),
            )
            all_on_new = self.conn.execute(
                "SELECT COUNT(*) FROM items WHERE batch_id=? AND guideline_id<>?",
                (batch_id, change["new_guideline_id"]),
            ).fetchone()[0]
            if all_on_new == 0:
                self.conn.execute(
                    "UPDATE batches SET guideline_id=? WHERE id=?",
                    (change["new_guideline_id"], batch_id),
                )
            self.conn.execute(
                "UPDATE guideline_changes SET status='effective',effective_by=?,effective_at=? WHERE id=?",
                (manager_id, ts, change["id"]),
            )
        payload = self.guideline_change_status(batch_id)
        payload["idempotent"] = False
        return payload

    def cancel_guideline_change(self, batch_id: int, manager_id: int) -> dict:
        manager = self.conn.execute("SELECT role FROM users WHERE id=?", (manager_id,)).fetchone()
        if not manager or manager["role"] != "manager":
            raise DomainError("需要管理员权限")
        with self.transaction():
            change = self._pending_change(batch_id)
            if change is None:
                raise DomainError("该批次没有执行中的换版")
            self.conn.execute(
                "UPDATE guideline_changes SET status='cancelled',recomputed_at=? WHERE id=?",
                (_now(), change["id"]),
            )
        return self.guideline_change_status(batch_id)

    def guideline_change_status(self, batch_id: int) -> dict:
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise DomainError("批次不存在")
        change = self.conn.execute(
            "SELECT * FROM guideline_changes WHERE batch_id=? ORDER BY id DESC LIMIT 1", (batch_id,)
        ).fetchone()
        if change is None:
            return {"batch_id": batch_id, "change": None}
        old_g = self.conn.execute("SELECT * FROM guidelines WHERE id=?", (change["old_guideline_id"],)).fetchone()
        new_g = self.conn.execute("SELECT * FROM guidelines WHERE id=?", (change["new_guideline_id"],)).fetchone()
        todos = self.change_todos(change["id"])
        diff_lines = list(
            difflib.unified_diff(
                old_g["rules"].splitlines(),
                new_g["rules"].splitlines(),
                fromfile=f"原指南 {old_g['version']}",
                tofile=f"新指南 {new_g['version']}",
                lineterm="",
            )
        )
        scope = self._change_scope(change["id"])
        return {
            "batch_id": batch_id,
            "batch_current_guideline_id": batch["guideline_id"],
            "change": {
                "id": change["id"],
                "status": change["status"],
                "revision": change["revision"],
                "created_at": change["created_at"],
                "recomputed_at": change["recomputed_at"],
                "effective_at": change["effective_at"],
                "effective_by": change["effective_by"],
                "item_ids": sorted(scope),
            },
            "old_guideline": {"id": old_g["id"], "version": old_g["version"], "rules": old_g["rules"]},
            "new_guideline": {"id": new_g["id"], "version": new_g["version"], "rules": new_g["rules"]},
            "diff_lines": diff_lines,
            "todo_total": len(todos),
            "todo_done": sum(1 for t in todos if t["done"]),
            "todos": todos,
        }

    # --------------------------------------------------------- annotating...

    def submit_annotation(
        self, item_id: int, annotator_id: int, label: str, comment: str = "", guideline_id: int = 0
    ) -> int:
        if not label.strip():
            raise DomainError("标签不能为空")
        item = self.conn.execute(
            "SELECT i.*, b.status FROM items i JOIN batches b ON b.id=i.batch_id WHERE i.id=?", (item_id,)
        ).fetchone()
        assignment = self.conn.execute(
            "SELECT * FROM assignments WHERE item_id=? AND annotator_id=?", (item_id, annotator_id)
        ).fetchone()
        if not item or not assignment:
            raise DomainError("只能提交已分配条目的标注")
        if item["status"] == "frozen":
            raise DomainError("冻结批次不能修改标注")
        target_guideline, change, _ = self._resolve_guideline(item, guideline_id)
        with self.transaction():
            # 同一指南下补交标注会使该指南的旧仲裁结论过期；其他指南的结论不受影响。
            self.conn.execute(
                "DELETE FROM adjudications WHERE item_id=? AND guideline_id=?",
                (item_id, target_guideline),
            )
            try:
                cur = self.conn.execute(
                    "INSERT INTO annotations(item_id,annotator_id,guideline_id,label,comment,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (item_id, annotator_id, target_guideline, label.strip(), comment.strip(), _now()),
                )
            except sqlite3.IntegrityError:
                cur = self.conn.execute(
                    "UPDATE annotations SET label=?,comment=?,created_at=? "
                    "WHERE item_id=? AND annotator_id=? AND guideline_id=?",
                    (label.strip(), comment.strip(), _now(), item_id, annotator_id, target_guideline),
                )
                annotation_id = self.conn.execute(
                    "SELECT id FROM annotations WHERE item_id=? AND annotator_id=? AND guideline_id=?",
                    (item_id, annotator_id, target_guideline),
                ).fetchone()["id"]
            else:
                annotation_id = int(cur.lastrowid)
            self.conn.execute("UPDATE assignments SET status='submitted' WHERE id=?", (assignment["id"],))
            if change is not None:
                # 补交或补标都会让旧待办清单立即失效，按最新结果重算。
                self._bump_change(change["id"])
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
                (item_id, author_id, body.strip(), int(contains_answer), _now()),
            )
        return int(cur.lastrowid)

    def get_item_for_user(self, item_id: int, user_id: int) -> dict:
        item = self.conn.execute(
            "SELECT i.id,i.batch_id,i.ordinal,i.text,i.guideline_id,"
            "g.version AS guideline_version,g.rules "
            "FROM items i JOIN batches b ON b.id=i.batch_id JOIN guidelines g ON g.id=i.guideline_id "
            "WHERE i.id=?",
            (item_id,),
        ).fetchone()
        if not item:
            raise DomainError("条目不存在")
        own_rows = self.conn.execute(
            "SELECT guideline_id,id,label,comment,created_at FROM annotations WHERE item_id=? AND annotator_id=? "
            "ORDER BY id",
            (item_id, user_id),
        ).fetchall()
        revealed = len(own_rows) > 0
        discussions = []
        for row in self.conn.execute(
            "SELECT d.*,u.name FROM discussions d JOIN users u ON u.id=d.author_id WHERE d.item_id=? ORDER BY d.id",
            (item_id,),
        ).fetchall():
            if row["contains_answer"] and not revealed:
                discussions.append({"id": row["id"], "author": row["name"], "body": "提交自己的标注后才能查看此讨论", "hidden": True})
            else:
                discussions.append(dict(row))
        payload = dict(item)
        own_by_guide = {r["guideline_id"]: dict(r) for r in own_rows}
        payload["own_annotation"] = own_by_guide.get(item["guideline_id"])
        payload["own_annotations"] = [dict(r) for r in own_rows]
        payload["discussions"] = discussions
        change = self._pending_change(item["batch_id"])
        payload["pending_change"] = None
        if change is not None:
            in_scope = self.conn.execute(
                "SELECT 1 FROM guideline_change_items WHERE change_id=? AND item_id=?",
                (change["id"], item_id),
            ).fetchone()
            if in_scope:
                new_g = self.conn.execute(
                    "SELECT id,version,rules FROM guidelines WHERE id=?", (change["new_guideline_id"],)
                ).fetchone()
                old_g = self.conn.execute(
                    "SELECT id,version,rules FROM guidelines WHERE id=?", (change["old_guideline_id"],)
                ).fetchone()
                versions = {new_g["id"]: new_g["version"], old_g["id"]: old_g["version"]}
                fake_change = dict(change)
                todo = self._todo_for(fake_change, item, versions)
                payload["pending_change"] = {
                    "change_id": change["id"],
                    "revision": change["revision"],
                    "new_guideline": dict(new_g),
                    "diff_lines": list(
                        difflib.unified_diff(
                            old_g["rules"].splitlines(),
                            new_g["rules"].splitlines(),
                            fromfile=f"原指南 {old_g['version']}",
                            tofile=f"新指南 {new_g['version']}",
                            lineterm="",
                        )
                    ),
                    "todo": todo,
                    "own_pending_annotation": own_by_guide.get(new_g["id"]),
                }
        return payload

    def disagreements(self, batch_id: int) -> list[dict]:
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise DomainError("批次不存在")
        result = []
        for item in self.conn.execute("SELECT * FROM items WHERE batch_id=? ORDER BY ordinal", (batch_id,)).fetchall():
            rows = self.conn.execute(
                "SELECT a.*,u.name FROM annotations a JOIN users u ON u.id=a.annotator_id "
                "WHERE a.item_id=? AND a.guideline_id=? ORDER BY a.id",
                (item["id"], item["guideline_id"]),
            ).fetchall()
            labels = {row["label"] for row in rows}
            adj = self.conn.execute(
                "SELECT * FROM adjudications WHERE item_id=? AND guideline_id=?",
                (item["id"], item["guideline_id"]),
            ).fetchone()
            if len(rows) >= 2 and len(labels) > 1 and not adj:
                result.append({
                    "item_id": item["id"], "ordinal": item["ordinal"], "text": item["text"],
                    "guideline_id": item["guideline_id"],
                    "labels": [dict(row) for row in rows],
                })
        return result

    def adjudicate(
        self,
        item_id: int,
        final_label: str,
        reason: str,
        arbitrator_id: int,
        guideline_id: int = 0,
    ) -> int:
        user = self.conn.execute("SELECT role FROM users WHERE id=?", (arbitrator_id,)).fetchone()
        item = self.conn.execute(
            "SELECT i.*,b.status FROM items i JOIN batches b ON b.id=i.batch_id WHERE i.id=?", (item_id,)
        ).fetchone()
        if not item or not user or user["role"] != "arbitrator":
            raise DomainError("条目或仲裁员无效")
        if item["status"] == "frozen":
            raise DomainError("冻结批次不能重新仲裁")
        if not guideline_id:
            change = self._pending_change(item["batch_id"])
            if change is not None and self.conn.execute(
                "SELECT 1 FROM guideline_change_items WHERE change_id=? AND item_id=?",
                (change["id"], item_id),
            ).fetchone():
                new_count = self.conn.execute(
                    "SELECT COUNT(*) FROM annotations WHERE item_id=? AND guideline_id=?",
                    (item_id, change["new_guideline_id"]),
                ).fetchone()[0]
                guideline_id = change["new_guideline_id"] if new_count >= 2 else change["old_guideline_id"]
            else:
                guideline_id = item["guideline_id"]
        else:
            # 显式指定时：允许条目当前指南，或执行中涉及的新旧两版
            # （旧分歧与新补标可能需要分别仲裁）。
            allowed = {item["guideline_id"]}
            change = self._pending_change(item["batch_id"])
            if change is not None:
                allowed.add(change["old_guideline_id"])
                in_scope = self.conn.execute(
                    "SELECT 1 FROM guideline_change_items WHERE change_id=? AND item_id=?",
                    (change["id"], item["id"]),
                ).fetchone() is not None
                if in_scope:
                    allowed.add(change["new_guideline_id"])
            guide = self.conn.execute("SELECT 1 FROM guidelines WHERE id=?", (guideline_id,)).fetchone()
            if not guide or guideline_id not in allowed:
                raise DomainError("该指南不适用于此条目")
        rows = self.conn.execute(
            "SELECT label FROM annotations WHERE item_id=? AND guideline_id=?", (item_id, guideline_id)
        ).fetchall()
        if len(rows) < 2:
            raise DomainError("该指南下至少需要两份标注才能仲裁")
        if not final_label.strip() or len(reason.strip()) < 5:
            raise DomainError("最终标签必填，仲裁理由至少5个字符")
        change = self._pending_change(item["batch_id"])
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO adjudications(item_id,guideline_id,final_label,reason,arbitrator_id,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (item_id, guideline_id, final_label.strip(), reason.strip(), arbitrator_id, _now()),
                )
            except sqlite3.IntegrityError:
                cur = self.conn.execute(
                    "UPDATE adjudications SET final_label=?,reason=?,arbitrator_id=?,created_at=? "
                    "WHERE item_id=? AND guideline_id=?",
                    (final_label.strip(), reason.strip(), arbitrator_id, _now(), item_id, guideline_id),
                )
                adjudication_id = self.conn.execute(
                    "SELECT id FROM adjudications WHERE item_id=? AND guideline_id=?",
                    (item_id, guideline_id),
                ).fetchone()["id"]
            else:
                adjudication_id = int(cur.lastrowid)
            if change is not None:
                # 仲裁修改结果后旧清单立即失效。
                self._bump_change(change["id"])
        return int(adjudication_id)

    def consistency(self, batch_id: int) -> dict:
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise DomainError("批次不存在")
        items = self.conn.execute(
            "SELECT id,ordinal,guideline_id FROM items WHERE batch_id=? ORDER BY ordinal", (batch_id,)
        ).fetchall()
        per_item, agreement_pairs, total_pairs = [], 0, 0
        label_totals: Counter[str] = Counter()
        all_annotation_count = 0
        for item in items:
            labels = [r["label"] for r in self.conn.execute(
                "SELECT label FROM annotations WHERE item_id=? AND guideline_id=?",
                (item["id"], item["guideline_id"]),
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
        if self._pending_change(batch_id) is not None:
            raise DomainError("换版执行中，待补标确认生效后才能冻结")
        items = self.conn.execute("SELECT * FROM items WHERE batch_id=? ORDER BY ordinal", (batch_id,)).fetchall()
        if not items:
            raise DomainError("空批次不能冻结")
        missing, unresolved = [], []
        for item in items:
            rows = self.conn.execute(
                "SELECT label FROM annotations WHERE item_id=? AND guideline_id=?",
                (item["id"], item["guideline_id"]),
            ).fetchall()
            if len(rows) < 2:
                missing.append(item["id"])
                continue
            labels = {r["label"] for r in rows}
            adj = self.conn.execute(
                "SELECT * FROM adjudications WHERE item_id=? AND guideline_id=?",
                (item["id"], item["guideline_id"]),
            ).fetchone()
            if len(labels) > 1 and not adj:
                unresolved.append(item["id"])
        if missing:
            raise DomainError(f"条目缺少至少两份标注: {missing}")
        if unresolved:
            raise DomainError(f"仍有 {len(unresolved)} 条分歧未仲裁: {unresolved}")
        metrics = self.consistency(batch_id)
        frozen_at = _now()
        with self.transaction():
            for item in items:
                labels = [r["label"] for r in self.conn.execute(
                    "SELECT label FROM annotations WHERE item_id=? AND guideline_id=?",
                    (item["id"], item["guideline_id"]),
                ).fetchall()]
                adj = self.conn.execute(
                    "SELECT * FROM adjudications WHERE item_id=? AND guideline_id=?",
                    (item["id"], item["guideline_id"]),
                ).fetchone()
                if adj:
                    label, source, adj_id = adj["final_label"], "adjudication", adj["id"]
                else:
                    label, source, adj_id = labels[0], "consensus", None
                self.conn.execute(
                    "INSERT INTO gold_records(batch_id,item_id,guideline_id,label,source,adjudication_id,frozen_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (batch_id, item["id"], item["guideline_id"], label, source, adj_id, frozen_at),
                )
            self.conn.execute(
                "INSERT OR REPLACE INTO batch_freezes(batch_id,metrics_json,frozen_by,frozen_at) VALUES(?,?,?,?)",
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
            "SELECT g.item_id,i.ordinal,i.text,g.guideline_id,gl.version AS guideline_version,"
            "g.label,g.source,g.frozen_at FROM gold_records g "
            "JOIN items i ON i.id=g.item_id JOIN guidelines gl ON gl.id=g.guideline_id "
            "WHERE g.batch_id=? ORDER BY i.ordinal",
            (batch_id,),
        ).fetchall()
        return {
            "batch_id": batch_id, "batch_name": batch["name"], "frozen_at": freeze["frozen_at"],
            "metrics": json.loads(freeze["metrics_json"]), "records": [dict(row) for row in rows],
        }

    def snapshot(self) -> dict:
        return {
            "users": [dict(r) for r in self.conn.execute("SELECT id,name,role FROM users ORDER BY id")],
            "guidelines": [dict(r) for r in self.conn.execute("SELECT * FROM guidelines ORDER BY id")],
            "batches": [dict(r) for r in self.conn.execute("SELECT * FROM batches ORDER BY id")],
            "items": [dict(r) for r in self.conn.execute(
                "SELECT id,batch_id,ordinal,text,guideline_id FROM items ORDER BY batch_id,ordinal"
            )],
            "guideline_changes": [
                dict(r) for r in self.conn.execute(
                    "SELECT id,batch_id,old_guideline_id,new_guideline_id,status,revision,"
                    "created_at,recomputed_at,effective_by,effective_at FROM guideline_changes ORDER BY id"
                )
            ],
        }
