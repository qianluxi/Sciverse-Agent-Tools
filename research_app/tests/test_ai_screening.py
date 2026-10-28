from research_app.ai_screening import suggest_screening


class FakeClient:
    model = "test-model"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages, *, max_tokens=2048):
        self.calls.append((messages, max_tokens))
        return self.responses.pop(0)


def _candidates():
    return [
        {
            "candidate_key": "paper:a",
            "paper_id": "paper:a",
            "title": "Eligible shear wall trial",
            "year": 2024,
            "abstract": "A randomized trial of shear wall systems.",
        },
        {
            "candidate_key": "paper:b",
            "paper_id": "paper:b",
            "title": "Unrelated bridge study",
            "year": 2020,
            "abstract": "A study of bridge bearings.",
        },
    ]


def test_suggest_screening_returns_structured_evidence_and_uncertainty():
    client = FakeClient(
        [
            '{"papers":['
            '{"candidate_key":"paper:a","suggestion":"include",'
            '"criteria":["研究类型符合"],"rationale":"随机试验",'
            '"confidence":"high","evidence_quotes":['
            '{"quote":"Eligible shear wall trial","section":"标题"}]},'
            '{"candidate_key":"paper:b","suggestion":"exclude",'
            '"criteria":["主题不相关"],"rationale":"研究对象为桥梁",'
            '"confidence":"medium","evidence_quotes":['
            '{"quote":"bridge bearings","section":"摘要"}]}]}'
        ]
    )

    result = suggest_screening(
        client,
        {
            "question": "Do shear walls work?",
            "inclusion": "Shear wall trials",
            "exclusion": "Bridge studies",
        },
        _candidates(),
    )

    assert result["prompt_version"] == "screening-v1"
    assert result["items"][0]["suggestion"] == "include"
    assert result["items"][0]["criteria"] == ["研究类型符合"]
    assert result["items"][0]["quote_checks"][0]["verified"] is True
    assert result["items"][1]["suggestion"] == "exclude"
    assert result["items"][1]["quote_checks"][0]["verified"] is True
    prompt = client.calls[0][0][1]["content"]
    assert "Shear wall trials" in prompt
    assert "Bridge studies" in prompt


def test_suggest_screening_marks_missing_and_batch_errors():
    client = FakeClient(
        [
            '{"papers":[{"candidate_key":"paper:a","suggestion":"included",'
            '"criteria":"研究类型符合","rationale":"ok","confidence":"HIGH",'
            '"evidence_quotes":[]}]}'
        ]
    )

    result = suggest_screening(client, {}, _candidates())

    assert result["items"][0]["suggestion"] == "include"
    assert result["items"][0]["criteria"] == ["研究类型符合"]
    assert result["items"][0]["confidence"] == "high"
    assert result["items"][1]["suggestion"] == "uncertain"
    assert result["items"][1]["error"] == "AI 未返回该候选的筛选建议。"


def test_suggest_screening_isolates_batch_error():
    client = FakeClient(["not json"])

    result = suggest_screening(client, {}, _candidates())

    assert all(item["error"] for item in result["items"])
    assert all(item["suggestion"] == "uncertain" for item in result["items"])
