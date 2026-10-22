import csv
import io

from research_app.exports import (
    export_bibtex,
    export_markdown,
    export_results_csv,
    export_ris,
    export_screening_csv,
    format_authors,
)


def test_export_results_csv_preserves_unicode_and_semantic_chunks():
    records = [
        {
            "title": "研究文献",
            "author": [{"name": "张三"}, {"name": "Li"}],
            "publication_published_year": 2025,
            "doi": "10.1/example",
            "chunk": "semantic evidence",
            "doc_id": "doc-1",
        }
    ]

    output = export_results_csv(records).decode("utf-8-sig")
    row = next(csv.DictReader(io.StringIO(output)))

    assert row["title"] == "研究文献"
    assert row["authors"] == "张三, Li"
    assert row["abstract"] == "semantic evidence"
    assert row["doc_id"] == "doc-1"


def test_format_authors_accepts_string_and_empty_values():
    assert format_authors({"author": "Single author"}) == "Single author"
    assert format_authors({}) == ""


def test_ris_export_has_repeated_authors_and_record_terminator():
    data = export_ris(
        [
            {
                "title": "研究 & 方法",
                "author": [{"name": "张三"}, {"name": "Li"}],
                "publication_published_year": 2024,
                "publication_venue_name_unified": "期刊 A",
                "doi": "10.1000/test",
            }
        ]
    ).decode("utf-8")

    assert "TI  - 研究 & 方法" in data
    assert "AU  - 张三\nAU  - Li" in data
    assert "DO  - 10.1000/test" in data
    assert data.rstrip().endswith("ER  -")


def test_bibtex_export_escapes_fields_and_unique_keys():
    papers = [
        {"title": "A & B_1", "author": [{"name": "A Writer"}], "doi": "10.1000/same"},
        {"title": "Second", "author": [{"name": "B Writer"}], "doi": "10.1000/same"},
    ]

    data = export_bibtex(papers).decode("utf-8")

    assert "@misc{10_1000_same," in data
    assert "@misc{10_1000_same_2," in data
    assert r"A \& B\_1" in data
    assert "author = {A Writer}" in data


def test_markdown_export_contains_metadata_and_doi_link():
    data = export_markdown(
        [{"title": "A paper", "author": [{"name": "Researcher"}], "doi": "10.1000/x"}]
    ).decode("utf-8")

    assert "## A paper" in data
    assert "Researcher" in data
    assert "https://doi.org/10.1000/x" in data


def test_screening_csv_contains_decision_and_audit_fields():
    data = export_screening_csv(
        [
            {
                "candidate_key": "doi:10.1/x",
                "paper_id": "paper:1",
                "title": "筛选文献",
                "year": 2024,
                "doi": "10.1/x",
                "status": "excluded",
                "exclusion_reason": "主题不相关",
                "notes": "不符合研究问题",
                "updated_at": "2026-10-22T00:00:00+00:00",
            }
        ]
    ).decode("utf-8-sig")

    assert "排除" in data
    assert "主题不相关" in data
    assert "不符合研究问题" in data
