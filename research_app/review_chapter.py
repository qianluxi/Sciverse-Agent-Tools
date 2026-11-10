"""证据约束的论文综述章节草稿。

与阶段 6 的「综述提纲」不同，本模块产出的是**章节正文**，并假定人会继续改：
生成只是起点，页面提供逐节编辑、表格编辑、未纳入线索插补与拼装导出。

四道约束：
1. 只允许引用项目内已纳入文献的编号（[Cxxx]），越界即在校验报告中报出；
2. 仅题摘级文献不得描述方法细节与数值，只能陈述「摘要提到」；
3. 没有完成全文提取的证据时直接拒绝生成，避免产出无依据的空话；
4. 表格与「未纳入线索」不交给模型写，由程序按数据拼装，避免编造。

本模块不做持久化：章节草稿只存在于页面会话与用户下载的文件中，
不写入数据库、不写入论文，符合「AI 输出必须人工确认」的边界。
"""
from __future__ import annotations

import csv
import io
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from research_app.ai import AIError, SYSTEM_PROMPT
from research_app.ai_review import normalize_evidence_data
from research_app.exports import format_authors


CHAPTER_PROMPT_VERSION = "chapter-v2"
CITATION_RE = re.compile(r"\[(C\d{3,})\]")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")
TABLE_RE = re.compile(r"^\s*\|")
TABLE_CAPTION_RE = re.compile(r"^\s*\*\*\s*表\s*\d")
SECTION_HEADING_RE = re.compile(r"^\s{0,2}(#{1,2})\s+(.+?)\s*$")
NUMBERING_RE = re.compile(r"^\s*(?:第\s*[0-9一二三四五六七八九十]+\s*[章节]|(?:\d+(?:\.\d+)*)(?:[.、．)）])?)\s*")
QUANTITY_RE = re.compile(
    # 只看经济性/工程量口径。沉降 mm、温度、强度这类物理量不算经济指标，
    # 否则「最大沉降 30 mm」会被误判成有量化经济数据。
    r"\d[\d,.]*\s*(?:万元|亿元|元|%|％|㎡|平方米|m2|m²|立方米|m3|m³|立方|倍)"
)

# 证据版本来源与「是否人工确认」是两件事：ai_raw 只是 AI 提取结果，
# 有 findings 不代表已被人工核验。沿用证据矩阵页的中文标签保持一致。
EVIDENCE_SOURCE_LABELS = {
    "ai_raw": "AI 原始",
    "human_corrected": "人工校正",
    "final_adopted": "最终采用",
}
CONFIRMED_EVIDENCE_SOURCES = frozenset({"human_corrected", "final_adopted"})
ABSTRACT_LABEL = "仅题摘"
CONFIRMED_LABEL = "全文已核验"
UNCONFIRMED_LABEL = "全文已提取（待确认）"

# 小节骨架：模型只负责有证据支撑的正文，标题固定，便于逐节编辑与重新编号。
SECTION_SCOPE = "述评口径与检索边界"
SECTION_GAPS = "研究空白"
SECTION_SUMMARY = "本章小结"
SECTION_LEADS = "检索中发现但未纳入的线索"

DEFAULT_SECTION_PRESETS = (SECTION_SCOPE, SECTION_GAPS, SECTION_SUMMARY)


def evidence_level_label(source: dict[str, Any]) -> str:
    """把来源的证据级别转成可直接展示的标签。

    三档而不是两档：仅题摘 / 全文已提取（待确认） / 全文已核验。
    「已核验」只留给人工校正或人工采用过的版本，避免把 AI 原始提取
    记录说成已核验。
    """
    if str(source.get("evidence_level") or "") != "fulltext":
        return ABSTRACT_LABEL
    return CONFIRMED_LABEL if source.get("evidence_confirmed") else UNCONFIRMED_LABEL


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
8. 不要生成任何表格（Markdown 表格也不要用），表格由系统另行拼装。
9. 引用写成 [C001] 或 [C001][C002]，不要写「[C001, C002]」，不要写成上标。
10. 某个来源第一次出现时写成「第一作者（年份）[Cxxx]」；来源清单里作者为「未提供」的，
    只写编号，不要编造作者或期刊。
"""


def _norm_title(value: Any) -> str:
    return re.sub(r"[\s\u3000]+", "", str(value or "")).lower()


def attach_bibliography(
    sources: list[dict[str, Any]],
    papers: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """用检索快照里的原始记录回填作者、期刊等书目信息。

    候选表不保存作者与期刊，但 search_runs.snapshot_json 里有。
    匹配顺序：DOI 优先，其次规范化题名。已有值不覆盖，方便用户在页面里
    手工修正后再回填也不会被冲掉。
    """
    by_doi: dict[str, dict[str, Any]] = {}
    by_title: dict[str, dict[str, Any]] = {}
    for paper in papers or []:
        doi = str(paper.get("doi") or "").strip().lower()
        if doi:
            by_doi.setdefault(doi, paper)
        title = _norm_title(paper.get("title"))
        if title:
            by_title.setdefault(title, paper)
    for source in sources:
        doi = str(source.get("doi") or "").strip().lower()
        paper = by_doi.get(doi) or by_title.get(_norm_title(source.get("title"))) or {}
        if not source.get("authors"):
            source["authors"] = format_authors(paper)
        if not source.get("venue"):
            source["venue"] = str(
                paper.get("publication_venue_name_unified")
                or paper.get("publication_venue_name")
                or ""
            )
        if not source.get("year") and paper.get("publication_published_year"):
            source["year"] = paper.get("publication_published_year")
        if source.get("citation_count") is None:
            source["citation_count"] = paper.get("citation_count")
    return sources


def citation_label(source: dict[str, Any]) -> str:
    """把一条来源写成「第一作者等（年份）期刊」的形式，缺项就跳过。"""
    authors = str(source.get("authors") or "").strip()
    if authors:
        names = [name.strip() for name in re.split(r"[;,、]", authors) if name.strip()]
        if len(names) > 1:
            authors = f"{names[0]} 等"
        elif names:
            authors = names[0]
    year = str(source.get("year") or "").strip()
    if year.endswith(".0"):
        year = year[:-2]
    venue = str(source.get("venue") or "").strip()
    if not authors and not venue:
        return f"（书目待补，{year}）" if year else "（书目待补）"
    head = "，".join(part for part in (authors, year) if part)
    return f"{head}. {venue}" if head and venue else (head or venue)


def build_sources(
    matrix: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    *,
    papers: list[dict[str, Any]] | None = None,
    max_abstract_chars: int = 600,
    prefix: str = "C",
    key_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """把证据矩阵与筛选候选合并成模型可用的来源清单。

    只有题名/摘要裁决为 included 的候选才进入清单；证据版本按
    final_adopted → human_corrected → ai_raw 的顺序取最高优先级。
    输出按候选键排序后重新编号为 C001、C002……，原始候选键保留在
    candidate_key 字段中，便于追溯回工作台记录。
    papers 传入检索快照里的原始记录时，会顺带回填作者与期刊。

    key_map 用于**保住已有编号**：用户改过正文后再新增纳入文献，
    若按顺序重排，正文里的 [Cxxx] 会集体错位。传入
    {候选键: 编号} 时，已知候选沿用旧编号，新候选从最大编号往后接。
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
                "authors": "",
                "venue": "",
                "citation_count": None,
                "evidence_level": "fulltext" if has_evidence else "abstract",
                "evidence_confirmed": version_source in CONFIRMED_EVIDENCE_SOURCES,
                "evidence_source": version_source,
                "findings": str(data.get("findings") or ""),
                "methods": str(data.get("methods") or ""),
                "limitations": str(data.get("limitations") or ""),
                "quotes": quotes[:3],
                "abstract": str(candidate.get("abstract") or "")[:max_abstract_chars],
            }
        )
    sources.sort(key=lambda item: item["candidate_key"])
    _assign_keys(sources, prefix=prefix, key_map=key_map)
    return attach_bibliography(sources, papers)


def _assign_keys(
    sources: list[dict[str, Any]],
    *,
    prefix: str,
    key_map: dict[str, str] | None,
) -> None:
    """给来源分配编号，尽量不动已有编号。"""
    preserved = dict(key_map or {})
    used = {key for key in preserved.values() if key}
    taken = set()
    next_number = 0
    for key in used:
        match = re.fullmatch(rf"{re.escape(prefix)}(\d+)", key)
        if match:
            next_number = max(next_number, int(match.group(1)))
    for source in sources:
        candidate_key = source["candidate_key"]
        key = preserved.get(candidate_key)
        if key and key not in taken:
            source["key"] = key
            taken.add(key)
    for source in sources:
        if source["key"] in taken:
            continue
        next_number += 1
        source["key"] = f"{prefix}{next_number:03d}"
        taken.add(source["key"])


def _compact_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact = []
    for source in sources:
        item: dict[str, Any] = {
            "编号": source["key"],
            "题名": source["title"],
            "作者": source.get("authors") or "未提供",
            "期刊": source.get("venue") or "未提供",
            "年份": source["year"],
            "证据级别": evidence_level_label(source),
        }
        if source["evidence_level"] == "fulltext":
            item.update(
                {
                    "证据版本": EVIDENCE_SOURCE_LABELS.get(source["evidence_source"], source["evidence_source"] or "未标注"),
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


def prisma_digest(prisma: dict[str, Any] | None) -> str:
    """把 PRISMA 计数压成一段可核对的事实，供述评口径小节使用。"""
    if not prisma:
        return ""
    title_abstract = prisma.get("title_abstract") or {}
    fulltext = prisma.get("fulltext") or {}
    lines = [
        f"识别记录 {prisma.get('identified', '未知')} 条",
        f"去重移除 {prisma.get('duplicates_removed', '未知')} 条",
        f"题名/摘要纳入 {title_abstract.get('included', '未知')} 条",
        f"题名/摘要排除 {title_abstract.get('excluded', '未知')} 条",
        f"题名/摘要待定 {title_abstract.get('pending', '未知')} 条",
        f"全文纳入 {fulltext.get('included', '未知')} 条",
    ]
    return "；".join(lines) + "。"


def build_chapter_messages(
    question: str,
    sources: list[dict[str, Any]],
    chapter_title: str,
    *,
    prisma: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    fulltext = [item for item in sources if item["evidence_level"] == "fulltext"]
    confirmed = [item for item in fulltext if item["evidence_confirmed"]]
    pending = [item for item in fulltext if not item["evidence_confirmed"]]
    abstract = [item for item in sources if item["evidence_level"] != "fulltext"]
    pending_rule = (
        "其中 "
        + "、".join(item["key"] for item in pending)
        + " 的全文提取记录未经人工确认，涉及它们时只能写「该文提取记录显示……」，"
        "不得表述为已核实的事实或定论。\n"
        if pending
        else ""
    )
    digest = prisma_digest(prisma)
    digest_block = (
        f"检索与筛选计数（必须原样写进「{SECTION_SCOPE}」小节，不得改写数字）：{digest}\n"
        if digest
        else f"检索与筛选计数：本次未提供，请勿编造任何数量，只写来源清单规模。\n"
    )
    guidance = (
        f"章节标题：{chapter_title}\n"
        f"研究问题：{question}\n\n"
        f"来源清单共 {len(sources)} 篇：全文已核验（人工确认）{len(confirmed)} 篇，"
        f"全文已提取但未经人工确认 {len(pending)} 篇，仅题摘 {len(abstract)} 篇。\n"
        f"{pending_rule}"
        f"{digest_block}\n"
        "输出结构（严格按下列小节，标题用「## 」开头，标题内不要写序号）：\n"
        f"## {SECTION_SCOPE}\n"
        "1 段。写清检索与筛选计数、述评口径（只对全文证据作实质引证；未经人工确认的提取记录须标明为提取记录；"
        "仅题摘文献只描述研究取向），以及本章不覆盖的内容。\n"
        "## 主题：<主题名>\n"
        "2–4 个，按证据实际分组，不要凑数。每节先陈述文献做了什么，再评其证据强度与边界。\n"
        f"## {SECTION_GAPS}\n"
        "明确指出哪些问题尚无来源可回答，用「（证据缺口：……）」句式。\n"
        f"## {SECTION_SUMMARY}\n"
        "落到对研究设计的约束，不要空泛总结。\n\n"
        "不要生成表格；不要写「检索中发现但未纳入的线索」小节（由系统另行补）。\n"
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


def clean_heading(value: str) -> str:
    """去掉标题里的序号与尾随标点，只保留文字。

    拼装时统一重新编号，所以「2.1 述评口径」与「述评口径」视为同一标题，
    用户在页面里改名也不会把编号改乱。
    """
    text = NUMBERING_RE.sub("", str(value or "").strip())
    return text.strip(" ：:、.。　") or "未命名小节"


def split_sections(text: str) -> list[dict[str, str]]:
    """把模型输出的 Markdown 切成可逐节编辑的小节。

    只把一级/二级标题当小节边界，`###` 视为小节内部的层级，避免把
    一段正文切碎。第一个标题之前的内容归入「导语」。
    """
    body = _strip_fence(text)
    sections: list[dict[str, str]] = []
    heading = ""
    buffer: list[str] = []

    def flush() -> None:
        content = "\n".join(buffer).strip()
        if heading or content:
            sections.append({"heading": clean_heading(heading) if heading else "导语", "body": content})

    for line in body.splitlines():
        match = SECTION_HEADING_RE.match(line)
        if match:
            flush()
            heading = match.group(2)
            buffer = []
        else:
            buffer.append(line)
    flush()
    return [item for item in sections if item["body"] or item["heading"] != "导语"]


def build_citation_table_rows(
    sources: list[dict[str, Any]],
    check: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """表 2-1：逐篇列出证据级别与「可支持的陈述」边界。"""
    cited = set((check or {}).get("cited") or [])
    rows: list[dict[str, str]] = []
    for source in sources:
        label = evidence_level_label(source)
        if label == CONFIRMED_LABEL:
            support = "已人工核验，可作实质引证（结论仍须回原文核对）"
        elif label == UNCONFIRMED_LABEL:
            support = "提取记录未经人工确认，只能写作「该文提取记录显示……」"
        else:
            support = "仅有题摘，只能陈述摘要层面的研究取向，不得描述方法细节或数值"
        rows.append(
            {
                "编号": source["key"],
                "文献": citation_label(source),
                "证据级别": label,
                "本次是否引用": "是" if source["key"] in cited else "否",
                "可支持的陈述": support,
            }
        )
    return rows


def build_gap_table_rows(
    sources: list[dict[str, Any]],
    prisma: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """表 2-2：把证据结构上的缺口列成「现状 → 补足路径」。"""
    total = len(sources)
    fulltext = [item for item in sources if item["evidence_level"] == "fulltext"]
    confirmed = [item for item in fulltext if item["evidence_confirmed"]]
    quantified = [item for item in fulltext if QUANTITY_RE.search(str(item.get("findings") or ""))]
    rows = [
        {
            "研究空白": "可核验的全文证据数量有限",
            "证据现状": f"纳入 {total} 篇，全文 {len(fulltext)} 篇，其中人工确认 {len(confirmed)} 篇",
            "补足路径": "补齐全文并在证据矩阵完成人工校正后再作实质引证",
        },
        {
            "研究空白": "部分纳入文献停留在题摘层面",
            "证据现状": f"{total - len(fulltext)} 篇仅有题摘，占纳入文献的 {round((total - len(fulltext)) * 100 / total) if total else 0}%",
            "补足路径": "获取原文核验；在正文中降级为「摘要提到」的表述",
        },
        {
            "研究空白": "证据中的经济量化指标是否充分",
            "证据现状": f"全文证据中含金额、单价、比例或工程量的 {len(quantified)} 篇",
            "补足路径": "若为 0，本文需自行建立量化方案，或明示只作定性比较",
        },
    ]
    title_abstract = (prisma or {}).get("title_abstract") or {}
    if title_abstract.get("pending"):
        rows.append(
            {
                "研究空白": "筛选尚未完全收敛",
                "证据现状": f"题名/摘要仍有 {title_abstract['pending']} 条待定",
                "补足路径": "完成剩余裁决后，再确认纳入集合是否稳定",
            }
        )
    return rows


def build_number_table_rows(
    prisma: dict[str, Any] | None,
    sources: list[dict[str, Any]],
    check: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """表 2-3：正文里出现的每个数字都给出处，方便逐格回查。"""
    rows: list[dict[str, str]] = []
    if prisma:
        title_abstract = prisma.get("title_abstract") or {}
        fulltext = prisma.get("fulltext") or {}
        pairs = [
            ("识别记录", prisma.get("identified")),
            ("去重移除", prisma.get("duplicates_removed")),
            ("题名/摘要纳入", title_abstract.get("included")),
            ("题名/摘要排除", title_abstract.get("excluded")),
            ("题名/摘要待定", title_abstract.get("pending")),
            ("全文纳入（进入报告）", fulltext.get("included")),
        ]
        for label, value in pairs:
            rows.append(
                {
                    "指标": label,
                    "数值": "" if value is None else str(value),
                    "出处": "工作台 PRISMA（screening_candidates / screening_fulltext）",
                }
            )
    rows.append(
        {
            "指标": "本章纳入来源数",
            "数值": str(len(sources)),
            "出处": "本页来源清单（题摘裁决为纳入）",
        }
    )
    rows.append(
        {
            "指标": "全文证据条数",
            "数值": str(sum(1 for item in sources if item["evidence_level"] == "fulltext")),
            "出处": "证据矩阵版本（取最高优先级版本）",
        }
    )
    if check is not None:
        rows.append(
            {
                "指标": "正文引证次数",
                "数值": str(check.get("citation_count", 0)),
                "出处": "本页引证核查",
            }
        )
    return rows


def excluded_lead_options(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """列出可被「未纳入线索」小节提及的排除记录。"""
    options: list[dict[str, Any]] = []
    for candidate in candidates:
        if str(candidate.get("status") or "") != "excluded":
            continue
        key = str(candidate.get("candidate_key") or "")
        if not key:
            continue
        year = candidate.get("year")
        try:
            year = int(year) if year is not None else ""
        except (TypeError, ValueError):
            year = ""
        options.append(
            {
                "key": key,
                "title": str(candidate.get("title") or ""),
                "year": year,
                "reason": str(candidate.get("exclusion_reason") or "未记录"),
            }
        )
    options.sort(key=lambda item: (item["reason"], item["title"]))
    return options


def build_excluded_lead_text(
    items: list[dict[str, Any]],
    excluded_total: int,
) -> str:
    """生成「检索中发现但未纳入的线索」段落。

    这段刻意不由模型写：模型看不到被排除的文献，让它写必然编造。
    只做检索充分性陈述，不给任何结论，也不出现 [Cxxx] 编号。
    """
    if not items:
        return ""
    lines = [
        f"检索与筛选过程中另有 {excluded_total} 条记录依纳排标准被排除。"
        "其中与本章主题邻近、但因下列原因未进入证据集的有："
    ]
    for item in items:
        year = f"（{item['year']}）" if item.get("year") else ""
        lines.append(f"- 《{item['title']}》{year}，排除原因：{item['reason']}。")
    lines.append(
        "上述记录未取得全文证据，本章仅在检索充分性层面提及，不引用其结论，也不计入证据集。"
    )
    return "\n".join(lines)


def new_section(heading: str, body: str = "", *, section_id: str | None = None) -> dict[str, str]:
    return {
        "id": section_id or f"sec-{uuid.uuid4().hex[:8]}",
        "heading": heading,
        "body": body,
    }


def sections_from_markdown(text: str) -> list[dict[str, str]]:
    parsed = split_sections(text)
    return [new_section(item["heading"], item["body"]) for item in parsed]


def _cell(value: Any) -> str:
    text = str("" if value is None else value).replace("|", "／").strip()
    return re.sub(r"\s*\n\s*", " ", text)


def table_markdown(table: dict[str, Any], number: int) -> str:
    rows = table.get("rows") or []
    columns = table.get("columns") or (list(rows[0].keys()) if rows else [])
    lines = [f"**表 2-{number}　{table.get('title') or ''}**", ""]
    if not columns:
        return "\n".join(lines).strip()
    lines.append("| " + " | ".join(str(column) for column in columns) + " |")
    lines.append("|" + "|".join(["---"] * len(columns)) + "|")
    for row in rows:
        lines.append("| " + " | ".join(_cell(row.get(column, "")) for column in columns) + " |")
    return "\n".join(lines)


def assemble_chapter(
    sections: list[dict[str, Any]],
    tables: list[dict[str, Any]] | None = None,
    *,
    intro: str = "",
) -> str:
    """把小节与表格拼成最终章节 Markdown，编号按顺序重新生成。

    没有小节时只返回导语：一张孤零零的表格不构成章节。
    """
    tables = tables or []
    anchored: dict[str, list[dict[str, Any]]] = {}
    if not sections:
        return intro.strip()
    for table in tables:
        anchored.setdefault(str(table.get("anchor") or "end"), []).append(table)
    parts: list[str] = []
    if intro.strip():
        parts.append(intro.strip())
    number = 0
    for index, section in enumerate(sections, 1):
        heading = str(section.get("heading") or f"小节 {index}").strip()
        body = str(section.get("body") or "").strip()
        parts.append(f"## 2.{index} {heading}")
        if body:
            parts.append(body)
        for table in anchored.get(str(section.get("id")), []):
            number += 1
            parts.append(table_markdown(table, number))
    for table in anchored.get("end", []):
        number += 1
        parts.append(table_markdown(table, number))
    return "\n\n".join(part for part in parts if part).strip()


def draft_to_json(state: dict[str, Any]) -> bytes:
    payload = {
        "format": "sciverse-review-chapter-draft",
        "format_version": 1,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project_id": state.get("project_id", ""),
        "chapter_title": state.get("chapter_title", ""),
        "question": state.get("question", ""),
        "sections": state.get("sections") or [],
        "tables": state.get("tables") or [],
        "leads": state.get("leads") or [],
        "sources": [
            {
                "key": item.get("key"),
                "candidate_key": item.get("candidate_key"),
                "authors": item.get("authors", ""),
                "venue": item.get("venue", ""),
                "year": item.get("year", ""),
            }
            for item in state.get("sources") or []
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")


def draft_from_json(raw: bytes | str) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (TypeError, ValueError) as error:
        raise ValueError("草稿文件不是有效的 JSON。") from error
    if not isinstance(payload, dict) or payload.get("format") != "sciverse-review-chapter-draft":
        raise ValueError("草稿文件格式不符。")
    sections = payload.get("sections") or []
    if not isinstance(sections, list):
        raise ValueError("草稿文件的小节结构损坏。")
    normalized = []
    for item in sections:
        if not isinstance(item, dict):
            continue
        normalized.append(
            new_section(
                str(item.get("heading") or "未命名小节"),
                str(item.get("body") or ""),
                section_id=str(item.get("id") or "") or None,
            )
        )
    return {
        "chapter_title": str(payload.get("chapter_title") or ""),
        "question": str(payload.get("question") or ""),
        "sections": normalized,
        "tables": [table for table in (payload.get("tables") or []) if isinstance(table, dict)],
        "leads": payload.get("leads") or [],
        "sources": [item for item in (payload.get("sources") or []) if isinstance(item, dict)],
    }


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
    uncited_paragraphs: list[str] = []
    current_heading = ""
    for part in paragraphs:
        lines = part.splitlines()
        matched = SECTION_HEADING_RE.match(lines[0]) if lines else None
        if matched:
            current_heading = clean_heading(matched.group(2))
            part = "\n".join(lines[1:]).strip()
            if not part:
                continue
        if HEADING_RE.match(part) or TABLE_RE.match(part) or TABLE_CAPTION_RE.match(part):
            continue
        if CITATION_RE.search(part):
            continue
        # 「未纳入线索」按设计不含引证，只做检索充分性陈述，不能当成漏引报警。
        if current_heading == SECTION_LEADS:
            continue
        uncited_paragraphs.append(part)
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
            "文献": citation_label(source),
            "证据级别": evidence_level_label(source),
            "证据版本": EVIDENCE_SOURCE_LABELS.get(
                source["evidence_source"], source["evidence_source"] or "未标注"
            ),
            "是否被引用": "是" if source["key"] in cited else "否",
            "引证是否合法": "否" if source["key"] in set(check.get("invalid") or []) else "是",
        }
        for source in sources
    ]


def export_citation_report_csv(sources: list[dict[str, Any]], check: dict[str, Any]) -> bytes:
    rows = citation_report_rows(sources, check)
    fields = list(rows[0].keys()) if rows else [
        "编号",
        "候选键",
        "题名",
        "文献",
        "证据级别",
        "证据版本",
        "是否被引用",
        "引证是否合法",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
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
    human_edited: bool = False,
) -> bytes:
    header = (
        f"<!-- model: {model} | prompt_version: {CHAPTER_PROMPT_VERSION} | "
        f"generated_at: {datetime.now(timezone.utc).isoformat(timespec='seconds')} | "
        f"project_id: {project_id} | sources: {source_count} (fulltext: {fulltext_count}) | "
        f"human_edited: {'yes' if human_edited else 'no'} -->\n\n"
    )
    status = (
        "本文由 AI 起草并经人工在页面内逐节修改、补表，仍须逐条核对来源后再进入论文。"
        if human_edited
        else "本文为 AI 辅助生成的综述章节草稿，已在生成时做引证范围校验，"
        "但仍须逐条核对来源后再进入论文；未经人工确认不得作为最终稿。"
    )
    meta = (
        f"> {status}\n\n"
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
    prisma: dict[str, Any] | None = None,
    max_tokens: int = 8192,
) -> tuple[str, dict[str, Any]]:
    if not str(question or "").strip():
        raise ValueError("研究问题不能为空。")
    if not sources:
        raise ValueError("没有已纳入的文献，无法生成综述章节。")
    if not any(source["evidence_level"] == "fulltext" for source in sources):
        raise ValueError(
            "没有任何全文证据，生成章节只会是空话。"
            "请先在证据矩阵中补齐全文，或继续使用「综述提纲」。"
        )
    raw = client.chat(
        build_chapter_messages(
            question.strip(),
            sources,
            chapter_title.strip() or "第 2 章 文献综述",
            prisma=prisma,
        ),
        max_tokens=max_tokens,
    )
    chapter = _strip_fence(raw)
    if not chapter:
        raise AIError("AI 返回了空的章节内容。")
    return chapter, check_citations(chapter, sources)
