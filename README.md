# 语料标注与争议仲裁

项目使用 Python 标准库、SQLite 和 `http.server`，实现批次、指南版本、重复标注、分歧检测、仲裁、一致性指标、金标准冻结与导出，并以“提交前不可查看含答案讨论”的方式隔离讨论区答案。

标注指南支持**中途换版**：管理员为未冻结批次提交新版指南与适用条目范围后，系统按最新标注/仲裁结果实时重算待补标清单；标注员按新版指南补齐（旧版结论不计入新版要求）并由管理员确认后，新版指南才生效。换版可中断、可恢复，确认操作幂等（两名管理员同时确认只记录一次生效，失败后重试接着处理），未纳入范围的条目保持原指南。

## 启动

```bash
python app.py
```

默认地址 <http://127.0.0.1:8112>，默认数据库为 `corpus.db`。首次启动会写入两位标注员、一位仲裁员和一个含分歧的示例批次。

```bash
PORT=9002 CORPUS_DB=/tmp/corpus.db python app.py
```

## 测试

```bash
python -m unittest discover -s tests -v
```

测试包括：分配、标注、发现分歧、阻止提前冻结、仲裁、计算一致性、冻结和导出；提交答案前后讨论可见性变化、错误角色不能领取标注任务；以及指南换版的待补标生成、动态重算、旧结论不计入、确认幂等与失败重试、范围外保持原指南、链式换版与冻结导出一致性。

## 接口

- `POST /api/users`、`POST /api/guidelines`、`POST /api/batches`
- `POST /api/batches/{id}/items`、`POST /api/batches/{id}/assign`
- `POST /api/annotations`、`POST /api/adjudications`
- `GET /api/items/{id}?user_id=`
- `GET /api/batches/{id}/disagreements`
- `GET /api/batches/{id}/consistency`
- `POST /api/batches/{id}/freeze`
- `GET /api/batches/{id}/gold`
- `POST /api/batches/{id}/guideline-changes`（body：`manager_id`、`to_guideline_id`、`scope_item_ids` 为条目 id 数组或 `"all"`）
- `GET /api/batches/{id}/guideline-change`（当前换版单、版本差异、待补标清单与待办原因）
- `POST /api/batches/{id}/guideline-change/confirm`（幂等确认生效）
- `POST /api/batches/{id}/guideline-change/cancel`

一致性同时返回逐条成对一致率和 Fleiss Kappa。冻结要求每条至少有两人标注、没有未仲裁分歧；冻结后不能修改标注，导出结果来自不可变的 `gold_records`。

换版期间提交的标注/仲裁按新版指南记录，旧版结论保留但不计入新版待办与冻结；待补标清单在每次读取时按最新结果重算（`revision` 递增），因此补交或仲裁修改后旧清单立即失效。确认生效使用条件更新，并发或重试只产生一条生效记录；冻结与导出均按每条目的当前指南版本写入/读取，保证一致。
