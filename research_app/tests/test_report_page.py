from pathlib import Path

from streamlit.testing.v1 import AppTest

import research_app.storage as storage


PAGE_PATH = Path(__file__).parents[1] / "pages" / "11_研究报告.py"


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "database_path", lambda: tmp_path / "workbench.sqlite3")
    project_id = storage.create_screening_project("Review", "Q", "I", "E")
    storage.add_screening_candidates(
        project_id,
        [{"title": "Paper A", "unique_id": "paper:a", "abstract": "a"}],
    )
    storage.set_screening_decision(project_id, "uid:paper:a", "included")
    return project_id


def _app(project_id):
    app = AppTest.from_file(str(PAGE_PATH), default_timeout=30)
    app.session_state["current_project_id"] = project_id
    app.session_state["report_project_selector"] = project_id
    return app.run(timeout=30)


def test_report_page_renders_prisma(monkeypatch, tmp_path):
    project_id = _setup(monkeypatch, tmp_path)
    app = _app(project_id)

    assert not app.exception
    assert any("研究报告" in title.value for title in app.title)
    assert any("PRISMA 2020 计数" in markdown.value for markdown in app.markdown)
    assert any(
        selectbox.label == "选择题名/摘要纳入的文献"
        for selectbox in app.selectbox
    )


def test_report_page_saves_fulltext_decision(monkeypatch, tmp_path):
    project_id = _setup(monkeypatch, tmp_path)
    app = _app(project_id)

    radio = next(item for item in app.radio if item.label == "全文阶段")
    radio.set_value("included")
    next(
        button for button in app.button if button.label == "保存全文阶段决定"
    ).click()
    app.run(timeout=30)

    assert not app.exception
    decision = storage.get_fulltext_decision(project_id, "uid:paper:a")
    assert decision["status"] == "included"
    prisma = storage.prisma_counts(project_id)
    assert prisma["included_in_review"] == 1
