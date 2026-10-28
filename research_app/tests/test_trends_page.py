from pathlib import Path

from streamlit.testing.v1 import AppTest

import research_app.storage as storage
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


def test_trends_source_switch_clears_old_sample(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "database_path", lambda: tmp_path / "workbench.sqlite3")
    monkeypatch.setattr(sciverse.credentials, "resolve_token", lambda *_args: None)
    project_id = storage.create_screening_project("Trend project", "Q", "I", "E")
    storage.save_search_run(
        project_id,
        "structured",
        {"query": "project", "page": 1, "page_size": 20},
        [{"title": "Project paper", "publication_published_year": 2024}],
        1,
    )
    app = AppTest.from_file(str(PAGE_PATH), default_timeout=30)
    app.session_state["current_project_id"] = project_id
    app.session_state["trends_project_selector"] = project_id
    app.session_state["papers"] = [
        {"title": "Session paper", "publication_published_year": 2023}
    ]
    app.session_state["search_kind"] = "structured"
    app.session_state["search_params"] = {"query": "session"}
    app.run(timeout=30)
    app.session_state["trend_sample"] = [{"title": "Old sample"}]
    app.session_state["trend_source"] = "old"
    next(radio for radio in app.radio if radio.label == "分析数据源").set_value(
        "项目最近检索快照"
    ).run(timeout=30)

    assert not app.exception
    assert "trend_sample" not in app.session_state
    assert "trend_source" not in app.session_state
    assert any(metric.label == "去重后样本" and metric.value == "1" for metric in app.metric)
