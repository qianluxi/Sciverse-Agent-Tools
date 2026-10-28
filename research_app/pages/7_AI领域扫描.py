from __future__ import annotations

import hashlib
import json
import os

import streamlit as st

from research_app import storage
from research_app.ai import OpenAICompatibleClient
from research_app.ai_settings import default_profile
from research_app.field_scan import (
    CATEGORY_LABELS,
    FIELD_SCAN_PROMPT_VERSION,
    scan_documents,
    scan_report_markdown,
)
from research_app.project_ui import project_selector


st.set_page_config(page_title="AI 领域扫描 · Sciverse", page_icon="S", layout="wide")

st.title("AI 领域扫描")
st.caption(
    "对当前检索结果或项目快照做领域分类、逐篇核心概括，并归纳主题与空白。"
    "结果属于项目 AI 分析，不自动改变筛选决定。"
)

_default_profile = default_profile()
st.session_state.setdefault("ai_base_url", _default_profile["base_url"])
st.session_state.setdefault("ai_model", _default_profile["model"])
st.session_state.setdefault("ai_key_session", os.environ.get("OPENAI_API_KEY", ""))

with st.sidebar:
    project_id = project_selector("scan_project_selector")
    st.divider()
    st.markdown("### AI 服务")
    entered_base_url = st.text_input(
        "Base URL",
        value=st.session_state.get("ai_base_url", _default_profile["base_url"]),
        key="scan_base_url_input",
    )
    st.session_state["ai_base_url"] = entered_base_url.strip().rstrip("/")
    entered_key = st.text_input(
        "API Key",
        type="password",
        value=st.session_state.get("ai_key_session", ""),
        key="scan_key_input",
        help="只在当前浏览器会话使用，不写入本地数据库。",
    )
    st.session_state["ai_key_session"] = entered_key
    entered_model = st.text_input(
        "模型名",
        value=st.session_state.get("ai_model", _default_profile["model"]),
        key="scan_model_input",
    )
    st.session_state["ai_model"] = entered_model.strip()

if not project_id:
    st.info("请先在“研究项目”页创建或选择一个项目。")
    st.stop()

project_name = next(
    (
        project["name"]
        for project in storage.list_screening_projects()
        if project["id"] == project_id
    ),
    project_id,
)

base_url = st.session_state["ai_base_url"]
ai_key = st.session_state["ai_key_session"]
model = st.session_state["ai_model"]

session_papers = st.session_state.get("papers", [])
project_run = storage.latest_search_run(project_id)
source_options = []
if session_papers:
    source_options.append("当前会话检索结果")
if project_run:
    source_options.append("项目最近检索快照")
if not source_options:
    st.info("当前没有可扫描的检索结果。")
    st.stop()
data_source = source_options[0]
if len(source_options) > 1:
    data_source = st.radio("扫描数据源", source_options, horizontal=True)
if data_source == "项目最近检索快照" and project_run:
    papers = project_run["papers"]
    source_key = f"{project_id}:project:{project_run['id']}"
else:
    papers = session_papers
    source_key = (
        f"{project_id}:session:"
        f"{st.session_state.get('current_search_run_id') or 'no-run'}:"
        f"{st.session_state.get('search_params', {})}"
    )
if st.session_state.get("scan_source_key") != source_key:
    st.session_state["scan_source_key"] = source_key
    st.session_state.pop("scan_selected", None)

paper_map = {}
for paper in papers:
    key = str(paper.get("unique_id") or paper.get("doc_id") or paper.get("title"))
    if paper.get("title") and (paper.get("abstract") or paper.get("chunk")):
        paper_map[key] = paper
if not paper_map:
    st.info("所选数据源没有包含摘要的文献。")
    st.stop()

selected_ids = st.multiselect(
    "选择扫描论文",
    list(paper_map),
    format_func=lambda value: paper_map[value].get("title") or value,
    max_selections=20,
    key="scan_selected",
)
if len(paper_map) > 20:
    st.caption("单次最多扫描 20 篇；当前数据源共 " + str(len(paper_map)) + " 篇摘要。")

scan_clicked = st.button(
    "开始领域扫描",
    type="primary",
    disabled=not selected_ids or not ai_key or not base_url or not model,
)
if not ai_key:
    st.caption("请先在左侧输入 AI API Key。")

if scan_clicked:
    documents = []
    for paper_id in selected_ids:
        paper = paper_map[paper_id]
        documents.append(
            {
                "paper_id": paper.get("unique_id") or paper.get("doc_id") or paper_id,
                "title": paper.get("title") or "",
                "year": paper.get("publication_published_year") or "",
                "text": str(paper.get("abstract") or paper.get("chunk") or "")[:4000],
            }
        )
    input_hash = hashlib.sha256(
        json.dumps(documents, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    task_id = storage.create_ai_task(
        project_id,
        kind="field_scan",
        model=model,
        prompt_version=FIELD_SCAN_PROMPT_VERSION,
        input_hash=input_hash,
    )
    try:
        client = OpenAICompatibleClient(
            base_url=base_url,
            api_key=ai_key,
            model=model,
        )
        with st.spinner("正在分析领域分类、核心概括和空白…"):
            result = scan_documents(client, documents)
        storage.finish_ai_task(task_id, status="completed")
        storage.save_scan_report(
            project_id,
            task_id,
            result["themes"],
            result["gaps"],
        )
        storage.save_evidence_items(
            project_id,
            task_id,
            [
                {
                    **item,
                    "raw_data": item.get("data"),
                    "model": model,
                    "prompt_version": FIELD_SCAN_PROMPT_VERSION,
                }
                for item in result["items"]
            ],
        )
        st.session_state["scan_active_task_id"] = task_id
        st.rerun()
    except Exception as error:
        try:
            storage.finish_ai_task(task_id, status="failed", error=str(error))
        except Exception:
            pass
        st.error(str(error))

saved = storage.load_latest_scan(project_id)
if not saved:
    st.info("还没有领域扫描结果。选择论文后开始扫描。")
    st.stop()

st.caption(
    f"最近任务：{saved['task']['created_at']} · 模型：{saved['task']['model']} · "
    f"提示词：{saved['task']['prompt_version']}"
)
if saved["task"].get("error"):
    st.error(saved["task"]["error"])

reviewed_items = []
for item in saved["items"]:
    data = item.get("data") or {}
    with st.container(border=True):
        st.markdown(f"#### {item['title'] or '（无标题）'}")
        if item.get("error"):
            st.error(item["error"])
            reviewed_items.append(item)
            continue
        category_keys = list(CATEGORY_LABELS)
        current_category = data.get("category")
        if current_category not in category_keys:
            current_category = "other"
        category = st.selectbox(
            "分类",
            category_keys,
            index=category_keys.index(current_category),
            format_func=lambda value: CATEGORY_LABELS[value],
            key=f"scan_category_{item['paper_id']}",
        )
        summary = st.text_area(
            "核心概括",
            value=str(data.get("core_summary") or ""),
            height=90,
            key=f"scan_summary_{item['paper_id']}",
        )
        rationale = st.text_area(
            "分类理由",
            value=str(data.get("rationale") or ""),
            height=70,
            key=f"scan_rationale_{item['paper_id']}",
        )
        quotes = data.get("evidence_quotes") or []
        if quotes:
            st.caption("原文依据：")
            for quote in quotes:
                st.markdown(f"> {quote.get('quote', '')}")
        reviewed = st.checkbox(
            "我已核对这篇文献的扫描结果",
            value=bool(item.get("reviewed")),
            key=f"scan_reviewed_{item['paper_id']}",
        )
        note = st.text_area(
            "核对备注",
            value=str(item.get("reviewer_note") or ""),
            height=70,
            key=f"scan_note_{item['paper_id']}",
        )
        edited_item = {
            **item,
            "data": {
                **data,
                "category": category,
                "core_summary": summary,
                "rationale": rationale,
            },
            "reviewed": reviewed,
            "reviewer_note": note,
        }
        reviewed_items.append(edited_item)

try:
    storage.save_evidence_items(project_id, saved["task"]["id"], reviewed_items)
    st.caption("人工核对结果已保存到项目。")
except Exception as error:
    st.warning(f"扫描结果已生成，但保存核对状态失败：{error}")

st.markdown("### 领域主题")
if saved["themes"]:
    for theme in saved["themes"]:
        st.markdown(f"**{theme.get('theme') or '未命名主题'}**")
        st.write(theme.get("summary") or "")
        if theme.get("paper_ids"):
            st.caption("来源：" + "、".join(str(value) for value in theme["paper_ids"]))
else:
    st.caption("未生成主题汇总。")

st.markdown("### 领域空白与痛点")
if saved["gaps"]:
    for gap in saved["gaps"]:
        st.warning(gap.get("gap") or "未命名空白")
        if gap.get("evidence"):
            st.caption(f"依据：{gap['evidence']}")
        if gap.get("paper_ids"):
            st.caption("相关论文：" + "、".join(str(value) for value in gap["paper_ids"]))
else:
    st.caption("未生成空白汇总。")

report_saved = {
    "task": saved["task"],
    "items": reviewed_items,
    "themes": saved["themes"],
    "gaps": saved["gaps"],
}
markdown_report = scan_report_markdown(
    report_saved,
    project_name=project_name,
    project_id=project_id,
)
st.markdown("### 导出报告")
st.caption("Markdown 报告按表格整理分类概览、文献概括、原文依据、主题和研究空白。")
download_columns = st.columns(2)
with download_columns[0]:
    st.download_button(
        "下载可读报告（Markdown）",
        data=markdown_report.encode("utf-8"),
        file_name="sciverse-field-scan.md",
        mime="text/markdown",
        type="primary",
    )
with download_columns[1]:
    st.download_button(
        "下载原始数据（JSON）",
        data=(json.dumps(report_saved, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        ),
        file_name="sciverse-field-scan.json",
        mime="application/json",
    )
