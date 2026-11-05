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
        multiselect.label.startswith("参与提取的文献")
        for multiselect in app.multiselect
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
    inputs = storage.list_evidence_task_inputs(project_id, task["id"])
    assert len(inputs) == 1
    assert inputs[0]["input_source"] == "abstract"
    assert inputs[0]["input_text"] == "Study abstract text."


def _seed_versions(project_id):
    task_id = storage.create_ai_task(
        project_id,
        kind="evidence_matrix",
        model="m-base",
        prompt_version="evidence-v1",
        input_hash="page-hash",
    )
    base = {
        "research_question": "Q",
        "methods": "M",
        "sample": "S",
        "findings": "PLACEHOLDER",
        "limitations": "L",
        "evidence_quotes": [],
    }
    storage.append_evidence_version(
        project_id,
        "uid:paper:a",
        task_id,
        source="ai_raw",
        data={**base, "findings": "AI findings"},
        model="m-base",
        prompt_version="evidence-v1",
        title="Paper A",
        year=2024,
    )
    storage.append_evidence_version(
        project_id,
        "uid:paper:a",
        task_id,
        source="final_adopted",
        data={**base, "findings": "FINAL findings"},
        model="m-base",
        prompt_version="evidence-v1",
        title="Paper A",
        year=2024,
    )


def test_matrix_page_switches_edit_base_version(monkeypatch, tmp_path):
    project_id = _setup(monkeypatch, tmp_path)
    _seed_versions(project_id)
    app = _app(project_id)

    base_radio = next(radio for radio in app.radio if radio.label == "编辑基准版本")
    assert base_radio.value == "final_adopted"
    findings = next(field for field in app.text_area if field.label == "主要发现")
    assert findings.value == "FINAL findings"

    base_radio = next(radio for radio in app.radio if radio.label == "编辑基准版本")
    base_radio.set_value("ai_raw").run(timeout=30)
    findings = next(field for field in app.text_area if field.label == "主要发现")
    assert findings.value == "AI findings"

    findings.set_value("EDITED").run(timeout=30)
    next(
        button for button in app.button if button.label == "标记最终采用"
    ).click().run(timeout=30)

    assert not app.exception
    versions = storage.list_evidence_versions(
        project_id, "uid:paper:a", source="final_adopted"
    )
    assert versions[0]["version_no"] == 2
    assert versions[0]["data"]["findings"] == "EDITED"
    assert versions[0]["model"] == "m-base"
    assert versions[0]["prompt_version"] == "evidence-v1"


def test_matrix_page_keeps_matrix_when_no_papers_selected(monkeypatch, tmp_path):
    project_id = _setup(monkeypatch, tmp_path)
    _seed_versions(project_id)
    app = _app(project_id)

    app.multiselect[0].set_value([]).run(timeout=30)

    assert not app.exception
    assert any("矩阵与版本" in markdown.value for markdown in app.markdown)
