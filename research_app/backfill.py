"""一次性回填：用原始 corpus.json 补全检索快照里的书目字段。

早期版本的 ``search_runs.snapshot_json`` 只保存 6 个字段
（title / year / doi / abstract / unique_id / doc_id），作者、期刊、被引数
全部丢失，综述章节页只能把引证标签降级成“（书目待补）”。原始检索结果
（corpus.json）里其实有完整元数据，本模块把它重新灌回快照。

两条硬约束：

* 只补空缺，不覆盖已有值——快照里已有的题名、年份、摘要可能是人工修过的；
* 重复执行幂等——没有字段需要补时不会写库。
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Iterable, Mapping

from research_app import storage
from research_app.screening import normalize_doi, normalize_title

# 只回填书目相关字段。题名、摘要、DOI、unique_id 属于检索身份，一律不动。
BIBLIOGRAPHY_FIELDS: tuple[str, ...] = (
    "author",
    "publication_venue_name_unified",
    "publication_venue_name",
    "publication_venue_type",
    "publication_venue_issn",
    "publication_published_year",
    "publication_publisher",
    "citation_count",
    "citation_normalized_percentile",
    "influential_citation_count",
    "reference_count",
    "keywords",
    "language",
    "access_oa_status",
    "is_content_accessible",
)

_CORPUS_LIST_KEYS = ("records", "papers", "items", "results", "data")


def load_corpus(path: str | Path) -> list[dict[str, Any]]:
    """读取 corpus.json，接受裸数组或 ``{"records": [...]}`` 两种形状。"""
    raw = Path(path).expanduser().read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"corpus 文件不是合法 JSON：{path}") from exc
    if isinstance(payload, dict):
        for key in _CORPUS_LIST_KEYS:
            candidate = payload.get(key)
            if isinstance(candidate, list):
                payload = candidate
                break
        else:
            raise ValueError(f"corpus 文件里找不到论文数组：{path}")
    if not isinstance(payload, list):
        raise ValueError(f"corpus 文件格式无法识别：{path}")
    return [item for item in payload if isinstance(item, dict)]


def build_corpus_index(
    records: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """建三套键（unique_id / DOI / 规范化题名）；题名冲突的键直接丢弃。"""
    index: dict[str, dict[str, Any]] = {}
    ambiguous: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            continue
        paper = dict(record)
        unique_id = str(paper.get("unique_id") or "").strip()
        if unique_id:
            index.setdefault(f"uid:{unique_id}", paper)
        doi = normalize_doi(paper.get("doi"))
        if doi:
            index.setdefault(f"doi:{doi}", paper)
        title = normalize_title(paper.get("title"))
        if not title:
            continue
        key = f"title:{title}"
        existing = index.get(key)
        if existing is not None and existing.get("unique_id") != paper.get("unique_id"):
            # 归一化题名撞车时无法确定是哪一篇，宁可不匹配也不猜。
            ambiguous.add(key)
        index.setdefault(key, paper)
    for key in ambiguous:
        index.pop(key, None)
    return index


def _lookup_keys(paper: Mapping[str, Any]) -> list[str]:
    keys: list[str] = []
    unique_id = str(paper.get("unique_id") or "").strip()
    if unique_id:
        keys.append(f"uid:{unique_id}")
    doi = normalize_doi(paper.get("doi"))
    if doi:
        keys.append(f"doi:{doi}")
    title = normalize_title(paper.get("title"))
    if title:
        keys.append(f"title:{title}")
    return keys


def find_record(
    paper: Mapping[str, Any],
    index: Mapping[str, dict[str, Any]],
) -> dict[str, Any] | None:
    for key in _lookup_keys(paper):
        record = index.get(key)
        if record is not None:
            return record
    return None


def _is_blank(value: Any) -> bool:
    """``0`` 与 ``False`` 是有效值，只有 None、空串和空容器才算缺。"""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def merge_bibliography(
    paper: dict[str, Any],
    record: Mapping[str, Any],
) -> bool:
    """把 record 的书目字段补进 paper，返回是否有改动。"""
    changed = False
    for field in BIBLIOGRAPHY_FIELDS:
        value = record.get(field)
        if _is_blank(value):
            continue
        if _is_blank(paper.get(field)):
            paper[field] = value
            changed = True
    # 快照里的年份键叫 year，corpus 里叫 publication_published_year。
    # 两个键都没有时才补 year；已经有 publication_published_year 的记录
    # 不缺年份信息，补一个重复键只会让 dry-run 把新项目误判为"待回填"。
    if _is_blank(paper.get("year")) and _is_blank(paper.get("publication_published_year")):
        year = record.get("publication_published_year")
        if not _is_blank(year):
            paper["year"] = year
            changed = True
    return changed


def backfill_bibliography(
    *,
    corpus_path: str | Path,
    project_id: str | None = None,
    database_path: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """把 corpus 的书目信息回填到快照，返回汇总报告。

    ``project_id`` 为空时对库里每个项目各跑一遍。``dry_run`` 只统计不写库。
    """
    records = load_corpus(corpus_path)
    index = build_corpus_index(records)
    if not index:
        raise ValueError("corpus 里没有可用记录，未做任何写入。")

    totals: dict[str, int] = {
        "projects": 0,
        "runs_scanned": 0,
        "runs_skipped": 0,
        "runs_updated": 0,
        "papers_scanned": 0,
        "papers_changed": 0,
        "papers_unmatched": 0,
    }
    context = (
        storage.override_database_path(database_path)
        if database_path is not None
        else nullcontext()
    )
    with context:
        project_ids = (
            [project_id]
            if project_id
            else [str(item["id"]) for item in storage.list_screening_projects()]
        )
        for current in project_ids:
            totals["projects"] += 1
            matched_box = {"unmatched": 0}

            def enrich(paper: dict[str, Any], _index=index, _box=matched_box) -> bool:
                record = find_record(paper, _index)
                if record is None:
                    _box["unmatched"] += 1
                    return False
                return merge_bibliography(paper, record)

            report = storage.enrich_search_run_snapshots(
                current,
                enrich,
                dry_run=dry_run,
            )
            for key in ("runs_scanned", "runs_skipped", "runs_updated",
                        "papers_scanned", "papers_changed"):
                totals[key] += report[key]
            totals["papers_unmatched"] += matched_box["unmatched"]

    totals["corpus_records"] = len(records)
    totals["corpus_index_keys"] = len(index)
    totals["dry_run"] = dry_run
    return totals
