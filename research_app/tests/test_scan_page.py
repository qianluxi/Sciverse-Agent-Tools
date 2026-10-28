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
