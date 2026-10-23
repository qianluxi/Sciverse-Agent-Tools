import base64
from pathlib import Path

from streamlit.testing.v1 import AppTest

import research_app.service as service
import sciverse.credentials


PAGE_PATH = Path(__file__).parents[1] / "pages" / "5_图表证据板.py"


def test_evidence_page_extracts_figures_offline(monkeypatch):
    monkeypatch.setattr(sciverse.credentials, "resolve_token", lambda *_args: "test-token")
    calls = []
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jXioAAAAASUVORK5CYII="
    )

    def fake_call_api(token, action, **kwargs):
        calls.append((action, kwargs))
        assert token == "test-token"
        if action == "read_content":
            return {
                "text": "Evidence\n\n![Figure 1](dt=x/p1/f1.png)\n![Figure 2](dt=x/p1/f2.png)",
                "bytes_returned": 10,
                "next_offset": 10,
                "more": False,
            }
        if action == "get_resource":
            return png, "image/png"
        raise AssertionError(action)

    monkeypatch.setattr(service, "call_api", fake_call_api)
    app = AppTest.from_file(str(PAGE_PATH), default_timeout=30).run(timeout=30)
    app.session_state["papers"] = [
        {
            "title": "Paper",
            "unique_id": "paper:1",
            "doc_id": "doc-1",
            "is_content_accessible": True,
        }
    ]
    app.run(timeout=30)
    app.multiselect[0].set_value(["paper:1"]).run(timeout=30)
    next(button for button in app.button if button.label == "提取图表").click().run(timeout=30)

    assert not app.exception
    items = app.session_state["evidence_items"]
    assert len(items) == 2
    assert [item["file_name"] for item in items] == ["dt=x/p1/f1.png", "dt=x/p1/f2.png"]
    assert app.session_state["evidence_requests"] == 3
    download_keys = {button.key for button in app.get("download_button")}
    assert {"download_evidence_csv", "download_evidence_zip"} <= download_keys
