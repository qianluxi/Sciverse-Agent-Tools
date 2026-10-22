import csv
import io

from research_app.exports import export_results_csv, format_authors


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
