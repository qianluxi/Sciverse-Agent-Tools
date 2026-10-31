# 运行归档（阶段 9）

本目录归档阶段 9 的评估运行元数据与人工审核汇总，供实验复核。为遵守版权与最小化原则，**不提交完整摘要、原文引句和逐篇 AI 原文依据**；只提交任务元数据、输入哈希、评估指标与人工审核分布。

## 结构

- `benchmark/<task_id>/`
  - `run_metadata.json`：任务、模型、提示词版本、数据集版本与哈希、评估指标汇总
  - `input_manifest.json`：每篇候选的 `candidate_key`、`paper_id`、`year`、`gold` 与 `abstract_sha256`
  - `per_item_results.json`：逐篇 `candidate_key`、`gold`、AI `suggestion`、`confidence`、`criterion_ids`、`error`（无摘要/引句，可用于独立重算指标）
  - `evaluation.json`：冻结评估的 counts/metrics/confidence 分层（无原文）
- `human_review/<task_id>/`
  - `review_summary.json`：真实人工审核的复核状态分布、AI 建议分布、无摘要条目、计时与口径说明
  - `per_item_review.json`：逐篇 AI 建议、最终决定、复核状态与是否有摘要（无摘要/引句）

## 任务索引

- 基准评估：`f8f2c26135bc4ba3b54969073e948d72`（`glm-5.3-flash` / `screening-v2`，N=60）
- 真实人工审核：`08fa007e964648f4bb11c185ce751e1f`（`glm-5.3-flash` / `screening-v2`，30 篇）

## 未入库的原始输出

包含完整摘要、逐篇 AI 原文依据与引句的原始输出仍保留在本地（`runs/Benchmark Assessment/*.json` 等），未提交到公开仓库。如需复核原始输入/输出，可核对其 SHA-256（见 `benchmarks/dataset_checksums.sha256`）或通过私有通道获取。
