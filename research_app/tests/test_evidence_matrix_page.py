from pathlib import Path

from streamlit.testing.v1 import AppTest

import research_app.storage as storage


PAGE_PATH = Path(__file__).parents[1] / "pages" / "10_项目证据矩阵.py"


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "database_path", lambda: tmp_path / "workbench.sqlite3")
    project_id = storage.create_screening_project("Matrix", "Q", "I", "E")
    storage.add_screening_candidates(
        project_id,
        [
            {
                "title": "Paper A",
                "unique_id": "paper:a",
                "abstract": "Study abstract text.",
                "publication_published_year": 2024,
            }
        ],
    )
    storage.set_screening_decision(project_id, "uid:paper:a", "included")
    return project_id


def _app(project_id):
    app = AppTest.from_file(str(PAGE_PATH), default_timeout=30)
    app.session_state["current_project_id"] = project_id
    app.session_state["matrix_project_selector"] = project_id
    app.session_state["ai_key_session"] = "test-key"
    app.session_state["ai_model"] = "test-model"
    return app.run(timeout=30)


def test_evidence_matrix_page_renders_included_candidate(monkeypatch, tmp_path):
    project_id = _setup(monkeypatch, tmp_path)
    app = _app(project_id)

    assert not app.exception
    assert any("项目证据矩阵" in title.value for title in app.title)
    assert any(
        multiselect.label == "参与提取的文献" for multiselect in app.multiselect
    )


def test_evidence_matrix_page_plans_batches(monkeypatch, tmp_path):
    project_id = _setup(monkeypatch, tmp_path)
    app = _app(project_id)

    next(
        button
        for button in app.button
        if button.label == "新建任务并规划批次"
    ).click().run(timeout=30)

    assert not app.exception
    task = storage.get_latest_ai_task(project_id, "evidence_matrix")
    assert task is not None
    batches = storage.list_evidence_batches(project_id, task["id"])
    assert len(batches) == 1
    assert batches[0]["paper_keys"] == ["uid:paper:a"]
    assert batches[0]["status"] == "pending"
