from __future__ import annotations

import html
from math import ceil
import re
from typing import Any

import streamlit as st
from sciverse.credentials import resolve_token

from research_app.errors import describe_error
from research_app.exports import (
    export_bibtex,
    export_markdown,
    export_results_csv,
    export_ris,
    format_authors,
)
from research_app.search import parse_catalog_value, split_filter_values
from research_app.service import call_api, enrich_semantic_hits
from research_app.storage import (
    delete_saved_search,
    export_saved_searches_backup,
    list_recent_searches,
    list_saved_searches,
    record_search,
    restore_saved_searches_backup,
    save_search,
)


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
    message, details = describe_error(error)
    st.error(message)
    if details:
        with st.expander("查看错误详情"):
            st.code(details)


def _paper_identity(paper: dict[str, Any], index: int) -> str:
    identity = paper.get("unique_id") or paper.get("doc_id") or paper.get("title") or index
    return f"{identity}_{paper.get('chunk_id') or paper.get('offset', index)}"


def _execute_search(
    token: str,
    search_kind: str,
    params: dict[str, Any],
    *,
    preserve_selection: bool = False,
) -> None:
    enrichment = None
    if search_kind == "semantic":
        response = call_api(token, "semantic_search", **params)
        hits = response.get("hits", [])
        try:
            papers, requests, matched = enrich_semantic_hits(token, hits)
            enrichment = {
                "requests": requests,
                "matched": matched,
                "total": len(hits),
                "error": None,
            }
        except Exception as error:
            papers = hits
            enrichment = {
                "requests": 0,
                "matched": 0,
                "total": len(hits),
                "error": str(error),
            }
        total_count = len(papers)
        total_pages = 1
    else:
        response = call_api(token, "search_papers", **params)
        papers = response.get("results", [])
        total_count = response.get("total_count", len(papers))
        total_pages = response.get("total_pages") or max(1, ceil(total_count / params["page_size"]))

    st.session_state["semantic_enrichment"] = enrichment
    st.session_state["papers"] = papers
    st.session_state["has_searched"] = True
    st.session_state["search_kind"] = search_kind
    st.session_state["search_params"] = params
    st.session_state["total_count"] = total_count
    st.session_state["total_pages"] = total_pages
    st.session_state["selected_paper"] = None
    st.session_state["paper_text"] = ""
    st.session_state["content_next_offset"] = None
    if not preserve_selection:
        for key in tuple(st.session_state):
            if key.startswith("export_item_"):
                del st.session_state[key]
        st.session_state["export_selection"] = {}
    if not preserve_selection:
        try:
            record_search(search_kind, params, total_count)
        except Exception:
            st.warning("检索已完成，但本地历史记录未能保存。")


def _run_stored_search(token: str | None, search: dict[str, Any]) -> None:
    if not token:
        st.warning("请先在左侧输入 Sciverse API Token。")
        return
    try:
        with st.spinner("正在运行保存的检索…"):
            params = dict(search["params"])
            if search["mode"] == "structured":
                params["page"] = 1
            _execute_search(token, search["mode"], params)
        st.rerun()
    except Exception as error:
        _show_error(error)


with st.sidebar:
    st.markdown("## 连接 Sciverse")
    st.caption("输入在 Sciverse 控制台申请的 API Token。Token 只用于当前浏览器会话。")
    entered_token = st.text_input("API Token", type="password", placeholder="sci_...")
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
    try:
        saved_searches = list_saved_searches()
        recent_searches = list_recent_searches(limit=5)
    except Exception:
        saved_searches = []
        recent_searches = []
        st.warning("本地检索记录暂不可用；检索功能仍可继续使用。")
    if saved_searches:
        st.markdown("#### 已保存的检索")
        selected_saved_search = st.selectbox(
            "选择已保存检索",
            saved_searches,
            format_func=lambda item: item["name"],
            key="saved_search_choice",
        )
        saved_actions = st.columns(2)
        with saved_actions[0]:
            if st.button("运行", key="run_saved_search"):
                _run_stored_search(token, selected_saved_search)
        with saved_actions[1]:
            if st.button("删除", key="delete_saved_search"):
                try:
                    delete_saved_search(selected_saved_search["id"])
                    st.rerun()
                except Exception:
                    st.warning("无法删除这条保存的检索。")
    if recent_searches:
        with st.expander("最近检索"):
            for item in recent_searches:
                query = item["params"].get("query") or "结构化筛选"
                label = f"{query[:36]} · {item['result_count']} 条"
                if st.button(label, key=f"run_recent_{item['id']}"):
                    _run_stored_search(token, item)
    with st.expander("已保存检索的备份"):
        st.caption("备份文件只包含已命名保存的检索条件，不包含 Token、全文或附件。")
        try:
            backup = export_saved_searches_backup()
            st.download_button(
                "下载 JSON 备份",
                data=backup,
                file_name="sciverse-saved-searches.json",
                mime="application/json",
                key="download_search_backup",
            )
        except Exception as error:
            st.warning(f"无法生成本地备份：{error}")
        uploaded_backup = st.file_uploader(
            "恢复或合并 JSON 备份", type=["json"], key="upload_search_backup"
        )
        confirm_restore = st.checkbox(
            "确认将备份中的检索条件合并到本机", key="confirm_search_restore"
        )
        if st.button("合并备份", disabled=not uploaded_backup or not confirm_restore):
            try:
                count = restore_saved_searches_backup(uploaded_backup.getvalue())
                st.success(f"已导入 {count} 条新检索条件；重复记录会跳过。")
                st.rerun()
            except Exception as error:
                st.error(f"无法恢复备份：{error}")
    with st.expander("高级过滤字段目录"):
        if st.button("加载可用字段", disabled=not token, key="load_catalog"):
            try:
                st.session_state["catalog"] = call_api(
                    token, "list_catalog", include_sample_values=True
                )
            except Exception as error:
                _show_error(error)
        catalog = st.session_state.get("catalog")
        if catalog:
            fields = [entry for entry in catalog.get("fields", []) if entry.get("filterable")]
            st.caption(f"可过滤字段：{len(fields)} 个。选择字段可在检索表单中添加一个高级条件。")
            st.dataframe(
                [
                    {
                        "字段": entry.get("name"),
                        "类型": entry.get("type"),
                        "说明": entry.get("description", ""),
                    }
                    for entry in fields
                ],
                hide_index=True,
                use_container_width=True,
            )


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
advanced_filter_spec = None
advanced_filter_error = None

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
            st.caption(
                "语义检索会理解问题含义，但不能可靠排除书籍章节等类型；"
                "只要完整论文请使用“条件筛选”。"
            )
        search_clicked = st.form_submit_button(
            "查找相关文献", type="primary", use_container_width=True
        )
        search_kind = "semantic"
    else:
        query = st.text_input("关键词或题名（可选）", placeholder="例如：climate change adaptation")
        author = st.text_input(
            "作者姓名（可选，多个用逗号分隔）", placeholder="例如：Hinton, LeCun"
        )
        cols = st.columns(2)
        with cols[0]:
            year_from = st.number_input("起始年份", min_value=0, max_value=2100, value=0, step=1)
        with cols[1]:
            year_to = st.number_input("结束年份", min_value=0, max_value=2100, value=0, step=1)
        cols = st.columns(2)
        with cols[0]:
            journals = st.text_input("期刊/会议（可选，多个用逗号分隔）")
        with cols[1]:
            subjects = st.text_input("学科主题（可选，多个用逗号分隔）")
        oa_only = st.checkbox(
            "仅开放获取（OA）",
            help="按 access_is_oa=true 过滤，更可能找到可读取全文的论文；是否可读仍以实际读取结果为准。",
        )
        options = st.columns(3)
        with options[0]:
            sort_label = st.selectbox(
                "排序",
                ["Sciverse 默认排序", "最新发表", "最早发表", "不按年份排序"],
            )
        with options[1]:
            page_size = st.selectbox("每页结果", [10, 20, 50], index=1)
        with options[2]:
            doc_type_label = st.selectbox(
                "文献类型",
                ["论文（期刊/会议/预印本）", "期刊论文", "会议论文", "预印本", "全部类型"],
                help="默认排除书籍章节、社论、勘误等非论文类型。",
            )
        if query.strip() and sort_label in {"最新发表", "最早发表"}:
            st.caption("按年份排序会替代相关度排序；若要优先相关结果，请选择 Sciverse 默认排序。")

        catalog = st.session_state.get("catalog") or {}
        catalog_fields = [entry for entry in catalog.get("fields", []) if entry.get("filterable")]
        if catalog_fields:
            st.markdown("#### 高级字段过滤")
            field_names = ["不添加"] + [entry["name"] for entry in catalog_fields]
            field_name = st.selectbox("目录字段", field_names)
            operator_names = catalog.get("filter_operators") or ["EQ", "IN", "CONTAINS"]
            operators = [
                operator if operator.startswith("FILTER_OP_") else f"FILTER_OP_{operator}"
                for operator in operator_names
            ]
            operator_labels = {
                "FILTER_OP_EQ": "等于",
                "FILTER_OP_NE": "不等于",
                "FILTER_OP_IN": "属于任一值",
                "FILTER_OP_CONTAINS": "包含",
                "FILTER_OP_GTE": "大于或等于",
                "FILTER_OP_LTE": "小于或等于",
            }
            operator = st.selectbox(
                "匹配方式", operators, format_func=lambda item: operator_labels.get(item, item)
            )
            filter_value = st.text_input(
                "过滤值",
                placeholder="多个值用逗号分隔" if operator.endswith("_IN") else "填写一个值",
            )
            if field_name != "不添加":
                field_info = next(entry for entry in catalog_fields if entry["name"] == field_name)
                sample_values = field_info.get("sample_values") or []
                if sample_values:
                    st.caption("常见取值：" + "、".join(str(value) for value in sample_values[:6]))
                field_operators = field_info.get("operators") or operator_names
                field_operators = {
                    item if item.startswith("FILTER_OP_") else f"FILTER_OP_{item}"
                    for item in field_operators
                }
                if operator not in field_operators:
                    advanced_filter_error = "该字段不支持所选的匹配方式。"
                advanced_filter_spec = (
                    field_name,
                    field_info.get("type", "String"),
                    operator,
                    filter_value,
                )
        search_clicked = st.form_submit_button("筛选论文", type="primary", use_container_width=True)
        search_kind = "structured"

if search_clicked:
    if not token:
        st.warning("请先在左侧输入 Sciverse API Token。")
    elif mode_label == "智能语义检索" and not query.strip():
        st.warning("请先描述你想研究的问题。")
    elif mode_label == "条件筛选" and not (
        query.strip()
        or author.strip()
        or journals.strip()
        or subjects.strip()
        or year_from
        or year_to
        or (advanced_filter_spec and advanced_filter_spec[3].strip())
    ):
        st.warning("请至少填写一个关键词、作者或年份条件。")
    elif mode_label == "条件筛选" and year_from and year_to and year_from > year_to:
        st.warning("起始年份不能晚于结束年份。")
    elif (
        mode_label == "条件筛选"
        and advanced_filter_spec
        and advanced_filter_spec[3].strip()
        and advanced_filter_error
    ):
        st.warning(advanced_filter_error)
    else:
        try:
            with st.spinner("正在 Sciverse 学术文献库中检索…"):
                if mode_label == "智能语义检索":
                    quality = {"平衡（推荐）": "balanced", "更快": "fast", "更深入": "quality"}[
                        quality_label
                    ]
                    params = {"query": query.strip(), "top_k": top_k, "mode": quality}
                    search_kind = "semantic"
                else:
                    sort_by_year = {
                        "Sciverse 默认排序": "auto",
                        "最新发表": "desc",
                        "最早发表": "asc",
                        "不按年份排序": "none",
                    }[sort_label]
                    params = {"page": 1, "page_size": page_size, "sort_by_year": sort_by_year}
                    if query.strip():
                        params["query"] = query.strip()
                    if author.strip():
                        params["authors"] = split_filter_values(author)
                    if year_from:
                        params["year_from"] = year_from
                    if year_to:
                        params["year_to"] = year_to
                    if journals.strip():
                        params["journals"] = split_filter_values(journals)
                    if subjects.strip():
                        params["subjects"] = split_filter_values(subjects)
                    doc_types = {
                        "论文（期刊/会议/预印本）": ["article", "review", "conference", "preprint"],
                        "期刊论文": ["article", "review"],
                        "会议论文": ["conference"],
                        "预印本": ["preprint"],
                        "全部类型": [],
                    }[doc_type_label]
                    filters_advanced = []
                    if doc_types:
                        filters_advanced.append(
                            {
                                "field": "type",
                                "operator": "FILTER_OP_IN",
                                "value": doc_types,
                            }
                        )
                    if oa_only:
                        filters_advanced.append(
                            {
                                "field": "access_is_oa",
                                "operator": "FILTER_OP_EQ",
                                "value": "true",
                            }
                        )
                    if advanced_filter_spec and advanced_filter_spec[3].strip():
                        field_name, field_type, operator, raw_value = advanced_filter_spec
                        filters_advanced.append(
                            {
                                "field": field_name,
                                "operator": operator,
                                "value": parse_catalog_value(raw_value, field_type, operator),
                            }
                        )
                    if filters_advanced:
                        params["filters_advanced"] = filters_advanced
                    search_kind = "structured"
                _execute_search(token, search_kind, params)
                st.rerun()
        except ValueError as error:
            st.warning(f"筛选值格式不正确：{error}")
        except Exception as error:
            _show_error(error)


papers = st.session_state.get("papers", [])
if papers:
    st.divider()
    st.markdown('<div class="section-label">02 / 检索结果</div>', unsafe_allow_html=True)
    total_count = st.session_state.get("total_count", len(papers))
    st.subheader(f"找到 {total_count} 条相关文献")
    st.caption("选择一篇文献可查看摘要、标识信息和可访问的原文片段。")
    enrichment = st.session_state.get("semantic_enrichment")
    if st.session_state.get("search_kind") == "semantic" and enrichment:
        if enrichment.get("error"):
            st.warning(
                "语义检索已完成，但补全 unique_id 失败；"
                "这些结果暂时不能用于引用网络。可改用条件筛选。"
            )
        elif enrichment["matched"] < enrichment["total"]:
            st.warning(
                f"已补全 {enrichment['matched']}/{enrichment['total']} 条 unique_id；"
                "未匹配的结果不能用于引用网络。"
            )
    selected_now = st.session_state.get("selected_paper")
    if selected_now:
        st.info(
            f"当前查看：{selected_now.get('title') or '（无标题）'}。"
            "文献详情和“读取论文原文”在页面下方“03 / 文献阅读”。"
        )
    if st.session_state.get("search_kind") == "structured":
        search_params = st.session_state.get("search_params", {})
        current_page = search_params.get("page", 1)
        total_pages = st.session_state.get("total_pages", 1)
        st.caption(f"第 {current_page} / {total_pages} 页 · 本页 {len(papers)} 篇")
        if total_pages > 1:
            previous_col, _, next_col = st.columns([1, 4, 1])
            with previous_col:
                if st.button("上一页", disabled=current_page <= 1, key="previous_page"):
                    try:
                        with st.spinner("正在读取上一页…"):
                            _execute_search(
                                token,
                                "structured",
                                {**search_params, "page": current_page - 1},
                                preserve_selection=True,
                            )
                        st.rerun()
                    except Exception as error:
                        _show_error(error)
            with next_col:
                if st.button("下一页", disabled=current_page >= total_pages, key="next_page"):
                    try:
                        with st.spinner("正在读取下一页…"):
                            _execute_search(
                                token,
                                "structured",
                                {**search_params, "page": current_page + 1},
                                preserve_selection=True,
                            )
                        st.rerun()
                    except Exception as error:
                        _show_error(error)

    export_selection = st.session_state.setdefault("export_selection", {})
    for index, paper in enumerate(papers):
        identity = _paper_identity(paper, index)
        if st.session_state.get(f"export_item_{identity}"):
            export_selection[identity] = paper
        else:
            export_selection.pop(identity, None)

    selected_for_export = list(export_selection.values())
    export_papers = selected_for_export or papers
    if selected_for_export:
        st.caption(f"已选择 {len(selected_for_export)} 篇文献导出")
    else:
        st.caption("尚未勾选文献；导出按钮将导出当前页全部结果。")
    export_columns = st.columns(4)
    with export_columns[0]:
        st.download_button(
            "CSV",
            data=export_results_csv(export_papers),
            file_name="sciverse-literature.csv",
            mime="text/csv",
            key="download_csv",
            use_container_width=True,
        )
    with export_columns[1]:
        st.download_button(
            "RIS（Zotero）",
            data=export_ris(export_papers),
            file_name="sciverse-literature.ris",
            mime="application/x-research-info-systems",
            key="download_ris",
            use_container_width=True,
        )
    with export_columns[2]:
        st.download_button(
            "BibTeX",
            data=export_bibtex(export_papers),
            file_name="sciverse-literature.bib",
            mime="application/x-bibtex",
            key="download_bibtex",
            use_container_width=True,
        )
    with export_columns[3]:
        st.download_button(
            "Markdown",
            data=export_markdown(export_papers),
            file_name="sciverse-literature.md",
            mime="text/markdown",
            key="download_markdown",
            use_container_width=True,
        )

    for index, paper in enumerate(papers):
        title = paper.get("title") or "（暂无标题）"
        authors = format_authors(paper)
        year = paper.get("publication_published_year")
        venue = paper.get("publication_venue_name_unified")
        identity = _paper_identity(paper, index)
        export_key = f"export_item_{identity}"
        with st.container(border=True):
            title_col, score_col = st.columns([5, 1])
            with title_col:
                st.markdown(f"### {title}")
            with score_col:
                if (
                    st.session_state.get("search_kind") == "semantic"
                    and paper.get("score") is not None
                ):
                    st.metric("相关度", f"{paper['score']:.2f}")
            metadata = " · ".join(str(value) for value in (authors, year, venue) if value)
            safe_metadata = html.escape(metadata or "出版信息未提供")
            st.markdown(f'<div class="result-meta">{safe_metadata}</div>', unsafe_allow_html=True)
            abstract = paper.get("abstract") or paper.get("chunk")
            if abstract:
                st.write(abstract[:700] + ("…" if len(abstract) > 700 else ""))
            unique_id = paper.get("unique_id")
            if unique_id:
                st.caption(f"unique_id（引用网络可用）：`{unique_id}`")
            else:
                st.caption("此结果没有 unique_id；语义检索结果通常不带该字段，可改用条件筛选。")
            st.checkbox("加入导出清单", key=export_key)
            if st.button("查看文献", key=f"select_{index}"):
                st.session_state["selected_paper"] = paper
                st.session_state["paper_text"] = ""
                st.session_state["content_next_offset"] = None
                st.session_state["paper_relations"] = None
                st.toast("已选择文献，详情在下方“03 / 文献阅读”。")


selected = st.session_state.get("selected_paper")
if selected:
    st.divider()
    st.markdown('<div class="section-label">03 / 文献阅读</div>', unsafe_allow_html=True)
    st.subheader(selected.get("title") or "文献详情")
    doi = selected.get("doi")
    if doi:
        st.markdown(f"DOI: [{doi}](https://doi.org/{doi})")
    st.caption(
        f"unique_id（引用网络）：`{selected.get('unique_id') or '无'}` · "
        f"doc_id（全文读取）：`{selected.get('doc_id') or '无'}`"
    )
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
        offset = (
            selected.get("offset", 0) if st.session_state.get("search_kind") == "semantic" else 0
        )
        if st.button("读取论文原文", type="primary", key="read_content"):
            try:
                with st.spinner("正在读取论文内容…"):
                    content = call_api(
                        token, "read_content", doc_id=doc_id, offset=offset, limit=6000
                    )
                st.session_state["paper_text"] = content.get("text", "")
                st.session_state["content_next_offset"] = (
                    content.get("next_offset") if content.get("more") else None
                )
            except Exception as error:
                _show_error(error)

        if st.session_state.get("paper_text"):
            st.markdown("#### 原文")
            _display_content(st.session_state["paper_text"], token)
            next_offset = st.session_state.get("content_next_offset")
            if next_offset is not None and st.button("继续阅读下一段", key="read_more"):
                try:
                    with st.spinner("正在读取下一段…"):
                        content = call_api(
                            token, "read_content", doc_id=doc_id, offset=next_offset, limit=6000
                        )
                    st.session_state["paper_text"] += content.get("text", "")
                    st.session_state["content_next_offset"] = (
                        content.get("next_offset") if content.get("more") else None
                    )
                    st.rerun()
                except Exception as error:
                    _show_error(error)

    unique_id = selected.get("unique_id")
    with st.expander("引用、参考文献与相关工作"):
        if not unique_id:
            st.info("该检索结果没有论文关联 ID，暂时无法查询引用关系。")
        else:
            relation_labels = {
                "参考文献（本文引用）": "REFERENCES",
                "被引论文（引用本文）": "CITATIONS",
                "相关工作": "RELATED_WORKS",
            }
            with st.form("relations_form", border=False):
                relation_label = st.selectbox("关系类型", list(relation_labels))
                relation_page = st.number_input(
                    "关系页码", min_value=1, max_value=400, value=1, step=1
                )
                load_relations = st.form_submit_button("加载关系列表")
            if load_relations:
                try:
                    with st.spinner("正在加载论文关系…"):
                        relation_result = call_api(
                            token,
                            "list_paper_relations",
                            unique_id=unique_id,
                            relation=relation_labels[relation_label],
                            page=relation_page,
                            page_size=25,
                        )
                    st.session_state["paper_relations"] = {
                        "unique_id": unique_id,
                        "relation": relation_labels[relation_label],
                        "response": relation_result,
                    }
                except Exception as error:
                    _show_error(error)

            relation_state = st.session_state.get("paper_relations")
            if relation_state and relation_state["unique_id"] == unique_id:
                relation_result = relation_state["response"]
                items = relation_result.get("items", [])
                total_count = relation_result.get("total_count", len(items))
                page = relation_result.get("page", 1)
                page_size = relation_result.get("page_size", 25)
                total_pages = relation_result.get("total_pages") or max(
                    1, ceil(total_count / page_size)
                )
                st.caption(f"{total_count} 条关系 · 第 {page} / {total_pages} 页")
                if items:
                    for item in items:
                        st.markdown(f"**{item.get('title') or '（暂无标题）'}**")
                        identifier = item.get("id")
                        identifier_type = item.get("id_type")
                        if identifier:
                            st.caption(
                                " · ".join(
                                    str(value) for value in (identifier_type, identifier) if value
                                )
                            )
                else:
                    st.info("这一页没有关系记录。")

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

if st.session_state.get("has_searched") and st.session_state.get("search_params"):
    with st.expander("保存本次检索条件"):
        saved_name = st.text_input(
            "检索名称", placeholder="例如：近年气候适应研究", key="save_search_name"
        )
        if st.button("保存检索", key="save_search"):
            try:
                params = dict(st.session_state["search_params"])
                if st.session_state.get("search_kind") == "structured":
                    params["page"] = 1
                save_search(saved_name, st.session_state["search_kind"], params)
                st.success("检索条件已保存在本机。")
                st.rerun()
            except Exception as error:
                st.error(f"无法保存检索条件：{error}")
