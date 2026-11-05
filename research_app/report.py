from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import zipfile
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterator

from research_app import storage
from research_app.ai_review import export_matrix_csv, export_matrix_versions_csv
from research_app.csv_safe import write_safe_rows
from research_app.screening import (
    FULLTEXT_STATUS_LABELS,
    STATUS_LABELS,
)
from research_app.trends import analyze_papers


PRISMA_RECORD_COLUMNS = [
    "candidate_key",
    "title",
    "year",
    "doi",
    "source_run_ids",
    "title_abstract_status",
    "title_abstract_reason",
    "title_abstract_updated_at",
    "latest_screening_audit_id",
    "latest_screening_audit_at",
    "fulltext_status",
    "fulltext_reason",
    "fulltext_notes",
    "fulltext_updated_at",
    "latest_fulltext_audit_id",
    "latest_fulltext_audit_at",
    "ai_task_id",
    "ai_suggestion",
    "ai_review_status",
    "ai_review_decision",
]

METRIC_DEFINITIONS = {
    "identified": "project_import_events.identified_count 累计；多次导入按事件累计。",
    "duplicates_removed": "project_import_events.duplicate_count 累计；去重基于强标识符与规范化标题。",
    "unique_records": "screening_candidates 当前记录数。",
    "title_abstract": "screening_candidates.status 的当前分布。",
    "fulltext": "screening_fulltext 按状态分组；仅统计题名/摘要纳入的候选。",
    "included_in_review": "screening_fulltext.status='included' 的数量。",
    "evidence_matrix": "按论文汇总最新证据版本；引句校验只验证引句是否存在于输入文本。",
    "trends": "基于项目最近一次检索快照中的论文集合。",
    "citation_network": "引用网络为交互式分析，当前未持久化到项目。",
}


@contextlib.contextmanager
def _database_snapshot() -> Iterator[None]:
    source = storage.database_path()
    if not source.exists():
        yield
        return
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3")
    handle.close()
    try:
        with sqlite3.connect(source) as src, sqlite3.connect(handle.name) as dst:
            src.backup(dst)
        previous = os.environ.get("SCIVERSE_RESEARCH_DB")
        os.environ["SCIVERSE_RESEARCH_DB"] = handle.name
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("SCIVERSE_RESEARCH_DB", None)
            else:
                os.environ["SCIVERSE_RESEARCH_DB"] = previous
    finally:
        with contextlib.suppress(OSError):
            os.unlink(handle.name)


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


def _analysis_summary(project_id: str) -> dict[str, Any]:
    latest_run = storage.latest_search_run(project_id)
    papers = (latest_run or {}).get("papers") or []
    eligible_nodes = sum(
        1
        for paper in papers
        if paper.get("unique_id") or paper.get("doc_id")
    )
    if not papers:
        return {
            "trends": {
                "status": "insufficient_data",
                "sample_size": 0,
                "note": "项目没有可用的检索快照。",
            },
            "citation_network": {
                "status": "insufficient_data",
                "eligible_nodes": 0,
                "note": "项目没有可用的检索快照。",
            },
        }
    analysis = analyze_papers(papers)
    return {
        "trends": {
            "status": "generated",
            "source_run_id": (latest_run or {}).get("id"),
            "sample_size": analysis["sample_size"],
            "duplicates_removed": analysis["duplicates_removed"],
            "identifier_conflicts": analysis["identifier_conflicts"],
            "year_range": analysis["year_range"],
            "top_venues": analysis["venue_counts"][:5],
            "top_keywords": analysis["keyword_counts"][:10],
            "top_cited": analysis["top_cited"][:5],
            "missing": analysis["missing"],
        },
        "citation_network": {
            "status": "not_generated",
            "eligible_nodes": eligible_nodes,
            "note": "引用网络为交互式分析，当前未持久化到项目；此处仅记录可展开节点数。",
        },
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
        "analysis": _analysis_summary(project_id),
        "metric_definitions": METRIC_DEFINITIONS,
    }


def prisma_records(project_id: str) -> list[dict[str, Any]]:
    fulltext = {
        item["candidate_key"]: item
        for item in storage.list_fulltext_decisions(project_id)
    }
    sources: dict[str, list[str]] = {}
    for item in storage.list_all_candidate_sources(project_id):
        sources.setdefault(item["candidate_key"], []).append(item["run_id"])
    latest_screening_audit: dict[str, dict[str, Any]] = {}
    for item in storage.list_all_screening_audit(project_id):
        latest_screening_audit[item["candidate_key"]] = item
    latest_fulltext_audit: dict[str, dict[str, Any]] = {}
    for item in storage.list_all_fulltext_audit(project_id):
        latest_fulltext_audit[item["candidate_key"]] = item
    suggestions: dict[str, dict[str, Any]] = {}
    saved = storage.load_latest_screening_suggestions(project_id)
    if saved:
        for item in saved["suggestions"]:
            suggestions[item["candidate_key"]] = item

    rows = []
    for candidate in storage.list_all_screening_candidates(project_id):
        key = candidate["candidate_key"]
        decision = fulltext.get(key) or {}
        suggestion = suggestions.get(key) or {}
        screening_audit = latest_screening_audit.get(key) or {}
        fulltext_audit = latest_fulltext_audit.get(key) or {}
        rows.append(
            {
                "candidate_key": key,
                "title": candidate.get("title") or "",
                "year": candidate.get("year") or "",
                "doi": candidate.get("doi") or "",
                "source_run_ids": "；".join(sources.get(key, [])),
                "title_abstract_status": STATUS_LABELS.get(
                    candidate["status"], candidate["status"]
                ),
                "title_abstract_reason": candidate.get("exclusion_reason") or "",
                "title_abstract_updated_at": candidate.get("updated_at") or "",
                "latest_screening_audit_id": screening_audit.get("id") or "",
                "latest_screening_audit_at": screening_audit.get("changed_at") or "",
                "fulltext_status": FULLTEXT_STATUS_LABELS.get(
                    decision.get("status") or "not_sought",
                    decision.get("status") or "not_sought",
                ),
                "fulltext_reason": decision.get("exclusion_reason") or "",
                "fulltext_notes": decision.get("notes") or "",
                "fulltext_updated_at": decision.get("updated_at") or "",
                "latest_fulltext_audit_id": fulltext_audit.get("id") or "",
                "latest_fulltext_audit_at": fulltext_audit.get("changed_at") or "",
                "ai_task_id": suggestion.get("task_id") or "",
                "ai_suggestion": suggestion.get("suggestion") or "",
                "ai_review_status": suggestion.get("review_status") or "",
                "ai_review_decision": suggestion.get("review_decision") or "",
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
    analysis = report.get("analysis") or {}
    trends = analysis.get("trends") or {}
    network = analysis.get("citation_network") or {}
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
        f"| 全文：尚未寻求获取 | {fulltext['not_sought']} |",
        f"| 全文：已寻求未获取 | {fulltext['not_retrieved']} |",
        f"| 全文：已获取待评估 | {fulltext['retrieved_pending']} |",
        f"| 全文：已检索合计 | {fulltext['sought']} |",
        f"| 全文：已评估 | {fulltext['assessed']} |",
        f"| 全文：排除 | {fulltext['excluded']} |",
        f"| 全文：纳入 | {fulltext['included']} |",
        f"| **最终纳入综述** | **{prisma['included_in_review']}** |",
        "",
        f"全文阶段状态合计 {fulltext['partition_total']} / 题摘纳入 "
        f"{title_abstract['included']}（守恒：{'是' if fulltext['conservation_ok'] else '否'}）。",
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
            "## 趋势分析",
            "",
        ]
    )
    if trends.get("status") == "generated":
        lines.append(
            f"- 数据来源：最近检索快照 `{trends.get('source_run_id') or '未知'}`"
        )
        lines.append(
            f"- 样本量：{trends['sample_size']}（去重移除 "
            f"{trends['duplicates_removed']}，强标识符冲突 "
            f"{trends['identifier_conflicts']}）"
        )
        lines.append(f"- 年份范围：{trends['year_range'] or '未知'}")
        if trends.get("top_venues"):
            lines.append(
                "- 主要来源："
                + "、".join(
                    f"{item['venue']} {item['count']}"
                    for item in trends["top_venues"]
                )
            )
        if trends.get("top_keywords"):
            lines.append(
                "- 高频关键词："
                + "、".join(
                    f"{item['keyword']} {item['count']}"
                    for item in trends["top_keywords"]
                )
            )
        if trends.get("top_cited"):
            lines.append(
                "- 高被引："
                + "、".join(
                    f"{item['title']}（{item['citation_count']}）"
                    for item in trends["top_cited"]
                )
            )
    else:
        lines.append(f"- 未生成：{trends.get('note') or '数据不足'}")
    lines.extend(["", "## 引用网络", ""])
    if network.get("status") == "generated":
        lines.append(
            f"- 节点：{network.get('nodes', 0)}；边：{network.get('edges', 0)}"
        )
    else:
        lines.append(
            f"- 未生成：{network.get('note') or '数据不足'}"
            f"（可展开节点 {network.get('eligible_nodes', 0)} 个）"
        )
    lines.extend(["", "## 统计口径", ""])
    for key, definition in (report.get("metric_definitions") or {}).items():
        lines.append(f"- `{key}`：{definition}")
    lines.extend(
        [
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
            "- 引用网络当前为交互式分析且未持久化；报告只记录是否生成与可展开节点数。",
            "",
        ]
    )
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def export_project_report_json(report: dict[str, Any]) -> bytes:
    return (
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")


SEARCH_RUN_COLUMNS = [
    "id",
    "mode",
    "created_at",
    "result_count",
    "snapshot_count",
    "params_json",
]
CANDIDATE_SOURCE_COLUMNS = ["candidate_key", "run_id", "discovered_at"]
SCREENING_AUDIT_COLUMNS = [
    "id",
    "candidate_key",
    "old_status",
    "new_status",
    "old_reason",
    "new_reason",
    "old_notes",
    "new_notes",
    "changed_at",
    "ai_task_id",
    "ai_action",
]
FULLTEXT_AUDIT_COLUMNS = [
    "id",
    "candidate_key",
    "old_status",
    "new_status",
    "old_reason",
    "new_reason",
    "old_notes",
    "new_notes",
    "changed_at",
]


def _rows_csv(rows: list[dict[str, Any]], columns: list[str]) -> bytes:
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


def export_search_runs_csv(project_id: str) -> bytes:
    rows = [
        {
            "id": run["id"],
            "mode": run["mode"],
            "created_at": run["created_at"],
            "result_count": run["result_count"],
            "snapshot_count": run["snapshot_count"],
            "params_json": json.dumps(run["params"], ensure_ascii=False),
        }
        for run in storage.list_search_runs(project_id, limit=None)
    ]
    return _rows_csv(rows, SEARCH_RUN_COLUMNS)


def export_candidate_sources_csv(project_id: str) -> bytes:
    return _rows_csv(
        storage.list_all_candidate_sources(project_id),
        CANDIDATE_SOURCE_COLUMNS,
    )


def export_screening_audit_csv(project_id: str) -> bytes:
    return _rows_csv(
        storage.list_all_screening_audit(project_id),
        SCREENING_AUDIT_COLUMNS,
    )


def export_fulltext_audit_csv(project_id: str) -> bytes:
    return _rows_csv(
        storage.list_all_fulltext_audit(project_id),
        FULLTEXT_AUDIT_COLUMNS,
    )


def export_project_bundle(project_id: str) -> bytes:
    with _database_snapshot():
        report = build_project_report(project_id)
        files: dict[str, bytes] = {
            "report.md": render_project_report_markdown(report),
            "report.json": export_project_report_json(report),
            "prisma_records.csv": export_prisma_records_csv(project_id),
            "search_runs.csv": export_search_runs_csv(project_id),
            "candidate_sources.csv": export_candidate_sources_csv(project_id),
            "screening_audit.csv": export_screening_audit_csv(project_id),
            "fulltext_audit.csv": export_fulltext_audit_csv(project_id),
        }
        matrix = storage.load_evidence_matrix(project_id)
        if matrix:
            files["evidence_matrix.csv"] = export_matrix_csv(matrix)
            files["evidence_matrix_versions.csv"] = export_matrix_versions_csv(
                matrix
            )
        manifest = {
            "format": "sciverse-project-bundle",
            "format_version": 2,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "project_id": project_id,
            "snapshot": True,
            "files": [
                {
                    "name": name,
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
                for name, content in sorted(files.items())
            ],
        }
        files["manifest.json"] = (
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        buffer = io.BytesIO()
        with zipfile.ZipFile(
            buffer, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        return buffer.getvalue()
