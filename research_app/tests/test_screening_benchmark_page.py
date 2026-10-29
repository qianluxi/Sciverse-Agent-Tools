from pathlib import Path

from streamlit.testing.v1 import AppTest

import research_app.ai_screening as ai_screening
import research_app.storage as storage


PAGE_PATH = Path(__file__).parents[1] / "pages" / "9_筛选基准评估.py"


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "database_path", lambda: tmp_path / "workbench.sqlite3")
    project_id = storage.create_screening_project(
        "Benchmark",
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
                "abstract": "Randomized trial of X.",
            },
            {
                "title": "Excluded study",
                "unique_id": "paper:b",
                "abstract": "Bridge study.",
            },
        ],
    )
    storage.set_screening_decision(project_id, "uid:paper:a", "included")
    storage.set_screening_decision(
        project_id,
        "uid:paper:b",
        "excluded",
        reason="主题不相关",
    )
    return project_id


def _fake_benchmark(client, criteria, candidates):
    suggestions = []
    for candidate in candidates:
        is_eligible = candidate["candidate_key"] == "uid:paper:a"
        suggestions.append(
            {
                "candidate_key": candidate["candidate_key"],
                "paper_id": candidate["paper_id"],
                "title": candidate["title"],
                "year": candidate.get("year"),
                "suggestion": "include" if is_eligible else "exclude",
                "criteria": ["Randomized trials of X"] if is_eligible else ["Not about X"],
                "rationale": "Gold-aligned test suggestion",
                "confidence": "high" if is_eligible else "medium",
                "evidence_quotes": [],
                "quote_checks": [],
                "error": None,
            }
        )
    return {
        "items": suggestions,
        "model": "test-model",
        "prompt_version": "screening-v1",
    }


def test_benchmark_page_evaluates_against_fixed_gold_without_changing_status(
    monkeypatch, tmp_path
):
    project_id = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(ai_screening, "suggest_screening", _fake_benchmark)
    app = AppTest.from_file(str(PAGE_PATH), default_timeout=30)
    app.session_state["current_project_id"] = project_id
    app.session_state["benchmark_project_selector"] = project_id
    app.session_state["ai_key_session"] = "test-key"
    app.session_state["ai_model"] = "test-model"
    app.run(timeout=30)

    next(
        button for button in app.button if button.label == "生成基准筛选建议"
    ).click().run(timeout=30)

    assert not app.exception
    saved = storage.load_latest_screening_suggestions(
        project_id, kind="screening_benchmark"
    )
    assert saved is not None
    assert saved["task"]["status"] == "completed"
    candidates = storage.list_all_screening_candidates(project_id)
    assert {candidate["candidate_key"]: candidate["status"] for candidate in candidates} == {
        "uid:paper:a": "included",
        "uid:paper:b": "excluded",
    }
    assert any(
        metric.label == "纳入召回率" and metric.value == "100.0%"
        for metric in app.metric
    )
    assert any(
        metric.label == "错误排除率" and metric.value == "0.0%"
        for metric in app.metric
    )
    assert any("金标准纳入" in str(frame.value) for frame in app.dataframe)
    snapshot = storage.load_screening_snapshot(project_id, saved["task"]["id"])
    assert snapshot["gold"]["uid:paper:a"] == "included"
    assert snapshot["evaluation"]["metrics"]["include_recall"] == 1.0

    storage.set_screening_decision(
        project_id,
        "uid:paper:a",
        "excluded",
        reason="其他",
    )
    app.run(timeout=30)

    assert not app.exception
    assert any(
        metric.label == "纳入召回率" and metric.value == "100.0%"
        for metric in app.metric
    )
    assert (
        storage.load_screening_snapshot(project_id, saved["task"]["id"])["gold"][
            "uid:paper:a"
        ]
        == "included"
    )
