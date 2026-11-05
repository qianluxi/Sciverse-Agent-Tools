from __future__ import annotations

import csv
import io
import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from research_app import storage
from research_app.csv_safe import write_safe_rows
from research_app.screening import (
    FULLTEXT_STATUS_LABELS,
    STATUS_LABELS,
)


PRISMA_RECORD_COLUMNS = [
    "candidate_key",
    "title",
    "year",
    "doi",
    "title_abstract_status",
    "fulltext_status",
    "fulltext_reason",
    "fulltext_notes",
    "fulltext_updated_at",
]


def _evidence_summary(project_id: str) -> dict[str, Any]:
    matrix = storage.load_evidence_matrix(project_id)
    coverage = {"ai_raw": 0, "human_corrected": 0, "final_adopted": 0}
    input_sources: Counter[str] = Counter()
    errors = 0
    quotes_total = 0
    quotes_verified = 0
    for entry in matrix:
        versions = entry.get("versions") or {}
        for source in coverage:
            if source in versions:
                coverage[source] += 1
        ai_version = versions.get("ai_raw") or {}
        input_source = str(ai_version.get("input_source") or "")
        if input_source:
            input_sources[input_source] += 1
        if ai_version.get("error"):
            errors += 1
        for check in ai_version.get("quote_checks") or []:
            quotes_total += 1
            if check.get("verified") is True:
                quotes_verified += 1
    tasks = storage.list_ai_tasks_by_kind(project_id, "evidence_matrix", limit=100)
    return {
        "papers": len(matrix),
        "coverage": coverage,
        "input_sources": dict(input_sources),
        "errors": errors,
        "quote_checks": {"total": quotes_total, "verified": quotes_verified},
        "tasks": [
            {
                "id": task["id"],
                "status": task["status"],
                "model": task["model"],
                "prompt_version": task["prompt_version"],
                "created_at": task["created_at"],
            }
            for task in tasks
        ],
    }


def build_project_report(project_id: str) -> dict[str, Any]:
    project = storage.get_screening_project(project_id)
    return {
        "format": "sciverse-project-report",
        "format_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "schema_version": storage.SCHEMA_VERSION,
        "project": {
            "id": project["id"],
            "name": project["name"],
            "question": project["question"],
            "inclusion_criteria": project["inclusion_criteria"],
            "exclusion_criteria": project["exclusion_criteria"],
        },
        "lightweight_screening": storage.screening_stats(project_id),
        "prisma": storage.prisma_counts(project_id),
        "evidence_matrix": _evidence_summary(project_id),
    }


def prisma_records(project_id: str) -> list[dict[str, Any]]:
    fulltext = {
        item["candidate_key"]: item
        for item in storage.list_fulltext_decisions(project_id)
    }
    rows = []
    for candidate in storage.list_all_screening_candidates(project_id):
        key = candidate["candidate_key"]
        decision = fulltext.get(key) or {}
        rows.append(
            {
                "candidate_key": key,
                "title": candidate.get("title") or "",
                "year": candidate.get("year") or "",
                "doi": candidate.get("doi") or "",
                "title_abstract_status": STATUS_LABELS.get(
                    candidate["status"], candidate["status"]
                ),
                "fulltext_status": FULLTEXT_STATUS_LABELS.get(
                    decision.get("status") or "not_sought",
                    decision.get("status") or "not_sought",
                ),
                "fulltext_reason": decision.get("exclusion_reason") or "",
                "fulltext_notes": decision.get("notes") or "",
                "fulltext_updated_at": decision.get("updated_at") or "",
            }
        )
    return rows


def export_prisma_records_csv(project_id: str) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=PRISMA_RECORD_COLUMNS)
    writer.writeheader()
    write_safe_rows(writer, prisma_records(project_id))
    return output.getvalue().encode("utf-8-sig")


def render_project_report_markdown(report: dict[str, Any]) -> bytes:
    project = report["project"]
    prisma = report["prisma"]
    evidence = report["evidence_matrix"]
    title_abstract = prisma["title_abstract"]
    fulltext = prisma["fulltext"]
    lines = [
        f"# 研究报告：{project['name']}",
        "",
        f"- 研究问题：{project['question']}",
        f"- 生成时间：{report['generated_at']}",
        f"- 数据模型：Schema v{report['schema_version']}",
        f"- 项目：`{project['id']}`",
        "",
        "## PRISMA 2020 计数",
        "",
        "| 阶段 | 数量 |",
        "|---|---|",
        f"| 识别记录 | {prisma['identified']} |",
        f"| 去重移除 | {prisma['duplicates_removed']} |",
        f"| 去重后记录 | {prisma['unique_records']} |",
        f"| 题名/摘要筛选：待定 | {title_abstract['pending']} |",
        f"| 题名/摘要筛选：排除 | {title_abstract['excluded']} |",
        f"| 题名/摘要筛选：纳入 | {title_abstract['included']} |",
        f"| 全文：未获取 | {fulltext['not_sought']} |",
        f"| 全文：已检索 | {fulltext['sought']} |",
        f"| 全文：无法获取 | {fulltext['not_retrieved']} |",
        f"| 全文：已评估 | {fulltext['assessed']} |",
        f"| 全文：排除 | {fulltext['excluded']} |",
        f"| 全文：纳入 | {fulltext['included']} |",
        f"| **最终纳入综述** | **{prisma['included_in_review']}** |",
        "",
    ]
    if fulltext["excluded_reasons"]:
        lines.extend(["### 全文排除原因", "", "| 原因 | 数量 |", "|---|---|"])
        for item in fulltext["excluded_reasons"]:
            lines.append(f"| {item['reason'] or '未注明'} | {item['count']} |")
        lines.append("")
    if prisma["warnings"]:
        lines.extend(["### 口径警告", ""])
        for warning in prisma["warnings"]:
            lines.append(f"- {warning}")
        lines.append("")
    lines.extend(
        [
            "## 证据矩阵",
            "",
            f"- 有证据的论文：{evidence['papers']}",
            f"- 版本覆盖：AI 原始 {evidence['coverage']['ai_raw']}、"
            f"人工校正 {evidence['coverage']['human_corrected']}、"
            f"最终采用 {evidence['coverage']['final_adopted']}",
            f"- 输入来源："
            + "、".join(
                f"{key} {value}" for key, value in evidence["input_sources"].items()
            ),
            f"- 提取失败：{evidence['errors']}",
            f"- 引句自动校验：{evidence['quote_checks']['verified']}/"
            f"{evidence['quote_checks']['total']}（仅校验引句是否存在于输入文本）",
            "",
            "## 溯源",
            "",
            f"- 检索快照：{prisma['traceability']['search_runs']} 个",
            f"- 导入事件：{len(prisma['traceability']['import_events'])} 次",
            f"- 证据矩阵任务：{len(evidence['tasks'])} 个",
        ]
    )
    for task in evidence["tasks"][:10]:
        lines.append(
            f"  - `{task['id']}` · {task['status']} · {task['model']} · "
            f"{task['prompt_version']} · {task['created_at']}"
        )
    lines.extend(
        [
            "",
            "## 说明与限制",
            "",
            "- 题名/摘要纳入不等于最终纳入；只有全文阶段记录为“全文纳入”才计入最终纳入。",
            "- 引句自动校验只验证引句是否存在于输入文本，不代表证据字段的内容正确。",
            "- 轻量筛选计数与完整 PRISMA 2020 口径分开呈现，不应混用。",
            "",
        ]
    )
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def export_project_report_json(report: dict[str, Any]) -> bytes:
    return (
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
