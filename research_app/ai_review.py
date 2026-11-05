from __future__ import annotations

import csv
import io
import unicodedata
from datetime import datetime, timezone
from typing import Any

from research_app.csv_safe import write_safe_rows


FIELD_KEYS = (
    "research_question",
    "methods",
    "sample",
    "findings",
    "limitations",
)
FIELD_LABELS = {
    "research_question": "研究问题",
    "methods": "方法",
    "sample": "样本或数据",
    "findings": "主要发现",
    "limitations": "局限性",
}
CSV_COLUMNS = [
    "paper_id",
    "title",
    "year",
    *FIELD_KEYS,
    "quote_1",
    "quote_1_section",
    "quote_1_verification",
    "quote_2",
    "quote_2_section",
    "quote_2_verification",
    "quote_3",
    "quote_3_section",
    "quote_3_verification",
    "reviewed",
    "reviewer_note",
    "model",
    "prompt_version",
    "generated_at",
    "error",
]


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def normalize_evidence_data(data: Any) -> dict[str, Any]:
    payload = data if isinstance(data, dict) else {}
    normalized: dict[str, Any] = {
        field: _text(payload.get(field)) or "未提及" for field in FIELD_KEYS
    }
    quotes = []
    raw_quotes = payload.get("evidence_quotes")
    if isinstance(raw_quotes, list):
        for raw_quote in raw_quotes:
            if isinstance(raw_quote, dict):
                quote = _text(raw_quote.get("quote"))
                section = _text(raw_quote.get("section"))
            elif isinstance(raw_quote, str):
                quote = _text(raw_quote)
                section = ""
            else:
                continue
            if quote:
                quotes.append({"quote": quote, "section": section})
    normalized["evidence_quotes"] = quotes[:3]
    return normalized


def _match_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(char for char in normalized if char.isalnum())


def verify_quotes(data: dict[str, Any], source_text: str) -> list[dict[str, Any]]:
    source = _match_text(source_text)
    checks = []
    for quote in data.get("evidence_quotes") or []:
        quote_text = _text(quote.get("quote")) if isinstance(quote, dict) else ""
        needle = _match_text(quote_text)
        checks.append(
            {
                "quote": quote_text,
                "section": _text(quote.get("section")) if isinstance(quote, dict) else "",
                "verified": bool(needle) and needle in source,
            }
        )
    return checks


def quote_sources(section: str, title: str, text: str) -> list[str]:
    section_key = section.casefold()
    title_markers = ("标题", "题名", "title")
    text_markers = ("摘要", "abstract", "文本", "text", "正文")
    is_title = any(marker in section_key for marker in title_markers)
    is_text = any(marker in section_key for marker in text_markers)
    if is_title and not is_text:
        return [title] if title else []
    if is_text and not is_title:
        return [text] if text else []
    return [source for source in (title, text) if source]


def verify_quotes_with_sections(
    data: dict[str, Any],
    title: str,
    text: str,
) -> list[dict[str, Any]]:
    checks = []
    for quote in data.get("evidence_quotes") or []:
        quote_data = {"evidence_quotes": [quote]}
        section = _text(quote.get("section")) if isinstance(quote, dict) else ""
        sources = quote_sources(section, title, text)
        verified = any(
            verify_quotes(quote_data, source)[0]["verified"] for source in sources
        )
        checks.append(
            {
                "quote": _text(quote.get("quote")) if isinstance(quote, dict) else "",
                "section": section,
                "verified": verified,
            }
        )
    return checks


def quote_status(check: dict[str, Any]) -> str:
    if not check:
        return "未提供自动校验"
    if check.get("verified") is True:
        return "已在输入文本中匹配"
    if check.get("verified") is False:
        return "未在输入文本中匹配，请人工核对"
    return "人工修改，未自动校验"


def _year_text(value: Any) -> str:
    text = str(value or "")
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def evidence_rows(evidence: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows = []
    for item in evidence:
        data = normalize_evidence_data(item.get("data"))
        checks = item.get("quote_checks") or []
        quotes = data.get("evidence_quotes") or []
        row = {
            "paper_id": str(item.get("paper_id") or ""),
            "title": str(item.get("title") or ""),
            "year": _year_text(item.get("year")),
            "reviewed": "是" if item.get("reviewed") else "否",
            "reviewer_note": str(item.get("reviewer_note") or ""),
            "model": str(item.get("model") or ""),
            "prompt_version": str(item.get("prompt_version") or ""),
            "generated_at": str(item.get("generated_at") or ""),
            "error": str(item.get("error") or ""),
        }
        row.update({field: data[field] for field in FIELD_KEYS})
        for index in range(1, 4):
            position = index - 1
            if position < len(quotes):
                quote = quotes[position]
                check = checks[position] if position < len(checks) else {}
                row[f"quote_{index}"] = str(quote.get("quote") or "")
                row[f"quote_{index}_section"] = str(quote.get("section") or "")
                row[f"quote_{index}_verification"] = quote_status(check)
            else:
                row[f"quote_{index}"] = ""
                row[f"quote_{index}_section"] = ""
                row[f"quote_{index}_verification"] = ""
        rows.append(row)
    return rows


def export_evidence_csv(evidence: list[dict[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    write_safe_rows(writer, evidence_rows(evidence))
    return output.getvalue().encode("utf-8-sig")


def export_evidence_markdown(evidence: list[dict[str, Any]]) -> bytes:
    rows = evidence_rows(evidence)
    models = "、".join(sorted({row["model"] for row in rows if row["model"]})) or "未知"
    versions = (
        "、".join(sorted({row["prompt_version"] for row in rows if row["prompt_version"]}))
        or "未知"
    )
    lines = [
        "# AI 证据表",
        "",
        f"- 模型：{models}",
        f"- 提示词版本：{versions}",
        f"- 导出时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "- 说明：AI 输出仅供人工核对，不代表最终结论。",
        "",
    ]
    for row in rows:
        title = row["title"] or "（无标题）"
        lines.extend(
            [
                f"## {title}",
                "",
                f"- 来源：`{row['paper_id']}`",
                f"- 年份：{row['year'] or '未知'}",
                f"- 人工核对：{row['reviewed']}",
                f"- 模型：{row['model'] or '未知'}",
                f"- 提示词版本：{row['prompt_version'] or '未知'}",
                f"- 生成时间：{row['generated_at'] or '未知'}",
            ]
        )
        if row["error"]:
            lines.extend(["", f"**提取失败**：{row['error']}"])
        for field in FIELD_KEYS:
            lines.append(f"- {FIELD_LABELS[field]}：{row[field]}")
        quote_entries = [
            (index, row[f"quote_{index}"], row[f"quote_{index}_section"], row[f"quote_{index}_verification"])
            for index in range(1, 4)
            if row[f"quote_{index}"]
        ]
        if quote_entries:
            lines.extend(["", "### 原文引句", ""])
            for index, quote, section, status in quote_entries:
                location = section or "未知"
                lines.extend(
                    [
                        f"**引句 {index}**",
                        "",
                        f"> {quote}",
                        ">",
                        f"> 位置：{location} · 校验：{status}",
                        "",
                    ]
                )
        else:
            lines.extend(["", "**原文引句**：AI 未提供，无法自动核对。"])
        if row["reviewer_note"]:
            lines.extend(["", f"**核对备注**：{row['reviewer_note']}"])
        lines.extend(["", "---", ""])
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


MATRIX_SOURCES = ("ai_raw", "human_corrected", "final_adopted")
MATRIX_SOURCE_LABELS = {
    "ai_raw": "AI 原始",
    "human_corrected": "人工校正",
    "final_adopted": "最终采用",
}
MATRIX_INPUT_LABELS = {
    "full_text": "全文",
    "abstract": "摘要",
    "none": "无文本",
}
MATRIX_CSV_COLUMNS = [
    "paper_id",
    "title",
    "year",
    "value_source",
    "batch_id",
    "ai_version",
    "human_version",
    "final_version",
    "ai_task_id",
    "ai_batch_id",
    "human_task_id",
    "human_batch_id",
    "final_task_id",
    "final_batch_id",
    "input_source",
    "input_error",
    *FIELD_KEYS,
    "quote_1",
    "quote_1_section",
    "quote_1_verification",
    "quote_2",
    "quote_2_section",
    "quote_2_verification",
    "quote_3",
    "quote_3_section",
    "quote_3_verification",
    "reviewer_note",
    "task_id",
    "model",
    "prompt_version",
    "error",
]


def _matrix_choice(versions: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    for source in ("final_adopted", "human_corrected", "ai_raw"):
        version = versions.get(source)
        if version is not None:
            return source, version
    return "", {}


def matrix_rows(matrix: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows = []
    for paper in matrix:
        versions = paper.get("versions") or {}
        source, version = _matrix_choice(versions)
        ai_version = versions.get("ai_raw") or {}
        human_version = versions.get("human_corrected") or {}
        final_version = versions.get("final_adopted") or {}
        data = normalize_evidence_data(version.get("data"))
        quotes = data.get("evidence_quotes") or []
        checks = version.get("quote_checks") or []
        row = {
            "paper_id": str(paper.get("paper_id") or ""),
            "title": str(paper.get("title") or ""),
            "year": _year_text(paper.get("year")),
            "value_source": MATRIX_SOURCE_LABELS.get(source, source),
            "batch_id": str(version.get("batch_id") or ""),
            "ai_version": str(ai_version.get("version_no") or ""),
            "human_version": str(human_version.get("version_no") or ""),
            "final_version": str(final_version.get("version_no") or ""),
            "ai_task_id": str(ai_version.get("task_id") or ""),
            "ai_batch_id": str(ai_version.get("batch_id") or ""),
            "human_task_id": str(human_version.get("task_id") or ""),
            "human_batch_id": str(human_version.get("batch_id") or ""),
            "final_task_id": str(final_version.get("task_id") or ""),
            "final_batch_id": str(final_version.get("batch_id") or ""),
            "input_source": MATRIX_INPUT_LABELS.get(
                str(version.get("input_source") or ""), ""
            ),
            "input_error": str(version.get("input_error") or ""),
            "reviewer_note": str(version.get("reviewer_note") or ""),
            "task_id": str(version.get("task_id") or ""),
            "model": str(version.get("model") or ""),
            "prompt_version": str(version.get("prompt_version") or ""),
            "error": str(version.get("error") or ""),
        }
        row.update({field: data[field] for field in FIELD_KEYS})
        for index in range(1, 4):
            position = index - 1
            if position < len(quotes):
                quote = quotes[position]
                check = checks[position] if position < len(checks) else {}
                row[f"quote_{index}"] = str(quote.get("quote") or "")
                row[f"quote_{index}_section"] = str(quote.get("section") or "")
                row[f"quote_{index}_verification"] = quote_status(check)
            else:
                row[f"quote_{index}"] = ""
                row[f"quote_{index}_section"] = ""
                row[f"quote_{index}_verification"] = ""
        rows.append(row)
    return rows


def export_matrix_csv(matrix: list[dict[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=MATRIX_CSV_COLUMNS)
    writer.writeheader()
    write_safe_rows(writer, matrix_rows(matrix))
    return output.getvalue().encode("utf-8-sig")


def export_matrix_markdown(
    matrix: list[dict[str, Any]],
    *,
    task: dict[str, Any] | None = None,
) -> bytes:
    rows = matrix_rows(matrix)
    models = "、".join(sorted({row["model"] for row in rows if row["model"]})) or "未知"
    versions = (
        "、".join(
            sorted({row["prompt_version"] for row in rows if row["prompt_version"]})
        )
        or "未知"
    )
    lines = [
        "# 项目级证据矩阵",
        "",
        f"- 任务：`{(task or {}).get('id') or '未知'}`",
        f"- 模型：{models}",
        f"- 提示词版本：{versions}",
        f"- 导出时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "- 说明：AI 原始、人工校正与最终采用版本分开保存；本表展示当前采用值。",
        "",
    ]
    for row in rows:
        title = row["title"] or "（无标题）"
        lines.extend(
            [
                f"## {title}",
                "",
                f"- 来源：`{row['paper_id']}`",
                f"- 年份：{row['year'] or '未知'}",
                f"- 当前采用：{row['value_source']}（ai={row['ai_version'] or '—'}，"
                f"human={row['human_version'] or '—'}，final={row['final_version'] or '—'}）",
                f"- AI 原始：v{row['ai_version'] or '—'} · 任务 `{row['ai_task_id'] or '—'}`"
                f" · 批次 `{row['ai_batch_id'] or '—'}`",
                f"- 人工校正：v{row['human_version'] or '—'} · 任务 `{row['human_task_id'] or '—'}`"
                f" · 批次 `{row['human_batch_id'] or '—'}`",
                f"- 最终采用：v{row['final_version'] or '—'} · 任务 `{row['final_task_id'] or '—'}`"
                f" · 批次 `{row['final_batch_id'] or '—'}`",
                f"- 输入来源：{row['input_source'] or '未知'}"
                + (f"（{row['input_error']}）" if row["input_error"] else ""),
                f"- 模型：{row['model'] or '未知'}",
                f"- 提示词版本：{row['prompt_version'] or '未知'}",
            ]
        )
        if row["error"]:
            lines.extend(["", f"**提取失败**：{row['error']}"])
        for field in FIELD_KEYS:
            lines.append(f"- {FIELD_LABELS[field]}：{row[field]}")
        quote_entries = [
            (index, row[f"quote_{index}"], row[f"quote_{index}_section"], row[f"quote_{index}_verification"])
            for index in range(1, 4)
            if row[f"quote_{index}"]
        ]
        if quote_entries:
            lines.extend(["", "### 原文引句", ""])
            for index, quote, section, status in quote_entries:
                lines.extend(
                    [
                        f"**引句 {index}**",
                        "",
                        f"> {quote}",
                        ">",
                        f"> 位置：{section or '未知'} · 校验：{status}",
                        "",
                    ]
                )
        if row["reviewer_note"]:
            lines.extend(["", f"**审核备注**：{row['reviewer_note']}"])
        lines.extend(["", "---", ""])
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")
