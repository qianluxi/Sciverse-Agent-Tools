from __future__ import annotations

import csv
from typing import Any

_DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_csv_value(value: Any) -> Any:
    if isinstance(value, str) and value.lstrip().startswith(_DANGEROUS_PREFIXES):
        return "'" + value
    return value


def safe_csv_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: safe_csv_value(value) for key, value in row.items()}


def write_safe_rows(
    writer: csv.DictWriter,
    rows: list[dict[str, Any]],
) -> None:
    for row in rows:
        writer.writerow(safe_csv_row(row))
