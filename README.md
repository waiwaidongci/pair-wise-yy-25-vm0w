# 语料标注与争议仲裁

项目使用 Python 标准库、SQLite 和 `http.server`，实现批次、指南版本、重复标注、分歧检测、仲裁、一致性指标、金标准冻结与导出，并以“提交前不可查看含答案讨论”的方式隔离讨论区答案。

指南换版是一个**可中断的执行过程**：管理员只对选定的条目范围发起换版，系统按原指南结论生成待补标清单，标注员按新指南补齐（必要时重新仲裁）后管理员确认，新指南才生效；执行中任何补交或仲裁修改都会让旧清单立即失效并按最新结果重算。

## 启动

```bash
python app.py
```

默认地址 <http://127.0.0.1:8112>，默认数据库为 `corpus.db`。首次启动会写入两位标注员、一位仲裁员、一位管理员、两版指南和一个含分歧的示例批次。旧数据库会自动迁移（条目增加按条目记录的 `guideline_id`，仲裁唯一键变为“条目+指南”）。

```bash
PORT=9002 CORPUS_DB=/tmp/corpus.db python app.py
```

## 测试

```bash
python -m unittest discover -s tests -v
```

测试包括：分配、标注、发现分歧、阻止提前冻结、仲裁、计算一致性、冻结和导出；讨论答案隔离与角色校验；以及换版流程——待补标清单与待办原因、补交/仲裁使清单失效并重算（`revision` 递增）、旧指南结论不得计入新指南、未纳入范围条目保留原指南、失败重试与双管理员并发确认只生效一次、中断撤销、冻结与导出版本一致。

## 接口

- `POST /api/users`、`POST /api/guidelines`、`POST /api/batches`
- `POST /api/batches/{id}/items`、`POST /api/batches/{id}/assign`
- `POST /api/annotations`（可带 `guideline_id`；换版范围内默认按待生效新指南，显式传旧指南用于补交）
- `POST /api/adjudications`（可带 `guideline_id`；范围内默认新指南，新指南不足两份时回退旧指南）
- `GET /api/items/{id}?user_id=`（返回当前指南，以及执行中的新指南、版本差异和本人待补标信息）
- `GET /api/batches/{id}/disagreements`
- `GET /api/batches/{id}/consistency`
- `POST /api/batches/{id}/freeze`
- `GET /api/batches/{id}/gold`（每条记录含 `guideline_id` 与 `guideline_version`）
- 换版执行单：
  - `POST /api/batches/{id}/guideline-change/submit`：`{manager_id,new_guideline_id,item_ids?}`，`item_ids` 省略表示整批
  - `GET  /api/batches/{id}/guideline-change`：状态、新旧指南、unified diff、逐条目待办原因与完成情况、`revision`
  - `POST /api/batches/{id}/guideline-change/confirm`：清单全部完成才生效；重复/并发确认幂等，只记录一次生效
  - `POST /api/batches/{id}/guideline-change/cancel`：中断执行单，条目指南保持原样

## 换版语义

- 每个批次至多有一个 `pending` 执行单；确认要求范围内每条在新指南下至少两份标注，且分歧均已按新指南仲裁。
- 待补标清单不缓存结论，始终按最新标注/仲裁实时计算；任何写入都会使 `revision` 加一。补交某指南的标注只会作废该指南下的仲裁，不影响另一版结论。
- 确认生效只更新范围内条目的指南；全部条目都切到新指南时批次默认指南才随之更新。部分换版后，冻结与导出逐行记录实际指南版本。
- 换版执行中禁止冻结、禁止新增条目；冻结后不能修改标注，导出结果来自不可变的 `gold_records`。
