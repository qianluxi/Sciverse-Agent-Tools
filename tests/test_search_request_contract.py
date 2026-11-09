"""守卫：agent 可见的 search_papers 入参必须覆盖深翻页、字段投影与聚合。

历史上 `openapi.yaml` 的 `SearchPapersRequest` 漏了 cursor / fields / facets，
于是 openai-tools.json、anthropic-tools.json 和 MCP 生成的 schema 里全都没有，
agent 无法深翻页（page*page_size > 10000 后只能靠 cursor），也无法做字段投影。
"""
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
EXPECTED = {"cursor", "fields", "facets"}


def test_search_papers_request_schema_exposes_pagination_and_projection():
    spec = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    props = spec["components"]["schemas"]["SearchPapersRequest"]["properties"]
    missing = EXPECTED - props.keys()
    assert not missing, f"SearchPapersRequest 缺少 {sorted(missing)}"


def test_generated_openai_tools_include_search_pagination_params():
    data = json.loads((ROOT / "dist" / "openai-tools.json").read_text(encoding="utf-8"))
    tool = next(t for t in data["tools"] if t["function"]["name"] == "search_papers")
    props = tool["function"]["parameters"]["properties"]
    missing = EXPECTED - props.keys()
    assert not missing, f"派生的 openai tool schema 缺少 {sorted(missing)}"


def test_generated_anthropic_tools_include_search_pagination_params():
    data = json.loads((ROOT / "dist" / "anthropic-tools.json").read_text(encoding="utf-8"))
    tool = next(t for t in data["tools"] if t["name"] == "search_papers")
    props = tool["input_schema"]["properties"]
    missing = EXPECTED - props.keys()
    assert not missing, f"派生的 anthropic tool schema 缺少 {sorted(missing)}"
