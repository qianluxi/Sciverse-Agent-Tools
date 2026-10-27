from pathlib import Path

from streamlit.testing.v1 import AppTest

import research_app.storage as storage


PAGE_PATH = Path(__file__).parents[1] / "pages" / "0_研究项目.py"


def test_project_overview_renders_selected_project(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "database_path", lambda: tmp_path / "workbench.sqlite3")
    project_id = storage.create_screening_project(
        "Project Alpha",
        "How does it work?",
        "Include",
        "Exclude",
    )

    app = AppTest.from_file(str(PAGE_PATH), default_timeout=30)
    app.session_state["current_project_id"] = project_id
    app.session_state["overview_project_selector"] = project_id
    app.run(timeout=30)

    assert not app.exception
    assert any("Project Alpha" in block.value for block in app.markdown)
    assert any("How does it work?" in block.value for block in app.markdown)


def test_project_page_restores_saved_search_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "database_path", lambda: tmp_path / "workbench.sqlite3")
    project_id = storage.create_screening_project("Project Alpha", "Q", "Include", "Exclude")
    papers = [{"title": "Saved paper", "unique_id": "paper:saved"}]
    storage.save_search_run(
        project_id,
        "structured",
        {"query": "saved", "page": 1, "page_size": 20},
        papers,
        1,
    )

    app = AppTest.from_file(str(PAGE_PATH), default_timeout=30)
    app.session_state["current_project_id"] = project_id
    app.session_state["overview_project_selector"] = project_id
    app.run(timeout=30)
    next(button for button in app.button if button.label == "载入检索快照").click().run(
        timeout=30
    )

    assert not app.exception
    assert app.session_state["papers"] == papers
    assert app.session_state["current_search_run_id"]
