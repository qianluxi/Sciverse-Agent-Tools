import httpx
import pytest

from research_app.ai import (
    AIError,
    OpenAICompatibleClient,
    extract_evidence,
    generate_outline,
    parse_json_object,
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


def _document():
    return {
        "paper_id": "paper:1",
        "title": "Paper",
        "year": 2024,
        "text": "We studied 100 patients and found improvement.",
    }


def test_parse_json_object_accepts_fenced_output():
    assert parse_json_object('```json\n{"findings": "ok"}\n```') == {"findings": "ok"}


def test_parse_json_object_rejects_non_json():
    with pytest.raises(AIError, match="JSON"):
        parse_json_object("no object here")


def test_extract_evidence_keeps_source_and_error_isolated():
    client = FakeClient(
        [
            '{"research_question": "Q", "methods": "RCT", "evidence_quotes": [{"quote": "improvement"}]}',
            AIError("bad response"),
        ]
    )
    documents = [_document(), {**_document(), "paper_id": "paper:2"}]

    results = extract_evidence(client, documents)

    assert results[0]["paper_id"] == "paper:1"
    assert results[0]["data"]["methods"] == "RCT"
    assert results[1]["error"] == "bad response"
    assert results[1]["data"] is None


def test_generate_outline_requires_evidence_and_calls_model():
    client = FakeClient(["# 提纲\n\n## 背景\n证据 [paper:1]"])
    evidence = [{"paper_id": "paper:1", "data": {"findings": "improvement"}}]

    outline = generate_outline(client, "What works?", evidence)

    assert "paper:1" in outline
    assert client.calls[0][1] == 4096
    with pytest.raises(ValueError, match="证据"):
        generate_outline(client, "What works?", [{"paper_id": "x", "data": None}])


def test_client_validates_configuration_and_endpoint():
    with pytest.raises(ValueError, match="不能为空"):
        OpenAICompatibleClient(base_url="", api_key="k", model="m")
    with pytest.raises(ValueError, match="http"):
        OpenAICompatibleClient(base_url="api.example", api_key="k", model="m")

    client = OpenAICompatibleClient(
        base_url="https://api.example/v1/",
        api_key="k",
        model="m",
    )
    assert client.endpoint == "https://api.example/v1/chat/completions"
    direct = OpenAICompatibleClient(
        base_url="https://api.example/v1/chat/completions",
        api_key="k",
        model="m",
    )
    assert direct.endpoint == "https://api.example/v1/chat/completions"


def test_client_surfaces_provider_error_body(monkeypatch):
    request = httpx.Request("POST", "https://api.example/v1/chat/completions")
    response = httpx.Response(
        400,
        text='{"error":"unsupported model"}',
        request=request,
    )
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: response)
    client = OpenAICompatibleClient(
        base_url="https://api.example/v1",
        api_key="secret",
        model="model",
    )

    try:
        client.chat([{"role": "user", "content": "hi"}])
    except AIError as error:
        assert "HTTP 400" in str(error)
        assert "unsupported model" in str(error)
        assert "secret" not in str(error)
    else:
        raise AssertionError("expected AIError")


def test_client_sends_stable_session_and_user_agent(monkeypatch):
    captured_headers = []

    def fake_post(*args, **kwargs):
        captured_headers.append(kwargs["headers"])
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "OK"}}]},
            request=httpx.Request("POST", "https://api.example/v1/chat/completions"),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    client = OpenAICompatibleClient(
        base_url="https://api.example/v1",
        api_key="secret",
        model="model",
        session_id="session-1",
    )
    client.chat([{"role": "user", "content": "hi"}])
    client.chat([{"role": "user", "content": "again"}])

    assert captured_headers[0]["x-opencode-session"] == "session-1"
    assert captured_headers[1]["x-opencode-session"] == "session-1"
    assert "sciverse-research-workbench" in captured_headers[0]["User-Agent"]


def test_client_retries_empty_length_response_with_larger_budget(monkeypatch):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(kwargs["json"]["max_tokens"])
        request = httpx.Request("POST", "https://api.example/v1/chat/completions")
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": "", "reasoning_content": "thinking"},
                            "finish_reason": "length",
                        }
                    ]
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]},
            request=request,
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    client = OpenAICompatibleClient(
        base_url="https://api.example/v1",
        api_key="secret",
        model="model",
    )

    assert client.chat([{"role": "user", "content": "hi"}]) == "OK"
    assert calls == [2048, 4096]


def test_client_reports_empty_content_reason(monkeypatch):
    def fake_post(*args, **kwargs):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "", "reasoning_content": "thinking"},
                        "finish_reason": "length",
                    }
                ]
            },
            request=httpx.Request("POST", "https://api.example/v1/chat/completions"),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    client = OpenAICompatibleClient(
        base_url="https://api.example/v1",
        api_key="secret",
        model="model",
    )

    with pytest.raises(AIError, match="finish_reason='length'"):
        client.chat([{"role": "user", "content": "hi"}])


def test_generate_outline_extracts_json_markdown_field():
    client = FakeClient(['```json\n{"markdown_outline": "# 提纲\\n\\n## 背景\\n证据"}\n```'])
    evidence = [{"paper_id": "paper:1", "data": {"findings": "improvement"}}]

    outline = generate_outline(client, "What works?", evidence)

    assert outline == "# 提纲\n\n## 背景\n证据"
    assert "markdown_outline" not in outline
