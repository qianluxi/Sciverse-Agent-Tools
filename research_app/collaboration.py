from __future__ import annotations

import csv
import io
import json
from typing import Any

from research_app import storage
from research_app.csv_safe import write_safe_rows


BLIND_DECISION_COLUMNS = [
    "project_id",
    "candidate_key",
    "title",
    "reviewer_id",
    "reviewer_name",
    "reviewer_role",
    "status",
    "exclusion_reason",
    "notes",
    "revision",
    "ai_assisted",
    "ai_task_id",
    "ai_suggestion",
    "ai_confidence",
    "created_at",
    "updated_at",
]

RESOLUTION_COLUMNS = [
    "id",
    "candidate_key",
    "resolution_type",
    "final_status",
    "final_reason",
    "notes",
    "coordinator_id",
    "created_at",
    "decisions_json",
]


def _write_csv(rows: list[dict[str, Any]], columns: list[str]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    write_safe_rows(
        writer,
        [
            {column: row.get(column, "") for column in columns}
            for row in rows
        ],
    )
    return output.getvalue().encode("utf-8-sig")


def export_blind_decisions_csv(project_id: str) -> bytes:
    titles = {
        item["candidate_key"]: item.get("title") or ""
        for item in storage.list_all_screening_candidates(project_id)
    }
    rows = []
    for item in storage.list_blind_decisions(project_id):
        rows.append(
            {
                **item,
                "title": titles.get(item["candidate_key"], ""),
                "ai_assisted": "是" if item.get("ai_assisted") else "否",
            }
        )
    return _write_csv(rows, BLIND_DECISION_COLUMNS)


def export_resolutions_csv(project_id: str) -> bytes:
    rows = []
    for item in storage.list_conflict_resolutions(project_id):
        rows.append(
            {
                **item,
                "decisions_json": json.dumps(
                    item.get("decisions") or [], ensure_ascii=False
                ),
            }
        )
    return _write_csv(rows, RESOLUTION_COLUMNS)
