from __future__ import annotations

from math import ceil

import streamlit as st

from research_app import storage
from research_app.exports import export_screening_csv
from research_app.screening import EXCLUSION_REASONS, STATUS_LABELS


st.set_page_config(page_title="系统综述筛选 · Sciverse", page_icon="S", layout="wide")

st.title("系统综述筛选")
st.caption("从“研究项目”选择项目，将检索结果去重后逐条筛选。全部决定和变更记录保存在本机。")

if "screening_message" in st.session_state:
    st.success(st.session_state.pop("screening_message"))


def _label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def _show_error(error: Exception) -> None:
    st.error(str(error))


projects = storage.list_screening_projects()
if not projects:
    st.info("尚未创建研究项目。请先到左侧“研究项目”页面创建项目。")
    st.stop()

project_ids = [project["id"] for project in projects]
default_index = 0
if st.session_state.get("screening_project_id") in project_ids:
    default_index = project_ids.index(st.session_state["screening_project_id"])
project_id = st.selectbox(
    "当前项目",
    project_ids,
    index=default_index,
    format_func=lambda value: next(
        project["name"] for project in projects if project["id"] == value
    ),
    key="screening_project_selector",
)
st.session_state["screening_project_id"] = project_id
st.session_state["current_project_id"] = project_id
project = storage.get_screening_project(project_id)

st.markdown(f"### {project['name']}")
st.write(f"**研究问题：** {project['question']}")
st.caption(f"纳入标准：{project['inclusion_criteria'] or '未填写'}")
st.caption(f"排除标准：{project['exclusion_criteria'] or '未填写'}")

stats = storage.screening_stats(project_id)
metrics = st.columns(6)
for column, label, value in zip(
    metrics,
    ["识别", "去重", "待定", "纳入", "排除", "已筛选"],
    [
        stats["identified"],
        stats["duplicates"],
        stats["pending"],
        stats["included"],
        stats["excluded"],
        stats["screened"],
    ],
):
    column.metric(label, value)
st.caption(f"去重后候选：{stats['unique']} 条。PRISMA 计数均由底层候选和决定记录计算。")

current_papers = st.session_state.get("papers", [])
selected_items = list(st.session_state.get("export_selection", {}).items())
selected_papers = [paper for _, paper in selected_items]
export_sources = st.session_state.get("export_sources", {})
current_run_id = st.session_state.get("current_search_run_id")
if selected_items:
    papers_to_add = selected_papers
    source_run_ids = [export_sources.get(identity) for identity, _ in selected_items]
else:
    papers_to_add = current_papers
    source_run_ids = [current_run_id] * len(current_papers)
if papers_to_add:
    if selected_items:
        st.caption(
            f"将使用主页已选择的 {len(selected_papers)} 篇文献，并保留各自页面的检索来源；"
            "未选择时才会使用当前页全部结果。"
        )
        button_label = f"将已选择的 {len(selected_papers)} 篇文献加入项目"
    else:
        st.caption("主页未选择文献，将使用当前页全部检索结果。")
        button_label = f"将当前页全部 {len(current_papers)} 条加入项目"
    if st.button(button_label, type="primary"):
        try:
            result = storage.add_screening_candidates(
                project_id,
                papers_to_add,
                source_run_ids=source_run_ids,
            )
            message = (
                f"已加入 {result['added']} 条候选；"
                f"识别 {result['identified']} 条，其中重复 {result['duplicates']} 条。"
            )
            if result.get("conflicts"):
                message += f" 另有 {result['conflicts']} 条强标识符冲突，请人工核对。"
            if result.get("manual_source_created"):
                message += " 部分文献来源属于其他项目，已建立“手动选择”快照。"
            st.session_state["screening_message"] = message
            st.rerun()
        except Exception as error:
            _show_error(error)
else:
    st.info("先在主页完成检索，再回到此页把当前结果加入候选队列。")

st.divider()
queue_tab, duplicates_tab, audit_tab, export_tab = st.tabs(
    ["筛选队列", "重复记录", "审计记录", "导出与 PRISMA"]
)

with queue_tab:
    filter_label = st.radio(
        "显示范围", ["待定", "纳入", "排除", "全部"], horizontal=True, key="queue_filter"
    )
    filter_status = {"待定": "pending", "纳入": "included", "排除": "excluded", "全部": None}[
        filter_label
    ]
    page_size = 10
    candidates, total = storage.list_screening_candidates(
        project_id, status=filter_status, page=1, page_size=page_size
    )
    total_pages = max(1, ceil(total / page_size))
    if total_pages > 1:
        page = st.number_input(
            "队列页码", min_value=1, max_value=total_pages, value=1, step=1, key="queue_page"
        )
        candidates, total = storage.list_screening_candidates(
            project_id, status=filter_status, page=int(page), page_size=page_size
        )
    st.caption(f"共 {total} 条；当前显示 {len(candidates)} 条。")

    if not candidates:
        st.info("当前筛选范围内没有候选文献。")
    for candidate in candidates:
        key = candidate["candidate_key"]
        with st.container(border=True):
            st.markdown(f"#### {candidate['title']}")
            if key.startswith("conflict:"):
                st.error(
                    "身份冲突：该记录的强标识符指向不同候选，"
                    "请先在“重复记录”页核对，再决定筛选状态。"
                )
            metadata = " · ".join(
                str(value) for value in (candidate.get("year"), candidate.get("doi")) if value
            )
            st.caption(metadata or "缺少年份和 DOI")
            st.caption(
                f"当前决定：{_label(candidate['status'])}"
                + (
                    f" · 原因：{candidate['exclusion_reason']}"
                    if candidate.get("exclusion_reason")
                    else ""
                )
            )
            if candidate.get("notes"):
                st.caption(f"备注：{candidate['notes']}")
            if key.startswith("conflict:"):
                st.caption("解决身份冲突前，此候选不能保存筛选决定。")
            else:
                with st.form(f"decision_{key}"):
                    status_options = ["pending", "included", "excluded"]
                    current_index = status_options.index(candidate["status"])
                    new_status = st.radio(
                        "决定",
                        status_options,
                        index=current_index,
                        format_func=_label,
                        horizontal=True,
                    )
                    reason_index = (
                        EXCLUSION_REASONS.index(candidate["exclusion_reason"])
                        if candidate.get("exclusion_reason") in EXCLUSION_REASONS
                        else 0
                    )
                    reason = st.selectbox(
                        "排除原因（仅排除时必填）",
                        EXCLUSION_REASONS,
                        index=reason_index,
                    )
                    notes = st.text_area("备注", value=candidate.get("notes") or "", height=80)
                    if st.form_submit_button("保存决定"):
                        try:
                            storage.set_screening_decision(
                                project_id,
                                key,
                                new_status,
                                reason=reason if new_status == "excluded" else None,
                                notes=notes,
                            )
                            st.session_state["screening_message"] = "筛选决定已保存。"
                            st.rerun()
                        except Exception as error:
                            _show_error(error)

with duplicates_tab:
    duplicates = storage.list_screening_duplicates(project_id)
    if duplicates:
        st.dataframe(
            [
                {
                    "标题": item["title"],
                    "重复依据": item["basis"],
                    "时间": item["created_at"],
                }
                for item in duplicates
            ],
            hide_index=True,
            use_container_width=True,
        )
    else:
        st.info("尚未发现重复候选。")

with audit_tab:
    audit = storage.list_screening_audit(project_id)
    if audit:
        st.dataframe(
            [
                {
                    "时间": item["changed_at"],
                    "候选键": item["candidate_key"],
                    "原状态": _label(item["old_status"]) if item["old_status"] else "",
                    "新状态": _label(item["new_status"]),
                    "原原因": item["old_reason"] or "",
                    "新原因": item["new_reason"] or "",
                    "原备注": item["old_notes"] or "",
                    "新备注": item["new_notes"] or "",
                }
                for item in audit
            ],
            hide_index=True,
            use_container_width=True,
        )
    else:
        st.info("尚无筛选决定记录。")

with export_tab:
    st.markdown("#### PRISMA 数量")
    st.json(stats)
    all_candidates = storage.list_all_screening_candidates(project_id)
    st.download_button(
        "下载筛选结果 CSV",
        data=export_screening_csv(all_candidates),
        file_name="sciverse-screening.csv",
        mime="text/csv",
        key="download_screening_csv",
    )
    st.caption("CSV 包含每条候选的状态、排除原因、备注和更新时间，可用于复核筛选过程。")
