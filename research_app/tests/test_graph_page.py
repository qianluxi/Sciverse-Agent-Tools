from pathlib import Path

from streamlit.testing.v1 import AppTest

import research_app.service as service
import sciverse.credentials


PAGE_PATH = Path(__file__).parents[1] / "pages" / "3_引用网络.py"


def test_graph_page_loads_and_exports_network(monkeypatch):
    monkeypatch.setattr(sciverse.credentials, "resolve_token", lambda *_args: "test-token")
    calls = []

    def fake_call_api(token, action, **kwargs):
        calls.append((action, kwargs))
        assert token == "test-token"
        if action == "list_paper_relations":
            assert kwargs["unique_id"] == "paper:center"
            return {
                "items": [
                    {"id": "10.1000/a", "id_type": "doi", "title": "Paper A"},
                    {"id": "paper:b", "id_type": "unique_id", "title": "Paper B"},
                ]
            }
        if action == "search_papers":
            field = kwargs["filters_advanced"][0]["field"]
            if field == "doi":
                return {
                    "results": [
                        {
                            "doi": "10.1000/a",
                            "title": "Paper A",
                            "publication_published_year": 2021,
                            "publication_venue_name_unified": "Journal A",
                        }
                    ]
                }
            return {
                "results": [
                    {
                        "unique_id": "paper:b",
                        "title": "Paper B",
                        "publication_published_year": 2022,
                        "publication_venue_name_unified": "Journal B",
                    }
                ]
            }
        raise AssertionError(f"Unexpected API action: {action}")

    monkeypatch.setattr(service, "call_api", fake_call_api)
    app = AppTest.from_file(str(PAGE_PATH), default_timeout=30).run(timeout=30)
    app.session_state["papers"] = [
        {
            "title": "Center paper",
            "unique_id": "paper:center",
            "publication_published_year": 2020,
        }
    ]
    app.run(timeout=30)
    next(button for button in app.button if button.label == "加载一层引用网络").click().run(
        timeout=30
    )

    assert not app.exception
    assert calls[0][0] == "list_paper_relations"
    assert len(app.get("graphviz_chart")) == 1
    rows = str(app.dataframe[0].value)
    assert all(title in rows for title in ("Center paper", "Paper A", "Paper B"))
    download_labels = {button.label for button in app.get("download_button")}
    assert {"导出 GraphML", "导出边列表 CSV"} <= download_labels

    next(button for button in app.button if button.label == "补全节点元数据").click().run(
        timeout=30
    )

    assert not app.exception
    assert "Journal A" in str(app.dataframe[0].value)
    assert "2022" in str(app.dataframe[0].value)
    assert any(metric.value == "3" for metric in app.metric if metric.label == "元数据请求次数")
