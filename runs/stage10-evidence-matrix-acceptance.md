# 阶段 10 验收记录：项目级证据矩阵

记录时间：2026-11-01（含两轮专家审阅修订）
范围：把阶段 6 的单次 5 篇证据提取扩展为项目级、可暂停续做、可比较版本的证据矩阵。

## 1. 数据模型（Schema v14）

- `evidence_batches`：批次序号、状态（pending/running/completed/failed/skipped）、计划论文、请求数、完成数、失败数、错误汇总、起止时间。
- `evidence_item_versions`：只追加证据版本；`source` 为 `ai_raw` / `human_corrected` / `final_adopted`；按「项目+论文+来源」递增 `version_no`；记录任务、批次、模型、提示词、审核备注与错误。
- `evidence_task_inputs`：任务输入快照；记录每篇文献的 `doc_id`、来源 run_id、输入来源（`full_text`/`abstract`/`none`）、实际输入文本、文本哈希、长度与降级原因。
- 幂等与校验：批次按「任务+批次序号」复用且论文清单必须一致；版本去重纳入内容、引句校验、模型、提示词、错误与审核备注；版本写入校验任务类型（`evidence`/`evidence_matrix`）、项目与批次归属。

## 2. 关键行为

- 任务创建时冻结每篇输入；恢复任务只读冻结输入，不依赖当前筛选范围或最新检索快照。`input_hash` 纳入每篇文本哈希。
- 全文读取失败、无全文、未配置 Token、全文为空都会记录来源与原因，页面展示降级清单；如需更换输入需新建任务。
- 批量分片运行，支持“运行下一批”“继续运行至上限”“重试失败批次”；失败重试以任务中失败论文集合为准，包含 pending 批次中的失败项。
- 429/超时/连接失败指数退避；单次请求上限；崩溃后 `running` 批次可恢复。
- 批次记录完成数/失败数与错误汇总；重新进入 `running` 时清空 `finished_at`。
- 审核区并排展示三个版本，提供字段级差异、完整版本历史（含历史证据字段与引句）；编辑基准版本可选，默认顺序与导出取值一致（最终采用 → 人工校正 → AI 原始）。
- 人工校正/最终采用版本继承基准版本的模型与提示词版本，避免溯源错配。
- 导出 CSV/Markdown 均列出任务、批次、论文标识、各版本任务/批次、输入来源与原因。

## 3. 验收标准对照

| 验收项 | 结果 | 证据 |
|---|---|---|
| 中断后可从上次任务继续 | 通过 | `test_run_stops_at_request_cap_and_resumes` |
| 重复执行不重复产生证据 | 通过 | `test_plan_batches_chunks_and_is_idempotent`、`test_run_uses_frozen_input_and_resumes`、`test_append_evidence_version_is_append_only` |
| 恢复使用冻结输入 | 通过 | `test_task_inputs_are_frozen`、`test_run_uses_frozen_input_and_resumes` |
| 部分失败可重试（含 pending 批次） | 通过 | `test_retry_covers_failed_items_in_pending_batch` |
| 每条证据可逐条复核 | 通过 | 页面三版本与版本历史；`test_matrix_page_switches_edit_base_version` |
| 版本差异可查看 | 通过 | 页面字段差异表；版本历史内容展开 |
| 导出可回溯 | 通过 | `test_matrix_exports_include_batch_and_version_provenance` |
| 当前选择为空仍可查看/续做 | 通过 | `test_matrix_page_keeps_matrix_when_no_papers_selected` |

## 4. 自动化验证

- `pytest research_app/tests/ -v`：228 passed、2 skipped。
- `test_evidence_matrix.py`：批次幂等与清单冲突、状态与时间戳（含 `finished_at` 重置）、版本只追加与备注变更、任务/批次归属校验、输入冻结、矩阵取最新、冻结项目仍可追加。
- `test_evidence_matrix_engine.py`：分片、冻结输入与续做、指数退避、请求上限、pending 批次失败项重试、部分失败计数、导出溯源。
- `test_evidence_matrix_page.py`：页面渲染、规划批次并冻结输入、切换编辑基准版本并保存正确内容、无选中文献时矩阵仍可查看。

## 5. 专家审阅修订记录

- 第一轮：任务输入未冻结 → 新增 `evidence_task_inputs`；三版本无对比 → 并排展示、字段差异、版本历史；批次幂等/归属/备注/计数/导出溯源等 P2 修复。
- 第二轮：
  - P1-1 失败重试覆盖 pending 批次中的失败项，并新增回归测试。
  - P1-2 编辑框 key 纳入基准版本，切换版本后内容正确；新增页面交互测试。
  - P2-1 当前选择为空不再阻断已有任务与矩阵查看。
  - P2-2 版本历史展示历史证据字段与引句。
  - P2-3 人工/最终版本继承基准版本模型与提示词。
  - P2-4 批次重新运行时清空 `finished_at`。
  - P2-5 本验收记录统一为 v14 最终版本。
  - P2-6 Markdown 导出补齐三版本任务/批次溯源。

## 6. 剩余限制

- 真实 API 小样本试运行（5–10 篇，至少含全文可读与仅摘要）尚未执行；当前验收以离线 mock 与页面交互测试为准。
- 评估结果/重试 attempt 的独立版本化仍按阶段 9 延期项跟踪。
- 每篇耗时自动计时未实现（阶段 9 的人工计时仍为外部秒表）。
- Windows exe 按交付策略不随日常更新重建，交付前再重建并做干净环境验收。

## 7. 复现

```bash
python -m pip install -r research_app/requirements.txt
python -m pip install -e packages/python pytest
python -m pytest research_app/tests/ -v
```

真实运行：启动应用，选择项目，在“项目证据矩阵”页配置 Sciverse Token、AI Key 和模型，选择范围后“新建任务并规划批次”（会冻结输入），再“运行下一批”或“继续运行至上限”。
