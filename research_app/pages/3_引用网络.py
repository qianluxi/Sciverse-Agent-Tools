from __future__ import annotations

from typing import Any

import httpx
import streamlit as st
from sciverse.credentials import resolve_token

from research_app import storage
from research_app.graph import (
    RELATION_LABELS,
    build_network,
    can_expand,
    export_edges_csv,
    export_graphml,
    filter_network,
    render_dot,
)
from research_app.project_ui import project_selector
from research_app.screening import normalize_doi
from research_app.service import call_api


st.set_page_config(page_title="引用网络 · Sciverse", page_icon="S", layout="wide")

st.title("引用网络")
st.caption("围绕一篇论文按需浏览引用、被引和相关工作；缺失元数据会明确标记，不静默补齐。")

with st.sidebar:
    entered_token = st.text_input("API Token", type="password", placeholder="sci_...")
    token = entered_token.strip() or resolve_token()
    if not token:
        st.info("请先输入 Sciverse API Token。")
    project_id = project_selector("graph_project_selector")


def _center_from_paper(paper: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": paper.get("unique_id") or paper.get("doc_id"),
        "title": paper.get("title"),
        "year": paper.get("publication_published_year"),
        "venue": paper.get("publication_venue_name_unified"),
        "citation_count": paper.get("citation_count"),
        "id_type": "unique_id",
    }


def _load_relations(
    token: str,
    unique_id: str,
    relation: str,
    page: int,
    page_size: int,
) -> list[dict[str, Any]]:
    result = call_api(
        token,
        "list_paper_relations",
        unique_id=unique_id,
        relation=relation,
        page=page,
        page_size=page_size,
    )
    return result.get("items", [])


def _show_error(error: Exception) -> None:
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 404:
        st.warning(
            "未找到该节点的关系记录。关系接口只接受 Sciverse 的 unique_id"
            "（通常以 paper: 开头）；Semantic Scholar 数字 ID 不能直接展开。"
        )
        return
    st.error(str(error))


def _enrich_nodes(token: str, nodes: list[dict[str, Any]]) -> tuple[int, list[str]]:
    groups: dict[str, list[str]] = {"doi": [], "unique_id": []}
    for node in nodes:
        if node.get("metadata_source") == "api":
            continue
        node_id = node["id"]
        id_type = str(node.get("id_type") or "").casefold()
        if "doi" in id_type or node_id.startswith("10.") or "doi.org/" in node_id:
            groups["doi"].append(normalize_doi(node_id))
        elif ":" in node_id:
            groups["unique_id"].append(node_id)
    requests = 0
    errors: list[str] = []
    metadata: dict[str, dict[str, Any]] = {}
    for field, values in groups.items():
        for start in range(0, len(values), 50):
            chunk = values[start : start + 50]
            requests += 1
            try:
                result = call_api(
                    token,
                    "search_papers",
                    filters_advanced=[{"field": field, "operator": "FILTER_OP_IN", "value": chunk}],
                    page=1,
                    page_size=50,
                )
            except Exception as error:
                errors.append(f"{field} 元数据补全失败：{error}")
                continue
            for paper in result.get("results", []):
                doi = normalize_doi(paper.get("doi"))
                unique_id = str(paper.get("unique_id") or "")
                if doi:
                    metadata[doi] = paper
                if unique_id:
                    metadata[unique_id] = paper

    for node in nodes:
        if node.get("metadata_source") == "api":
            continue
        paper = metadata.get(normalize_doi(node["id"])) or metadata.get(node["id"])
        if not paper:
            continue
        node["title"] = paper.get("title") or node["title"]
        node["year"] = paper.get("publication_published_year")
        node["venue"] = paper.get("publication_venue_name_unified") or ""
        node["citation_count"] = paper.get("citation_count")
        node["metadata_source"] = "api"
    return requests, errors


def _reset_network() -> None:
    for key in (
        "network_nodes",
        "network_edges",
        "network_center",
        "network_warnings",
        "metadata_requests",
        "enrichment_errors",
    ):
        st.session_state.pop(key, None)


st.markdown("### 1. 选择中心论文")
session_papers = st.session_state.get("papers", [])
project_run = storage.latest_search_run(project_id) if project_id else None
source_options = []
if session_papers:
    source_options.append("当前会话检索结果")
if project_run:
    source_options.append("项目最近检索快照")
if not source_options:
    st.info("当前没有可用检索结果。请先在主页检索或载入项目快照。")
    st.stop()
data_source = source_options[0]
if len(source_options) > 1:
    data_source = st.radio("数据来源", source_options, horizontal=True)
if data_source == "项目最近检索快照" and project_run:
    papers = project_run["papers"]
    st.caption("当前使用项目检索快照；引用网络结果属于本页独立分析，不自动写入项目。")
else:
    papers = session_papers
    st.caption("当前使用会话检索结果；引用网络结果属于本页独立分析，不自动写入项目。")
paper_options = [paper for paper in papers if paper.get("unique_id")]
center_mode = st.radio("中心来源", ["当前检索结果", "手动输入"], horizontal=True)
center_paper: dict[str, Any] | None = None
if center_mode == "当前检索结果":
    if paper_options:
        st.caption("下拉框显示“标题 · unique_id”；只有带 unique_id 的结果会出现在这里。")
        selected_paper = st.selectbox(
            "当前检索结果",
            paper_options,
            format_func=lambda paper: (
                f"{paper.get('title') or '（无标题）'} · {paper.get('unique_id')}"
            ),
        )
        center_paper = _center_from_paper(selected_paper)
    else:
        st.info(
            "主页还没有带 unique_id 的检索结果。语义检索通常不返回 unique_id；"
            "请改用主页的“条件筛选”，结果卡片上会显示 unique_id。"
        )
else:
    manual_id = st.text_input("中心论文 unique_id", placeholder="paper:10.1000/example")
    manual_title = st.text_input("标题（可选）")
    if manual_id.strip():
        center_paper = {
            "id": manual_id.strip(),
            "title": manual_title.strip(),
            "id_type": "unique_id",
        }

relation_label = st.selectbox("首次加载的关系", list(RELATION_LABELS.values()))
relation = next(key for key, label in RELATION_LABELS.items() if label == relation_label)
st.caption(
    "某类关系返回 0 条通常表示该论文在当前 Sciverse 库中没有这类记录，"
    "不是操作失败；可依次尝试被引论文、相关工作。"
)
page_size = st.selectbox("每页关系数量", [25, 50, 100], index=1)
load_clicked = st.button("加载一层引用网络", type="primary", disabled=not token or not center_paper)

if load_clicked and token and center_paper:
    try:
        with st.spinner("正在加载关系列表…"):
            items = _load_relations(token, center_paper["id"], relation, 1, page_size)
            network = build_network(center_paper, relation, items)
            if not items:
                network["warnings"].append(f"{relation_label} 返回 0 条记录；请尝试其他关系类型。")
        _reset_network()
        st.session_state["network_nodes"] = {node["id"]: node for node in network["nodes"]}
        st.session_state["network_edges"] = network["edges"]
        st.session_state["network_center"] = center_paper
        st.session_state["network_warnings"] = network["warnings"]
        st.session_state["metadata_requests"] = 1
        st.session_state["enrichment_errors"] = []
        st.rerun()
    except Exception as error:
        _show_error(error)

if not st.session_state.get("network_nodes"):
    st.info("选择中心论文和关系类型后加载网络。后续可对任意节点继续展开一层。")
    st.stop()

nodes = st.session_state["network_nodes"]
edges = st.session_state["network_edges"]
center = st.session_state["network_center"]

st.markdown("### 2. 按需展开")
expandable_ids = [node_id for node_id, node in nodes.items() if can_expand(node)]
st.caption(
    "只有 Sciverse 的 `paper:...` unique_id 能继续展开；"
    "Semantic Scholar 数字 ID 和仅 DOI 的节点需要先补全元数据。"
)
expand_cols = st.columns([3, 2, 1, 1])
with expand_cols[0]:
    expand_id = st.selectbox(
        "从哪个节点展开",
        expandable_ids,
        format_func=lambda value: f"{nodes[value].get('title') or value} · {value}",
        disabled=not expandable_ids,
    )
with expand_cols[1]:
    expand_relation_label = st.selectbox(
        "展开关系", list(RELATION_LABELS.values()), key="expand_relation"
    )
with expand_cols[2]:
    expand_page = st.number_input("关系页码", min_value=1, max_value=400, value=1, step=1)
with expand_cols[3]:
    expand_clicked = st.button("展开", disabled=not token or not expand_id)

if not expandable_ids:
    st.info(
        "当前节点都没有可展开的 Sciverse unique_id。"
        "可回到第 1 节用中心论文展开“被引论文”，或先尝试补全元数据。"
    )

if expand_clicked and token and expand_id:
    try:
        expand_relation = next(
            key for key, label in RELATION_LABELS.items() if label == expand_relation_label
        )
        with st.spinner("正在展开节点…"):
            items = _load_relations(token, expand_id, expand_relation, int(expand_page), page_size)
            network = build_network(
                {"id": expand_id, "title": nodes[expand_id].get("title")},
                expand_relation,
                items,
                nodes=nodes,
                edges=edges,
            )
            if not items:
                network["warnings"].append(
                    f"{expand_relation_label} 在页码 {int(expand_page)} 返回 0 条记录。"
                )
        node_map = {node["id"]: node for node in network["nodes"]}
        for node_id, node in node_map.items():
            node["is_center"] = node_id == center["id"]
        st.session_state["network_nodes"] = node_map
        st.session_state["network_edges"] = network["edges"]
        st.session_state["network_warnings"] = sorted(
            set(st.session_state.get("network_warnings", []) + network["warnings"])
        )
        st.session_state["metadata_requests"] = st.session_state.get("metadata_requests", 0) + 1
        st.rerun()
    except Exception as error:
        _show_error(error)

st.markdown("### 3. 元数据与过滤")
st.caption(
    "关系接口只返回标题和标识；年份、期刊、引用量需要通过额外检索补全。"
    "未补全的节点标记为 relation，不会被当成完整元数据。"
)
metadata_cols = st.columns(3)
with metadata_cols[0]:
    if st.button("补全节点元数据", disabled=not token):
        try:
            with st.spinner("正在批量补全元数据…"):
                requests, errors = _enrich_nodes(token, list(nodes.values()))
            st.session_state["network_nodes"] = {node["id"]: node for node in nodes.values()}
            st.session_state["metadata_requests"] = (
                st.session_state.get("metadata_requests", 0) + requests
            )
            st.session_state["enrichment_errors"] = errors
            st.rerun()
        except Exception as error:
            _show_error(error)
with metadata_cols[1]:
    st.metric("元数据请求次数", st.session_state.get("metadata_requests", 0))
with metadata_cols[2]:
    st.metric("节点数", len(nodes))

for error in st.session_state.get("enrichment_errors", []):
    st.warning(error)
for warning in st.session_state.get("network_warnings", []):
    st.caption(f"注意：{warning}")

filter_cols = st.columns(4)
with filter_cols[0]:
    year_from = st.number_input("起始年份", min_value=0, max_value=2100, value=0, step=1)
with filter_cols[1]:
    year_to = st.number_input("结束年份", min_value=0, max_value=2100, value=0, step=1)
with filter_cols[2]:
    venue_filter = st.text_input("期刊包含")
with filter_cols[3]:
    min_citations = st.number_input("最低引用量", min_value=0, value=0, step=1)

filtered, dropped = filter_network(
    list(nodes.values()),
    edges,
    year_from=year_from or None,
    year_to=year_to or None,
    venue=venue_filter,
    min_citations=min_citations or None,
)
if dropped:
    st.warning(f"有 {dropped} 个节点因缺少所需元数据或不满足条件被过滤。")

st.markdown("### 4. 网络与节点详情")
st.graphviz_chart(render_dot(filtered["nodes"], filtered["edges"]), use_container_width=True)
st.caption(
    f"显示 {len(filtered['nodes'])} 个节点、{len(filtered['edges'])} 条边。"
    "实线为有向关系，虚线为相关工作。"
)

node_rows = [
    {
        "标题": node["title"],
        "ID": node["id"],
        "年份": str(node["year"]) if node.get("year") is not None else "",
        "期刊": node.get("venue") or "",
        "引用量": (str(node["citation_count"]) if node.get("citation_count") is not None else ""),
        "元数据来源": node.get("metadata_source"),
        "中心": "是" if node.get("is_center") else "",
    }
    for node in filtered["nodes"]
]
st.dataframe(node_rows, hide_index=True, use_container_width=True)

detail_id = st.selectbox(
    "查看节点详情",
    [node["id"] for node in filtered["nodes"]],
    format_func=lambda value: next(
        node["title"] for node in filtered["nodes"] if node["id"] == value
    ),
)
detail = next(node for node in filtered["nodes"] if node["id"] == detail_id)
st.json(detail)

export_cols = st.columns(2)
with export_cols[0]:
    st.download_button(
        "导出 GraphML",
        data=export_graphml(filtered["nodes"], filtered["edges"]),
        file_name="sciverse-citation-network.graphml",
        mime="application/graphml+xml",
        key="download_graphml",
    )
with export_cols[1]:
    st.download_button(
        "导出边列表 CSV",
        data=export_edges_csv(filtered["edges"]),
        file_name="sciverse-citation-edges.csv",
        mime="text/csv",
        key="download_edges",
    )
