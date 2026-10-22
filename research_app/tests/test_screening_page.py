from pathlib import Path

from streamlit.testing.v1 import AppTest

import research_app.storage as storage


PAGE_PATH = Path(__file__).parents[1] / "pages" / "2_系统综述筛选.py"


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "database_path", lambda: tmp_path / "workbench.sqlite3")


def test_screening_page_creates_project_and_screens_candidate(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    app = AppTest.from_file(str(PAGE_PATH), default_timeout=30).run(timeout=30)

    assert not app.exception
    next(field for field in app.text_input if field.label == "项目名称").set_value("Review")
    next(field for field in app.text_area if field.label == "研究问题").set_value(
        "Does intervention X work?"
    )
    next(field for field in app.text_area if field.label == "纳入标准").set_value("Trials")
    next(field for field in app.text_area if field.label == "排除标准").set_value("Not about X")
    next(button for button in app.button if button.label == "创建项目").click().run(timeout=30)

    projects = storage.list_screening_projects()
    assert len(projects) == 1
    project_id = projects[0]["id"]

    app.session_state["papers"] = [
        {
            "title": "Eligible study",
            "doi": "10.1000/eligible",
            "unique_id": "paper:eligible",
            "publication_published_year": 2024,
        }
    ]
    app.run(timeout=30)
    next(button for button in app.button if "加入项目" in button.label).click().run(timeout=30)

    assert storage.screening_stats(project_id)["pending"] == 1
    next(radio for radio in app.radio if radio.label == "决定").set_value("excluded")
    next(field for field in app.selectbox if field.label.startswith("排除原因")).set_value(
        "主题不相关"
    )
    next(button for button in app.button if button.label == "保存决定").click().run(timeout=30)

    assert not app.exception
    stats = storage.screening_stats(project_id)
    assert stats["excluded"] == 1
    assert stats["pending"] == 0
    assert len(storage.list_screening_audit(project_id)) == 1
