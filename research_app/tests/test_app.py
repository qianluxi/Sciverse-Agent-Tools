import base64
from pathlib import Path

from streamlit.testing.v1 import AppTest

import sciverse.credentials
import research_app.service as service
import research_app.storage as storage


APP_PATH = Path(__file__).parents[1] / "app.py"


def _isolate_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "database_path", lambda: tmp_path / "workbench.sqlite3")


def test_app_renders_without_credentials(monkeypatch, tmp_path):
    _isolate_storage(monkeypatch, tmp_path)
    monkeypatch.delenv("SCIVERSE_API_TOKEN", raising=False)
    monkeypatch.setattr(sciverse.credentials, "resolve_token", lambda *_args: None)

    app = AppTest.from_file(str(APP_PATH), default_timeout=30).run(timeout=30)

    assert not app.exception
    assert app.radio[0].value == "智能语义检索"
    assert app.text_area[0].label == "你想了解什么？"


def test_structured_search_reads_text_and_figure_offline(monkeypatch, tmp_path):
    _isolate_storage(monkeypatch, tmp_path)
    monkeypatch.delenv("SCIVERSE_API_TOKEN", raising=False)
    monkeypatch.setattr(sciverse.credentials, "resolve_token", lambda *_args: None)
    actions = []
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jXioAAAAASUVORK5CYII="
    )
    read_count = 0

    def fake_call_api(token, action, **kwargs):
        nonlocal read_count
        assert token == "sv-offline-test"
        actions.append((action, kwargs))
        if action == "search_papers":
            return {
                "results": [
                    {
                        "title": "Fixture paper",
                        "author": [{"name": "Researcher"}],
                        "unique_id": "paper:test",
                        "doc_id": "doc-test",
                        "is_content_accessible": True,
                    }
                ],
                "total_count": 1,
                "page": 1,
                "page_size": 20,
            }
        if action == "read_content":
            read_count += 1
            if read_count > 1:
                return {
                    "text": "Continued excerpt",
                    "bytes_returned": 17,
                    "next_offset": 57,
                    "more": False,
                }
            return {
                "text": "Evidence excerpt\n\n![Figure](figure.png)",
                "bytes_returned": 40,
                "next_offset": 40,
                "more": True,
            }
        if action == "get_resource":
            return image, "image/png"
        raise AssertionError(f"Unexpected API action: {action}")

    monkeypatch.setattr(service, "call_api", fake_call_api)
    app = AppTest.from_file(str(APP_PATH), default_timeout=30).run(timeout=30)
    app.radio[0].set_value("条件筛选").run(timeout=30)
    next(widget for widget in app.text_input if widget.label == "API Token").set_value(
        "sv-offline-test"
    )
    next(widget for widget in app.text_input if widget.label == "关键词或题名（可选）").set_value(
        "climate"
    )
    next(button for button in app.button if button.label == "筛选论文").click().run(timeout=30)

    assert not app.exception
    assert any(action == "search_papers" for action, _ in actions)
    assert actions[0][1]["query"] == "climate"
    assert actions[0][1]["page_size"] == 20
    assert any("Fixture paper" in block.value for block in app.markdown)

    next(button for button in app.button if button.label == "查看文献").click().run(timeout=30)
    next(button for button in app.button if button.label == "读取论文原文").click().run(timeout=30)

    assert not app.exception
    assert any("Evidence excerpt" in block.value for block in app.markdown)
    assert [action for action, _ in actions][-2:] == ["read_content", "get_resource"]

    next(button for button in app.button if button.label == "继续阅读下一段").click().run(
        timeout=30
    )

    assert not app.exception
    assert any("Continued excerpt" in block.value for block in app.markdown)
    assert read_count == 2


def test_semantic_search_uses_hits_response_offline(monkeypatch, tmp_path):
    _isolate_storage(monkeypatch, tmp_path)
    monkeypatch.delenv("SCIVERSE_API_TOKEN", raising=False)
    monkeypatch.setattr(sciverse.credentials, "resolve_token", lambda *_args: None)
    calls = []

    def fake_call_api(token, action, **kwargs):
        calls.append((action, kwargs))
        assert token == "sv-offline-test"
        assert action == "semantic_search"
        return {
            "hits": [
                {
                    "title": "Semantic fixture",
                    "doc_id": "doc-semantic",
                    "offset": 10,
                    "score": 0.9,
                    "chunk": "Relevant evidence chunk",
                }
            ]
        }

    monkeypatch.setattr(service, "call_api", fake_call_api)
    app = AppTest.from_file(str(APP_PATH), default_timeout=30).run(timeout=30)
    next(widget for widget in app.text_input if widget.label == "API Token").set_value(
        "sv-offline-test"
    )
    app.text_area[0].set_value("How does this topic work?")
    next(button for button in app.button if button.label == "查找相关文献").click().run(timeout=30)

    assert not app.exception
    assert calls[0][0] == "semantic_search"
    assert calls[0][1]["query"] == "How does this topic work?"
    assert calls[0][1]["mode"] == "balanced"
    assert any("Semantic fixture" in block.value for block in app.markdown)
    assert any("Relevant evidence chunk" in block.value for block in app.markdown)


def test_empty_structured_results_show_guidance_offline(monkeypatch, tmp_path):
    _isolate_storage(monkeypatch, tmp_path)
    monkeypatch.delenv("SCIVERSE_API_TOKEN", raising=False)
    monkeypatch.setattr(sciverse.credentials, "resolve_token", lambda *_args: None)
    monkeypatch.setattr(
        service,
        "call_api",
        lambda *_args, **_kwargs: {
            "results": [],
            "total_count": 0,
            "page": 1,
            "page_size": 20,
        },
    )

    app = AppTest.from_file(str(APP_PATH), default_timeout=30).run(timeout=30)
    app.radio[0].set_value("条件筛选").run(timeout=30)
    next(widget for widget in app.text_input if widget.label == "API Token").set_value(
        "sv-offline-test"
    )
    next(widget for widget in app.text_input if widget.label == "关键词或题名（可选）").set_value(
        "unlikely query"
    )
    next(button for button in app.button if button.label == "筛选论文").click().run(timeout=30)

    assert not app.exception
    assert any("没有找到符合条件的文献" in info.value for info in app.info)


def test_structured_pagination_and_common_filters_offline(monkeypatch, tmp_path):
    _isolate_storage(monkeypatch, tmp_path)
    monkeypatch.delenv("SCIVERSE_API_TOKEN", raising=False)
    monkeypatch.setattr(sciverse.credentials, "resolve_token", lambda *_args: None)
    calls = []

    def fake_call_api(token, action, **kwargs):
        assert token == "sv-offline-test"
        calls.append((action, kwargs))
        page = kwargs["page"]
        return {
            "results": [{"title": f"Fixture paper {page}", "doc_id": f"doc-{page}"}],
            "total_count": 21,
            "page": page,
            "page_size": kwargs["page_size"],
            "total_pages": 2,
        }

    monkeypatch.setattr(service, "call_api", fake_call_api)
    app = AppTest.from_file(str(APP_PATH), default_timeout=30).run(timeout=30)
    app.radio[0].set_value("条件筛选").run(timeout=30)
    next(widget for widget in app.text_input if widget.label == "API Token").set_value(
        "sv-offline-test"
    )
    next(widget for widget in app.text_input if widget.label == "关键词或题名（可选）").set_value(
        "climate"
    )
    next(widget for widget in app.text_input if widget.label.startswith("作者姓名")).set_value(
        "Hinton, LeCun"
    )
    next(widget for widget in app.text_input if widget.label.startswith("期刊/会议")).set_value(
        "Nature, Science"
    )
    next(widget for widget in app.text_input if widget.label.startswith("学科主题")).set_value(
        "biology; medicine"
    )
    next(widget for widget in app.number_input if widget.label == "起始年份").set_value(2020)
    next(button for button in app.button if button.label == "筛选论文").click().run(timeout=30)

    assert not app.exception
    action, params = calls[0]
    assert action == "search_papers"
    assert params["page"] == 1
    assert params["authors"] == ["Hinton", "LeCun"]
    assert params["journals"] == ["Nature", "Science"]
    assert params["subjects"] == ["biology", "medicine"]
    assert params["year_from"] == 2020
    assert any("Fixture paper 1" in block.value for block in app.markdown)

    next(button for button in app.button if button.label == "下一页").click().run(timeout=30)

    assert calls[-1][1]["page"] == 2
    assert any("Fixture paper 2" in block.value for block in app.markdown)

    next(widget for widget in app.text_input if widget.label == "检索名称").set_value(
        "Climate project"
    )
    next(button for button in app.button if button.label == "保存检索").click().run(timeout=30)
    saved = storage.list_saved_searches()
    recent = storage.list_recent_searches()
    assert saved[0]["name"] == "Climate project"
    assert saved[0]["params"]["page"] == 1
    assert recent[0]["params"]["page"] == 1


def test_catalog_fields_build_typed_advanced_filter_offline(monkeypatch, tmp_path):
    _isolate_storage(monkeypatch, tmp_path)
    monkeypatch.delenv("SCIVERSE_API_TOKEN", raising=False)
    monkeypatch.setattr(sciverse.credentials, "resolve_token", lambda *_args: None)
    calls = []

    def fake_call_api(token, action, **kwargs):
        assert token == "sv-offline-test"
        calls.append((action, kwargs))
        if action == "list_catalog":
            return {
                "fields": [
                    {
                        "name": "citation_count",
                        "type": "Integer",
                        "filterable": True,
                        "operators": ["GTE"],
                        "description": "Number of citations",
                    }
                ],
                "filter_operators": ["EQ", "IN", "GTE"],
            }
        if action == "search_papers":
            return {"results": [], "total_count": 0, "page": 1, "page_size": 20}
        raise AssertionError(f"Unexpected API action: {action}")

    monkeypatch.setattr(service, "call_api", fake_call_api)
    app = AppTest.from_file(str(APP_PATH), default_timeout=30).run(timeout=30)
    next(widget for widget in app.text_input if widget.label == "API Token").set_value(
        "sv-offline-test"
    ).run(timeout=30)
    next(button for button in app.button if button.label == "加载可用字段").click().run(timeout=30)
    app.radio[0].set_value("条件筛选").run(timeout=30)
    next(widget for widget in app.selectbox if widget.label == "目录字段").set_value(
        "citation_count"
    )
    next(widget for widget in app.selectbox if widget.label == "匹配方式").set_value(
        "FILTER_OP_GTE"
    )
    next(widget for widget in app.text_input if widget.label == "过滤值").set_value("100")
    next(button for button in app.button if button.label == "筛选论文").click().run(timeout=30)

    search = next(kwargs for action, kwargs in calls if action == "search_papers")
    assert search["filters_advanced"] == [
        {"field": "citation_count", "operator": "FILTER_OP_GTE", "value": 100}
    ]
    assert not app.exception


def test_relation_list_uses_unique_id_and_requested_page(monkeypatch, tmp_path):
    _isolate_storage(monkeypatch, tmp_path)
    monkeypatch.delenv("SCIVERSE_API_TOKEN", raising=False)
    monkeypatch.setattr(sciverse.credentials, "resolve_token", lambda *_args: None)
    calls = []

    def fake_call_api(token, action, **kwargs):
        assert token == "sv-offline-test"
        calls.append((action, kwargs))
        if action == "search_papers":
            return {
                "results": [{"title": "Seed paper", "unique_id": "paper:seed"}],
                "total_count": 1,
                "page": 1,
                "page_size": 20,
            }
        if action == "list_paper_relations":
            return {
                "items": [{"id": "10.1/cited", "id_type": "DOI", "title": "Citing paper"}],
                "total_count": 30,
                "page": kwargs["page"],
                "page_size": kwargs["page_size"],
                "total_pages": 2,
            }
        raise AssertionError(f"Unexpected API action: {action}")

    monkeypatch.setattr(service, "call_api", fake_call_api)
    app = AppTest.from_file(str(APP_PATH), default_timeout=30).run(timeout=30)
    app.radio[0].set_value("条件筛选").run(timeout=30)
    next(widget for widget in app.text_input if widget.label == "API Token").set_value(
        "sv-offline-test"
    )
    next(widget for widget in app.text_input if widget.label == "关键词或题名（可选）").set_value(
        "seed"
    )
    next(button for button in app.button if button.label == "筛选论文").click().run(timeout=30)
    next(button for button in app.button if button.label == "查看文献").click().run(timeout=30)
    next(widget for widget in app.number_input if widget.label == "关系页码").set_value(2)
    next(button for button in app.button if button.label == "加载关系列表").click().run(timeout=30)

    relation_call = next(args for action, args in calls if action == "list_paper_relations")
    assert relation_call == {
        "unique_id": "paper:seed",
        "relation": "REFERENCES",
        "page": 2,
        "page_size": 25,
    }
    assert not app.exception
    assert any("Citing paper" in block.value for block in app.markdown)


def test_export_selection_survives_page_changes_and_resets_for_new_search(monkeypatch, tmp_path):
    _isolate_storage(monkeypatch, tmp_path)
    monkeypatch.delenv("SCIVERSE_API_TOKEN", raising=False)
    monkeypatch.setattr(sciverse.credentials, "resolve_token", lambda *_args: None)

    def fake_call_api(token, action, **kwargs):
        assert token == "sv-offline-test"
        assert action == "search_papers"
        page = kwargs["page"]
        return {
            "results": [
                {
                    "title": f"Fixture paper {page}",
                    "unique_id": f"paper:{page}",
                    "doc_id": f"doc-{page}",
                }
            ],
            "total_count": 2,
            "page": page,
            "page_size": 1,
            "total_pages": 2,
        }

    monkeypatch.setattr(service, "call_api", fake_call_api)
    app = AppTest.from_file(str(APP_PATH), default_timeout=30).run(timeout=30)
    app.radio[0].set_value("条件筛选").run(timeout=30)
    next(widget for widget in app.text_input if widget.label == "API Token").set_value(
        "sv-offline-test"
    )
    next(widget for widget in app.text_input if widget.label == "关键词或题名（可选）").set_value(
        "first"
    )
    next(widget for widget in app.selectbox if widget.label == "每页结果").set_value(10)
    next(button for button in app.button if button.label == "筛选论文").click().run(timeout=30)
    next(widget for widget in app.checkbox if widget.label == "加入导出清单").set_value(True).run(
        timeout=30
    )
    next(button for button in app.button if button.label == "下一页").click().run(timeout=30)
    next(widget for widget in app.checkbox if widget.label == "加入导出清单").set_value(True).run(
        timeout=30
    )

    assert len(app.session_state["export_selection"]) == 2

    next(widget for widget in app.text_input if widget.label == "关键词或题名（可选）").set_value(
        "second"
    )
    next(button for button in app.button if button.label == "筛选论文").click().run(timeout=30)

    assert app.session_state["export_selection"] == {}
