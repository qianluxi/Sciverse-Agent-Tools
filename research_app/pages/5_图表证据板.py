from __future__ import annotations

from typing import Any

import streamlit as st
from sciverse.credentials import resolve_token

from research_app.evidence import (
    MAX_IMAGES_PER_PAPER,
    MAX_PAPERS_PER_RUN,
    export_evidence_manifest_csv,
    export_evidence_zip,
    extract_figure_refs,
    is_readable,
)
from research_app.service import call_api


st.set_page_config(page_title="图表证据板 · Sciverse", page_icon="S", layout="wide")

st.title("图表证据板")
st.caption("从可访问的论文原文中提取图表，保留论文来源和文件路径，便于整理综述证据。")

with st.sidebar:
    entered_token = st.text_input("API Token", type="password", placeholder="sci_...")
    token = entered_token.strip() or resolve_token()
    if not token:
        st.info("提取图表需要 Sciverse API Token。")


def _show_error(error: Exception) -> None:
    st.error(str(error))


papers = st.session_state.get("papers", [])
if not papers:
    st.info("主页还没有检索结果。请先检索，再回到此页提取图表。")
    st.stop()

eligible = [paper for paper in papers if is_readable(paper)]
with st.expander("查看哪些文献可读全文", expanded=False):
    st.caption(
        "可读全文 = 有 doc_id 且 is_content_accessible=true。"
        "缺少 doc_id 表示没有全文记录；false 表示当前账号无权读取。"
    )
    st.dataframe(
        [
            {
                "标题": paper.get("title") or "（无标题）",
                "unique_id": paper.get("unique_id") or "",
                "doc_id": paper.get("doc_id") or "",
                "可读全文": (
                    "是"
                    if is_readable(paper)
                    else ("无全文记录" if not paper.get("doc_id") else "无权限/不可读")
                ),
            }
            for paper in papers
        ],
        hide_index=True,
        use_container_width=True,
    )
if not eligible:
    st.info("当前结果没有 is_content_accessible=true 的论文，无法提取图表。")
    st.stop()

paper_map: dict[str, dict[str, Any]] = {}
for paper in eligible:
    key = str(paper.get("unique_id") or paper.get("doc_id"))
    paper_map[key] = paper

st.markdown("### 选择论文")
st.caption(
    f"下拉框只列出可读全文的论文；单次最多选择 {MAX_PAPERS_PER_RUN} 篇，"
    f"每篇最多提取 {MAX_IMAGES_PER_PAPER} 张图。只读取原文第一段，不保存全文。"
)
selected_ids = st.multiselect(
    "可读取全文的检索结果",
    list(paper_map),
    format_func=lambda value: paper_map[value].get("title") or value,
    max_selections=MAX_PAPERS_PER_RUN,
)
extract_clicked = st.button(
    "提取图表",
    type="primary",
    disabled=not token or not selected_ids,
)

if extract_clicked and token:
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    requests = 0
    with st.spinner("正在读取原文并提取图表…"):
        for paper_id in selected_ids:
            paper = paper_map[paper_id]
            doc_id = paper.get("doc_id")
            try:
                requests += 1
                content = call_api(
                    token,
                    "read_content",
                    doc_id=doc_id,
                    offset=0,
                    limit=8000,
                )
            except Exception as error:
                errors.append(f"{paper.get('title') or paper_id}：读取原文失败（{error}）")
                continue
            refs = extract_figure_refs(content.get("text", ""))
            if not refs:
                errors.append(f"{paper.get('title') or paper_id}：原文中没有图表引用")
                continue
            for ref in refs:
                try:
                    requests += 1
                    image, mime = call_api(token, "get_resource", file_name=ref["file_name"])
                except Exception as error:
                    errors.append(
                        f"{paper.get('title') or paper_id}：{ref['file_name']} 加载失败（{error}）"
                    )
                    continue
                items.append(
                    {
                        "paper_id": str(paper.get("unique_id") or doc_id),
                        "title": paper.get("title") or "",
                        "year": paper.get("publication_published_year") or "",
                        "doi": paper.get("doi") or "",
                        "alt": ref["alt"],
                        "file_name": ref["file_name"],
                        "mime": mime,
                        "image": image,
                        "note": "",
                        "archive_name": "",
                    }
                )
    st.session_state["evidence_items"] = items
    st.session_state["evidence_errors"] = errors
    st.session_state["evidence_requests"] = requests

items = st.session_state.get("evidence_items", [])
errors = st.session_state.get("evidence_errors", [])
requests_used = st.session_state.get("evidence_requests", 0)

if items or errors:
    metrics = st.columns(3)
    metrics[0].metric("图表数量", len(items))
    metrics[1].metric("请求次数", requests_used)
    metrics[2].metric("跳过/失败", len(errors))

if errors:
    with st.expander("查看跳过和失败原因", expanded=False):
        for error in errors:
            st.warning(error)

if not items:
    if items == [] and errors:
        st.info("没有提取到可展示的图表。")
    else:
        st.info("选择论文后点击“提取图表”。")
    st.stop()

st.markdown("### 证据板")
for index, item in enumerate(items):
    with st.container(border=True):
        image_col, detail_col = st.columns([1, 2])
        with image_col:
            st.image(item["image"], caption=item["alt"], use_container_width=True)
        with detail_col:
            st.markdown(f"#### {item['title'] or '（无标题）'}")
            st.caption(f"来源 ID：`{item['paper_id']}` · 文件：`{item['file_name']}`")
            if item.get("doi"):
                st.caption(f"DOI: {item['doi']} · 年份：{item.get('year') or '未知'}")
            note = st.text_input(
                "我的备注",
                value=item.get("note", ""),
                key=f"evidence_note_{index}",
            )
            item["note"] = note

st.markdown("### 导出")
export_cols = st.columns(2)
with export_cols[0]:
    st.download_button(
        "下载证据清单 CSV",
        data=export_evidence_manifest_csv(items),
        file_name="sciverse-evidence.csv",
        mime="text/csv",
        key="download_evidence_csv",
    )
with export_cols[1]:
    st.download_button(
        "下载图表 ZIP",
        data=export_evidence_zip(items),
        file_name="sciverse-evidence.zip",
        mime="application/zip",
        key="download_evidence_zip",
    )

if st.button("清空证据板"):
    st.session_state.pop("evidence_items", None)
    st.session_state.pop("evidence_errors", None)
    st.session_state.pop("evidence_requests", None)
    st.rerun()
