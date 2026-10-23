from __future__ import annotations

import csv
import html
import io
from typing import Any


RELATION_LABELS = {
    "REFERENCES": "参考文献（本文引用）",
    "CITATIONS": "被引论文（引用本文）",
    "RELATED_WORKS": "相关工作",
}


def _node(
    node_id: str,
    *,
    title: str = "",
    year: int | None = None,
    venue: str = "",
    citation_count: int | None = None,
    id_type: str = "",
    metadata_source: str = "unknown",
    is_center: bool = False,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "title": title or "（未知标题）",
        "year": year,
        "venue": venue,
        "citation_count": citation_count,
        "id_type": id_type,
        "metadata_source": metadata_source,
        "is_center": is_center,
    }


def can_expand(node: dict[str, Any]) -> bool:
    """Only Sciverse unique_ids can be passed back to list_paper_relations."""
    id_type = str(node.get("id_type") or "").casefold()
    node_id = str(node.get("id") or "")
    return id_type in {"sciverse", "unique_id"} or node_id.startswith("paper:")


def build_network(
    center: dict[str, Any],
    relation: str,
    items: list[dict[str, Any]],
    *,
    nodes: dict[str, dict[str, Any]] | None = None,
    edges: list[dict[str, Any]] | None = None,
    max_nodes: int = 100,
) -> dict[str, Any]:
    if relation not in RELATION_LABELS:
        raise ValueError("不支持的引用关系类型。")
    node_map = dict(nodes or {})
    edge_list = list(edges or [])
    warnings: list[str] = []
    center_id = str(center.get("id") or center.get("unique_id") or "")
    if not center_id:
        raise ValueError("中心论文缺少稳定 ID。")
    if center_id not in node_map:
        node_map[center_id] = _node(
            center_id,
            title=str(center.get("title") or ""),
            year=center.get("year"),
            venue=str(center.get("venue") or ""),
            citation_count=center.get("citation_count"),
            id_type=str(center.get("id_type") or "unique_id"),
            metadata_source="api",
            is_center=True,
        )
    else:
        node_map[center_id]["is_center"] = True

    existing_edges = {(edge["source"], edge["target"], edge["relation"]) for edge in edge_list}
    for item in items:
        node_id = str(item.get("id") or "").strip()
        if not node_id:
            continue
        if node_id == center_id:
            warnings.append("已忽略指向中心论文自身的循环边。")
            continue
        if node_id not in node_map and len(node_map) >= max_nodes:
            warnings.append(f"节点超过 {max_nodes} 个，其余节点已截断。")
            break
        if node_id not in node_map:
            node_map[node_id] = _node(
                node_id,
                title=str(item.get("title") or ""),
                id_type=str(item.get("id_type") or ""),
                metadata_source="relation",
            )
        if relation == "REFERENCES":
            source, target, directed = center_id, node_id, True
        elif relation == "CITATIONS":
            source, target, directed = node_id, center_id, True
        else:
            source, target, directed = center_id, node_id, False
        edge_key = (source, target, relation)
        if edge_key in existing_edges:
            warnings.append("已忽略重复边。")
            continue
        existing_edges.add(edge_key)
        edge_list.append(
            {
                "source": source,
                "target": target,
                "relation": relation,
                "directed": directed,
            }
        )
    return {
        "nodes": list(node_map.values()),
        "edges": edge_list,
        "warnings": sorted(set(warnings)),
    }


def filter_network(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    year_from: int | None = None,
    year_to: int | None = None,
    venue: str = "",
    min_citations: int | None = None,
) -> tuple[dict[str, Any], int]:
    year_active = year_from is not None or year_to is not None
    venue_active = bool(venue.strip())
    citations_active = min_citations is not None and min_citations > 0
    if not (year_active or venue_active or citations_active):
        return {"nodes": nodes, "edges": edges}, 0

    kept: list[dict[str, Any]] = []
    missing_metadata = 0
    for node in nodes:
        if node.get("is_center"):
            kept.append(node)
            continue
        include = True
        if year_active:
            year = node.get("year")
            if year is None:
                include = False
            else:
                if year_from is not None and year < year_from:
                    include = False
                if year_to is not None and year > year_to:
                    include = False
        if venue_active:
            node_venue = str(node.get("venue") or "").casefold()
            if not node_venue:
                include = False
            elif venue.strip().casefold() not in node_venue:
                include = False
        if citations_active:
            citations = node.get("citation_count")
            if citations is None or citations < min_citations:
                include = False
        if include:
            kept.append(node)
        else:
            missing_metadata += 1
    kept_ids = {node["id"] for node in kept}
    filtered_edges = [
        edge for edge in edges if edge["source"] in kept_ids and edge["target"] in kept_ids
    ]
    return {"nodes": kept, "edges": filtered_edges}, missing_metadata


def export_edges_csv(edges: list[dict[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=["source", "target", "relation", "directed"])
    writer.writeheader()
    writer.writerows(edges)
    return output.getvalue().encode("utf-8-sig")


def export_graphml(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> bytes:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">',
        '  <key id="title" for="node" attr.name="title" attr.type="string"/>',
        '  <key id="year" for="node" attr.name="year" attr.type="int"/>',
        '  <key id="venue" for="node" attr.name="venue" attr.type="string"/>',
        '  <key id="metadata_source" for="node" attr.name="metadata_source" attr.type="string"/>',
        '  <key id="relation" for="edge" attr.name="relation" attr.type="string"/>',
        '  <graph id="citation-network" edgedefault="directed">',
    ]
    for node in nodes:
        node_id = html.escape(str(node["id"]), quote=True)
        lines.append(f'    <node id="{node_id}">')
        for key in ("title", "venue", "metadata_source"):
            value = node.get(key)
            if value not in (None, ""):
                lines.append(f'      <data key="{key}">{html.escape(str(value))}</data>')
        if node.get("year") is not None:
            lines.append(f'      <data key="year">{int(node["year"])}</data>')
        lines.append("    </node>")
    for index, edge in enumerate(edges, start=1):
        lines.append(
            f'    <edge id="e{index}" source="{html.escape(str(edge["source"]), quote=True)}" '
            f'target="{html.escape(str(edge["target"]), quote=True)}">'
        )
        lines.append(f'      <data key="relation">{html.escape(str(edge["relation"]))}</data>')
        lines.append("    </edge>")
    lines.extend(["  </graph>", "</graphml>"])
    return ("\n".join(lines) + "\n").encode("utf-8")


def render_dot(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> str:
    def esc(value: Any) -> str:
        return str(value).replace("\\", "\\\\").replace('"', '\\"')

    lines = [
        "digraph citation_network {",
        '  rankdir="LR";',
        '  node [shape="box", style="rounded"];',
    ]
    for node in nodes:
        label = node.get("title") or node["id"]
        if node.get("year"):
            label = f"{label}\\n{node['year']}"
        color = "lightblue" if node.get("is_center") else "white"
        lines.append(
            f'  "{esc(node["id"])}" [label="{esc(label)}", fillcolor="{color}", style="filled,rounded"];'
        )
    for edge in edges:
        style = "solid" if edge.get("directed") else "dashed"
        lines.append(f'  "{esc(edge["source"])}" -> "{esc(edge["target"])}" [style="{style}"];')
    lines.append("}")
    return "\n".join(lines)
