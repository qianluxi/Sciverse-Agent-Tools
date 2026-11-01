from __future__ import annotations

import hashlib
import json
import os
import uuid
from typing import Any, Callable

import streamlit as st
from sciverse.credentials import resolve_token

from research_app import storage
from research_app.ai import EVIDENCE_PROMPT_VERSION, OpenAICompatibleClient
from research_app.ai_review import (
    FIELD_KEYS,
    FIELD_LABELS,
    export_matrix_csv,
    export_matrix_markdown,
    normalize_evidence_data,
    quote_status,
)
from research_app.ai_settings import default_profile
from research_app.evidence_matrix import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_MAX_REQUESTS,
    pending_batches,
    plan_batches,
    run_pending_batches,
)
from research_app.project_ui import project_selector
from research_app.service import call_api


TASK_KIND = "evidence_matrix"

st.set_page_config(
    page_title="项目证据矩阵 · Sciverse", page_icon="S", layout="wide"
)
st.title("项目证据矩阵")
st.caption(
    "按项目批量提取结构化证据，分批保存、可暂停续做。AI 原始、人工校正和最终采用"
    "分别保存为独立版本，不覆盖历史；本页不会改变筛选决定。"
)

if "matrix_message" in st.session_state:
    st.success(st.session_state.pop("matrix_message"))
if "matrix_error" in st.session_state:
    st.error(st.session_state.pop("matrix_error"))

_default_profile = default_profile()
st.session_state.setdefault("ai_base_url", _default_profile["base_url"])
st.session_state.setdefault("ai_model", _default_profile["model"])
st.session_state.setdefault(
    "ai_key_session", os.environ.get("OPENAI_API_KEY", "")
)

with st.sidebar:
    st.markdown("### Sciverse")
    sciverse_token = st.text_input(
        "Sciverse API Token", type="password", placeholder="sci_..."
    )
    sciverse_token = sciverse_token.strip() or resolve_token()
    st.divider()
    project_id = project_selector("matrix_project_selector")
    st.divider()
    st.markdown("### AI 服务")
    entered_base_url = st.text_input(
        "Base URL",
        value=st.session_state.get("ai_base_url", _default_profile["base_url"]),
        key="matrix_base_url",
    )
    st.session_state["ai_base_url"] = entered_base_url.strip().rstrip("/")
    entered_key = st.text_input(
        "API Key",
        type="password",
        value=st.session_state.get("ai_key_session", ""),
        key="matrix_key",
        help="只在当前浏览器会话使用，不写入本地数据库。",
    )
    st.session_state["ai_key_session"] = entered_key
    entered_model = st.text_input(
        "模型名",
        value=st.session_state.get("ai_model", _default_profile["model"]),
        key="matrix_model",
    )
    st.session_state["ai_model"] = entered_model.strip()

if not project_id:
    st.info("请先在“研究项目”页创建或选择一个项目。")
    st.stop()

project = storage.get_screening_project(project_id)
base_url = st.session_state["ai_base_url"]
ai_key = st.session_state["ai_key_session"]
model = st.session_state["ai_model"]

st.markdown(f"### {project['name']}")
st.write(f"**研究问题：** {project['question']}")

candidates = storage.list_all_screening_candidates(project_id)
if not candidates:
    st.info("当前项目还没有候选文献，请先在检索页加入项目。")
    st.stop()

paper_docs: dict[str, dict[str, Any]] = {}
latest_run = storage.latest_search_run(project_id)
for paper in (latest_run or {}).get("papers") or []:
    keys = [
        str(paper.get("unique_id") or ""),
        str(paper.get("doc_id") or ""),
    ]
    for key in keys:
        if key:
            paper_docs.setdefault(key, paper)

st.markdown("### 证据范围")
scope_label = st.radio(
    "筛选状态",
    ["仅纳入", "纳入 + 待定", "全部"],
    horizontal=True,
    key="matrix_scope",
)
scope_statuses = {
    "仅纳入": {"included"},
    "纳入 + 待定": {"included", "pending"},
    "全部": {"included", "pending", "excluded"},
}[scope_label]
scoped = [
    candidate
    for candidate in candidates
    if candidate["status"] in scope_statuses
]
paper_map: dict[str, dict[str, Any]] = {}
for candidate in scoped:
    paper_id = str(candidate.get("candidate_key") or candidate.get("paper_id") or "")
    if not paper_id:
        continue
    doc = (
        paper_docs.get(str(candidate.get("paper_id") or ""))
        or paper_docs.get(paper_id)
        or {}
    )
    paper_map[paper_id] = {
        "paper_id": paper_id,
        "title": candidate.get("title") or doc.get("title") or "",
        "year": candidate.get("year") or doc.get("publication_published_year"),
        "abstract": candidate.get("abstract") or doc.get("abstract") or "",
        "doc_id": doc.get("doc_id") or "",
    }

if not paper_map:
    st.info("当前范围没有可提取证据的候选文献。")
    st.stop()


def _text_provider(paper: dict[str, Any]) -> str:
    doc_id = str(paper.get("doc_id") or "")
    if doc_id and sciverse_token:
        try:
            content = call_api(
                sciverse_token,
                "read_content",
                doc_id=doc_id,
                offset=0,
                limit=6000,
            )
            text = str(content.get("text") or "").strip()
            if text:
                return text
        except Exception:
            pass
    return str(paper.get("abstract") or "").strip()


window_papers = st.multiselect(
    "参与提取的文献",
    list(paper_map),
    default=list(paper_map),
    format_func=lambda value: paper_map[value].get("title") or value,
    max_selections=200,
    key=f"matrix_selected_{project_id}",
)
if not window_papers:
    st.info("请选择至少一篇文献。")
    st.stop()

option_columns = st.columns(2)
with option_columns[0]:
    batch_size = st.number_input(
        "每批篇数",
        min_value=1,
        max_value=20,
        value=DEFAULT_BATCH_SIZE,
        step=1,
        key="matrix_batch_size",
    )
with option_columns[1]:
    max_requests = st.number_input(
        "单次运行最多 AI 请求",
        min_value=1,
        max_value=200,
        value=DEFAULT_MAX_REQUESTS,
        step=1,
        key="matrix_max_requests",
    )

task = storage.get_latest_ai_task(project_id, TASK_KIND)
batches = (
    storage.list_evidence_batches(project_id, task["id"]) if task else []
)
ai_ready = bool(ai_key and base_url and model)

if not ai_ready:
    st.caption("请在侧栏配置 API Key、Base URL 和模型名后再运行。")

action_columns = st.columns(4)
plan_clicked = action_columns[0].button(
    "新建任务并规划批次",
    disabled=not ai_ready or not window_papers,
)
run_one_clicked = action_columns[1].button(
    "运行下一批", disabled=not ai_ready or not batches
)
run_all_clicked = action_columns[2].button(
    "继续运行至上限", disabled=not ai_ready or not batches
)
retry_clicked = action_columns[3].button(
    "重试失败批次", disabled=not ai_ready or not batches
)

if plan_clicked:
    papers = [paper_map[key] for key in window_papers]
    input_hash = hashlib.sha256(
        json.dumps(
            {
                "papers": [paper["paper_id"] for paper in papers],
                "model": model,
                "prompt_version": EVIDENCE_PROMPT_VERSION,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    task_id = storage.create_ai_task(
        project_id,
        kind=TASK_KIND,
        model=model,
        prompt_version=EVIDENCE_PROMPT_VERSION,
        input_hash=input_hash,
    )
    try:
        plan_batches(
            project_id,
            task_id,
            papers,
            batch_size=int(batch_size),
        )
        storage.finish_ai_task(task_id, status="running")
        st.session_state["matrix_message"] = (
            f"已规划 {len(papers)} 篇文献。点击“运行下一批”开始提取。"
        )
    except Exception as error:  # noqa: BLE001
        storage.finish_ai_task(task_id, status="failed", error=str(error))
        st.session_state["matrix_error"] = str(error)
    st.rerun()

if (run_one_clicked or run_all_clicked or retry_clicked) and task:
    client = OpenAICompatibleClient(
        base_url=base_url,
        api_key=ai_key,
        model=model,
    )
    try:
        with st.spinner("正在批量提取证据…"):
            result = run_pending_batches(
                project_id,
                task["id"],
                client,
                paper_map,
                _text_provider,
                retry_failed=retry_clicked,
                max_batches=1 if run_one_clicked else None,
                max_requests=int(max_requests),
            )
        counts = pending_batches(project_id, task["id"])
        remaining = counts["pending"] + counts["running"]
        status = storage.evidence_task_status(project_id, task["id"])
        if remaining:
            storage.finish_ai_task(task["id"], status="running")
        elif status["failed"]:
            storage.finish_ai_task(
                task["id"],
                status="completed",
                error=f"{len(status['failed'])} 篇证据仍失败",
            )
        else:
            storage.finish_ai_task(task["id"], status="completed")
        message = (
            f"本次运行 {result.batches_run} 批，请求 {result.requests} 次；"
            f"成功 {result.completed}、失败 {result.failed}"
        )
        if result.paused:
            message += "；已达请求上限，可继续运行"
        st.session_state["matrix_message"] = message
    except Exception as error:  # noqa: BLE001
        st.session_state["matrix_error"] = str(error)
    st.rerun()

if task:
    status = storage.evidence_task_status(project_id, task["id"])
    counts = pending_batches(project_id, task["id"])
    total_papers = sum(len(batch["paper_keys"]) for batch in batches)
    requests_used = sum(int(batch.get("request_count") or 0) for batch in batches)
    metrics = st.columns(5)
    metrics[0].metric("计划论文", total_papers)
    metrics[1].metric("已完成", len(status["completed"]))
    metrics[2].metric("失败", len(status["failed"]))
    metrics[3].metric("待运行批次", counts["pending"] + counts["running"])
    metrics[4].metric("已用请求", requests_used)
    st.caption(
        f"任务：`{task['id']}` · 模型：{task['model']} · "
        f"提示词：{task['prompt_version']} · 状态：{task['status']}"
    )
    if task.get("error"):
        st.warning(task["error"])
    if batches:
        st.dataframe(
            [
                {
                    "批次": batch["batch_index"] + 1,
                    "状态": batch["status"],
                    "篇数": len(batch["paper_keys"]),
                    "请求": batch["request_count"],
                    "开始": batch["started_at"] or "",
                    "结束": batch["finished_at"] or "",
                    "错误": batch["error"] or "",
                }
                for batch in batches
            ],
            hide_index=True,
            use_container_width=True,
        )
else:
    st.caption("还没有证据矩阵任务。先规划批次。")

matrix = storage.load_evidence_matrix(project_id)
if matrix:
    st.markdown("### 矩阵与版本")
    st.dataframe(
        [
            {
                "论文": entry["title"] or entry["paper_id"],
                "AI 版本": (entry["versions"].get("ai_raw") or {}).get(
                    "version_no", ""
                ),
                "人工版本": (entry["versions"].get("human_corrected") or {}).get(
                    "version_no", ""
                ),
                "最终版本": (entry["versions"].get("final_adopted") or {}).get(
                    "version_no", ""
                ),
                "提取错误": (entry["versions"].get("ai_raw") or {}).get(
                    "error"
                )
                or "",
            }
            for entry in matrix
        ],
        hide_index=True,
        use_container_width=True,
    )

    review_options = [entry["paper_id"] for entry in matrix]
    matrix_by_paper = {entry["paper_id"]: entry for entry in matrix}
    selected_review = st.multiselect(
        "选择要审核的论文",
        review_options,
        default=review_options[:5],
        format_func=lambda value: matrix_by_paper[value]["title"] or value,
        max_selections=20,
        key=f"matrix_review_{project_id}",
    )

    for paper_id in selected_review:
        entry = matrix_by_paper[paper_id]
        versions = entry["versions"]
        chosen_source = (
            "human_corrected"
            if "human_corrected" in versions
            else "ai_raw"
        )
        chosen = versions.get(chosen_source) or {}
        with st.expander(
            f"{entry['title'] or paper_id} · AI v{(versions.get('ai_raw') or {}).get('version_no', '—')}"
            f" · 人工 v{(versions.get('human_corrected') or {}).get('version_no', '—')}"
            f" · 最终 v{(versions.get('final_adopted') or {}).get('version_no', '—')}",
            expanded=False,
        ):
            ai_version = versions.get("ai_raw") or {}
            if ai_version.get("error"):
                st.error(f"AI 提取失败：{ai_version['error']}")
            data = normalize_evidence_data(chosen.get("data"))
            quotes = data.get("evidence_quotes") or []
            checks = chosen.get("quote_checks") or []
            if quotes:
                for index, quote in enumerate(quotes):
                    check = checks[index] if index < len(checks) else {}
                    st.markdown(f"> {quote.get('quote', '')}")
                    st.caption(
                        f"{quote.get('section') or '位置未注明'} · {quote_status(check)}"
                    )
            else:
                st.caption("该版本没有原文引句。")
            with st.form(f"matrix_edit_{paper_id}"):
                edited = {}
                for field in FIELD_KEYS:
                    edited[field] = st.text_area(
                        FIELD_LABELS[field],
                        value=str(data.get(field) or ""),
                        height=68,
                        key=f"matrix_{paper_id}_{field}",
                    )
                note = st.text_area(
                    "审核备注",
                    value=str(chosen.get("reviewer_note") or ""),
                    height=68,
                    key=f"matrix_{paper_id}_note",
                )
                save_human = st.form_submit_button("保存人工校正版本")
                save_final = st.form_submit_button("标记最终采用")
            if save_human or save_final:
                payload = {
                    **edited,
                    "evidence_quotes": quotes,
                }
                target_task_id = (
                    chosen.get("task_id") or (task or {}).get("id") or ""
                )
                try:
                    if not target_task_id:
                        raise ValueError("缺少可关联的 AI 任务。")
                    storage.append_evidence_version(
                        project_id,
                        paper_id,
                        target_task_id,
                        source="human_corrected" if save_human else "final_adopted",
                        data=payload,
                        quote_checks=checks,
                        model=model,
                        prompt_version=EVIDENCE_PROMPT_VERSION,
                        reviewer_note=note,
                        title=entry["title"] or "",
                        year=entry["year"],
                        batch_id=chosen.get("batch_id"),
                    )
                    st.session_state["matrix_message"] = (
                        "已保存人工校正版本。"
                        if save_human
                        else "已标记最终采用版本。"
                    )
                except Exception as error:  # noqa: BLE001
                    st.session_state["matrix_error"] = str(error)
                st.rerun()

    download_columns = st.columns(2)
    with download_columns[0]:
        st.download_button(
            "导出证据矩阵 CSV",
            data=export_matrix_csv(matrix),
            file_name="sciverse-evidence-matrix.csv",
            mime="text/csv",
            key="download_matrix_csv",
        )
    with download_columns[1]:
        st.download_button(
            "导出证据矩阵 Markdown",
            data=export_matrix_markdown(matrix, task=task),
            file_name="sciverse-evidence-matrix.md",
            mime="text/markdown",
            key="download_matrix_markdown",
        )
