from __future__ import annotations

import streamlit as st

from research_app import storage
from research_app.ai_review import export_matrix_csv
from research_app.project_ui import project_selector
from research_app.report import (
    build_project_report,
    export_prisma_records_csv,
    export_project_bundle,
    export_project_report_json,
    render_project_report_markdown,
)
from research_app.screening import (
    FULLTEXT_EXCLUSION_REASONS,
    FULLTEXT_STATUS_LABELS,
    FULLTEXT_STATUSES,
    STATUS_LABELS,
)


st.set_page_config(page_title="研究报告 · Sciverse", page_icon="S", layout="wide")
st.title("研究报告")
st.caption(
    "从项目底层记录生成 PRISMA 2020 计数、筛选汇总与证据矩阵摘要。"
    "题名/摘要纳入与全文最终纳入分开呈现，不把轻量计数当作完整 PRISMA。"
)

if "report_message" in st.session_state:
    st.success(st.session_state.pop("report_message"))
if "report_error" in st.session_state:
    st.error(st.session_state.pop("report_error"))

with st.sidebar:
    project_id = project_selector("report_project_selector")

if not project_id:
    st.info("请先在“研究项目”页创建或选择一个项目。")
    st.stop()

project = storage.get_screening_project(project_id)
st.markdown(f"### {project['name']}")
st.write(f"**研究问题：** {project['question']}")

report = build_project_report(project_id)
prisma = report["prisma"]
title_abstract = prisma["title_abstract"]
fulltext = prisma["fulltext"]

st.markdown("### PRISMA 2020 计数")
metrics = st.columns(5)
metrics[0].metric("识别记录", prisma["identified"])
metrics[1].metric("去重移除", prisma["duplicates_removed"])
metrics[2].metric("题摘纳入", title_abstract["included"])
metrics[3].metric("全文纳入", fulltext["included"])
metrics[4].metric("题摘待定", title_abstract["pending"])

st.dataframe(
    [
        {"阶段": "识别记录", "数量": prisma["identified"]},
        {"阶段": "去重移除", "数量": prisma["duplicates_removed"]},
        {"阶段": "去重后记录", "数量": prisma["unique_records"]},
        {"阶段": "题名/摘要待定", "数量": title_abstract["pending"]},
        {"阶段": "题名/摘要排除", "数量": title_abstract["excluded"]},
        {"阶段": "题名/摘要纳入", "数量": title_abstract["included"]},
        {"阶段": "全文尚未寻求获取", "数量": fulltext["not_sought"]},
        {"阶段": "全文已寻求未获取", "数量": fulltext["not_retrieved"]},
        {"阶段": "全文已获取待评估", "数量": fulltext["retrieved_pending"]},
        {"阶段": "全文已检索合计", "数量": fulltext["sought"]},
        {"阶段": "全文已评估", "数量": fulltext["assessed"]},
        {"阶段": "全文排除", "数量": fulltext["excluded"]},
        {"阶段": "全文纳入（最终纳入）", "数量": fulltext["included"]},
    ],
    hide_index=True,
    use_container_width=True,
)
for warning in prisma["warnings"]:
    st.warning(warning)
if fulltext["excluded_reasons"]:
    st.caption("全文排除原因：")
    st.dataframe(
        [
            {"原因": item["reason"] or "未注明", "数量": item["count"]}
            for item in fulltext["excluded_reasons"]
        ],
        hide_index=True,
        use_container_width=True,
    )

st.markdown("### 全文阶段记录")
stale = storage.list_stale_fulltext_decisions(project_id)
if stale:
    st.warning(
        f"发现 {len(stale)} 条历史不一致记录：题名/摘要状态不是纳入，"
        "但仍保留全文阶段记录。请重置后再修改题摘决定。"
    )
    stale_map = {item["candidate_key"]: item for item in stale}
    stale_key = st.selectbox(
        "选择历史不一致记录",
        list(stale_map),
        format_func=lambda value: stale_map[value].get("candidate_title") or value,
        key=f"stale_fulltext_{project_id}",
    )
    stale_columns = st.columns(2)
    if stale_columns[0].button(
        "重置所选历史全文记录",
        key=f"stale_reset_one_{project_id}",
    ):
        try:
            storage.reset_fulltext_decision(project_id, stale_key)
            st.session_state["report_message"] = "已重置所选历史全文记录。"
            st.rerun()
        except Exception as error:  # noqa: BLE001
            st.error(str(error))
    if stale_columns[1].button(
        f"重置全部 {len(stale)} 条历史记录",
        key=f"stale_reset_all_{project_id}",
    ):
        try:
            for item in stale:
                storage.reset_fulltext_decision(
                    project_id, item["candidate_key"]
                )
            st.session_state["report_message"] = (
                f"已重置 {len(stale)} 条历史全文记录。"
            )
            st.rerun()
        except Exception as error:  # noqa: BLE001
            st.error(str(error))

included_candidates = [
    candidate
    for candidate in storage.list_all_screening_candidates(project_id)
    if candidate["status"] == "included"
]
if not included_candidates:
    st.info("当前没有题名/摘要纳入的候选，无法记录全文阶段。")
else:
    candidate_map = {
        candidate["candidate_key"]: candidate for candidate in included_candidates
    }
    selected_key = st.selectbox(
        "选择题名/摘要纳入的文献",
        list(candidate_map),
        format_func=lambda value: candidate_map[value].get("title") or value,
        key=f"fulltext_candidate_{project_id}",
    )
    candidate = candidate_map[selected_key]
    current = storage.get_fulltext_decision(project_id, selected_key) or {}
    current_status = current.get("status") or "not_sought"
    st.caption(
        f"当前：{FULLTEXT_STATUS_LABELS.get(current_status, current_status)}"
        + (
            f" · 原因：{current.get('exclusion_reason')}"
            if current.get("exclusion_reason")
            else ""
        )
    )
    with st.form(f"fulltext_form_{selected_key}"):
        status_options = list(FULLTEXT_STATUSES)
        status = st.radio(
            "全文阶段",
            status_options,
            index=status_options.index(current_status),
            format_func=lambda value: FULLTEXT_STATUS_LABELS.get(value, value),
            horizontal=True,
            key=f"ft_status_{selected_key}",
        )
        reason_index = (
            FULLTEXT_EXCLUSION_REASONS.index(current["exclusion_reason"])
            if current.get("exclusion_reason") in FULLTEXT_EXCLUSION_REASONS
            else 0
        )
        reason = st.selectbox(
            "全文排除原因（仅全文排除时必填）",
            FULLTEXT_EXCLUSION_REASONS,
            index=reason_index,
            key=f"ft_reason_{selected_key}",
        )
        notes = st.text_area(
            "全文阶段备注",
            value=current.get("notes") or "",
            height=70,
            key=f"ft_notes_{selected_key}",
        )
        if st.form_submit_button("保存全文阶段决定"):
            try:
                storage.set_fulltext_decision(
                    project_id,
                    selected_key,
                    status,
                    reason=reason if status == "excluded" else None,
                    notes=notes,
                )
                st.session_state["report_message"] = "全文阶段决定已保存。"
                st.rerun()
            except Exception as error:  # noqa: BLE001
                st.error(str(error))

    if storage.get_fulltext_decision(project_id, selected_key):
        if st.button(
            "重置全文阶段（允许修改题名/摘要决定）",
            key=f"ft_reset_{selected_key}",
        ):
            try:
                storage.reset_fulltext_decision(project_id, selected_key)
                st.session_state["report_message"] = "已重置全文阶段。"
                st.rerun()
            except Exception as error:  # noqa: BLE001
                st.error(str(error))

    decisions = storage.list_fulltext_decisions(project_id, active_only=True)
    if decisions:
        title_map = {
            item["candidate_key"]: item.get("title") or item["candidate_key"]
            for item in storage.list_all_screening_candidates(project_id)
        }
        st.caption("已记录的全文阶段：")
        st.dataframe(
            [
                {
                    "文献": title_map.get(item["candidate_key"], item["candidate_key"]),
                    "全文阶段": FULLTEXT_STATUS_LABELS.get(
                        item["status"], item["status"]
                    ),
                    "原因": item["exclusion_reason"] or "",
                    "备注": item["notes"] or "",
                    "更新时间": item["updated_at"],
                }
                for item in decisions
            ],
            hide_index=True,
            use_container_width=True,
        )
    audit = storage.list_fulltext_audit(project_id, limit=50)
    if audit:
        with st.expander("全文阶段审计"):
            st.dataframe(
                [
                    {
                        "时间": item["changed_at"],
                        "文献": item["candidate_key"],
                        "原状态": FULLTEXT_STATUS_LABELS.get(
                            item["old_status"] or "", "未记录"
                        ),
                        "新状态": FULLTEXT_STATUS_LABELS.get(
                            item["new_status"], item["new_status"]
                        ),
                        "原原因": item["old_reason"] or "",
                        "新原因": item["new_reason"] or "",
                        "备注": item["new_notes"] or "",
                    }
                    for item in audit
                ],
                hide_index=True,
                use_container_width=True,
            )

st.markdown("### 报告预览")
markdown = render_project_report_markdown(report).decode("utf-8")
st.markdown(markdown)

st.markdown("### 导出")
download_columns = st.columns(4)
with download_columns[0]:
    st.download_button(
        "下载研究报告 Markdown",
        data=render_project_report_markdown(report),
        file_name="sciverse-project-report.md",
        mime="text/markdown",
        key="download_project_report_md",
    )
with download_columns[1]:
    st.download_button(
        "下载研究报告 JSON",
        data=export_project_report_json(report),
        file_name="sciverse-project-report.json",
        mime="application/json",
        key="download_project_report_json",
    )
with download_columns[2]:
    st.download_button(
        "下载 PRISMA 逐条记录 CSV",
        data=export_prisma_records_csv(project_id),
        file_name="sciverse-prisma-records.csv",
        mime="text/csv",
        key="download_prisma_records",
    )
with download_columns[3]:
    matrix = storage.load_evidence_matrix(project_id)
    st.download_button(
        "下载证据矩阵 CSV",
        data=export_matrix_csv(matrix) if matrix else b"",
        file_name="sciverse-evidence-matrix.csv",
        mime="text/csv",
        key="download_report_evidence_matrix",
        disabled=not matrix,
    )

st.download_button(
    "下载可追溯成果包 ZIP（报告 + PRISMA + 来源/审计 + 三版本证据 + manifest）",
    data=export_project_bundle(project_id),
    file_name="sciverse-project-bundle.zip",
    mime="application/zip",
    key="download_project_bundle",
)
