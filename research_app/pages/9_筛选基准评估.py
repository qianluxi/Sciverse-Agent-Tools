from __future__ import annotations

import hashlib
import json
import os

import streamlit as st

from research_app import storage
from research_app.ai import OpenAICompatibleClient
from research_app.ai_screening import (
    CONFIDENCE_LABELS,
    SCREENING_PROMPT_VERSION,
    SUGGESTION_LABELS,
    suggest_screening,
)
from research_app.ai_settings import default_profile
from research_app.project_ui import project_selector
from research_app.screening import candidate_key, identifier_keys, paper_id
from research_app.screening_criteria import build_project_criteria
from research_app.screening_eval import evaluate_screening_suggestions


BENCHMARK_KIND = "screening_benchmark"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


st.set_page_config(page_title="筛选基准评估 · Sciverse", layout="wide")
st.title("筛选基准评估")
st.caption(
    "使用独立基准项目的已标注候选评估 AI 筛选质量。"
    "金标准必须在运行前固定；本页不会改变任何候选的筛选状态。"
)

_default_profile = default_profile()
st.session_state.setdefault("ai_base_url", _default_profile["base_url"])
st.session_state.setdefault("ai_model", _default_profile["model"])
st.session_state.setdefault("ai_key_session", os.environ.get("OPENAI_API_KEY", ""))

with st.sidebar:
    project_id = project_selector("benchmark_project_selector")
    st.divider()
    st.markdown("### AI 服务")
    entered_base_url = st.text_input(
        "Base URL",
        value=st.session_state.get("ai_base_url", _default_profile["base_url"]),
        key="benchmark_base_url_input",
    )
    st.session_state["ai_base_url"] = entered_base_url.strip().rstrip("/")
    entered_key = st.text_input(
        "API Key",
        type="password",
        value=st.session_state.get("ai_key_session", ""),
        key="benchmark_key_input",
        help="只在当前浏览器会话使用，不写入本地数据库。",
    )
    st.session_state["ai_key_session"] = entered_key
    entered_model = st.text_input(
        "模型名",
        value=st.session_state.get("ai_model", _default_profile["model"]),
        key="benchmark_model_input",
    )
    st.session_state["ai_model"] = entered_model.strip()

if not project_id:
    st.info("请先在“研究项目”页创建或选择一个独立基准项目。")
    st.stop()

project = storage.get_screening_project(project_id)
st.markdown(f"### {project['name']}")
st.write(f"**研究问题：** {project['question']}")
st.caption(f"纳入标准：{project['inclusion_criteria'] or '未填写'}")
st.caption(f"排除标准：{project['exclusion_criteria'] or '未填写'}")
st.caption(
    "建议使用独立于日常筛选项目的固定基准集；不要根据评估结果反复修改同一批金标准。"
)

all_candidates = storage.list_all_screening_candidates(project_id)
labeled = [
    candidate
    for candidate in all_candidates
    if candidate["status"] in {"included", "excluded"}
    and not candidate["candidate_key"].startswith("conflict:")
]
pending_count = sum(
    1
    for candidate in all_candidates
    if candidate["status"] == "pending"
    and not candidate["candidate_key"].startswith("conflict:")
)
if pending_count:
    st.warning(f"基准项目还有 {pending_count} 条待定候选，本页只评估已标注候选。")
if not labeled:
    st.info("基准项目还没有已标注纳入或排除的候选。")
    st.stop()

fallback_papers = []
latest_run = storage.latest_search_run(project_id)
if latest_run:
    fallback_papers.extend(latest_run.get("papers") or [])
fallback_papers.extend(st.session_state.get("papers", []))
paper_lookup = {}
for index, paper in enumerate(fallback_papers):
    key = candidate_key(paper, index)
    paper_lookup[key] = paper
    paper_lookup[paper_id(paper, key)] = paper
    for identifier in identifier_keys(paper):
        paper_lookup[identifier] = paper

for candidate in labeled:
    fallback = (
        paper_lookup.get(candidate["candidate_key"])
        or paper_lookup.get(candidate.get("paper_id") or "")
        or {}
    )
    candidate["benchmark_abstract"] = (
        candidate.get("abstract")
        or fallback.get("abstract")
        or fallback.get("chunk")
        or ""
    )

candidate_map = {candidate["candidate_key"]: candidate for candidate in labeled}
selected_keys = st.multiselect(
    "参与评估的已标注文献",
    list(candidate_map),
    default=list(candidate_map),
    max_selections=100,
    format_func=lambda value: candidate_map[value].get("title") or value,
    key=f"benchmark_selected_{project_id}",
)
if not selected_keys:
    st.info("请选择至少一条已标注文献。")
    st.stop()
missing_abstract = sum(
    1 for key in selected_keys if not candidate_map[key].get("benchmark_abstract")
)
if missing_abstract:
    st.caption(f"所选文献中有 {missing_abstract} 篇没有摘要，将仅按标题判断。")

base_url = st.session_state["ai_base_url"]
ai_key = st.session_state["ai_key_session"]
model = st.session_state["ai_model"]
generate_clicked = st.button(
    "生成基准筛选建议",
    type="primary",
    disabled=not ai_key or not base_url or not model,
)
if not ai_key:
    st.caption("请先在侧栏配置 AI API Key。")

if generate_clicked:
    documents = []
    for key in selected_keys:
        candidate = candidate_map[key]
        abstract = str(candidate.get("benchmark_abstract") or "")[:4000]
        source_runs = [
            source["run_id"]
            for source in storage.list_candidate_sources(project_id, key)
            if source.get("run_id")
        ]
        documents.append(
            {
                "candidate_key": key,
                "paper_id": candidate.get("paper_id") or key,
                "title": candidate.get("title") or "",
                "year": candidate.get("year"),
                "abstract": abstract,
                "abstract_hash": hashlib.sha256(
                    abstract.encode("utf-8")
                ).hexdigest(),
                "source_run_ids": source_runs,
            }
        )
    criteria = build_project_criteria(project)
    gold_for_generation = {
        key: candidate_map[key]["status"] for key in selected_keys
    }
    input_hash = hashlib.sha256(
        json.dumps(
            {"criteria": criteria, "candidates": documents},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    task_id = storage.create_ai_task(
        project_id,
        kind=BENCHMARK_KIND,
        model=model,
        prompt_version=SCREENING_PROMPT_VERSION,
        input_hash=input_hash,
    )
    try:
        storage.create_screening_snapshot(
            project_id,
            task_id,
            kind=BENCHMARK_KIND,
            criteria=criteria,
            items=documents,
            gold=gold_for_generation,
        )
        client = OpenAICompatibleClient(
            base_url=base_url,
            api_key=ai_key,
            model=model,
        )
        with st.spinner("正在生成基准筛选建议…"):
            result = suggest_screening(client, criteria, documents)
        result_items = result.get("items") or []
        failed_items = [item for item in result_items if item.get("error")]
        if not result_items or len(failed_items) == len(result_items):
            task_status = "failed"
            first_error = next(
                (item.get("error") for item in failed_items if item.get("error")),
                "未返回分析结果",
            )
            task_error = (
                "基准评估未返回任何建议。"
                if not result_items
                else f"全部 {len(result_items)} 篇文献分析失败：{first_error}"
            )
        else:
            task_status = "completed"
            task_error = None
        storage.save_screening_suggestions(project_id, task_id, result_items)
        evaluation = evaluate_screening_suggestions(
            result_items,
            gold_for_generation,
        )
        storage.save_screening_snapshot_evaluation(task_id, evaluation)
        storage.finish_ai_task(task_id, status=task_status, error=task_error)
        st.rerun()
    except Exception as error:
        try:
            storage.finish_ai_task(task_id, status="failed", error=str(error))
        except Exception:
            pass
        st.error(str(error))

saved = storage.load_latest_screening_suggestions(project_id, kind=BENCHMARK_KIND)
if not saved:
    st.info("还没有基准评估建议。确认金标准后生成。")
    st.stop()

st.caption(
    f"最近任务：{saved['task']['created_at']} · 模型：{saved['task']['model']} · "
    f"提示词：{saved['task']['prompt_version']}"
)
if saved["task"].get("error"):
    st.error(saved["task"]["error"])

snapshot = storage.load_screening_snapshot(project_id, saved["task"]["id"])
if not snapshot:
    st.warning("该评估任务来自旧版本，没有冻结金标准快照；请重新生成基准评估。")
    st.stop()
snapshot_gold = snapshot.get("gold") or {}
failed_suggestions = [
    item for item in saved["suggestions"] if item.get("error")
]
if failed_suggestions:
    if ai_key and base_url and model:
        if st.button(f"重试失败条目（{len(failed_suggestions)}）"):
            snapshot_items = {
                item["candidate_key"]: item for item in snapshot.get("items") or []
            }
            documents = []
            for item in failed_suggestions:
                source = snapshot_items.get(item["candidate_key"])
                if not source:
                    continue
                documents.append(
                    {
                        "candidate_key": item["candidate_key"],
                        "paper_id": source.get("paper_id")
                        or item["candidate_key"],
                        "title": source.get("title") or "",
                        "year": source.get("year"),
                        "abstract": source.get("abstract") or "",
                    }
                )
            try:
                client = OpenAICompatibleClient(
                    base_url=base_url,
                    api_key=ai_key,
                    model=model,
                )
                with st.spinner("正在重试失败条目…"):
                    result = suggest_screening(client, snapshot["criteria"], documents)
                storage.save_screening_suggestions(
                    project_id,
                    saved["task"]["id"],
                    result["items"],
                )
                refreshed = storage.load_latest_screening_suggestions(
                    project_id,
                    kind=BENCHMARK_KIND,
                )
                updated_suggestions = (
                    refreshed["suggestions"]
                    if refreshed
                    else saved["suggestions"]
                )
                evaluation = evaluate_screening_suggestions(
                    updated_suggestions,
                    snapshot_gold,
                )
                storage.save_screening_snapshot_evaluation(
                    saved["task"]["id"],
                    evaluation,
                )
                st.rerun()
            except Exception as error:
                st.error(str(error))
    else:
        st.caption("请在侧栏配置 API Key 后重试失败条目。")

suggestions_for_selection = [
    item
    for item in saved["suggestions"]
    if item["candidate_key"] in snapshot_gold
]
if not suggestions_for_selection:
    st.info("冻结快照没有对应的基准建议，请重新生成。")
    st.stop()

evaluation = snapshot.get("evaluation")
if not evaluation:
    evaluation = evaluate_screening_suggestions(
        suggestions_for_selection,
        snapshot_gold,
    )
    storage.save_screening_snapshot_evaluation(saved["task"]["id"], evaluation)
st.caption(
    f"冻结基准集：{snapshot['dataset_version']} · "
    f"哈希：{snapshot['dataset_hash'][:12]} · 样本：{len(snapshot_gold)}"
)
counts = evaluation["counts"]
metrics = evaluation["metrics"]
st.markdown("### 质量指标")
metric_columns = st.columns(5)
metric_columns[0].metric("纳入召回率", _pct(metrics["include_recall"]))
metric_columns[1].metric("错误排除率", _pct(metrics["false_exclusion_rate"]))
metric_columns[2].metric("纳入漏排率", _pct(metrics["missed_inclusion_rate"]))
metric_columns[3].metric("纳入未排除率", _pct(metrics["non_exclusion_recall"]))
metric_columns[4].metric("不确定率", _pct(metrics["uncertain_rate"]))
st.caption(
    "纳入召回率 = 正确建议纳入数 / 金标准纳入数；"
    "错误排除率 = 错误排除数 / AI 建议排除数；"
    "纳入漏排率 = 错误排除数 / 金标准纳入数。"
)

st.markdown("### 样本与分母")
st.dataframe(
    [
        {"项目": "评估样本", "数量": evaluation["total"]},
        {"项目": "金标准纳入", "数量": counts["gold_included"]},
        {"项目": "金标准排除", "数量": counts["gold_excluded"]},
        {"项目": "AI 建议纳入", "数量": counts["ai_include"]},
        {"项目": "AI 建议排除", "数量": counts["ai_exclude"]},
        {"项目": "AI 不确定", "数量": counts["ai_uncertain"]},
        {"项目": "正确纳入", "数量": counts["true_include"]},
        {"项目": "错误排除", "数量": counts["false_exclusion"]},
        {"项目": "正确排除", "数量": counts["excluded_correct"]},
        {"项目": "缺失建议", "数量": counts["missing_suggestions"]},
        {"项目": "模型错误", "数量": counts["ai_errors"]},
    ],
    hide_index=True,
    use_container_width=True,
)

st.markdown("### 按置信度分层")
confidence_rows = []
for row in evaluation["confidence"]:
    confidence_rows.append(
        {
            "置信度": CONFIDENCE_LABELS.get(row["confidence"], "未知"),
            "样本": row["total"],
            "金标准纳入": row["gold_included"],
            "纳入召回率": _pct(row["include_recall"]),
            "错误排除率": _pct(row["false_exclusion_rate"]),
            "AI 不确定": row["ai_uncertain"],
        }
    )
st.dataframe(confidence_rows, hide_index=True, use_container_width=True)
st.caption("模型自报置信度只用于分层观察，不能当作校准概率。")

st.markdown("### 逐篇建议")
st.dataframe(
    [
        {
            "文献": item.get("title") or "",
            "金标准": "纳入"
            if snapshot_gold.get(item["candidate_key"]) == "included"
            else "排除",
            "AI 建议": SUGGESTION_LABELS.get(item.get("suggestion"), item.get("suggestion")),
            "置信度": CONFIDENCE_LABELS.get(
                str(item.get("confidence") or ""), str(item.get("confidence") or "")
            ),
            "理由": item.get("rationale") or "",
            "错误": item.get("error") or "",
        }
        for item in suggestions_for_selection
    ],
    hide_index=True,
    use_container_width=True,
)

st.download_button(
    "下载基准评估 JSON",
    data=(
        json.dumps(
            {
                "task": saved["task"],
                "snapshot": snapshot,
                "gold": snapshot_gold,
                "suggestions": suggestions_for_selection,
                "evaluation": evaluation,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    ).encode("utf-8"),
    file_name="sciverse-screening-benchmark.json",
    mime="application/json",
)
