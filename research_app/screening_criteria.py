from __future__ import annotations

import re
from typing import Any


def _split_criteria(text: Any) -> list[str]:
    values = []
    for part in re.split(r"[\n;；]+", str(text or "")):
        value = re.sub(r"^\s*(?:[-*•]|\d+[.)、])\s*", "", part).strip()
        if value and value not in values:
            values.append(value)
    return values


def build_project_criteria(project: dict[str, Any]) -> dict[str, Any]:
    items = []
    for index, text in enumerate(
        _split_criteria(project.get("inclusion_criteria")),
        start=1,
    ):
        items.append({"id": f"I{index}", "kind": "inclusion", "text": text})
    for index, text in enumerate(
        _split_criteria(project.get("exclusion_criteria")),
        start=1,
    ):
        items.append({"id": f"E{index}", "kind": "exclusion", "text": text})
    return {
        "question": str(project.get("question") or "").strip(),
        "items": items,
    }


def criteria_map(criteria: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {
        str(item.get("id")): item
        for item in criteria.get("items") or []
        if item.get("id")
    }


def criteria_prompt_text(criteria: dict[str, Any]) -> str:
    lines = []
    for item in criteria.get("items") or []:
        lines.append(f"{item.get('id')}: {item.get('text')}")
    return "\n".join(lines) or "未提供编号标准"
