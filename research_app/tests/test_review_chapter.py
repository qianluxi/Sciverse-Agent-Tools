import csv
import io

import pytest

from research_app.ai import AIError
from research_app.review_chapter import (
    build_chapter_messages,
    build_sources,
    check_citations,
    check_summary,
    citation_report_rows,
    export_citation_report_csv,
    generate_review_chapter,
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
    assert sources[1]["evidence_level"] == "abstract"
    assert sources[1]["findings"] == ""


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
    assert "只对全文核验文献作实质引证" in prompt
    assert "摘要文本" in prompt


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
    assert rows[1]["是否被引用"] == "否"

    data = export_citation_report_csv(sources, check)
    assert data.startswith(b"\xef\xbb\xbf")
    parsed = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
    assert len(parsed) == 2
    assert parsed[0]["编号"] == "C001"
