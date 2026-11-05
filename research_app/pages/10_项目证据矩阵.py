from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from typing import Any

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
from research_app.screening import candidate_key, identifier_keys, paper_id
from research_app.service import call_api


TASK_KIND = "evidence_matrix"
SOURCE_LABELS = {
    "ai_raw": "AI 原始",
    "human_corrected": "人工校正",
    "final_adopted": "最终采用",
}
INPUT_SOURCE_LABELS = {"full_text": "全文", "abstract": "摘要", "none": "无文本"}


st.set_page_config(
    page_title="项目证据矩阵 · Sciverse", page_icon="S", layout="wide"
)
st.title("项目证据矩阵")
st.caption(
    "按项目批量提取结构化证据，分批保存、可暂停续做。任务创建时会冻结每篇文献的"
    "输入文本与来源；AI 原始、人工校正和最终采用分别保存为独立版本，不覆盖历史。"
    "本页不会改变筛选决定。"
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


def _project_paper_docs() -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for run in storage.list_search_runs(project_id):
        full = storage.get_search_run(project_id, run["id"])
        for index, paper in enumerate((full or {}).get("papers") or []):
            key = candidate_key(paper, index)
            for identity in (
                key,
                paper_id(paper, key),
                *identifier_keys(paper),
            ):
                if identity:
                    lookup.setdefault(identity, paper)
    return lookup


def _resolve_input(paper: dict[str, Any]) -> tuple[str, str, str | None]:
    text = ""
    source = "none"
    error: str | None = None
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
                source = "full_text"
            else:
                error = "全文接口返回空文本"
        except Exception as exc:  # noqa: BLE001
            error = f"全文读取失败：{exc}"
    if not text:
        abstract = str(paper.get("abstract") or "").strip()
        if abstract:
            text = abstract
            source = "abstract"
            if not error:
                error = (
                    "没有可用全文，使用摘要"
                    if not doc_id
                    else "未配置 Sciverse Token，使用摘要"
                )
        else:
            source = "none"
            error = error or "没有可用文本"
    return text[:6000], source, error


paper_docs = _project_paper_docs()

paper_map: dict[str, dict[str, Any]] = {}
window_papers: list[str] = []
batch_size = DEFAULT_BATCH_SIZE
max_requests = DEFAULT_MAX_REQUESTS

if candidates:
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
        candidate for candidate in candidates if candidate["status"] in scope_statuses
    ]
    for candidate in scoped:
        identity = str(candidate.get("candidate_key") or candidate.get("paper_id") or "")
        if not identity:
            continue
        doc = (
            paper_docs.get(str(candidate.get("paper_id") or ""))
            or paper_docs.get(identity)
            or {}
        )
        paper_map[identity] = {
            "paper_id": identity,
            "candidate_key": identity,
            "title": candidate.get("title") or doc.get("title") or "",
            "year": candidate.get("year") or doc.get("publication_published_year"),
            "abstract": candidate.get("abstract") or doc.get("abstract") or "",
            "doc_id": doc.get("doc_id") or "",
        }

    if paper_map:
        window_papers = st.multiselect(
            "参与提取的文献（仅用于新建任务）",
            list(paper_map),
            default=list(paper_map),
            format_func=lambda value: paper_map[value].get("title") or value,
            max_selections=200,
            key=f"matrix_selected_{project_id}",
        )
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
    else:
        st.caption("当前范围没有可提取证据的候选文献；仍可查看和续做已有任务。")
else:
    st.caption("当前项目还没有候选文献；仍可查看已有证据矩阵。")

matrix_tasks = storage.list_ai_tasks_by_kind(project_id, TASK_KIND, limit=10)
task = matrix_tasks[0] if matrix_tasks else None
if len(matrix_tasks) > 1:
    task_labels = {
        item["id"]: f"{item['id'][:8]} · {item['status']} · {item['created_at']}"
        for item in matrix_tasks
    }
    selected_task_id = st.selectbox(
        "证据矩阵任务",
        list(task_labels),
        format_func=lambda value: task_labels[value],
        key=f"matrix_task_selector_{project_id}",
    )
    task = next(item for item in matrix_tasks if item["id"] == selected_task_id)
if matrix_tasks:
    task_rows = []
    for item in matrix_tasks:
        task_status = storage.evidence_task_status(project_id, item["id"])
        task_counts = pending_batches(project_id, item["id"])
        task_rows.append(
            {
                "任务": item["id"][:8],
                "状态": item["status"],
                "创建": item["created_at"],
                "已完成": len(task_status["completed"]),
                "失败": len(task_status["failed"]),
                "待处理批次": task_counts["pending"] + task_counts["running"],
            }
        )
    st.dataframe(task_rows, hide_index=True, use_container_width=True)
batches = storage.list_evidence_batches(project_id, task["id"]) if task else []
ai_ready = bool(ai_key and base_url and model)

if not ai_ready:
    st.caption("请在侧栏配置 API Key、Base URL 和模型名后再运行。")

action_columns = st.columns(4)
plan_clicked = False
if paper_map:
    plan_clicked = action_columns[0].button(
        "新建任务并规划批次",
        disabled=not ai_ready or not window_papers,
    )
else:
    action_columns[0].caption("无可规划文献")
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
    frozen_items = []
    content_fingerprints = []
    for paper in papers:
        text, source, error = _resolve_input(paper)
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        source_run_ids = [
            source_item["run_id"]
            for source_item in storage.list_candidate_sources(
                project_id, paper["paper_id"]
            )
            if source_item.get("run_id")
        ]
        frozen_items.append(
            {
                "paper_id": paper["paper_id"],
                "candidate_key": paper["candidate_key"],
                "title": paper["title"],
                "year": paper["year"],
                "doc_id": paper["doc_id"],
                "source_run_ids": source_run_ids,
                "input_source": source,
                "input_text": text,
                "input_error": error,
            }
        )
        content_fingerprints.append(
            {"paper_id": paper["paper_id"], "text_hash": text_hash, "source": source}
        )
    input_hash = hashlib.sha256(
        json.dumps(
            {
                "papers": content_fingerprints,
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
        frozen = storage.save_evidence_task_inputs(
            project_id, task_id, frozen_items
        )
        plan_batches(
            project_id,
            task_id,
            papers,
            batch_size=int(batch_size),
        )
        storage.finish_ai_task(task_id, status="running")
        degraded = sum(1 for item in frozen_items if item["input_source"] != "full_text")
        st.session_state[f"matrix_task_selector_{project_id}"] = task_id
        st.session_state["matrix_message"] = (
            f"已冻结 {frozen['saved']} 篇输入并规划 {len(papers)} 篇；"
            f"其中 {degraded} 篇使用摘要或无可用文本。点击“运行下一批”开始提取。"
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
    inputs = storage.list_evidence_task_inputs(project_id, task["id"])
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
    if inputs:
        source_counts = Counter(item["input_source"] for item in inputs)
        st.caption(
            "冻结输入："
            f"全文 {source_counts.get('full_text', 0)}、"
            f"摘要 {source_counts.get('abstract', 0)}、"
            f"无文本 {source_counts.get('none', 0)}"
        )
        degraded = [
            item for item in inputs if item["input_source"] != "full_text"
        ]
        if degraded:
            with st.expander(f"输入降级或缺失（{len(degraded)}）"):
                st.caption(
                    "这些输入已在任务创建时冻结；如需改用全文或修正文本，"
                    "请新建任务重新规划。"
                )
                st.dataframe(
                    [
                        {
                            "论文": item["title"] or item["paper_id"],
                            "输入来源": INPUT_SOURCE_LABELS.get(
                                item["input_source"], item["input_source"]
                            ),
                            "说明": item["input_error"] or "",
                        }
                        for item in degraded
                    ],
                    hide_index=True,
                    use_container_width=True,
                )
    if batches:
        st.dataframe(
            [
                {
                    "批次": batch["batch_index"] + 1,
                    "状态": batch["status"],
                    "篇数": len(batch["paper_keys"]),
                    "完成": batch.get("completed_count", 0),
                    "失败": batch.get("failed_count", 0),
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
                "输入": INPUT_SOURCE_LABELS.get(
                    (entry["versions"].get("ai_raw") or {}).get("input_source")
                    or "",
                    "",
                ),
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

    for paper_key in selected_review:
        entry = matrix_by_paper[paper_key]
        versions = entry["versions"]
        ai_version = versions.get("ai_raw") or {}
        human_version = versions.get("human_corrected") or {}
        final_version = versions.get("final_adopted") or {}
        with st.expander(
            f"{entry['title'] or paper_key} · AI v{ai_version.get('version_no', '—')}"
            f" · 人工 v{human_version.get('version_no', '—')}"
            f" · 最终 v{final_version.get('version_no', '—')}",
            expanded=False,
        ):
            version_columns = st.columns(3)
            for column, source in zip(
                version_columns, ("ai_raw", "human_corrected", "final_adopted")
            ):
                version = versions.get(source) or {}
                with column:
                    st.markdown(f"**{SOURCE_LABELS[source]}**")
                    if not version:
                        st.caption("尚无该版本")
                        continue
                    st.caption(
                        f"v{version.get('version_no')} · "
                        f"{version.get('created_at') or ''}"
                    )
                    if version.get("input_source"):
                        st.caption(
                            "输入："
                            + INPUT_SOURCE_LABELS.get(
                                version["input_source"], version["input_source"]
                            )
                        )
                    if version.get("input_error"):
                        st.caption(f"输入说明：{version['input_error']}")
                    if version.get("error"):
                        st.error(version["error"])
                        continue
                    data = normalize_evidence_data(version.get("data"))
                    for field in FIELD_KEYS:
                        st.caption(f"{FIELD_LABELS[field]}：{data.get(field)}")
                    if version.get("reviewer_note"):
                        st.caption(f"备注：{version['reviewer_note']}")

            ai_data = normalize_evidence_data(ai_version.get("data"))
            human_data = normalize_evidence_data(human_version.get("data"))
            final_data = normalize_evidence_data(final_version.get("data"))
            diff_rows = []
            for field in FIELD_KEYS:
                differences = []
                if human_version and ai_data.get(field) != human_data.get(field):
                    differences.append("AI≠人工")
                base = human_data if human_version else ai_data
                if final_version and final_data.get(field) != base.get(field):
                    differences.append("前版≠最终")
                diff_rows.append(
                    {
                        "字段": FIELD_LABELS[field],
                        "AI 原始": ai_data.get(field),
                        "人工校正": human_data.get(field) if human_version else "—",
                        "最终采用": final_data.get(field) if final_version else "—",
                        "差异": "、".join(differences),
                    }
                )
            st.markdown("**字段差异**")
            st.dataframe(diff_rows, hide_index=True, use_container_width=True)

            st.markdown("**完整版本历史**")
            history = storage.list_evidence_versions(project_id, paper_key)
            st.dataframe(
                [
                    {
                        "来源": SOURCE_LABELS.get(item["source"], item["source"]),
                        "版本": item["version_no"],
                        "时间": item["created_at"],
                        "模型": item["model"],
                        "提示词": item["prompt_version"],
                        "审核备注": item["reviewer_note"],
                        "错误": item["error"] or "",
                    }
                    for item in history
                ],
                hide_index=True,
                use_container_width=True,
            )
            for item in history:
                with st.container(border=True):
                    st.markdown(
                        f"**{SOURCE_LABELS.get(item['source'], item['source'])} "
                        f"v{item['version_no']}** · {item['created_at']}"
                    )
                    if item.get("error"):
                        st.error(item["error"])
                        continue
                    historical = normalize_evidence_data(item.get("data"))
                    for field in FIELD_KEYS:
                        st.caption(
                            f"{FIELD_LABELS[field]}：{historical.get(field)}"
                        )
                    historical_quotes = historical.get("evidence_quotes") or []
                    historical_checks = item.get("quote_checks") or []
                    for index, quote in enumerate(historical_quotes):
                        check = (
                            historical_checks[index]
                            if index < len(historical_checks)
                            else {}
                        )
                        st.markdown(f"> {quote.get('quote', '')}")
                        st.caption(
                            f"{quote.get('section') or '位置未注明'} · "
                            f"{quote_status(check)}"
                        )
                    if item.get("reviewer_note"):
                        st.caption(f"审核备注：{item['reviewer_note']}")

            available_sources = [
                source
                for source in ("final_adopted", "human_corrected", "ai_raw")
                if source in versions
            ]
            base_source = st.radio(
                "编辑基准版本",
                available_sources,
                index=0,
                format_func=lambda value: SOURCE_LABELS[value],
                horizontal=True,
                key=f"matrix_base_{paper_key}",
            )
            base = versions[base_source]
            base_data = normalize_evidence_data(base.get("data"))
            with st.form(f"matrix_edit_{paper_key}_{base_source}"):
                edited = {}
                for field in FIELD_KEYS:
                    edited[field] = st.text_area(
                        FIELD_LABELS[field],
                        value=str(base_data.get(field) or ""),
                        height=68,
                        key=f"matrix_{paper_key}_{base_source}_{field}",
                    )
                note = st.text_area(
                    "审核备注",
                    value=str(base.get("reviewer_note") or ""),
                    height=68,
                    key=f"matrix_{paper_key}_{base_source}_note",
                )
                save_human = st.form_submit_button("保存人工校正版本")
                save_final = st.form_submit_button("标记最终采用")
            if save_human or save_final:
                payload = {
                    **edited,
                    "evidence_quotes": base_data.get("evidence_quotes") or [],
                }
                target_task_id = base.get("task_id") or (task or {}).get("id") or ""
                try:
                    if not target_task_id:
                        raise ValueError("缺少可关联的 AI 任务。")
                    storage.append_evidence_version(
                        project_id,
                        paper_key,
                        target_task_id,
                        source="human_corrected" if save_human else "final_adopted",
                        data=payload,
                        quote_checks=base.get("quote_checks") or [],
                        model=base.get("model") or model,
                        prompt_version=base.get("prompt_version")
                        or EVIDENCE_PROMPT_VERSION,
                        reviewer_note=note,
                        title=entry["title"] or "",
                        year=entry["year"],
                        batch_id=base.get("batch_id"),
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
