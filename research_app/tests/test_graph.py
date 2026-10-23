import csv
import io

from research_app.graph import (
    build_network,
    can_expand,
    export_edges_csv,
    export_graphml,
    filter_network,
    render_dot,
)


def _center():
    return {"id": "paper:center", "title": "Center", "year": 2020, "venue": "Nature"}


def test_relation_direction_and_duplicate_edges():
    items = [
        {"id": "10.1/a", "id_type": "doi", "title": "A"},
        {"id": "10.1/a", "id_type": "doi", "title": "A duplicate"},
    ]

    references = build_network(_center(), "REFERENCES", items)
    citations = build_network(_center(), "CITATIONS", items)

    assert references["edges"][0]["source"] == "paper:center"
    assert references["edges"][0]["target"] == "10.1/a"
    assert citations["edges"][0]["source"] == "10.1/a"
    assert citations["edges"][0]["target"] == "paper:center"
    assert len(references["edges"]) == 1
    assert any("重复边" in warning for warning in references["warnings"])


def test_self_loop_is_ignored_and_missing_metadata_is_kept():
    network = build_network(
        _center(),
        "RELATED_WORKS",
        [
            {"id": "paper:center", "title": "Center"},
            {"id": "unknown-node", "title": "Unknown metadata"},
        ],
    )

    assert len(network["nodes"]) == 2
    assert any("循环边" in warning for warning in network["warnings"])
    unknown = next(node for node in network["nodes"] if node["id"] == "unknown-node")
    assert unknown["year"] is None
    assert unknown["metadata_source"] == "relation"


def test_filters_keep_center_and_drop_unknown_metadata():
    network = build_network(
        _center(),
        "REFERENCES",
        [
            {"id": "known", "title": "Known"},
            {"id": "unknown", "title": "Unknown"},
        ],
    )
    for node in network["nodes"]:
        if node["id"] == "known":
            node.update({"year": 2021, "venue": "Science", "citation_count": 12})

    filtered, dropped = filter_network(
        network["nodes"], network["edges"], year_from=2020, venue="Science", min_citations=10
    )

    assert dropped == 1
    assert {node["id"] for node in filtered["nodes"]} == {"paper:center", "known"}


def test_hundred_node_limit_truncates_with_warning():
    items = [{"id": f"node-{index}", "title": f"Node {index}"} for index in range(120)]

    network = build_network(_center(), "REFERENCES", items, max_nodes=100)

    assert len(network["nodes"]) == 100
    assert any("截断" in warning for warning in network["warnings"])


def test_graph_exports_escape_values_and_include_edges():
    network = build_network(
        _center(),
        "REFERENCES",
        [{"id": "10.1/a", "title": "A & B"}],
    )

    graphml = export_graphml(network["nodes"], network["edges"]).decode("utf-8")
    edge_csv = export_edges_csv(network["edges"]).decode("utf-8-sig")
    dot = render_dot(network["nodes"], network["edges"])

    assert "A &amp; B" in graphml
    assert '<graph id="citation-network"' in graphml
    assert next(csv.DictReader(io.StringIO(edge_csv)))["source"] == "paper:center"
    assert "digraph citation_network" in dot


def test_only_sciverse_ids_are_expandable():
    assert can_expand({"id": "paper:10.1/x", "id_type": "sciverse"})
    assert can_expand({"id": "paper:10.1/x", "id_type": ""})
    assert not can_expand({"id": "11319685", "id_type": "semantic_scholar"})
    assert not can_expand({"id": "10.5555/123", "id_type": "doi"})
