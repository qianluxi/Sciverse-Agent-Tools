from __future__ import annotations

import re


SEARCH_TOTAL_COUNT_LIMIT = 10000


def total_count_is_capped(total_count: int | None) -> bool:
    return int(total_count or 0) >= SEARCH_TOTAL_COUNT_LIMIT


def format_total_count(total_count: int | None) -> str:
    count = max(0, int(total_count or 0))
    if total_count_is_capped(count):
        return f"{count:,}（上限）"
    return f"{count:,}"


def split_filter_values(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,，;；]", value) if part.strip()]


def _parse_scalar(value: str, field_type: str) -> str | int | float | bool:
    value = value.strip()
    if "integer" in field_type.lower():
        return int(value)
    if "float" in field_type.lower():
        return float(value)
    if "boolean" in field_type.lower():
        normalized = value.casefold()
        if normalized in {"true", "是", "1"}:
            return True
        if normalized in {"false", "否", "0"}:
            return False
        raise ValueError("布尔类型请填写 true/false 或 是/否。")
    if not value:
        raise ValueError("请填写过滤值。")
    return value


def parse_catalog_value(
    value: str, field_type: str, operator: str
) -> str | int | float | bool | list[str | int | float | bool]:
    if operator.endswith("_IN"):
        values = split_filter_values(value)
        if not values:
            raise ValueError("请填写至少一个过滤值。")
        return [_parse_scalar(item, field_type) for item in values]
    return _parse_scalar(value, field_type)
