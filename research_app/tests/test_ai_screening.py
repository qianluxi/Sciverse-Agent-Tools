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
            '"criterion_ids":["I1"],"rationale":"随机试验",'
            '"confidence":"high","evidence_quotes":['
            '{"quote":"Eligible shear wall trial","section":"标题"}]},'
            '{"candidate_key":"paper:b","suggestion":"exclude",'
            '"criterion_ids":["E1"],"rationale":"研究对象为桥梁",'
            '"confidence":"medium","evidence_quotes":['
            '{"quote":"bridge bearings","section":"摘要"}]}]}'
        ]
    )

    result = suggest_screening(
        client,
        {
            "question": "Do shear walls work?",
            "items": [
                {"id": "I1", "kind": "inclusion", "text": "Shear wall trials"},
                {"id": "E1", "kind": "exclusion", "text": "Bridge studies"},
            ],
        },
        _candidates(),
    )

    assert result["prompt_version"] == "screening-v2"
    assert result["items"][0]["suggestion"] == "include"
    assert result["items"][0]["criteria"] == ["I1: Shear wall trials"]
    assert result["items"][0]["quote_checks"][0]["verified"] is True
    assert result["items"][1]["suggestion"] == "exclude"
    assert result["items"][1]["criteria"] == ["E1: Bridge studies"]
    assert result["items"][1]["quote_checks"][0]["verified"] is True
    assert result["items"][0]["invalid_criteria"] == []
    prompt = client.calls[0][0][1]["content"]
    assert "I1: Shear wall trials" in prompt
    assert "E1: Bridge studies" in prompt


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
    assert result["items"][0]["criteria"] == []
    assert result["items"][0]["confidence"] == "high"
    assert result["items"][1]["suggestion"] == "uncertain"
    assert result["items"][1]["error"] == "AI 未返回该候选的筛选建议。"


def test_suggest_screening_does_not_assign_unknown_ids_by_position():
    client = FakeClient(
        [
            '{"papers":['
            '{"candidate_key":"paper:a","suggestion":"include","confidence":"high"},'
            '{"candidate_key":"paper:c","suggestion":"exclude","confidence":"high"}]}'
        ]
    )

    result = suggest_screening(client, {}, _candidates())

    assert result["items"][0]["suggestion"] == "include"
    assert result["items"][1]["suggestion"] == "uncertain"
    assert result["items"][1]["error"] == "AI 未返回该候选的筛选建议。"
    assert "paper:c" not in [item["candidate_key"] for item in result["items"]]


def test_suggest_screening_rejects_duplicate_ids():
    client = FakeClient(
        [
            '{"papers":['
            '{"candidate_key":"paper:a","suggestion":"include","confidence":"high"},'
            '{"candidate_key":"paper:a","suggestion":"exclude","confidence":"high"}]}'
        ]
    )

    result = suggest_screening(client, {}, _candidates())

    assert result["items"][0]["suggestion"] == "uncertain"
    assert "重复" in result["items"][0]["error"]
    assert result["items"][1]["error"] == "AI 未返回该候选的筛选建议。"


def test_suggest_screening_isolates_batch_error():
    client = FakeClient(["not json", "not json"])

    result = suggest_screening(client, {}, _candidates())

    assert all(item["error"] for item in result["items"])
    assert all(item["suggestion"] == "uncertain" for item in result["items"])


def test_suggest_screening_marks_unknown_criterion_ids_as_invalid():
    client = FakeClient(
        [
            '{"papers":[{"candidate_key":"paper:a","suggestion":"include",'
            '"criterion_ids":["I1","I9"],"rationale":"ok","confidence":"high",'
            '"evidence_quotes":[]}]}'
        ]
    )

    result = suggest_screening(
        client,
        {
            "question": "Q",
            "items": [
                {"id": "I1", "kind": "inclusion", "text": "Eligible trials"}
            ],
        },
        _candidates(),
    )

    assert result["items"][0]["criteria"] == ["I1: Eligible trials"]
    assert result["items"][0]["invalid_criteria"] == ["I9"]


def test_suggest_screening_retries_failed_batch_once(monkeypatch):
    from research_app.ai import AIError

    class FlakyClient:
        model = "test-model"

        def __init__(self):
            self.calls = 0

        def chat(self, messages, *, max_tokens=2048):
            self.calls += 1
            if self.calls == 1:
                raise AIError("ReadTimeout")
            return (
                '{"papers":['
                '{"candidate_key":"paper:a","suggestion":"include",'
                '"criterion_ids":[],"rationale":"ok","confidence":"high",'
                '"evidence_quotes":[]},'
                '{"candidate_key":"paper:b","suggestion":"exclude","criterion_ids":[],'
                '"rationale":"ok","confidence":"high","evidence_quotes":[]}'
                "]}"
            )

    client = FlakyClient()
    result = suggest_screening(client, {}, _candidates())

    assert client.calls == 2
    assert all(not item.get("error") for item in result["items"])
    assert result["items"][0]["suggestion"] == "include"
