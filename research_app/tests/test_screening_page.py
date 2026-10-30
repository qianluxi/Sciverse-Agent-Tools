from pathlib import Path

from streamlit.testing.v1 import AppTest

import research_app.ai_translation as ai_translation
import research_app.storage as storage


PAGE_PATH = Path(__file__).parents[1] / "pages" / "2_系统综述筛选.py"


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "database_path", lambda: tmp_path / "workbench.sqlite3")


def test_screening_page_screens_candidate_in_existing_project(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = storage.create_screening_project(
        "Review",
        "Does intervention X work?",
        "Trials",
        "Not about X",
    )
    app = AppTest.from_file(str(PAGE_PATH), default_timeout=30)
    app.session_state["screening_project_id"] = project_id
    app.session_state["screening_project_selector"] = project_id
    app.session_state["current_project_id"] = project_id
    app.run(timeout=30)

    assert not app.exception
    projects = storage.list_screening_projects()
    assert len(projects) == 1

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


def test_screening_page_adds_only_selected_papers(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = storage.create_screening_project("Review", "Q", "Include", "Exclude")
    selected = {"title": "Selected", "unique_id": "paper:selected"}
    other = {"title": "Other", "unique_id": "paper:other"}
    app = AppTest.from_file(str(PAGE_PATH), default_timeout=30)
    app.session_state["screening_project_id"] = project_id
    app.session_state["screening_project_selector"] = project_id
    app.session_state["current_project_id"] = project_id
    app.session_state["papers"] = [selected, other]
    app.session_state["export_selection"] = {"selected": selected}
    app.run(timeout=30)

    next(
        button
        for button in app.button
        if button.label == "将已选择的 1 篇文献加入项目"
    ).click().run(timeout=30)

    assert not app.exception
    assert storage.screening_stats(project_id)["pending"] == 1
    candidates, _ = storage.list_screening_candidates(project_id)
    assert [candidate["title"] for candidate in candidates] == ["Selected"]


def test_screening_page_shows_translated_title_and_abstract(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "SCIVERSE_TRANSLATION_CACHE",
        str(tmp_path / "titles.json"),
    )
    monkeypatch.setenv(
        "SCIVERSE_ABSTRACT_TRANSLATION_CACHE",
        str(tmp_path / "abstracts.json"),
    )
    project_id = storage.create_screening_project("Review", "Q", "Include", "Exclude")
    storage.add_screening_candidates(
        project_id,
        [
            {
                "title": "Seismic retrofit of RC columns",
                "abstract": "This study tests seismic retrofit of RC columns.",
                "unique_id": "paper:translated",
                "doi": "10.1000/translated",
            }
        ],
    )
    title_calls = []
    abstract_calls = []

    def fake_titles(client, titles):
        title_calls.extend(titles)
        return {title: "RC 柱抗震加固" for title in titles}

    def fake_abstracts(client, abstracts):
        abstract_calls.extend(abstracts)
        return {
            abstract: "本研究测试 RC 柱的抗震加固。"
            for abstract in abstracts
        }

    monkeypatch.setattr(ai_translation, "translate_titles", fake_titles)
    monkeypatch.setattr(ai_translation, "translate_abstracts", fake_abstracts)
    app = AppTest.from_file(str(PAGE_PATH), default_timeout=30)
    app.session_state["screening_project_id"] = project_id
    app.session_state["screening_project_selector"] = project_id
    app.session_state["current_project_id"] = project_id
    app.session_state["ai_key_session"] = "test-key"
    app.session_state["ai_model"] = "test-model"
    app.run(timeout=30)

    assert not app.exception
    assert title_calls == ["Seismic retrofit of RC columns"]
    assert abstract_calls == ["This study tests seismic retrofit of RC columns."]
    assert any("RC 柱抗震加固" in item.value for item in app.caption)
    assert any(
        "本研究测试 RC 柱的抗震加固。" in item.value for item in app.markdown
    )


def test_screening_page_flags_missing_abstract_and_missing_audit(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = storage.create_screening_project("Review", "Q", "Include", "Exclude")
    storage.add_screening_candidates(
        project_id,
        [{"title": "No abstract study", "unique_id": "paper:no-abstract"}],
    )
    app = AppTest.from_file(str(PAGE_PATH), default_timeout=30)
    app.session_state["screening_project_id"] = project_id
    app.session_state["screening_project_selector"] = project_id
    app.session_state["current_project_id"] = project_id
    app.run(timeout=30)

    assert not app.exception
    assert any("无可用摘要" in warning.value for warning in app.warning)
    assert any("尚无人工审核审计记录" in warning.value for warning in app.warning)
