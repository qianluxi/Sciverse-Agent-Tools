from pathlib import Path

from streamlit.testing.v1 import AppTest

import research_app.storage as storage


PAGE_PATH = Path(__file__).parents[1] / "pages" / "12_协作审核.py"


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "database_path", lambda: tmp_path / "workbench.sqlite3")
    project_id = storage.create_screening_project("Review", "Q", "I", "E")
    storage.add_screening_candidates(
        project_id, [{"title": "Paper A", "unique_id": "paper:a"}]
    )
    reviewer_one = storage.create_reviewer("Alice", "reviewer")
    reviewer_two = storage.create_reviewer("Bob", "reviewer")
    coordinator = storage.create_reviewer("Carol", "coordinator")
    storage.set_blind_decision(
        project_id, "uid:paper:a", reviewer_one, "included"
    )
    storage.set_blind_decision(
        project_id,
        "uid:paper:a",
        reviewer_two,
        "excluded",
        reason="主题不相关",
    )
    return project_id, coordinator


def _app(project_id, reviewer_id):
    app = AppTest.from_file(str(PAGE_PATH), default_timeout=30)
    app.session_state["current_project_id"] = project_id
    app.session_state["collab_project_selector"] = project_id
    app.session_state["collab_current_reviewer"] = reviewer_id
    return app.run(timeout=30)


def test_collaboration_page_coordinator_resolves_conflict(monkeypatch, tmp_path):
    project_id, coordinator = _setup(monkeypatch, tmp_path)
    app = _app(project_id, coordinator)

    assert not app.exception
    assert any("协作审核" in title.value for title in app.title)
    next(
        button for button in app.button if button.label == "提交冲突解决"
    ).click().run(timeout=30)

    assert not app.exception
    candidates = {
        item["candidate_key"]: item
        for item in storage.list_all_screening_candidates(project_id)
    }
    assert candidates["uid:paper:a"]["status"] == "included"
    resolutions = storage.list_conflict_resolutions(project_id)
    assert resolutions[0]["resolution_type"] == "conflict"


def test_collaboration_page_reviewer_cannot_see_coordinator_actions(
    monkeypatch, tmp_path
):
    project_id, _ = _setup(monkeypatch, tmp_path)
    reviewer = storage.create_reviewer("Eve", "reviewer")
    app = _app(project_id, reviewer)

    assert not app.exception
    assert any("只有协调者" in caption.value for caption in app.caption)
