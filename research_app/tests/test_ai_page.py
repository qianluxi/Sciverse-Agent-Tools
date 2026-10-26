from pathlib import Path

from streamlit.testing.v1 import AppTest

import research_app.ai_settings as ai_settings
import sciverse.credentials


PAGE_PATH = Path(__file__).parents[1] / "pages" / "6_AI证据与提纲.py"


def _isolate_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_settings, "settings_path", lambda: tmp_path / "ai-settings.json")


def test_ai_page_renders_without_key(monkeypatch, tmp_path):
    _isolate_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(sciverse.credentials, "resolve_token", lambda *_args: None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    app = AppTest.from_file(str(PAGE_PATH), default_timeout=30).run(timeout=30)
    app.session_state["papers"] = [
        {
            "title": "Paper",
            "unique_id": "paper:1",
            "doc_id": "doc-1",
            "abstract": "A study.",
        }
    ]
    app.session_state["search_params"] = {"query": "What works?"}
    app.run(timeout=30)

    assert not app.exception
    assert any(widget.label == "论文" for widget in app.multiselect)
    assert next(button for button in app.button if button.label == "提取结构化证据").disabled

    base_url = next(field for field in app.selectbox if field.label == "Base URL")
    assert base_url.value == "https://opencode.ai/zen/go/v1"
    next(field for field in app.text_input if field.label == "API Key").set_value("test-key")
    next(field for field in app.text_input if field.label == "模型名").set_value(
        "deepseek-v4.1-flash"
    )
    app.run(timeout=30)

    assert next(field for field in app.selectbox if field.label == "Base URL").value == (
        "https://opencode.ai/zen/go/v1"
    )
    assert next(field for field in app.text_input if field.label == "API Key").value == "test-key"
    assert next(field for field in app.text_input if field.label == "模型名").value == (
        "deepseek-v4.1-flash"
    )


def test_ai_page_uses_recorded_base_url_and_model(monkeypatch, tmp_path):
    _isolate_settings(monkeypatch, tmp_path)
    ai_settings.record_ai_profile("https://api.example.com/v1", "example-model")
    monkeypatch.setattr(sciverse.credentials, "resolve_token", lambda *_args: None)

    app = AppTest.from_file(str(PAGE_PATH), default_timeout=30).run(timeout=30)

    assert not app.exception
    base_url = next(field for field in app.selectbox if field.label == "Base URL")
    assert base_url.value == "https://api.example.com/v1"
    assert "https://api.example.com/v1" in base_url.options
    assert next(field for field in app.text_input if field.label == "模型名").value == (
        "example-model"
    )


def test_ai_page_shows_editable_evidence_and_exports(monkeypatch, tmp_path):
    _isolate_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(sciverse.credentials, "resolve_token", lambda *_args: None)
    app = AppTest.from_file(str(PAGE_PATH), default_timeout=30).run(timeout=30)
    app.session_state["papers"] = [
        {
            "title": "Paper",
            "unique_id": "paper:1",
            "doc_id": "doc-1",
            "abstract": "A study.",
        }
    ]
    app.session_state["ai_evidence"] = [
        {
            "paper_id": "paper:1",
            "title": "Paper",
            "year": 2024,
            "model": "test-model",
            "prompt_version": "evidence-v1",
            "data": {
                "research_question": "Does it work?",
                "methods": "RCT",
                "sample": "100 patients",
                "findings": "Improvement",
                "limitations": "Single site",
                "evidence_quotes": [
                    {"quote": "We studied 100 patients.", "section": "Methods"}
                ],
            },
            "quote_checks": [
                {
                    "quote": "We studied 100 patients.",
                    "section": "Methods",
                    "verified": True,
                }
            ],
            "error": None,
        }
    ]
    app.run(timeout=30)

    assert not app.exception
    assert any(field.label == "研究问题" for field in app.text_area)
    assert any(field.label == "我已核对这篇文献的 AI 证据" for field in app.checkbox)
    labels = {button.label for button in app.get("download_button")}
    assert {"下载证据表 CSV", "下载证据表 Markdown"} <= labels

    next(field for field in app.text_area if field.label == "研究问题").set_value(
        "Edited question"
    )
    app.run(timeout=30)
    assert not app.exception
    assert next(field for field in app.text_area if field.label == "研究问题").value == (
        "Edited question"
    )


def test_ai_page_switching_base_url_loads_its_last_model(monkeypatch, tmp_path):
    _isolate_settings(monkeypatch, tmp_path)
    ai_settings.record_ai_profile("https://api.example.com/v1", "older-model")
    ai_settings.record_ai_profile("https://opencode.ai/zen/go/v1", "newer-model")
    monkeypatch.setattr(sciverse.credentials, "resolve_token", lambda *_args: None)

    app = AppTest.from_file(str(PAGE_PATH), default_timeout=30).run(timeout=30)
    next(field for field in app.selectbox if field.label == "Base URL").set_value(
        "https://api.example.com/v1"
    ).run(timeout=30)

    assert not app.exception
    assert next(field for field in app.selectbox if field.label == "Base URL").value == (
        "https://api.example.com/v1"
    )
    assert next(field for field in app.text_input if field.label == "模型名").value == (
        "older-model"
    )
