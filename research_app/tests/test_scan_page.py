from pathlib import Path

from streamlit.testing.v1 import AppTest

import research_app.field_scan as field_scan
import research_app.storage as storage


PAGE_PATH = Path(__file__).parents[1] / "pages" / "7_AI领域扫描.py"


def test_scan_page_runs_and_saves_results(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "database_path", lambda: tmp_path / "workbench.sqlite3")
    project_id = storage.create_screening_project("Scan project", "Q", "I", "E")

    def fake_scan(client, documents):
        item = {
            "paper_id": documents[0]["paper_id"],
            "title": documents[0]["title"],
            "year": documents[0]["year"],
            "data": {
                "category": "foundational",
                "core_summary": "经典研究。",
                "rationale": "早期工作。",
                "confidence": "high",
                "evidence_quotes": [],
            },
            "quote_checks": [],
            "error": None,
        }
        return {
            "items": [item],
            "themes": [{"theme": "剪力墙", "summary": "主题", "paper_ids": [item["paper_id"]]}],
            "gaps": [{"gap": "缺少长期数据", "evidence": "样本有限", "paper_ids": [item["paper_id"]]}],
            "aggregation_error": None,
            "model": "test-model",
            "prompt_version": "field-scan-v1",
        }

    monkeypatch.setattr(field_scan, "scan_documents", fake_scan)
    app = AppTest.from_file(str(PAGE_PATH), default_timeout=30)
    app.session_state["current_project_id"] = project_id
    app.session_state["scan_project_selector"] = project_id
    app.session_state["ai_key_session"] = "test-key"
    app.session_state["ai_model"] = "test-model"
    app.session_state["papers"] = [
        {
            "title": "Classic paper",
            "unique_id": "paper:a",
            "abstract": "This is a classic study.",
            "publication_published_year": 2002,
        }
    ]
    app.run(timeout=30)
    app.multiselect[0].set_value(["paper:a"]).run(timeout=30)
    next(button for button in app.button if button.label == "开始领域扫描").click().run(
        timeout=30
    )

    assert not app.exception
    saved = storage.load_latest_scan(project_id)
    assert saved is not None
    assert saved["items"][0]["data"]["category"] == "foundational"
    assert saved["themes"][0]["theme"] == "剪力墙"
    assert any("Classic paper" in block.value for block in app.markdown)
    downloads = app.get("download_button")
    assert any(
        button.label == "下载可读报告（Markdown）" for button in downloads
    )
    assert any(button.label == "下载原始数据（JSON）" for button in downloads)


def _seed_scan(
    project_id,
    paper_id,
    *,
    title,
    summary,
    reviewed,
    note="",
):
    task_id = storage.create_ai_task(
        project_id,
        kind="field_scan",
        model="test-model",
        prompt_version="field-scan-v1",
        input_hash=f"hash-{project_id}-{paper_id}",
    )
    storage.save_evidence_items(
        project_id,
        task_id,
        [
            {
                "paper_id": paper_id,
                "title": title,
                "year": 2024,
                "data": {
                    "category": "foundational",
                    "core_summary": summary,
                    "rationale": "Seeded rationale",
                    "confidence": "high",
                    "evidence_quotes": [],
                },
                "quote_checks": [],
                "reviewed": reviewed,
                "reviewer_note": note,
                "model": "test-model",
                "prompt_version": "field-scan-v1",
            }
        ],
    )
    storage.save_scan_report(project_id, task_id, [], [])
    storage.finish_ai_task(task_id, status="completed")
    return task_id


def _app_for_project(project_id, paper_id):
    app = AppTest.from_file(str(PAGE_PATH), default_timeout=30)
    app.session_state["current_project_id"] = project_id
    app.session_state["scan_project_selector"] = project_id
    app.session_state["ai_key_session"] = "test-key"
    app.session_state["ai_model"] = "test-model"
    app.session_state["papers"] = [
        {
            "title": "Shared paper",
            "unique_id": paper_id,
            "abstract": "Shared abstract.",
            "publication_published_year": 2024,
        }
    ]
    return app.run(timeout=30)


def test_scan_edit_state_is_isolated_by_project_and_task(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "database_path", lambda: tmp_path / "workbench.sqlite3")
    project_a = storage.create_screening_project("Scan project A", "Q", "I", "E")
    project_b = storage.create_screening_project("Scan project B", "Q", "I", "E")
    _seed_scan(
        project_a,
        "paper:shared",
        title="Shared paper",
        summary="Project A summary",
        reviewed=True,
        note="A note",
    )
    _seed_scan(
        project_b,
        "paper:shared",
        title="Shared paper",
        summary="Project B summary",
        reviewed=False,
        note="B note",
    )
    app = _app_for_project(project_a, "paper:shared")

    summary_a = next(area for area in app.text_area if area.label == "核心概括")
    assert summary_a.value == "Project A summary"
    summary_a.set_value("Project A edited").run(timeout=30)
    assert (
        storage.load_latest_scan(project_a)["items"][0]["data"]["core_summary"]
        == "Project A edited"
    )

    project_selector = next(
        box for box in app.selectbox if box.label == "当前研究项目"
    )
    project_selector.set_value(project_b).run(timeout=30)

    summary_b = next(area for area in app.text_area if area.label == "核心概括")
    reviewed_b = next(
        checkbox
        for checkbox in app.checkbox
        if checkbox.label == "我已核对这篇文献的扫描结果"
    )
    assert summary_b.value == "Project B summary"
    assert reviewed_b.value is False
    assert not app.exception
    saved_b = storage.load_latest_scan(project_b)
    assert saved_b["items"][0]["data"]["core_summary"] == "Project B summary"
    assert saved_b["items"][0]["reviewed"] is False
    assert (
        storage.load_latest_scan(project_a)["items"][0]["data"]["core_summary"]
        == "Project A edited"
    )


def test_scan_page_marks_all_failed_task_as_failed(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "database_path", lambda: tmp_path / "workbench.sqlite3")
    project_id = storage.create_screening_project("Scan project", "Q", "I", "E")

    def fake_scan(client, documents):
        return {
            "items": [
                {
                    "paper_id": documents[0]["paper_id"],
                    "title": documents[0]["title"],
                    "year": documents[0]["year"],
                    "data": None,
                    "quote_checks": [],
                    "error": "batch response was invalid",
                }
            ],
            "themes": [],
            "gaps": [],
            "aggregation_error": "summary request failed",
            "model": "test-model",
            "prompt_version": "field-scan-v1",
        }

    monkeypatch.setattr(field_scan, "scan_documents", fake_scan)
    app = _app_for_project(project_id, "paper:failed")
    app.multiselect[0].set_value(["paper:failed"]).run(timeout=30)
    next(button for button in app.button if button.label == "开始领域扫描").click().run(
        timeout=30
    )

    saved = storage.load_latest_scan(project_id)
    assert not app.exception
    assert saved["task"]["status"] == "failed"
    assert "全部 1 篇文献分析失败" in saved["task"]["error"]
    assert saved["aggregation_error"] == "summary request failed"
    assert any("主题与空白汇总失败" in warning.value for warning in app.warning)


def test_scan_page_keeps_results_when_aggregation_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "database_path", lambda: tmp_path / "workbench.sqlite3")
    project_id = storage.create_screening_project("Scan project", "Q", "I", "E")

    def fake_scan(client, documents):
        return {
            "items": [
                {
                    "paper_id": documents[0]["paper_id"],
                    "title": documents[0]["title"],
                    "year": documents[0]["year"],
                    "data": {
                        "category": "method",
                        "core_summary": "Successful item",
                        "rationale": "Valid response",
                        "confidence": "medium",
                        "evidence_quotes": [],
                    },
                    "quote_checks": [],
                    "error": None,
                }
            ],
            "themes": [],
            "gaps": [],
            "aggregation_error": "summary request failed",
            "model": "test-model",
            "prompt_version": "field-scan-v1",
        }

    monkeypatch.setattr(field_scan, "scan_documents", fake_scan)
    app = _app_for_project(project_id, "paper:partial")
    app.multiselect[0].set_value(["paper:partial"]).run(timeout=30)
    next(button for button in app.button if button.label == "开始领域扫描").click().run(
        timeout=30
    )

    saved = storage.load_latest_scan(project_id)
    assert not app.exception
    assert saved["task"]["status"] == "completed"
    assert saved["items"][0]["data"]["core_summary"] == "Successful item"
    assert saved["aggregation_error"] == "summary request failed"
    assert any("主题与空白汇总失败" in warning.value for warning in app.warning)
    assert any("主题汇总失败" in caption.value for caption in app.caption)
