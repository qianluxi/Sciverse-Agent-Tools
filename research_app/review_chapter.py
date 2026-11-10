"""证据约束的论文综述章节草稿。

与阶段 6 的「综述提纲」不同，本模块产出的是**章节正文草稿**，但加了三道约束：
1. 只允许引用项目内已纳入文献的编号（[Cxxx]），越界即在校验报告中报出；
2. 仅题摘级文献不得描述方法细节与数值，只能陈述「摘要提到」；
3. 没有完成全文核验的证据时直接拒绝生成，避免产出无依据的空话。

本模块不做持久化：章节草稿只存在于页面会话与用户下载的文件中，
不写入数据库、不写入论文，符合「AI 输出必须人工确认」的边界。
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timezone
from typing import Any

from research_app.ai import AIError, SYSTEM_PROMPT
from research_app.ai_review import normalize_evidence_data


CHAPTER_PROMPT_VERSION = "chapter-v1"
CITATION_RE = re.compile(r"\[(C\d{3,})\]")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")
TABLE_RE = re.compile(r"^\s*\|")

CHAPTER_SYSTEM_PROMPT = SYSTEM_PROMPT + """

你撰写的是论文的「文献综述」章节草稿，不是摘要、不是提纲、不是参考文献表。
硬性约束（违反任一条即视为不合格输出）：
1. 只允许引用来源清单里出现的编号，格式为 [C001]；不得引用清单外文献，不得虚构编号。
2. 每条具体论断（尤其方法、样本、数值、结论）后必须紧跟至少一个来源编号；
   没有来源的句子只能承担过渡或本章组织性说明的功能。
3. 仅有题摘的来源只能写「该文摘要提到／指出……」，不得描述其方法细节、样本量或具体数值。
4. 来源没有给出的数字一律不写；不得推算、换算，也不得用常识补充。
5. 证据不足处显式写「（证据缺口：……）」，不要用空话填补。
6. 不做「国内外研究均表明」这类全局断言，只陈述来源清单能支撑的范围。
7. 直接输出 Markdown 正文，不要代码块，不要输出 JSON，不要附加说明文字。
"""


def build_sources(
    matrix: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    *,
    max_abstract_chars: int = 600,
    prefix: str = "C",
) -> list[dict[str, Any]]:
    """把证据矩阵与筛选候选合并成模型可用的来源清单。

    只有题名/摘要裁决为 included 的候选才进入清单；证据版本按
    final_adopted → human_corrected → ai_raw 的顺序取最高优先级。
    输出按候选键排序后重新编号为 C001、C002……，原始候选键保留在
    candidate_key 字段中，便于追溯回工作台记录。
    """
    matrix_by_paper = {str(paper.get("paper_id") or ""): paper for paper in matrix}
    sources: list[dict[str, Any]] = []
    for candidate in candidates:
        if str(candidate.get("status") or "") != "included":
            continue
        paper_id = str(candidate.get("paper_id") or "")
        key = str(candidate.get("candidate_key") or "")
        if not key or key.startswith("conflict:"):
            continue
        paper = matrix_by_paper.get(paper_id) or {}
        versions = paper.get("versions") or {}
        version_source = ""
        version: dict[str, Any] = {}
        for name in ("final_adopted", "human_corrected", "ai_raw"):
            if versions.get(name):
                version_source, version = name, versions[name]
                break
        data = normalize_evidence_data(version.get("data")) if version else {}
        has_evidence = bool(version) and bool(data.get("findings") or data.get("evidence_quotes"))
        quotes = [
            str(quote.get("quote") or "").strip()
            for quote in (data.get("evidence_quotes") or [])
            if str(quote.get("quote") or "").strip()
        ]
        sources.append(
            {
                "key": key,
                "candidate_key": key,
                "paper_id": paper_id,
                "title": str(candidate.get("title") or paper.get("title") or ""),
                "year": candidate.get("year") or paper.get("year") or "",
                "doi": str(candidate.get("doi") or ""),
                "evidence_level": "fulltext" if has_evidence else "abstract",
                "evidence_source": version_source,
                "findings": str(data.get("findings") or ""),
                "methods": str(data.get("methods") or ""),
                "limitations": str(data.get("limitations") or ""),
                "quotes": quotes[:3],
                "abstract": str(candidate.get("abstract") or "")[:max_abstract_chars],
            }
        )
    sources.sort(key=lambda item: item["candidate_key"])
    for index, source in enumerate(sources, 1):
        source["key"] = f"{prefix}{index:03d}"
    return sources


def _compact_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact = []
    for source in sources:
        item: dict[str, Any] = {
            "编号": source["key"],
            "题名": source["title"],
            "年份": source["year"],
            "证据级别": "全文已核验" if source["evidence_level"] == "fulltext" else "仅题摘",
        }
        if source["evidence_level"] == "fulltext":
            item.update(
                {
                    "方法": source.get("methods") or "未提及",
                    "发现": source.get("findings") or "未提及",
                    "局限": source.get("limitations") or "未提及",
                    "逐字引句": source.get("quotes") or [],
                }
            )
        else:
            item["摘要"] = source.get("abstract") or "无摘要"
        compact.append(item)
    return compact


def build_chapter_messages(
    question: str,
    sources: list[dict[str, Any]],
    chapter_title: str,
) -> list[dict[str, str]]:
    fulltext = [item for item in sources if item["evidence_level"] == "fulltext"]
    abstract = [item for item in sources if item["evidence_level"] != "fulltext"]
    guidance = (
        f"章节标题：{chapter_title}\n"
        f"研究问题：{question}\n\n"
        f"来源清单共 {len(sources)} 篇：全文已核验 {len(fulltext)} 篇，仅题摘 {len(abstract)} 篇。\n"
        "请按以下结构写成一节可直接进入论文的综述章节：\n"
        "1. 开头用 1 段交代述评口径（只对全文核验文献作实质引证，仅题摘文献只描述研究取向）；\n"
        "2. 按主题分 2–4 个小节，每节先陈述文献做了什么，再评其证据强度与边界；\n"
        "3. 单列一小节「研究空白」，明确指出哪些问题尚无来源可回答；\n"
        "4. 结尾一段本章小结，落到对研究设计的约束，不要空泛总结。\n"
        "篇幅控制在 1500–3000 字。每个 [Cxxx] 必须是来源清单中的编号。\n\n"
        f"来源清单：{json.dumps(_compact_sources(sources), ensure_ascii=False)}"
    )
    return [
        {"role": "system", "content": CHAPTER_SYSTEM_PROMPT},
        {"role": "user", "content": guidance},
    ]


def _strip_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:markdown|md|text)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def check_citations(text: str, sources: list[dict[str, Any]]) -> dict[str, Any]:
    """检查草稿的引证是否越界、是否漏引全文证据、是否存在无引证段落。"""
    allowed = {str(source["key"]): source for source in sources}
    cited_all = CITATION_RE.findall(text)
    cited = sorted(set(cited_all))
    invalid = sorted(key for key in cited if key not in allowed)
    uncited_fulltext = sorted(
        key
        for key, source in allowed.items()
        if source["evidence_level"] == "fulltext" and key not in cited
    )
    abstract_only_cited = sorted(
        key
        for key in cited
        if key in allowed and allowed[key]["evidence_level"] != "fulltext"
    )
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    uncited_paragraphs = [
        part
        for part in paragraphs
        if not HEADING_RE.match(part)
        and not TABLE_RE.match(part)
        and not CITATION_RE.search(part)
    ]
    return {
        "cited": cited,
        "invalid": invalid,
        "uncited_fulltext": uncited_fulltext,
        "abstract_only_cited": abstract_only_cited,
        "citation_count": len(cited_all),
        "paragraph_count": len(paragraphs),
        "uncited_paragraphs": uncited_paragraphs,
        "ok": not invalid,
    }


def check_summary(check: dict[str, Any]) -> list[str]:
    """把校验结果转成适合直接展示的提示语。"""
    messages: list[str] = []
    if check["invalid"]:
        messages.append(
            "发现越界引证：" + "、".join(check["invalid"]) + "；这些编号不在来源清单中，必须删除或替换。"
        )
    if check["uncited_fulltext"]:
        messages.append(
            "有全文证据未被引用：" + "、".join(check["uncited_fulltext"]) + "；请确认是否为有意省略。"
        )
    if check["abstract_only_cited"]:
        messages.append(
            "引用了仅题摘文献：" + "、".join(check["abstract_only_cited"]) + "；只能作「摘要提到」层面的陈述。"
        )
    if check["uncited_paragraphs"]:
        messages.append(f"有 {len(check['uncited_paragraphs'])} 个段落不含任何引证，请逐段确认是否为过渡性文字。")
    if not messages:
        messages.append("引证检查通过：未发现越界编号，全文证据均被引用。")
    return messages


def citation_report_rows(
    sources: list[dict[str, Any]],
    check: dict[str, Any],
) -> list[dict[str, str]]:
    cited = set(check["cited"])
    return [
        {
            "编号": source["key"],
            "候选键": source["candidate_key"],
            "题名": source["title"],
            "证据级别": "全文已核验" if source["evidence_level"] == "fulltext" else "仅题摘",
            "是否被引用": "是" if source["key"] in cited else "否",
            "引证是否合法": "否" if source["key"] in check["invalid"] else "是",
        }
        for source in sources
    ]


def export_citation_report_csv(sources: list[dict[str, Any]], check: dict[str, Any]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=["编号", "候选键", "题名", "证据级别", "是否被引用", "引证是否合法"],
    )
    writer.writeheader()
    writer.writerows(citation_report_rows(sources, check))
    return buffer.getvalue().encode("utf-8-sig")


def render_chapter_markdown(
    chapter: str,
    *,
    question: str,
    chapter_title: str,
    model: str,
    project_id: str,
    source_count: int,
    fulltext_count: int,
) -> bytes:
    header = (
        f"<!-- model: {model} | prompt_version: {CHAPTER_PROMPT_VERSION} | "
        f"generated_at: {datetime.now(timezone.utc).isoformat(timespec='seconds')} | "
        f"project_id: {project_id} | sources: {source_count} (fulltext: {fulltext_count}) -->\n\n"
    )
    meta = (
        "> 本文为 AI 辅助生成的综述章节草稿，已在生成时做引证范围校验，"
        "但仍须逐条核对来源后再进入论文；未经人工确认不得作为最终稿。\n\n"
        f"> 章节标题：{chapter_title}　|　研究问题：{question}\n\n"
        "---\n\n"
    )
    return (header + meta + chapter.strip() + "\n").encode("utf-8")


def generate_review_chapter(
    client: Any,
    question: str,
    sources: list[dict[str, Any]],
    *,
    chapter_title: str = "第 2 章 文献综述",
    max_tokens: int = 8192,
) -> tuple[str, dict[str, Any]]:
    if not str(question or "").strip():
        raise ValueError("研究问题不能为空。")
    if not sources:
        raise ValueError("没有已纳入的文献，无法生成综述章节。")
    if not any(source["evidence_level"] == "fulltext" for source in sources):
        raise ValueError(
            "没有任何完成全文核验的证据，生成章节只会是空话。"
            "请先在证据矩阵中补齐全文，或继续使用「综述提纲」。"
        )
    raw = client.chat(
        build_chapter_messages(question.strip(), sources, chapter_title.strip() or "第 2 章 文献综述"),
        max_tokens=max_tokens,
    )
    chapter = _strip_fence(raw)
    if not chapter:
        raise AIError("AI 返回了空的章节内容。")
    return chapter, check_citations(chapter, sources)
