from pathlib import Path

from streamlit.testing.v1 import AppTest


PAGE_PATH = Path(__file__).parents[1] / "pages" / "1_新手指引.py"


def test_guide_page_renders_concrete_example():
    app = AppTest.from_file(str(PAGE_PATH), default_timeout=30).run(timeout=30)

    assert not app.exception
    assert any("新手指引" in title.value for title in app.title)
    content = "\n".join(markdown.value for markdown in app.markdown)
    assert "RC 柱抗震加固-示例" in content
    assert "seismic retrofit reinforced concrete column" in content
    assert "题名/摘要纳入" in content
