from __future__ import annotations

import asyncio
import csv
import html
import io
import re
from typing import Any

import streamlit as st
from sciverse import AgentToolsClient
from sciverse.credentials import resolve_token


st.set_page_config(
    page_title="文献研究助手 · Sciverse",
    page_icon="S",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    :root { --ink: #182b28; --muted: #61716c; --paper: #f5f5ef; --green: #236b56; --line: #dfe4da; }
    html, body, [class*="css"] { font-family: 'DM Sans', 'Noto Serif SC', sans-serif; }
    .stApp { background: var(--paper); color: var(--ink); }
    [data-testid="stSidebar"] { background: #e9eee6; border-right: 1px solid var(--line); }
    [data-testid="stSidebar"] h2 { font-family: 'Noto Serif SC', serif; }
    .hero { padding: 2.1rem 2.2rem 1.8rem; border-radius: 18px; color: #f5f5ef;
        background: radial-gradient(ellipse at 85% 0%, #427d64 0, transparent 38%), #193b32; margin-bottom: 1.5rem; }
    .hero-kicker { font: 500 .72rem 'DM Mono', monospace; letter-spacing: .16em; text-transform: uppercase; color: #b5d7c0; }
    .hero h1 { font: 600 clamp(1.8rem, 4vw, 2.7rem) 'Noto Serif SC', serif; letter-spacing: -.04em; margin: .55rem 0 .45rem; }
    .hero p { color: #d1dfd5; font-size: 1rem; max-width: 760px; margin: 0; }
    .section-label { font: 500 .72rem 'DM Mono', monospace; letter-spacing: .1em; color: var(--green); text-transform: uppercase; }
    .result-meta { color: var(--muted); font-size: .9rem; }
    .stButton > button[kind="primary"] { background: var(--green); border-color: var(--green); }
    .stButton > button { border-radius: 8px; }
    div[data-testid="stVerticalBlock"] div[data-testid="stVerticalBlock"] { gap: .7rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


async def _call_api(token: str, action: str, **kwargs: Any) -> Any:
    async with AgentToolsClient(token=token) as client:
        return await getattr(client, action)(**kwargs)


def call_api(token: str, action: str, **kwargs: Any) -> Any:
    return asyncio.run(_call_api(token, action, **kwargs))


def _authors(paper: dict[str, Any]) -> str:
    authors = paper.get("author") or paper.get("authors") or []
    if isinstance(authors, str):
        return authors
    names = [a.get("name", "") if isinstance(a, dict) else str(a) for a in authors]
    return ", ".join(name for name in names if name)


def _export_csv(papers: list[dict[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    columns = ["title", "authors", "year", "journal", "doi", "abstract", "unique_id", "doc_id"]
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    for paper in papers:
        writer.writerow(
            {
                "title": paper.get("title", ""),
                "authors": _authors(paper),
                "year": paper.get("publication_published_year", ""),
                "journal": paper.get("publication_venue_name_unified", ""),
                "doi": paper.get("doi", ""),
                "abstract": paper.get("abstract") or paper.get("chunk", ""),
                "unique_id": paper.get("unique_id", ""),
                "doc_id": paper.get("doc_id", ""),
            }
        )
    return output.getvalue().encode("utf-8-sig")


def _display_content(text: str, token: str) -> None:
    image_refs = re.findall(r"!\[([^\]]*)\]\(([^)]+)\)", text)
    readable_text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", "", text)
    st.markdown(readable_text)
    resource_cache = st.session_state.setdefault("resource_cache", {})
    for alt, file_name in image_refs[:4]:
        try:
            if file_name not in resource_cache:
                resource_cache[file_name] = call_api(token, "get_resource", file_name=file_name)[0]
            st.image(resource_cache[file_name], caption=alt or file_name)
        except Exception:
            st.caption(f"未能加载文中图片：{file_name}")


def _show_error(error: Exception) -> None:
    response = getattr(error, "response", None)
    if response is not None:
        st.error(f"服务暂时无法完成请求（HTTP {response.status_code}）。请检查 Token、检索条件或稍后重试。")
        with st.expander("查看错误详情"):
            st.code(response.text[:2000])
    else:
        st.error(f"请求失败：{error}")


with st.sidebar:
    st.markdown("## 连接 Sciverse")
    st.caption("输入在 Sciverse 控制台申请的 API Token。Token 只用于当前浏览器会话。")
    entered_token = st.text_input("API Token", type="password", placeholder="sv-...")
    token = entered_token.strip() or resolve_token()
    if token:
        st.success("已就绪，可以开始检索。")
    else:
        st.info("尚未设置 Token。请在上方输入，或先到 sciverse.space 申请。")
    st.link_button("前往 Sciverse 申请 Token", "https://sciverse.space")
    st.divider()
    st.markdown("#### 小提示")
    st.caption("可以直接用自然语言描述你想研究的问题；也可以按作者和发表年份筛选。")
    st.caption("全文是否可读取决于论文资源和你的账号权限。")


st.markdown(
    """
    <section class="hero">
      <div class="hero-kicker">SCIVERSE · RESEARCH DESK</div>
      <h1>把研究问题，变成一张文献地图。</h1>
      <p>用自然语言寻找相关研究，按条件缩小范围，阅读可访问的论文原文，并整理检索结果。</p>
    </section>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="section-label">01 / 开始检索</div>', unsafe_allow_html=True)
mode_label = st.radio(
    "选择检索方式",
    ["智能语义检索", "条件筛选"],
    horizontal=True,
    help="语义检索适合提出研究问题；条件筛选适合已知作者、年份或题名线索。",
)

with st.form("search_form", border=True):
    if mode_label == "智能语义检索":
        query = st.text_area(
            "你想了解什么？",
            placeholder="例如：深度学习如何帮助早期发现阿尔茨海默病？",
            height=90,
        )
        options = st.columns([1, 1, 2])
        with options[0]:
            top_k = st.selectbox("结果数量", [5, 10, 20], index=1)
        with options[1]:
            quality_label = st.selectbox("检索深度", ["平衡（推荐）", "更快", "更深入"])
        with options[2]:
            st.caption("语义检索会理解问题含义，不只匹配题目中的关键词。")
        search_clicked = st.form_submit_button("查找相关文献", type="primary", use_container_width=True)
        search_kind = "semantic"
    else:
        query = st.text_input("关键词或题名（可选）", placeholder="例如：climate change adaptation")
        cols = st.columns(3)
        with cols[0]:
            author = st.text_input("作者姓名（可选）", placeholder="例如：Hinton")
        with cols[1]:
            year_from = st.number_input("起始年份", min_value=0, max_value=2100, value=0, step=1)
        with cols[2]:
            year_to = st.number_input("结束年份", min_value=0, max_value=2100, value=0, step=1)
        search_clicked = st.form_submit_button("筛选论文", type="primary", use_container_width=True)
        search_kind = "structured"

if search_clicked:
    if not token:
        st.warning("请先在左侧输入 Sciverse API Token。")
    elif mode_label == "智能语义检索" and not query.strip():
        st.warning("请先描述你想研究的问题。")
    elif mode_label == "条件筛选" and not (query.strip() or author.strip() or year_from or year_to):
        st.warning("请至少填写一个关键词、作者或年份条件。")
    elif mode_label == "条件筛选" and year_from and year_to and year_from > year_to:
        st.warning("起始年份不能晚于结束年份。")
    else:
        try:
            with st.spinner("正在 Sciverse 学术文献库中检索…"):
                if mode_label == "智能语义检索":
                    quality = {"平衡（推荐）": "balanced", "更快": "fast", "更深入": "quality"}[quality_label]
                    response = call_api(token, "semantic_search", query=query.strip(), top_k=top_k, mode=quality)
                    papers = response.get("hits", [])
                    search_kind = "semantic"
                else:
                    params: dict[str, Any] = {"page_size": 20}
                    if query.strip():
                        params["query"] = query.strip()
                    if author.strip():
                        params["authors"] = [author.strip()]
                    if year_from:
                        params["year_from"] = year_from
                    if year_to:
                        params["year_to"] = year_to
                    response = call_api(token, "search_papers", **params)
                    papers = response.get("results", [])
                    search_kind = "structured"
                st.session_state["papers"] = papers
                st.session_state["has_searched"] = True
                st.session_state["search_kind"] = search_kind
                st.session_state["query"] = query.strip()
                st.session_state["selected_paper"] = None
                st.session_state["paper_text"] = ""
                st.session_state["content_next_offset"] = None
        except Exception as error:
            _show_error(error)


papers = st.session_state.get("papers", [])
if papers:
    st.divider()
    st.markdown('<div class="section-label">02 / 检索结果</div>', unsafe_allow_html=True)
    st.subheader(f"找到 {len(papers)} 条相关文献")
    st.caption("选择一篇文献可查看摘要、标识信息和可访问的原文片段。")
    st.download_button(
        "下载文献清单（CSV）",
        data=_export_csv(papers),
        file_name="sciverse-literature.csv",
        mime="text/csv",
        key="download_results",
    )

    for index, paper in enumerate(papers):
        title = paper.get("title") or "（暂无标题）"
        authors = _authors(paper)
        year = paper.get("publication_published_year")
        venue = paper.get("publication_venue_name_unified")
        with st.container(border=True):
            title_col, score_col = st.columns([5, 1])
            with title_col:
                st.markdown(f"### {title}")
            with score_col:
                if st.session_state.get("search_kind") == "semantic" and paper.get("score") is not None:
                    st.metric("相关度", f"{paper['score']:.2f}")
            metadata = " · ".join(str(value) for value in (authors, year, venue) if value)
            safe_metadata = html.escape(metadata or "出版信息未提供")
            st.markdown(f'<div class="result-meta">{safe_metadata}</div>', unsafe_allow_html=True)
            abstract = paper.get("abstract") or paper.get("chunk")
            if abstract:
                st.write(abstract[:700] + ("…" if len(abstract) > 700 else ""))
            if st.button("查看文献", key=f"select_{index}"):
                st.session_state["selected_paper"] = paper
                st.session_state["paper_text"] = ""
                st.session_state["content_next_offset"] = None


selected = st.session_state.get("selected_paper")
if selected:
    st.divider()
    st.markdown('<div class="section-label">03 / 文献阅读</div>', unsafe_allow_html=True)
    st.subheader(selected.get("title") or "文献详情")
    doi = selected.get("doi")
    if doi:
        st.markdown(f"DOI: [{doi}](https://doi.org/{doi})")
    abstract = selected.get("abstract") or selected.get("chunk")
    if abstract:
        with st.expander("查看摘要或检索命中的原文片段", expanded=True):
            st.write(abstract)

    accessible = selected.get("is_content_accessible", True)
    doc_id = selected.get("doc_id")
    if not doc_id:
        st.info("此文献没有可读取的全文记录。你仍可在上方查看检索到的摘要和文献信息。")
    elif not accessible:
        st.info("当前账号无法读取这篇论文的全文；可继续使用上方的文献信息。")
    else:
        offset = selected.get("offset", 0) if st.session_state.get("search_kind") == "semantic" else 0
        if st.button("读取论文原文", type="primary", key="read_content"):
            try:
                with st.spinner("正在读取论文内容…"):
                    content = call_api(token, "read_content", doc_id=doc_id, offset=offset, limit=6000)
                st.session_state["paper_text"] = content.get("text", "")
                st.session_state["content_next_offset"] = content.get("next_offset") if content.get("more") else None
            except Exception as error:
                _show_error(error)

        if st.session_state.get("paper_text"):
            st.markdown("#### 原文")
            _display_content(st.session_state["paper_text"], token)
            next_offset = st.session_state.get("content_next_offset")
            if next_offset is not None and st.button("继续阅读下一段", key="read_more"):
                try:
                    with st.spinner("正在读取下一段…"):
                        content = call_api(token, "read_content", doc_id=doc_id, offset=next_offset, limit=6000)
                    st.session_state["paper_text"] += content.get("text", "")
                    st.session_state["content_next_offset"] = content.get("next_offset") if content.get("more") else None
                    st.rerun()
                except Exception as error:
                    _show_error(error)

elif st.session_state.get("has_searched"):
    st.info("没有找到符合条件的文献。试试换一种说法、放宽年份范围，或改用语义检索。")
else:
    st.markdown(
        "<div style='padding:2rem 1rem;color:#61716c;text-align:center'>"
        "<span class='section-label'>YOUR NEXT DISCOVERY</span><br><br>"
        "输入一个研究问题，或切换到条件筛选开始探索文献。"
        "</div>",
        unsafe_allow_html=True,
    )
