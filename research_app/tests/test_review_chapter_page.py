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
    assert any("没有任何全文证据" in error.value for error in app.error)
    assert not [button for button in app.button if button.label == "生成章节草稿"]


def _add_evidence(project_id, source="ai_raw"):
    task = storage.create_ai_task(
        project_id,
        kind="evidence",
        model="test-model",
        prompt_version="evidence-v1",
        input_hash="hash",
    )
    data = {
        "methods": "模型",
        "findings": "成本下降",
        "limitations": "样本小",
        "evidence_quotes": [{"quote": "cost reduced"}],
    }
    storage.save_evidence_items(
        project_id,
        task,
        [
            {
                "paper_id": "paper:a",
                "title": "论文 A",
                "year": 2020,
                "data": data,
                "reviewed": source != "ai_raw",
            }
        ],
    )
    storage.append_evidence_version(
        project_id,
        "paper:a",
        task,
        source=source,
        data=data,
        quote_checks=[{"quote": "cost reduced", "verified": True}],
        model="test-model",
        prompt_version="evidence-v1",
        title="论文 A",
        year=2020,
    )
    return task


def test_page_enables_generation_when_fulltext_evidence_exists(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = _project_with_candidate()
    _add_evidence(project_id, source="ai_raw")

    app = _run(project_id)

    assert not app.exception
    buttons = [button for button in app.button if button.label == "生成章节草稿"]
    assert len(buttons) == 1
    assert buttons[0].disabled is True
    metrics = {metric.label: metric.value for metric in app.metric}
    # AI 原始提取不能算「已核验」
    assert int(metrics["全文已核验"]) == 0
    assert int(metrics["全文待确认"]) == 1
    assert int(metrics["仅题摘"]) == 0
    assert any("未经人工确认" in warning.value for warning in app.warning)


def test_page_counts_human_reviewed_evidence_as_verified(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = _project_with_candidate()
    _add_evidence(project_id, source="human_corrected")

    app = _run(project_id)

    assert not app.exception
    metrics = {metric.label: metric.value for metric in app.metric}
    assert int(metrics["全文已核验"]) == 1
    assert int(metrics["全文待确认"]) == 0
    assert not [warning for warning in app.warning if "未经人工确认" in warning.value]


def _seed_draft(project_id, sections, *, tables=None, title="第 2 章 文献综述"):
    return {
        "sources": [],
        "sections": sections,
        "tables": tables or [],
        "leads": [],
        "chapter_title": title,
        "question": "埋深对经济性的影响",
        "model": "test-model",
        "generated_text": "",
    }


def test_page_offers_the_five_editing_stages(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = _project_with_candidate()
    _add_evidence(project_id, source="human_corrected")

    app = _run(project_id)

    assert not app.exception
    assert len(app.tabs) == 5


def test_page_renders_edit_fields_for_existing_sections(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = _project_with_candidate()
    _add_evidence(project_id, source="human_corrected")
    sections = [
        {"id": "s1", "heading": "述评口径与检索边界", "body": "识别 458 条 [C001]。"},
        {"id": "s2", "heading": "研究空白", "body": "尚无来源回答该问题。"},
    ]
    app = AppTest.from_file(str(PAGE_PATH), default_timeout=30)
    app.session_state["current_project_id"] = project_id
    app.session_state["chapter_project_selector"] = project_id
    app.session_state[f"chapter_ws::{project_id}"] = _seed_draft(project_id, sections)

    app = app.run(timeout=30)

    assert not app.exception
    # 每节一个正文框；另有研究问题与新增小节的初始正文框
    assert len(app.text_area) == 2 + len(sections)
    metrics = {metric.label: metric.value for metric in app.metric}
    assert int(metrics["小节数"]) == 2
    assert int(metrics["引证次数"]) == 1
    assert not any("越界" in error.value for error in app.error)


def test_page_checks_citations_after_manual_edit(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = _project_with_candidate()
    _add_evidence(project_id, source="human_corrected")
    sections = [{"id": "s1", "heading": "述评口径", "body": "手改时写错了编号 [C999]。"}]
    app = AppTest.from_file(str(PAGE_PATH), default_timeout=30)
    app.session_state["current_project_id"] = project_id
    app.session_state["chapter_project_selector"] = project_id
    app.session_state[f"chapter_ws::{project_id}"] = _seed_draft(project_id, sections)

    app = app.run(timeout=30)

    assert not app.exception
    assert any("越界引证" in error.value for error in app.error)


def test_page_warns_before_overwriting_existing_sections(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = _project_with_candidate()
    _add_evidence(project_id, source="human_corrected")
    sections = [{"id": "s1", "heading": "述评口径", "body": "正文 [C001]。"}]
    app = AppTest.from_file(str(PAGE_PATH), default_timeout=30)
    app.session_state["current_project_id"] = project_id
    app.session_state["chapter_project_selector"] = project_id
    app.session_state[f"chapter_ws::{project_id}"] = _seed_draft(project_id, sections)

    app = app.run(timeout=30)

    assert not app.exception
    assert any("覆盖" in warning.value for warning in app.warning)


def test_page_mentions_missing_bibliography(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = _project_with_candidate()
    _add_evidence(project_id, source="human_corrected")

    app = _run(project_id)

    assert not app.exception
    assert any("作者或期刊" in info.value for info in app.info)
