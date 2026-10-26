from __future__ import annotations

import unicodedata
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
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(char for char in text if char.isalnum())


def strong_identifier_keys(paper: dict[str, Any]) -> list[str]:
    keys = []
    doi = normalize_doi(paper.get("doi"))
    if doi:
        keys.append(f"doi:{doi}")
    unique_id = str(paper.get("unique_id") or "").strip()
    if unique_id:
        keys.append(f"uid:{unique_id}")
    doc_id = str(paper.get("doc_id") or "").strip()
    if doc_id:
        keys.append(f"doc:{doc_id}")
    return keys


def identifier_keys(paper: dict[str, Any]) -> list[str]:
    keys = strong_identifier_keys(paper)
    title = normalize_title(paper.get("title"))
    if title:
        keys.append(f"title:{title}")
    return keys


def candidate_key(paper: dict[str, Any], index: int = 0) -> str:
    identifiers = identifier_keys(paper)
    return identifiers[0] if identifiers else f"unknown:{index}"


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
