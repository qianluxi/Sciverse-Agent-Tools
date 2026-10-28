from research_app.field_scan import scan_documents, scan_report_markdown


class FakeClient:
    model = "test-model"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages, *, max_tokens=2048):
        self.calls.append((messages, max_tokens))
        return self.responses.pop(0)


def _documents():
    return [
        {
            "paper_id": "paper:a",
            "title": "Classic shear wall study",
            "year": 2002,
            "text": "This paper studies shear wall behavior and reports a 10% cost reduction.",
        },
        {
            "paper_id": "paper:b",
            "title": "Recent steel plate shear wall tests",
            "year": 2024,
            "text": "Cyclic tests of steel plate shear walls are reported.",
        },
    ]


def test_scan_documents_classifies_summarizes_and_finds_gaps():
    client = FakeClient(
        [
            '{"papers":['
            '{"paper_id":"paper:a","category":"foundational",'
            '"core_summary":"经典研究。","rationale":"早期奠基工作。","confidence":"high",'
            '"evidence_quotes":[{"quote":"reports a 10% cost reduction","section":"摘要"}]},'
            '{"paper_id":"paper:b","category":"recent_progress",'
            '"core_summary":"近期试验研究。","rationale":"近年进展。","confidence":"medium",'
            '"evidence_quotes":[{"quote":"Cyclic tests of steel plate shear walls","section":"标题"}]}'
            ']}',
            '{"themes":[{"theme":"剪力墙","summary":"涵盖经典与近期研究。",'
            '"paper_ids":["paper:a","paper:b"]}],'
            '"gaps":[{"gap":"缺少长期性能数据。","evidence":"当前研究以短期试验为主。",'
            '"paper_ids":["paper:b"]}]}',
        ]
    )

    result = scan_documents(client, _documents())

    assert len(result["items"]) == 2
    assert result["items"][0]["data"]["category"] == "foundational"
    assert result["items"][0]["quote_checks"][0]["verified"] is True
    assert result["items"][1]["data"]["category"] == "recent_progress"
    assert result["themes"][0]["theme"] == "剪力墙"
    assert result["gaps"][0]["gap"].startswith("缺少")
    assert result["aggregation_error"] is None


def test_scan_documents_verifies_quotes_against_the_referenced_section():
    documents = [
        {
            "paper_id": "paper:title",
            "title": "Unique title phrase for shear walls",
            "year": 2024,
            "text": "The abstract contains a different sentence.",
        }
    ]
    client = FakeClient(
        [
            '{"papers":[{"paper_id":"paper:title","category":"recent_progress",'
            '"core_summary":"标题研究。","rationale":"标题命中。","confidence":"high",'
            '"evidence_quotes":['
            '{"quote":"Unique title phrase","section":"标题"},'
            '{"quote":"Unique title phrase","section":"摘要"},'
            '{"quote":"Unique title phrase","section":""}]}]}',
            '{"themes":[],"gaps":[]}',
        ]
    )

    result = scan_documents(client, documents)

    checks = result["items"][0]["quote_checks"]
    assert checks[0]["verified"] is True
    assert checks[1]["verified"] is False
    assert checks[2]["verified"] is True


def test_scan_documents_isolates_batch_error():
    client = FakeClient(["not json"])

    result = scan_documents(client, _documents())

    assert all(item["error"] for item in result["items"])
    assert result["themes"] == []
    assert result["gaps"] == []
    assert result["aggregation_error"] is None


def test_scan_report_markdown_renders_tables_and_escapes_cells():
    saved = {
        "task": {
            "created_at": "2026-10-28 09:00:00",
            "status": "completed",
            "model": "test-model",
            "prompt_version": "field-scan-v1",
        },
        "aggregation_error": "summary failed",
        "items": [
            {
                "paper_id": "paper:a",
                "title": "Steel | plate wall",
                "year": 2024,
                "data": {
                    "category": "recent_progress",
                    "core_summary": "Line one\nLine two <script>",
                    "rationale": "Recent tests",
                    "confidence": "high",
                    "evidence_quotes": [
                        {"quote": "Cyclic tests | validated", "section": "abstract"}
                    ],
                },
                "quote_checks": [{"verified": True}],
                "reviewed": True,
                "reviewer_note": "Keep",
                "error": None,
            },
            {
                "paper_id": "paper:b",
                "title": "Broken paper",
                "year": 2020,
                "data": None,
                "quote_checks": [],
                "reviewed": False,
                "reviewer_note": "",
                "error": "Model returned invalid JSON",
            },
        ],
        "themes": [
            {
                "theme": "Shear walls",
                "summary": "Theme summary",
                "paper_ids": ["paper:a"],
            }
        ],
        "gaps": [
            {
                "gap": "Long-term data",
                "evidence": "Limited follow-up",
                "paper_ids": ["paper:a", "paper:b"],
            }
        ],
    }

    report = scan_report_markdown(
        saved,
        project_name="Wall project",
        project_id="project:1",
        generated_at="2026-10-28 10:00:00",
    )

    assert report.startswith("# AI 领域扫描报告")
    assert "| 任务状态 | completed |" in report
    assert "| 汇总状态 | 失败：summary failed |" in report
    assert "| 近年新进展 | 1 | 1 |" in report
    assert "Steel \\| plate wall" in report
    assert "Line one<br>Line two &lt;script&gt;" in report
    assert "Cyclic tests \\| validated" in report
    assert "| Shear walls | Theme summary | P1 |" in report
    assert "| Long-term data | Limited follow-up | P1、P2 |" in report
    assert "## 处理失败的文献" in report
    assert "Model returned invalid JSON" in report
    assert "| P1 | 2024 |" in report


def test_scan_report_markdown_escapes_markdown_syntax():
    saved = {
        "task": {
            "created_at": "2026-10-28 09:00:00",
            "model": "test-model",
            "prompt_version": "field-scan-v1",
        },
        "items": [
            {
                "paper_id": "paper:a",
                "title": "[link](https://example.com) ![img](x) `code`",
                "year": 2024,
                "data": {
                    "category": "method",
                    "core_summary": "_italic_",
                    "rationale": "plain",
                    "confidence": "medium",
                    "evidence_quotes": [],
                },
                "quote_checks": [],
                "reviewed": False,
                "reviewer_note": "",
                "error": None,
            }
        ],
        "themes": [],
        "gaps": [],
    }

    report = scan_report_markdown(saved, generated_at="2026-10-28 10:00:00")

    assert "\\[link\\](https://example.com)" in report
    assert "\\!\\[img\\](x)" in report
    assert "\\`code\\`" in report
    assert "\\_italic\\_" in report
