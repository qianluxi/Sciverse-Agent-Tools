from pathlib import Path

from streamlit.testing.v1 import AppTest

import sciverse.credentials


PAGE_PATH = Path(__file__).parents[1] / "pages" / "4_趋势分析.py"


def test_trends_page_renders_current_results(monkeypatch):
    monkeypatch.setattr(sciverse.credentials, "resolve_token", lambda *_args: None)
    app = AppTest.from_file(str(PAGE_PATH), default_timeout=30).run(timeout=30)
    app.session_state["papers"] = [
        {
            "title": "Paper A",
            "doi": "10.1/a",
            "unique_id": "paper:a",
            "publication_published_year": 2024,
            "publication_venue_name_unified": "Venue A",
            "author": [{"name": "Alice"}],
            "keywords": ["ml", "nlp"],
            "citation_count": 10,
        },
        {
            "title": "Paper B",
            "doi": "10.1/b",
            "unique_id": "paper:b",
            "publication_published_year": 2023,
            "publication_venue_name_unified": "Venue B",
            "author": [{"name": "Bob"}],
            "keywords": ["ml"],
            "citation_count": 5,
        },
    ]
    app.session_state["search_kind"] = "structured"
    app.session_state["search_params"] = {"query": "metric learning"}
    app.run(timeout=30)

    assert not app.exception
    assert any(metric.label == "去重后样本" and metric.value == "2" for metric in app.metric)
    download_labels = {button.label for button in app.get("download_button")}
    assert "下载统计 ZIP（CSV）" in download_labels
