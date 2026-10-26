from research_app.ai_review import (
    evidence_rows,
    export_evidence_csv,
    export_evidence_markdown,
    normalize_evidence_data,
    quote_status,
    verify_quotes,
)


def _evidence():
    return [
        {
            "paper_id": "paper:1",
            "title": "Paper One",
            "year": 2024,
            "model": "test-model",
            "prompt_version": "evidence-v1",
            "generated_at": "2026-01-01T00:00:00+00:00",
            "data": {
                "research_question": "Does it work?",
                "methods": "RCT",
                "sample": "100 patients",
                "findings": "Improvement",
                "limitations": "Single site",
                "evidence_quotes": [
                    {"quote": "We studied 100 patients.", "section": "Methods"},
                    {"quote": "This sentence is invented.", "section": "Results"},
                ],
            },
            "quote_checks": [
                {
                    "quote": "We studied 100 patients.",
                    "section": "Methods",
                    "verified": True,
                },
                {
                    "quote": "This sentence is invented.",
                    "section": "Results",
                    "verified": False,
                },
            ],
            "reviewed": True,
            "reviewer_note": "Checked against PDF.",
        }
    ]


def test_normalize_evidence_data_fills_missing_fields_and_limits_quotes():
    data = normalize_evidence_data(
        {
            "findings": "  ok  ",
            "evidence_quotes": [
                {"quote": "one"},
                "two",
                {"quote": "three"},
                {"quote": "four"},
            ],
        }
    )

    assert data["findings"] == "ok"
    assert data["methods"] == "未提及"
    assert [quote["quote"] for quote in data["evidence_quotes"]] == ["one", "two", "three"]
    assert normalize_evidence_data(None)["findings"] == "未提及"


def test_verify_quotes_normalizes_case_whitespace_and_punctuation():
    data = {
        "evidence_quotes": [
            {"quote": "We studied 100 patients.", "section": "Methods"},
            {"quote": "not present", "section": ""},
        ]
    }

    checks = verify_quotes(data, "we studied\n100 patients")

    assert checks[0]["verified"] is True
    assert checks[1]["verified"] is False
    assert quote_status(checks[0]).startswith("已在")
    assert quote_status(checks[1]).startswith("未在")
    assert quote_status({}) == "未提供自动校验"
    assert quote_status({"verified": None}) == "人工修改，未自动校验"


def test_evidence_rows_and_csv_include_review_metadata():
    rows = evidence_rows(_evidence())

    assert rows[0]["reviewed"] == "是"
    assert rows[0]["reviewer_note"] == "Checked against PDF."
    assert rows[0]["year"] == "2024"
    assert rows[0]["quote_1"] == "We studied 100 patients."
    assert "已在输入文本中匹配" in rows[0]["quote_1_verification"]
    assert "未在输入文本中匹配" in rows[0]["quote_2_verification"]

    csv_text = export_evidence_csv(_evidence()).decode("utf-8-sig")
    header, *data_lines = csv_text.splitlines()
    assert header.startswith("paper_id,title,year,research_question")
    assert "quote_1,quote_1_section,quote_1_verification" in header
    assert len(data_lines) == 1
    assert "paper:1" in data_lines[0]
    assert "test-model" in data_lines[0]
    assert "evidence-v1" in data_lines[0]
    assert "Checked against PDF." in data_lines[0]
    assert "未在输入文本中匹配" in data_lines[0]


def test_evidence_rows_formats_float_years_and_missing_quotes():
    rows = evidence_rows(
        [
            {
                "paper_id": "paper:2",
                "title": "Paper Two",
                "year": 2002.0,
                "data": {"findings": "ok"},
            }
        ]
    )

    assert rows[0]["year"] == "2002"
    assert rows[0]["quote_1"] == ""
    assert rows[0]["quote_1_verification"] == ""


def test_markdown_export_keeps_source_and_verification():
    markdown = export_evidence_markdown(_evidence()).decode("utf-8")

    assert "# AI 证据表" in markdown
    assert "paper:1" in markdown
    assert "test-model" in markdown
    assert "We studied 100 patients." in markdown
    assert "未在输入文本中匹配" in markdown
    assert "Checked against PDF." in markdown


def test_csv_export_with_no_evidence_has_header():
    text = export_evidence_csv([]).decode("utf-8-sig")
    assert text.startswith("paper_id,title,year")
