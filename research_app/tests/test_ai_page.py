from pathlib import Path

from streamlit.testing.v1 import AppTest

import sciverse.credentials


PAGE_PATH = Path(__file__).parents[1] / "pages" / "6_AI证据与提纲.py"


def test_ai_page_renders_without_key(monkeypatch):
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
