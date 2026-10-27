from __future__ import annotations

from math import ceil

import streamlit as st

from research_app import storage
from research_app.project_ui import project_selector


st.set_page_config(page_title="研究项目 · Sciverse", page_icon="S", layout="wide")

st.title("研究项目")
st.caption("从一个研究项目进入检索、筛选、证据和交付；项目数据保存在本机 SQLite。")

with st.expander("新建研究项目", expanded=not storage.list_screening_projects()):
    with st.form("create_research_project"):
        project_name = st.text_input("项目名称", placeholder="例如：高层建筑抗震研究")
        question = st.text_area("研究问题", placeholder="例如：不同结构体系的抗震性能如何？")
        inclusion = st.text_area("纳入标准", placeholder="例如：同行评审实证研究；2015 年后发表")
        exclusion = st.text_area("排除标准", placeholder="例如：非建筑结构；无全文")
        create_clicked = st.form_submit_button("创建项目", type="primary")
    if create_clicked:
        try:
            project_id = storage.create_screening_project(
                project_name, question, inclusion, exclusion
            )
            st.session_state["current_project_id"] = project_id
            st.success("研究项目已创建。")
            st.rerun()
        except Exception as error:
            st.error(str(error))

project_id = project_selector("overview_project_selector")
if not project_id:
    st.info("请选择或创建一个研究项目。")
    st.stop()

overview = storage.project_overview(project_id)
project = overview["project"]
st.markdown(f"### {project['name']}")
st.write(f"**研究问题：** {project['question']}")
st.caption(f"纳入标准：{project['inclusion_criteria'] or '未填写'}")
st.caption(f"排除标准：{project['exclusion_criteria'] or '未填写'}")

metrics = st.columns(4)
metrics[0].metric("识别", overview["identified"])
metrics[1].metric("去重后候选", overview["unique"])
metrics[2].metric("待筛选", overview["pending"])
metrics[3].metric("身份冲突", overview["conflicts"])

metrics = st.columns(4)
metrics[0].metric("纳入", overview["included"])
metrics[1].metric("排除", overview["excluded"])
metrics[2].metric("检索快照", overview["search_runs"])
metrics[3].metric("证据条目", overview["evidence_items"])

stat_check = storage.inspect_project_statistics(project_id)[0]
if stat_check["needs_review"]:
    st.warning(
        "项目统计与当前检索快照/来源记录不一致，可能来自旧版本重复导入。"
        "请运行 `python -m research_app.maintenance stats` 检查，"
        "确认后用 `rebuild-stats --project-id ... --apply` 重建。"
    )
if overview["conflicts"]:
    st.warning("存在未解决的身份冲突，冲突候选不能直接保存筛选决定。")

st.markdown("### 最近检索")
recent_runs = storage.list_search_runs(project_id, limit=5)
if recent_runs:
    st.dataframe(
        [
            {
                "时间": run["created_at"],
                "方式": "条件筛选" if run["mode"] == "structured" else "语义检索",
                "总命中数": run["result_count"],
                "本页快照数": run["snapshot_count"],
                "条件": str(run["params"]),
            }
            for run in recent_runs
        ],
        hide_index=True,
        use_container_width=True,
    )
    run_ids = [run["id"] for run in recent_runs]
    selected_run_id = st.selectbox(
        "选择要恢复的检索快照",
        run_ids,
        format_func=lambda value: next(
            f"{run['created_at']} · {run['snapshot_count']} 篇"
            for run in recent_runs
            if run["id"] == value
        ),
    )
    if st.button("载入检索快照"):
        run = storage.get_search_run(project_id, selected_run_id)
        if run:
            st.session_state["papers"] = run["papers"]
            st.session_state["has_searched"] = True
            st.session_state["search_kind"] = run["mode"]
            st.session_state["search_params"] = run["params"]
            st.session_state["current_search_run_id"] = run["id"]
            st.session_state["total_count"] = run["result_count"]
            page_size = int(run["params"].get("page_size") or len(run["papers"]) or 1)
            st.session_state["total_pages"] = max(
                1,
                ceil(run["result_count"] / page_size),
            )
            st.session_state["selected_paper"] = None
            st.session_state["paper_text"] = ""
            st.session_state["content_next_offset"] = None
            st.session_state["export_selection"] = {}
            st.session_state["export_sources"] = {}
            for key in ("trend_sample", "trend_source", "trend_requests", "trend_truncated"):
                st.session_state.pop(key, None)
            st.success("检索快照已载入；打开“文献发现”即可查看和继续操作。")
else:
    st.caption("还没有保存到本项目的检索快照。回到文献发现页检索后会自动保存。")

st.markdown("### 最近 AI 任务")
tasks = storage.list_ai_tasks(project_id, limit=5)
if tasks:
    st.dataframe(tasks, hide_index=True, use_container_width=True)
else:
    st.caption("还没有 AI 证据任务。")

st.markdown("### 项目导出清单")
st.caption("清单包含项目概况、统计、检索快照、候选来源、筛选状态和 AI 任务，便于复核数据范围。")
st.download_button(
    "下载项目清单 JSON",
    data=storage.project_manifest(project_id),
    file_name=f"sciverse-project-{project_id}.json",
    mime="application/json",
)

st.markdown("### 开始工作")
st.caption("使用左侧页面导航继续：文献发现与检索、去重与筛选、AI 证据与提纲。")

st.divider()
with st.expander("删除当前研究项目", expanded=False):
    st.error(
        "删除会永久移除该项目的检索快照、候选、筛选决定、审计记录、AI 任务和证据，且无法撤销。"
    )
    confirmation = st.text_input(
        "输入项目名称确认删除",
        placeholder=project["name"],
    )
    if st.button(
        "永久删除项目",
        disabled=confirmation.strip() != project["name"],
    ):
        try:
            if storage.delete_screening_project(project_id):
                for key in (
                    "current_project_id",
                    "screening_project_id",
                    "current_search_run_id",
                    "ai_evidence",
                    "ai_task_id",
                    "ai_loaded_project",
                    "ai_warnings",
                    "ai_outline",
                    "ai_editor_epoch",
                ):
                    st.session_state.pop(key, None)
                st.success("研究项目已删除。")
                st.rerun()
            else:
                st.warning("项目不存在或已被删除。")
        except Exception as error:
            st.error(str(error))
