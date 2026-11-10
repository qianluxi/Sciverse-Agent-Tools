import csv
import io

import pytest

from research_app.ai import AIError
from research_app.review_chapter import (
    assemble_chapter,
    attach_bibliography,
    build_chapter_messages,
    build_citation_table_rows,
    build_excluded_lead_text,
    build_gap_table_rows,
    build_number_table_rows,
    build_sources,
    check_citations,
    check_summary,
    citation_label,
    citation_report_rows,
    draft_from_json,
    draft_to_json,
    evidence_level_label,
    excluded_lead_options,
    export_citation_report_csv,
    generate_review_chapter,
    split_sections,
)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages, *, max_tokens=1200):
        self.calls.append((messages, max_tokens))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _candidate(key, *, status="included", title=None, abstract="摘要文本"):
    return {
        "candidate_key": key,
        "paper_id": f"paper:{key}",
        "status": status,
        "title": title or f"Title {key}",
        "year": 2020,
        "doi": f"10.1000/{key}",
        "abstract": abstract,
    }


def _matrix(key, *, findings="该文报告了成本下降", version="ai_raw", methods="数值模型"):
    return {
        "paper_id": f"paper:{key}",
        "title": f"Title {key}",
        "year": 2020,
        "versions": {
            version: {
                "data": {
                    "methods": methods,
                    "findings": findings,
                    "limitations": "样本有限",
                    "evidence_quotes": [{"quote": "quote one"}],
                },
                "quote_checks": [{"quote": "quote one", "verified": True}],
            }
        },
    }


def test_build_sources_filters_excluded_and_renumbers():
    candidates = [
        _candidate("uid:b"),
        _candidate("uid:a"),
        _candidate("uid:c", status="excluded"),
    ]
    sources = build_sources([_matrix("uid:a")], candidates)

    assert [item["key"] for item in sources] == ["C001", "C002"]
    assert [item["candidate_key"] for item in sources] == ["uid:a", "uid:b"]
    assert sources[0]["evidence_level"] == "fulltext"
    assert sources[0]["evidence_confirmed"] is False
    assert sources[1]["evidence_level"] == "abstract"
    assert sources[1]["findings"] == ""


def test_ai_raw_evidence_is_not_labelled_as_verified():
    """AI 原始提取只代表有提取结果，不等于人工核验过。"""
    sources = build_sources([_matrix("uid:a", version="ai_raw")], [_candidate("uid:a")])

    assert sources[0]["evidence_level"] == "fulltext"
    assert sources[0]["evidence_confirmed"] is False
    assert evidence_level_label(sources[0]) == "全文已提取（待确认）"


@pytest.mark.parametrize("version", ["human_corrected", "final_adopted"])
def test_human_reviewed_evidence_is_labelled_as_verified(version):
    sources = build_sources([_matrix("uid:a", version=version)], [_candidate("uid:a")])

    assert sources[0]["evidence_confirmed"] is True
    assert evidence_level_label(sources[0]) == "全文已核验"


def test_abstract_source_label_is_abstract_only():
    sources = build_sources([], [_candidate("uid:a")])

    assert evidence_level_label(sources[0]) == "仅题摘"


def test_build_sources_prefers_final_adopted_version():
    paper = {
        "paper_id": "paper:uid:a",
        "title": "Title",
        "year": 2020,
        "versions": {
            "ai_raw": {"data": {"findings": "AI 原始"}, "quote_checks": []},
            "human_corrected": {"data": {"findings": "人工校正"}, "quote_checks": []},
            "final_adopted": {"data": {"findings": "最终采用"}, "quote_checks": []},
        },
    }
    sources = build_sources([paper], [_candidate("uid:a")])

    assert sources[0]["findings"] == "最终采用"
    assert sources[0]["evidence_source"] == "final_adopted"


def test_chapter_messages_expose_methods_only_for_fulltext_sources():
    candidates = [_candidate("uid:a"), _candidate("uid:b")]
    sources = build_sources([_matrix("uid:a", methods="PLAXIS 二维模型")], candidates)

    messages = build_chapter_messages("埋深如何影响造价", sources, "第 2 章 文献综述")
    prompt = messages[-1]["content"]

    assert "PLAXIS 二维模型" in prompt
    assert "仅题摘" in prompt
    assert "只对全文证据作实质引证" in prompt
    assert "摘要文本" in prompt


def test_chapter_messages_flag_unconfirmed_evidence():
    """未人工确认的全文证据要进提示词，并要求模型写成「提取记录」而非定论。"""
    candidates = [_candidate("uid:a"), _candidate("uid:b")]
    sources = build_sources([_matrix("uid:a"), _matrix("uid:b", version="human_corrected")], candidates)

    prompt = build_chapter_messages("问题", sources, "第 2 章")[-1]["content"]

    assert "全文已提取但未经人工确认 1 篇" in prompt
    assert "全文已核验（人工确认）1 篇" in prompt
    assert "其中 C001 的全文提取记录未经人工确认" in prompt
    assert "该文提取记录显示" in prompt
    assert '"证据版本": "AI 原始"' in prompt
    assert '"证据版本": "人工校正"' in prompt


def test_check_citations_flags_invalid_and_missing_fulltext():
    candidates = [_candidate("uid:a"), _candidate("uid:b")]
    sources = build_sources([_matrix("uid:a"), _matrix("uid:b")], candidates)
    text = "### 2.1 结构\n\n已有研究比较了三种楼盖体系 [C001]，另有研究给出沉降预测 [C099]。"

    check = check_citations(text, sources)

    assert check["cited"] == ["C001", "C099"]
    assert check["invalid"] == ["C099"]
    assert check["uncited_fulltext"] == ["C002"]
    assert check["ok"] is False


def test_check_citations_flags_abstract_only_reference():
    candidates = [_candidate("uid:a"), _candidate("uid:b")]
    sources = build_sources([_matrix("uid:a")], candidates)
    text = "设计要点类文献提出以经济成本施工 [C001][C002]。"

    check = check_citations(text, sources)

    assert check["abstract_only_cited"] == ["C002"]
    assert check["invalid"] == []


def test_check_citations_counts_uncited_paragraphs():
    sources = build_sources([_matrix("uid:a")], [_candidate("uid:a")])
    text = "### 2.1\n\n这一段没有任何引证。\n\n这一段有引证 [C001]。"

    check = check_citations(text, sources)

    assert len(check["uncited_paragraphs"]) == 1
    assert "没有任何引证" in check["uncited_paragraphs"][0]


def test_check_summary_reports_invalid_first():
    sources = build_sources([_matrix("uid:a")], [_candidate("uid:a")])
    check = check_citations("正文 [C999]", sources)

    messages = check_summary(check)

    assert "越界引证" in messages[0]
    assert "C999" in messages[0]


def test_generate_rejects_when_no_fulltext_evidence():
    sources = build_sources([], [_candidate("uid:a")])
    client = FakeClient(["不应被调用"])

    with pytest.raises(ValueError, match="全文"):
        generate_review_chapter(client, "问题", sources)
    assert client.calls == []


def test_generate_rejects_empty_question():
    sources = build_sources([_matrix("uid:a")], [_candidate("uid:a")])

    with pytest.raises(ValueError, match="研究问题"):
        generate_review_chapter(FakeClient(["x"]), "  ", sources)


def test_generate_returns_chapter_and_citation_check():
    sources = build_sources([_matrix("uid:a")], [_candidate("uid:a")])
    client = FakeClient(["```markdown\n### 2.1 结构体系\n\n该文报告成本下降 [C001]。\n```"])

    chapter, check = generate_review_chapter(client, "埋深如何影响造价", sources)

    assert chapter.startswith("### 2.1")
    assert not chapter.endswith("```")
    assert check["invalid"] == []
    assert check["cited"] == ["C001"]
    assert client.calls[0][1] == 8192


def test_generate_rejects_empty_model_output():
    sources = build_sources([_matrix("uid:a")], [_candidate("uid:a")])

    with pytest.raises(AIError):
        generate_review_chapter(FakeClient(["   "]), "问题", sources)


def test_citation_report_and_csv_include_candidate_key():
    candidates = [_candidate("uid:a"), _candidate("uid:b")]
    sources = build_sources([_matrix("uid:a")], candidates)
    check = check_citations("正文 [C001]", sources)

    rows = citation_report_rows(sources, check)
    assert rows[0]["候选键"] == "uid:a"
    assert rows[0]["证据版本"] == "AI 原始"
    assert rows[1]["是否被引用"] == "否"

    data = export_citation_report_csv(sources, check)
    assert data.startswith(b"\xef\xbb\xbf")
    parsed = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
    assert len(parsed) == 2
    assert parsed[0]["编号"] == "C001"
    assert parsed[0]["证据级别"] == "全文已提取（待确认）"


# --------------------------------------------------------------------------
# 页面内编辑所需的能力：书目回填、分节、表格、线索、拼装、草稿往返
# --------------------------------------------------------------------------
def _paper(key, *, authors=("张三", "李四"), venue="地下空间与工程学报", year=2019, doi=None):
    return {
        "doi": doi if doi is not None else f"10.1000/{key}",
        "title": f"Title {key}",
        "author": [{"name": name} for name in authors],
        "publication_venue_name_unified": venue,
        "publication_published_year": year,
        "citation_count": 7,
    }


def test_attach_bibliography_fills_authors_and_venue_by_doi():
    sources = build_sources([_matrix("uid:a")], [_candidate("uid:a")])

    attach_bibliography(sources, [_paper("uid:a")])

    assert sources[0]["authors"] == "张三, 李四"
    assert sources[0]["venue"] == "地下空间与工程学报"
    # 候选已有年份时不覆盖，避免把题摘记录年份改掉
    assert sources[0]["year"] == 2020
    assert citation_label(sources[0]) == "张三 等，2020. 地下空间与工程学报"
    assert sources[0]["citation_count"] == 7


def test_attach_bibliography_falls_back_to_normalized_title():
    sources = build_sources([], [_candidate("uid:a")])
    paper = _paper("uid:a", doi="10.9999/other")

    attach_bibliography(sources, [paper])

    assert sources[0]["authors"] == "张三, 李四"


def test_attach_bibliography_keeps_user_filled_values():
    sources = build_sources([], [_candidate("uid:a")])
    sources[0]["authors"] = "手工补的作者"
    sources[0]["venue"] = "手工补的期刊"

    attach_bibliography(sources, [_paper("uid:a")])

    assert sources[0]["authors"] == "手工补的作者"
    assert sources[0]["venue"] == "手工补的期刊"


def test_citation_label_degrades_gracefully():
    assert citation_label({}) == "（书目待补）"
    assert citation_label({"authors": "王五", "year": 2018}) == "王五，2018"


def test_split_sections_keeps_h3_inside_body():
    text = (
        "## 2.1 述评口径与检索边界\n\n第一段 [C001]\n\n### 子标题\n\n细节 [C002]\n\n"
        "## 主题：结构体系\n\n第二段 [C003]"
    )

    sections = split_sections(text)

    assert [item["heading"] for item in sections] == ["述评口径与检索边界", "主题：结构体系"]
    assert "### 子标题" in sections[0]["body"]
    assert sections[1]["body"] == "第二段 [C003]"


def test_split_sections_treats_leading_text_as_intro():
    sections = split_sections("开头一段说明。\n\n## 研究空白\n\n缺口 [C001]")

    assert sections[0]["heading"] == "导语"
    assert "开头一段说明" in sections[0]["body"]
    assert sections[1]["heading"] == "研究空白"


def test_assemble_chapter_renumbers_and_places_tables():
    sections = [
        {"id": "s1", "heading": "述评口径", "body": "正文 [C001]"},
        {"id": "s2", "heading": "研究空白", "body": "缺口"},
    ]
    tables = [
        {
            "id": "t1",
            "title": "引证表",
            "columns": ["编号", "说明"],
            "rows": [{"编号": "C001", "说明": "a|b"}],
            "anchor": "s1",
        },
        {
            "id": "t2",
            "title": "数字表",
            "columns": ["指标"],
            "rows": [{"指标": "458"}],
            "anchor": "end",
        },
    ]

    text = assemble_chapter(sections, tables, intro="> 导语")

    assert text.startswith("> 导语")
    assert "## 2.1 述评口径" in text
    assert "## 2.2 研究空白" in text
    assert "a／b" in text  # 表格单元里的竖线必须转义，否则会撑坏表格
    assert text.index("**表 2-1") < text.index("## 2.2")
    assert text.index("**表 2-2") > text.index("## 2.2")


def test_assemble_chapter_without_sections_is_empty():
    assert assemble_chapter([], [{"id": "t", "title": "表", "columns": ["a"], "rows": [{"a": "1"}]}]) == ""


def test_citation_table_rows_state_what_each_source_can_support():
    candidates = [_candidate("uid:a"), _candidate("uid:b")]
    sources = build_sources(
        [_matrix("uid:a", version="human_corrected")],
        candidates,
    )

    rows = build_citation_table_rows(sources, check_citations("正文 [C001]", sources))

    assert rows[0]["证据级别"] == "全文已核验"
    assert "实质引证" in rows[0]["可支持的陈述"]
    assert rows[0]["本次是否引用"] == "是"
    assert rows[1]["证据级别"] == "仅题摘"
    assert "摘要层面" in rows[1]["可支持的陈述"]


def test_gap_table_rows_count_quantified_and_pending():
    sources = build_sources(
        [_matrix("uid:a", findings="单方造价降低 12%", version="human_corrected")],
        [_candidate("uid:a"), _candidate("uid:b")],
    )
    prisma = {"title_abstract": {"pending": 5}}

    rows = build_gap_table_rows(sources, prisma)

    quantified = next(row for row in rows if "量化指标" in row["研究空白"])
    assert "1 篇" in quantified["证据现状"]
    assert any("待定" in row["证据现状"] for row in rows)


def test_number_table_rows_carry_source_of_each_number():
    sources = build_sources([_matrix("uid:a")], [_candidate("uid:a")])
    prisma = {
        "identified": 458,
        "duplicates_removed": 0,
        "title_abstract": {"included": 7, "excluded": 104, "pending": 347},
        "fulltext": {"included": 2},
    }

    rows = build_number_table_rows(prisma, sources, check_citations("正文 [C001]", sources))

    values = {row["指标"]: row["数值"] for row in rows}
    assert values["识别记录"] == "458"
    assert values["题名/摘要待定"] == "347"
    assert values["正文引证次数"] == "1"
    assert all(row["出处"] for row in rows)


def test_excluded_lead_text_mentions_records_without_citing_them():
    candidates = [
        _candidate("uid:x", status="excluded", title="某个被排除的研究"),
        _candidate("uid:y", status="included"),
    ]
    candidates[0]["exclusion_reason"] = "主题不相关"

    options = excluded_lead_options(candidates)
    text = build_excluded_lead_text(options, 104)

    assert [item["key"] for item in options] == ["uid:x"]
    assert "某个被排除的研究" in text
    assert "主题不相关" in text
    assert "104 条" in text
    assert "[C" not in text  # 这段只讲检索充分性，不允许出现引证编号


def test_chapter_messages_include_prisma_counts_and_section_skeleton():
    sources = build_sources([_matrix("uid:a")], [_candidate("uid:a")])
    prisma = {
        "identified": 458,
        "duplicates_removed": 0,
        "title_abstract": {"included": 7, "excluded": 104, "pending": 347},
        "fulltext": {"included": 2},
    }

    prompt = build_chapter_messages("埋深如何影响造价", sources, "第 2 章 文献综述", prisma=prisma)[-1][
        "content"
    ]

    assert "识别记录 458 条" in prompt
    assert "题名/摘要待定 347 条" in prompt
    assert "## 述评口径与检索边界" in prompt
    assert "## 研究空白" in prompt
    assert "不要生成表格" in prompt


def test_draft_json_round_trip_keeps_edits():
    sources = build_sources([_matrix("uid:a")], [_candidate("uid:a")])
    sources[0]["authors"] = "张三 等"
    state = {
        "project_id": "p1",
        "chapter_title": "第 2 章",
        "question": "问题",
        "sections": [{"id": "s1", "heading": "述评口径", "body": "人工改过的正文 [C001]"}],
        "tables": [{"id": "cite", "title": "表", "columns": ["编号"], "rows": [{"编号": "C001"}], "anchor": "end"}],
        "leads": ["uid:x"],
        "sources": sources,
    }

    restored = draft_from_json(draft_to_json(state))

    assert restored["chapter_title"] == "第 2 章"
    assert restored["sections"][0]["body"] == "人工改过的正文 [C001]"
    assert restored["leads"] == ["uid:x"]
    assert restored["sources"][0]["authors"] == "张三 等"


def test_draft_from_json_rejects_foreign_payload():
    with pytest.raises(ValueError, match="格式"):
        draft_from_json(b'{"format": "something-else"}')
    with pytest.raises(ValueError, match="JSON"):
        draft_from_json(b"not json")


def test_gap_table_does_not_count_settlement_as_economic_metric():
    """沉降 mm 是物理量，不是经济指标，不能算作量化经济证据。"""
    sources = build_sources(
        [
            _matrix(
                "uid:a",
                findings="最大土体沉降 20 mm，建筑附加沉降 30 mm，均满足规范；未给出造价指标。",
                version="human_corrected",
            )
        ],
        [_candidate("uid:a")],
    )

    rows = build_gap_table_rows(sources, None)

    quantified = next(row for row in rows if "经济量化指标" in row["研究空白"])
    assert "0 篇" in quantified["证据现状"]


def test_build_sources_keeps_existing_numbers_when_new_source_appears():
    """改过正文之后再新增纳入文献，旧编号不能被重排，否则 [Cxxx] 全体错位。"""
    candidates = [_candidate("uid:b"), _candidate("uid:c")]
    first = build_sources([], candidates)
    key_map = {item["candidate_key"]: item["key"] for item in first}
    assert key_map == {"uid:b": "C001", "uid:c": "C002"}

    candidates.append(_candidate("uid:a"))  # 排序后落在最前
    second = build_sources([], candidates, key_map=key_map)
    numbered = {item["candidate_key"]: item["key"] for item in second}

    assert numbered["uid:b"] == "C001"
    assert numbered["uid:c"] == "C002"
    assert numbered["uid:a"] == "C003"


def test_build_sources_keeps_numbers_after_removal():
    candidates = [_candidate("uid:a"), _candidate("uid:b"), _candidate("uid:c")]
    key_map = {item["candidate_key"]: item["key"] for item in build_sources([], candidates)}

    remaining = build_sources([], [item for item in candidates if item["candidate_key"] != "uid:b"], key_map=key_map)

    assert {item["candidate_key"]: item["key"] for item in remaining} == {"uid:a": "C001", "uid:c": "C003"}


def test_check_citations_ignores_table_captions_and_lead_section():
    sources = build_sources([_matrix("uid:a")], [_candidate("uid:a")])
    text = (
        "## 述评口径\n\n正文有引证 [C001]。\n\n"
        "**表 2-1　纳入文献**\n\n| 编号 | 文献 |\n|---|---|\n| C001 | 甲 |\n\n"
        "## 检索中发现但未纳入的线索\n\n检索与筛选过程中另有 104 条记录被排除。\n\n"
        "- 《某某》排除原因：研究类型不符。"
    )

    check = check_citations(text, sources)

    assert check["uncited_paragraphs"] == []
