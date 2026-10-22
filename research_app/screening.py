from __future__ import annotations

import re
from typing import Any


EXCLUSION_REASONS = (
    "主题不相关",
    "研究类型不符",
    "无全文",
    "重复发表",
    "数据不足",
    "其他",
)

STATUS_LABELS = {
    "pending": "待定",
    "included": "纳入",
    "excluded": "排除",
}


def normalize_doi(value: Any) -> str:
    text = str(value or "").strip().casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text


def normalize_title(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").casefold())


def candidate_key(paper: dict[str, Any], index: int = 0) -> str:
    doi = normalize_doi(paper.get("doi"))
    if doi:
        return f"doi:{doi}"
    unique_id = str(paper.get("unique_id") or "").strip()
    if unique_id:
        return f"uid:{unique_id}"
    doc_id = str(paper.get("doc_id") or "").strip()
    if doc_id:
        return f"doc:{doc_id}"
    title = normalize_title(paper.get("title"))
    if title:
        return f"title:{title}"
    return f"unknown:{index}"


def duplicate_basis(key: str) -> str:
    prefix = key.split(":", 1)[0]
    return {
        "doi": "DOI 相同",
        "uid": "unique_id 相同",
        "doc": "doc_id 相同",
        "title": "标题相同",
    }.get(prefix, "缺少稳定标识")


def paper_id(paper: dict[str, Any], key: str) -> str:
    return str(paper.get("unique_id") or paper.get("doc_id") or paper.get("doi") or key)
