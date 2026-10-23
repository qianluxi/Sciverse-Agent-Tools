from __future__ import annotations

import os
from typing import Any

import streamlit as st
from sciverse.credentials import resolve_token

from research_app.ai import (
    AIError,
    OpenAICompatibleClient,
    extract_evidence,
    generate_outline,
)
from research_app.service import call_api


st.set_page_config(page_title="AI 证据与提纲 · Sciverse", page_icon="S", layout="wide")

st.title("AI 证据与提纲")
st.caption(
    "用你自备的 OpenAI 兼容模型从论文中提取结构化证据，再生成可人工复核的综述提纲。"
    "AI 输出不会自动写入 Zotero 或最终论文。"
)

with st.sidebar:
    st.markdown("### Sciverse")
    sciverse_token = st.text_input("Sciverse API Token", type="password", placeholder="sci_...")
    sciverse_token = sciverse_token.strip() or resolve_token()
    st.divider()
    st.markdown("### AI 服务（OpenAI 兼容）")
    base_url = st.text_input(
        "Base URL",
        value=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        placeholder="https://api.openai.com/v1",
    )
    ai_key = st.text_input(
        "API Key",
        type="password",
        value=os.environ.get("OPENAI_API_KEY", ""),
        help="只在当前会话使用，不写入本地数据库。",
    )
    model = st.text_input(
        "模型名",
        value=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
    )


def _show_error(error: Exception) -> None:
    st.error(str(error))


def _paper_text(paper: dict[str, Any], warnings: list[str]) -> str:
    doc_id = paper.get("doc_id")
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
        except Exception as error:
            warnings.append(f"{paper.get('title') or doc_id}：读取全文失败，改用摘要（{error}）")
    text = str(paper.get("abstract") or paper.get("chunk") or "").strip()
    if text:
        return text
    warnings.append(f"{paper.get('title') or paper.get('unique_id')}：没有可用文本，已跳过")
    return ""


papers = st.session_state.get("papers", [])
if not papers:
    st.info("主页还没有检索结果。请先检索，再回到此页提取证据。")
    st.stop()

paper_map: dict[str, dict[str, Any]] = {}
for paper in papers:
    key = str(paper.get("unique_id") or paper.get("doc_id") or paper.get("title"))
    if paper.get("unique_id") or paper.get("doc_id") or paper.get("abstract") or paper.get("chunk"):
        paper_map[key] = paper

st.markdown("### 选择论文")
st.caption("单次最多选择 5 篇；优先读取全文前 6000 字，失败时使用摘要。")
selected_ids = st.multiselect(
    "论文",
    list(paper_map),
    format_func=lambda value: paper_map[value].get("title") or value,
    max_selections=5,
)
extract_clicked = st.button(
    "提取结构化证据",
    type="primary",
    disabled=not selected_ids or not ai_key or not model or not base_url,
)

if extract_clicked:
    warnings: list[str] = []
    documents = []
    for paper_id in selected_ids:
        paper = paper_map[paper_id]
        text = _paper_text(paper, warnings)
        if not text:
            continue
        documents.append(
            {
                "paper_id": paper.get("unique_id") or paper.get("doc_id") or paper_id,
                "title": paper.get("title") or "",
                "year": paper.get("publication_published_year") or "",
                "text": text[:6000],
            }
        )
    try:
        client = OpenAICompatibleClient(
            base_url=base_url,
            api_key=ai_key,
            model=model,
        )
        with st.spinner("正在调用 AI 提取证据…"):
            evidence = extract_evidence(client, documents)
        st.session_state["ai_evidence"] = evidence
        st.session_state["ai_warnings"] = warnings
        st.session_state["ai_outline"] = ""
    except Exception as error:
        _show_error(error)

warnings = st.session_state.get("ai_warnings", [])
for warning in warnings:
    st.warning(warning)

evidence = st.session_state.get("ai_evidence", [])
if evidence:
    st.markdown("### 文献证据")
    st.caption("每条证据都保留来源论文；请人工核对引文是否确实来自原文。")
    for item in evidence:
        with st.container(border=True):
            st.markdown(f"#### {item.get('title') or '（无标题）'}")
            st.caption(f"来源：`{item.get('paper_id')}` · 年份：{item.get('year') or '未知'}")
            if item.get("error"):
                st.error(item["error"])
            else:
                st.json(item.get("data"))

    st.markdown("### 综述提纲")
    default_question = str(st.session_state.get("search_params", {}).get("query") or "")
    question = st.text_area(
        "综述问题",
        value=default_question,
        placeholder="例如：现有研究如何评估该干预的效果？",
        height=80,
    )
    if st.button("生成综述提纲", type="primary"):
        try:
            client = OpenAICompatibleClient(
                base_url=base_url,
                api_key=ai_key,
                model=model,
            )
            with st.spinner("正在生成提纲…"):
                outline = generate_outline(client, question, evidence)
            st.session_state["ai_outline"] = outline
        except AIError as error:
            _show_error(error)
        except Exception as error:
            _show_error(error)

    outline = st.session_state.get("ai_outline", "")
    if outline:
        st.markdown(outline)
        st.download_button(
            "下载提纲 Markdown",
            data=outline.encode("utf-8"),
            file_name="sciverse-review-outline.md",
            mime="text/markdown",
            key="download_outline",
        )
        st.caption("提纲为 AI 辅助草稿，不包含最终论文正文；请逐条核对来源后再使用。")

    if st.button("清空 AI 结果"):
        st.session_state.pop("ai_evidence", None)
        st.session_state.pop("ai_warnings", None)
        st.session_state.pop("ai_outline", None)
        st.rerun()
