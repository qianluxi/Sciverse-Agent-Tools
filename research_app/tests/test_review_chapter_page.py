from pathlib import Path

from streamlit.testing.v1 import AppTest

import research_app.ai_settings as ai_settings
import research_app.storage as storage


PAGE_PATH = Path(__file__).parents[1] / "pages" / "13_综述章节草稿.py"


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_settings, "settings_path", lambda: tmp_path / "ai-settings.json")
    monkeypatch.setattr(storage, "database_path", lambda: tmp_path / "workbench.sqlite3")


def _project_with_candidate():
    project_id = storage.create_screening_project("综述项目", "埋深对经济性的影响", "I1", "E1")
    storage.add_screening_candidates(
        project_id,
        [{"title": "论文 A", "unique_id": "paper:a", "year": 2020, "abstract": "摘要 A"}],
    )
    storage.set_screening_decision(project_id, "uid:paper:a", "included")
    return project_id


def _run(project_id):
    app = AppTest.from_file(str(PAGE_PATH), default_timeout=30)
    app.session_state["current_project_id"] = project_id
    app.session_state["chapter_project_selector"] = project_id
    return app.run(timeout=30)


def test_page_without_project_asks_to_create(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)

    app = AppTest.from_file(str(PAGE_PATH), default_timeout=30).run(timeout=30)

    assert not app.exception
    assert any("研究项目" in info.value for info in app.info)


def test_page_blocks_generation_without_fulltext_evidence(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = _project_with_candidate()

    app = _run(project_id)

    assert not app.exception
    assert any("没有完成全文核验" in error.value for error in app.error)
    assert not [button for button in app.button if button.label == "生成章节草稿"]


def test_page_enables_generation_when_fulltext_evidence_exists(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = _project_with_candidate()
    task = storage.create_ai_task(
        project_id,
        kind="evidence",
        model="test-model",
        prompt_version="evidence-v1",
        input_hash="hash",
    )
    storage.save_evidence_items(
        project_id,
        task,
        [
            {
                "paper_id": "paper:a",
                "title": "论文 A",
                "year": 2020,
                "data": {
                    "methods": "模型",
                    "findings": "成本下降",
                    "limitations": "样本小",
                    "evidence_quotes": [{"quote": "cost reduced"}],
                },
                "reviewed": False,
            }
        ],
    )
    storage.append_evidence_version(
        project_id,
        "paper:a",
        task,
        source="ai_raw",
        data={
            "methods": "模型",
            "findings": "成本下降",
            "limitations": "样本小",
            "evidence_quotes": [{"quote": "cost reduced"}],
        },
        quote_checks=[{"quote": "cost reduced", "verified": True}],
        model="test-model",
        prompt_version="evidence-v1",
        title="论文 A",
        year=2020,
    )

    app = _run(project_id)

    assert not app.exception
    buttons = [button for button in app.button if button.label == "生成章节草稿"]
    assert len(buttons) == 1
    assert buttons[0].disabled is True
    metrics = {metric.label: metric.value for metric in app.metric}
    assert int(metrics["全文已核验"]) == 1
    assert int(metrics["仅题摘"]) == 0
