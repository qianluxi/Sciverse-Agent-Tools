import base64
from pathlib import Path

from streamlit.testing.v1 import AppTest

import sciverse.credentials
import research_app.service as service


APP_PATH = Path(__file__).parents[1] / "app.py"


def test_app_renders_without_credentials(monkeypatch):
    monkeypatch.delenv("SCIVERSE_API_TOKEN", raising=False)
    monkeypatch.setattr(sciverse.credentials, "resolve_token", lambda *_args: None)

    app = AppTest.from_file(str(APP_PATH), default_timeout=30).run(timeout=30)

    assert not app.exception
    assert app.radio[0].value == "智能语义检索"
    assert app.text_area[0].label == "你想了解什么？"


def test_structured_search_reads_text_and_figure_offline(monkeypatch):
    monkeypatch.delenv("SCIVERSE_API_TOKEN", raising=False)
    monkeypatch.setattr(sciverse.credentials, "resolve_token", lambda *_args: None)
    actions = []
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jXioAAAAASUVORK5CYII="
    )
    read_count = 0

    def fake_call_api(token, action, **kwargs):
        nonlocal read_count
        assert token == "sv-offline-test"
        actions.append((action, kwargs))
        if action == "search_papers":
            return {
                "results": [
                    {
                        "title": "Fixture paper",
                        "author": [{"name": "Researcher"}],
                        "unique_id": "paper:test",
                        "doc_id": "doc-test",
                        "is_content_accessible": True,
                    }
                ],
                "total_count": 1,
                "page": 1,
                "page_size": 20,
            }
        if action == "read_content":
            read_count += 1
            if read_count > 1:
                return {
                    "text": "Continued excerpt",
                    "bytes_returned": 17,
                    "next_offset": 57,
                    "more": False,
                }
            return {
                "text": "Evidence excerpt\n\n![Figure](figure.png)",
                "bytes_returned": 40,
                "next_offset": 40,
                "more": True,
            }
        if action == "get_resource":
            return image, "image/png"
        raise AssertionError(f"Unexpected API action: {action}")

    monkeypatch.setattr(service, "call_api", fake_call_api)
    app = AppTest.from_file(str(APP_PATH), default_timeout=30).run(timeout=30)
    app.radio[0].set_value("条件筛选").run(timeout=30)
    next(widget for widget in app.text_input if widget.label == "API Token").set_value(
        "sv-offline-test"
    )
    next(widget for widget in app.text_input if widget.label == "关键词或题名（可选）").set_value(
        "climate"
    )
    next(button for button in app.button if button.label == "筛选论文").click().run(timeout=30)

    assert not app.exception
    assert any(action == "search_papers" for action, _ in actions)
    assert actions[0][1]["query"] == "climate"
    assert actions[0][1]["page_size"] == 20
    assert any("Fixture paper" in block.value for block in app.markdown)

    next(button for button in app.button if button.label == "查看文献").click().run(timeout=30)
    next(button for button in app.button if button.label == "读取论文原文").click().run(timeout=30)

    assert not app.exception
    assert any("Evidence excerpt" in block.value for block in app.markdown)
    assert [action for action, _ in actions][-2:] == ["read_content", "get_resource"]

    next(button for button in app.button if button.label == "继续阅读下一段").click().run(
        timeout=30
    )

    assert not app.exception
    assert any("Continued excerpt" in block.value for block in app.markdown)
    assert read_count == 2


def test_semantic_search_uses_hits_response_offline(monkeypatch):
    monkeypatch.delenv("SCIVERSE_API_TOKEN", raising=False)
    monkeypatch.setattr(sciverse.credentials, "resolve_token", lambda *_args: None)
    calls = []

    def fake_call_api(token, action, **kwargs):
        calls.append((action, kwargs))
        assert token == "sv-offline-test"
        assert action == "semantic_search"
        return {
            "hits": [
                {
                    "title": "Semantic fixture",
                    "doc_id": "doc-semantic",
                    "offset": 10,
                    "score": 0.9,
                    "chunk": "Relevant evidence chunk",
                }
            ]
        }

    monkeypatch.setattr(service, "call_api", fake_call_api)
    app = AppTest.from_file(str(APP_PATH), default_timeout=30).run(timeout=30)
    next(widget for widget in app.text_input if widget.label == "API Token").set_value(
        "sv-offline-test"
    )
    app.text_area[0].set_value("How does this topic work?")
    next(button for button in app.button if button.label == "查找相关文献").click().run(timeout=30)

    assert not app.exception
    assert calls[0][0] == "semantic_search"
    assert calls[0][1]["query"] == "How does this topic work?"
    assert calls[0][1]["mode"] == "balanced"
    assert any("Semantic fixture" in block.value for block in app.markdown)
    assert any("Relevant evidence chunk" in block.value for block in app.markdown)


def test_empty_structured_results_show_guidance_offline(monkeypatch):
    monkeypatch.delenv("SCIVERSE_API_TOKEN", raising=False)
    monkeypatch.setattr(sciverse.credentials, "resolve_token", lambda *_args: None)
    monkeypatch.setattr(
        service,
        "call_api",
        lambda *_args, **_kwargs: {
            "results": [],
            "total_count": 0,
            "page": 1,
            "page_size": 20,
        },
    )

    app = AppTest.from_file(str(APP_PATH), default_timeout=30).run(timeout=30)
    app.radio[0].set_value("条件筛选").run(timeout=30)
    next(widget for widget in app.text_input if widget.label == "API Token").set_value(
        "sv-offline-test"
    )
    next(widget for widget in app.text_input if widget.label == "关键词或题名（可选）").set_value(
        "unlikely query"
    )
    next(button for button in app.button if button.label == "筛选论文").click().run(timeout=30)

    assert not app.exception
    assert any("没有找到符合条件的文献" in info.value for info in app.info)
