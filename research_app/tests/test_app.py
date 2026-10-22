from pathlib import Path

from streamlit.testing.v1 import AppTest

import sciverse.credentials


APP_PATH = Path(__file__).parents[1] / "app.py"


def test_app_renders_without_credentials(monkeypatch):
    monkeypatch.delenv("SCIVERSE_API_TOKEN", raising=False)
    monkeypatch.setattr(sciverse.credentials, "resolve_token", lambda *_args: None)

    app = AppTest.from_file(str(APP_PATH), default_timeout=30).run(timeout=30)

    assert not app.exception
    assert app.radio[0].value == "智能语义检索"
    assert app.text_area[0].label == "你想了解什么？"
