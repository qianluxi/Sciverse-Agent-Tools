from research_app.field_scan import scan_documents


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


def test_scan_documents_isolates_batch_error():
    client = FakeClient(["not json"])

    result = scan_documents(client, _documents())

    assert all(item["error"] for item in result["items"])
    assert result["themes"] == []
    assert result["gaps"] == []
    assert result["aggregation_error"] is None
