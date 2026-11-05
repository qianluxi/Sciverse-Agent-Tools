# 阶段 10 验收记录：项目级证据矩阵

记录时间：2026-11-01
范围：把阶段 6 的单次 5 篇证据提取扩展为项目级、可暂停续做、可比较版本的证据矩阵。

## 交付内容

- `research_app/evidence_matrix.py`：批次规划与运行引擎。
  - `plan_batches`：按批大小分片，重复规划不重复建批次。
  - `run_pending_batches`：按批次运行、按任务续做、失败批次重试、429/超时/连接失败指数退避、单次请求上限、崩溃后 `running` 批次恢复。
  - 无可用文本记录为失败版本并可重试；内容一致时不重复写版本。
- `research_app/storage.py`（Schema v13）：
  - `evidence_batches`：批次序号、状态、计划论文、请求数、起止时间、错误。
  - `evidence_item_versions`：只追加版本；`source` 为 `ai_raw` / `human_corrected` / `final_adopted`；按项目+论文+来源递增版本号。
  - `load_evidence_matrix`：每篇论文按来源取最新版本。
  - 冻结的基准项目仍可追加证据版本（不触及金标准与候选）。
- `research_app/pages/10_项目证据矩阵.py`：项目级批量提取、批次进度、失败原因、暂停/续做、逐篇三版本复核与保存、导出。
- `research_app/ai_review.py`：`export_matrix_csv` / `export_matrix_markdown`，导出附任务、批次、论文标识、模型、提示词版本与三个版本号。

## 验收标准对照

| 验收项 | 结果 | 证据 |
|---|---|---|
| 中断后可从上次任务继续 | 通过 | `test_run_stops_at_request_cap_and_resumes`（达到请求上限后暂停，再次运行继续完成） |
| 重复执行不重复产生证据 | 通过 | `test_plan_batches_chunks_and_is_idempotent`、`test_run_pending_batches_extracts_and_resumes`、`test_append_evidence_version_is_append_only` |
| 每条证据可逐条复核 | 通过 | 页面逐篇展开编辑；`test_load_evidence_matrix_returns_latest_per_source` |
| 版本差异可查看 | 通过 | 页面显示 AI/人工/最终三个版本号与当前采用值；导出含三版本号 |
| 导出可回溯到项目和论文 | 通过 | `test_matrix_exports_include_provenance`（CSV/Markdown 含 task_id、paper_id、model、prompt_version） |

## 自动化验证

- `pytest research_app/tests/ -v`：225 passed、2 skipped。
- 新增测试：
  - `test_evidence_matrix.py`（存储层：批次幂等与清单校验、状态/时间戳、版本只追加与去重、备注变更、任务/批次归属校验、输入冻结、矩阵取最新、冻结项目仍可追加）。
  - `test_evidence_matrix_engine.py`（引擎：分片、续做、冻结输入、指数退避、请求上限、部分失败计数、导出溯源）。
  - `test_evidence_matrix_page.py`（页面：渲染纳入候选、规划批次并冻结输入）。

## 专家审阅修订（2026-11-01）

- P1-1：任务创建时通过 `evidence_task_inputs` 冻结每篇输入文本、哈希、来源（全文/摘要/无文本）、`doc_id`、来源 run_id 与降级原因；恢复任务只读冻结输入。`input_hash` 纳入文本哈希。
- P1-2：审核区并排展示 AI 原始、人工校正、最终采用三个版本，提供字段级差异和完整版本历史；编辑基准版本默认与导出取值顺序一致。
- P2：批次幂等校验论文清单；版本去重纳入备注；批次记录完成/失败数与错误汇总；版本写入校验任务类型与批次归属；导出增加批次 ID、各版本任务/批次与输入来源。
- `pytest research_app/tests/ -v`：225 passed、2 skipped。

## 边界与未完成项

- 本次未使用真实 API Key 运行大规模提取；验收以离线 mock 测试和页面 smoke 测试为准。专家建议正式使用前做一次 5–10 篇（含全文与仅摘要）真实试运行，尚未执行。
- 评估结果与重试 attempt 的独立版本化仍按阶段 9 延期项跟踪，不属于阶段 10。
- 每篇耗时自动计时未实现（阶段 9 的人工计时仍为外部秒表）。
- Windows exe 按交付策略不随日常更新重建，交付前再重建并做干净环境验收。

## 复现

```bash
python -m pip install -r research_app/requirements.txt
python -m pip install -e packages/python pytest
python -m pytest research_app/tests/ -v
```

真实运行：启动应用，选择项目，在“项目证据矩阵”页配置 Sciverse Token、AI Key 和模型，选择范围后“新建任务并规划批次”，再“运行下一批”或“继续运行至上限”。
