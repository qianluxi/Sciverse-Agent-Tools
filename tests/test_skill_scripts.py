"""行为守卫：脚本式 skill 必须自己翻译便捷参数、自己补分页默认值。

踩过的两个坑：

1. ClawHub 的 `search_papers.mjs` 把入参原样透传，`authors` / `year_from` /
   `sort_advanced` / `filters_advanced` 这些便利字段后端不认、静默丢弃——
   看起来检索成功，实际条件没生效。
2. `read_content.mjs` 不传 offset 时服务端返回整篇全文并忽略 limit，
   一次调用就可能把几十万码点塞进模型上下文。

这两类 bug 用“源码里有没有某个字符串”是查不出来的（运算符映射写错也能过），
所以这里真的把脚本跑一遍：用 `node --import` 预加载一个 fetch 桩，
捕获脚本实际发出的请求，再和 Python SDK 的翻译结果逐字段比对。

两份 skill bundle（clawhub / web）各自独立分发，无法共享文件，所以两处都要测。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIRS = (
    ROOT / "clawhub" / "scripts",
    ROOT / "generators" / "web_skill_assets" / "scripts",
)

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="需要 node 才能跑 skill 脚本")

# 预加载桩：替换 fetch，记录出站请求，进程退出时写入 CAPTURE_PATH。
_STUB = """
import fs from "node:fs";

process.env.SCIVERSE_API_TOKEN = "test-token";
let captured = null;
globalThis.fetch = async (url, init = {}) => {
  captured = { url: String(url), method: init.method ?? "GET", body: init.body ?? null };
  return {
    ok: true,
    status: 200,
    json: async () => ({}),
    text: async () => "{}",
    headers: { get: () => "application/json" },
  };
};
process.on("exit", () => {
  fs.writeFileSync(process.env.CAPTURE_PATH, JSON.stringify(captured));
});
"""


def _run_script(tmp_path: Path, scripts: Path, name: str, payload: dict) -> dict:
    """跑一个 skill 脚本，返回它实际发给后端的请求。"""
    stub = tmp_path / "fetch-stub.mjs"
    stub.write_text(_STUB, encoding="utf-8")
    capture = tmp_path / "captured.json"

    completed = subprocess.run(
        [
            NODE,
            "--import",
            stub.as_uri(),
            str(scripts / name),
            json.dumps(payload),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "CAPTURE_PATH": str(capture)},
    )
    assert captured_exists(capture), (
        f"{scripts.parent.name}/{name} 没有发出请求\n"
        f"stdout={completed.stdout}\nstderr={completed.stderr}"
    )
    data = json.loads(capture.read_text(encoding="utf-8"))
    assert data is not None, f"{scripts.parent.name}/{name} 未调用 fetch"
    return data


def captured_exists(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


def _python_reference(payload: dict) -> dict:
    """Python SDK 的翻译结果——脚本必须和它一致。"""
    client = pytest.importorskip("sciverse.client")
    return client._to_backend_payload(payload)


CONVENIENCE_PAYLOAD = {
    "authors": ["Hinton"],
    "year_from": 2020,
    "journals": ["Nature"],
    "page_size": 5,
    "fields": ["doc_id", "title"],
}


@requires_node
@pytest.mark.parametrize("scripts", SCRIPT_DIRS, ids=lambda p: p.parent.name)
def test_search_papers_script_translates_convenience_params(scripts: Path, tmp_path: Path):
    request = _run_script(tmp_path, scripts, "search_papers.mjs", CONVENIENCE_PAYLOAD)
    body = json.loads(request["body"])

    # 1) 便捷字段不能被原样透传（后端会静默忽略）
    for leaked in ("authors", "year_from", "journals"):
        assert leaked not in body, f"{scripts.parent.name} 把 {leaked} 原样透传了"

    # 2) 必须翻译成 canonical filters
    filters = body.get("filters") or []
    by_field = {(f["field"], f["operator"]): f["value"] for f in filters}
    assert by_field[("author", "FILTER_OP_IN")] == ["Hinton"]
    assert by_field[("publication_published_year", "FILTER_OP_GTE")] == 2020
    assert by_field[("publication_venue_name_unified", "FILTER_OP_IN")] == ["Nature"]

    # 3) 纯结构化查询要按年份降序（后端默认序实质乱序）
    assert body["sort"] == [
        {"field": "publication_published_year", "order": "SORT_ORDER_DESC"}
    ]


@requires_node
@pytest.mark.parametrize("scripts", SCRIPT_DIRS, ids=lambda p: p.parent.name)
def test_search_papers_script_matches_python_sdk(scripts: Path, tmp_path: Path):
    """脚本与 Python SDK 的翻译结果必须逐字段一致（三处复制、此处对账）。"""
    pytest.importorskip("sciverse.client")
    request = _run_script(tmp_path, scripts, "search_papers.mjs", CONVENIENCE_PAYLOAD)
    assert json.loads(request["body"]) == _python_reference(CONVENIENCE_PAYLOAD)


@requires_node
@pytest.mark.parametrize("scripts", SCRIPT_DIRS, ids=lambda p: p.parent.name)
def test_search_papers_script_forwards_pagination_and_boosts(scripts: Path, tmp_path: Path):
    """cursor / facets / 三个 boost 曾经只在 schema 里有，脚本和 MCP 都丢了。"""
    payload = {
        "query": "graphene",
        "cursor": "eyJvZmZzZXQiOjI1fQ==",
        "facets": {"publication_published_year": {"size": 10}},
        "freshness_boost": "MILD",
        "impact_boost": "STRONG",
        "language_affinity": "MILD",
    }
    body = json.loads(_run_script(tmp_path, scripts, "search_papers.mjs", payload)["body"])

    assert body["cursor"] == payload["cursor"]
    assert body["facets"] == payload["facets"]
    assert body["freshness_boost"] == "MILD"
    assert body["impact_boost"] == "STRONG"
    assert body["language_affinity"] == "MILD"


@requires_node
@pytest.mark.parametrize("scripts", SCRIPT_DIRS, ids=lambda p: p.parent.name)
def test_read_content_script_sets_pagination_defaults(scripts: Path, tmp_path: Path):
    request = _run_script(tmp_path, scripts, "read_content.mjs", {"doc_id": "abc"})
    query = dict(
        part.split("=", 1) for part in request["url"].split("?", 1)[1].split("&")
    )
    assert query["doc_id"] == "abc"
    assert query["offset"] == "0", "缺 offset 时服务端返回整篇全文"
    assert query["limit"] == "4096"


@requires_node
@pytest.mark.parametrize("scripts", SCRIPT_DIRS, ids=lambda p: p.parent.name)
def test_read_content_script_keeps_explicit_window(scripts: Path, tmp_path: Path):
    request = _run_script(
        tmp_path, scripts, "read_content.mjs", {"doc_id": "abc", "offset": 120, "limit": 256}
    )
    query = dict(
        part.split("=", 1) for part in request["url"].split("?", 1)[1].split("&")
    )
    assert query["offset"] == "120"
    assert query["limit"] == "256"
