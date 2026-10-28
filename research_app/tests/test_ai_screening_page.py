from pathlib import Path

from streamlit.testing.v1 import AppTest

import research_app.ai_screening as ai_screening
import research_app.storage as storage


PAGE_PATH = Path(__file__).parents[1] / "pages" / "8_AI筛选建议.py"


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "database_path", lambda: tmp_path / "workbench.sqlite3")
    project_id = storage.create_screening_project(
        "Screening review",
        "Does intervention X work?",
        "Randomized trials of X",
        "Not about X",
    )
    storage.add_screening_candidates(
        project_id,
        [
            {
                "title": "Eligible study",
                "unique_id": "paper:a",
                "abstract": "Randomized trial of intervention X.",
            }
        ],
    )
    return project_id


def _app(project_id):
    app = AppTest.from_file(str(PAGE_PATH), default_timeout=30)
    app.session_state["current_project_id"] = project_id
    app.session_state["ai_screening_project_selector"] = project_id
    app.session_state["ai_key_session"] = "test-key"
    app.session_state["ai_model"] = "test-model"
    return app.run(timeout=30)


def _fake_suggestion(candidate_key):
    return {
        "items": [
            {
                "candidate_key": candidate_key,
                "paper_id": "paper:a",
                "title": "Eligible study",
                "year": 2024,
                "suggestion": "include",
                "criteria": ["Randomized trials of X"],
                "rationale": "Randomized trial of X.",
                "confidence": "high",
                "evidence_quotes": [
                    {"quote": "Randomized trial", "section": "摘要"}
                ],
                "quote_checks": [
                    {"quote": "Randomized trial", "section": "摘要", "verified": True}
                ],
                "error": None,
            }
        ],
        "model": "test-model",
        "prompt_version": "screening-v1",
    }


def test_ai_screening_page_requires_human_decision(monkeypatch, tmp_path):
    project_id = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        ai_screening,
        "suggest_screening",
        lambda client, criteria, candidates: _fake_suggestion(candidates[0]["candidate_key"]),
    )
    app = _app(project_id)
    app.multiselect[0].set_value(["uid:paper:a"]).run(timeout=30)
    next(
        button for button in app.button if button.label == "生成 AI 筛选建议"
    ).click().run(timeout=30)

    suggestions = storage.load_latest_screening_suggestions(project_id)
    assert suggestions["task"]["status"] == "completed"
    assert suggestions["suggestions"][0]["review_status"] == "pending"
    assert storage.list_all_screening_candidates(project_id)[0]["status"] == "pending"

    next(
        radio for radio in app.radio if radio.label == "人工最终决定"
    ).set_value("included")
    next(
        button for button in app.button if button.label == "保存人工决定"
    ).click().run(timeout=30)

    assert not app.exception
    assert storage.list_all_screening_candidates(project_id)[0]["status"] == "included"
    saved = storage.load_latest_screening_suggestions(project_id)
    assert saved["suggestions"][0]["review_status"] == "accepted"
    audit = storage.list_screening_audit(project_id)
    assert audit[0]["ai_action"] == "accepted"


def test_ai_screening_page_rejects_without_changing_status(monkeypatch, tmp_path):
    project_id = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        ai_screening,
        "suggest_screening",
        lambda client, criteria, candidates: _fake_suggestion(candidates[0]["candidate_key"]),
    )
    app = _app(project_id)
    app.multiselect[0].set_value(["uid:paper:a"]).run(timeout=30)
    next(
        button for button in app.button if button.label == "生成 AI 筛选建议"
    ).click().run(timeout=30)
    next(
        button for button in app.button if button.label == "保存人工决定"
    ).click().run(timeout=30)

    assert not app.exception
    assert storage.list_all_screening_candidates(project_id)[0]["status"] == "pending"
    saved = storage.load_latest_screening_suggestions(project_id)
    assert saved["suggestions"][0]["review_status"] == "rejected"
    assert saved["suggestions"][0]["review_decision"] is None
    assert storage.list_screening_audit(project_id) == []


def test_ai_screening_page_marks_all_failed_task(monkeypatch, tmp_path):
    project_id = _setup(monkeypatch, tmp_path)

    def failed_scan(client, criteria, candidates):
        return {
            "items": [
                {
                    "candidate_key": candidates[0]["candidate_key"],
                    "paper_id": "paper:a",
                    "title": "Eligible study",
                    "year": 2024,
                    "suggestion": "uncertain",
                    "criteria": [],
                    "rationale": "",
                    "confidence": "low",
                    "evidence_quotes": [],
                    "quote_checks": [],
                    "error": "invalid response",
                }
            ],
            "model": "test-model",
            "prompt_version": "screening-v1",
        }

    monkeypatch.setattr(ai_screening, "suggest_screening", failed_scan)
    app = _app(project_id)
    app.multiselect[0].set_value(["uid:paper:a"]).run(timeout=30)
    next(
        button for button in app.button if button.label == "生成 AI 筛选建议"
    ).click().run(timeout=30)

    saved = storage.load_latest_screening_suggestions(project_id)
    assert saved["task"]["status"] == "failed"
    assert "全部 1 篇文献分析失败" in saved["task"]["error"]
    assert any("全部 1 篇文献分析失败" in error.value for error in app.error)
