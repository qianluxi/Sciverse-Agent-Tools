import importlib.util
import io
import zipfile

import pytest

from research_app.trends import analyze_papers, export_trends_png, export_trends_zip


def _papers():
    return [
        {
            "doi": "10.1/a",
            "unique_id": "paper:a",
            "title": "A",
            "publication_published_year": 2024,
            "publication_venue_name_unified": "Venue A",
            "author": [{"name": "Alice"}],
            "keywords": ["ml", "nlp"],
            "citation_count": 10,
        },
        {
            "doi": "10.1/b",
            "unique_id": "paper:b",
            "title": "B",
            "publication_published_year": 2023,
            "publication_venue_name_unified": "Venue A",
            "author": [{"name": "Bob"}],
            "keywords": ["ml"],
            "citation_count": 5,
        },
        {
            "doi": "10.1/c",
            "unique_id": "paper:c",
            "title": "C",
            "publication_published_year": 2021,
            "publication_venue_name_unified": "Venue B",
            "author": [{"name": "Alice"}],
            "keywords": ["nlp"],
            "citation_count": 0,
        },
        {
            "doi": "10.1/missing",
            "title": "Missing metadata",
            "citation_count": None,
        },
        {
            "doi": "https://doi.org/10.1/A",
            "title": "Duplicate A",
            "publication_published_year": 2024,
            "citation_count": 100,
        },
    ]


def test_analysis_dedupes_across_doi_and_unique_id():
    papers = [
        {
            "title": "Shared work",
            "doi": "10.1000/shared",
            "unique_id": "paper:shared",
            "publication_published_year": 2024,
        },
        {
            "title": "Shared work",
            "unique_id": "paper:shared",
            "publication_published_year": 2024,
        },
        {
            "title": "Shared title but different work",
            "doi": "10.1000/other-a",
            "unique_id": "paper:other-a",
            "publication_published_year": 2023,
        },
        {
            "title": "Shared title but different work",
            "doi": "10.1000/other-b",
            "unique_id": "paper:other-b",
            "publication_published_year": 2022,
        },
    ]

    analysis = analyze_papers(papers)

    assert analysis["sample_size"] == 3
    assert analysis["duplicates_removed"] == 1


def test_analysis_does_not_merge_conflicting_strong_identifiers():
    cases = [
        [
            {"title": "Same", "doi": "10.1000/a", "unique_id": "paper:A"},
            {"title": "Same", "doi": "10.1000/a", "unique_id": "paper:B"},
        ],
        [
            {"title": "Same", "doi": "10.1000/a", "unique_id": "paper:A"},
            {"title": "Same", "doi": "10.1000/b", "unique_id": "paper:A"},
        ],
    ]

    for papers in cases:
        forward = analyze_papers(papers)
        reverse = analyze_papers(list(reversed(papers)))
        for analysis in (forward, reverse):
            assert analysis["sample_size"] == 2
            assert analysis["duplicates_removed"] == 0
            assert analysis["identifier_conflicts"] == 1


def test_analysis_uses_unicode_titles_for_duplicates():
    papers = [
        {"title": "Привет, мир!", "publication_published_year": 2024},
        {"title": "привет мир", "publication_published_year": 2024},
    ]

    analysis = analyze_papers(papers)

    assert analysis["sample_size"] == 1
    assert analysis["duplicates_removed"] == 1


def test_duplicate_metadata_is_merged_regardless_of_order():
    weak = {"title": "Order paper", "publication_published_year": None}
    strong = {
        "title": "Order paper",
        "publication_published_year": 2024,
        "publication_venue_name_unified": "Venue",
        "author": [{"name": "Alice"}],
        "citation_count": 10,
        "doi": "10.1000/order",
        "unique_id": "paper:order",
    }

    forward = analyze_papers([weak, strong])
    reverse = analyze_papers([strong, weak])

    for analysis in (forward, reverse):
        assert analysis["sample_size"] == 1
        assert analysis["duplicates_removed"] == 1
        assert analysis["year_counts"] == [{"year": 2024, "count": 1}]
        assert analysis["venue_counts"] == [{"venue": "Venue", "count": 1}]
        assert analysis["top_cited"][0]["citation_count"] == 10
        assert analysis["top_cited"][0]["venue"] == "Venue"


def test_analysis_dedupes_and_computes_missing_rates():
    analysis = analyze_papers(_papers())

    assert analysis["sample_size"] == 4
    assert analysis["duplicates_removed"] == 1
    assert analysis["year_range"] == [2021, 2024]
    assert analysis["year_counts"] == [
        {"year": 2021, "count": 1},
        {"year": 2023, "count": 1},
        {"year": 2024, "count": 1},
    ]
    assert analysis["venue_counts"] == [
        {"venue": "Venue A", "count": 2},
        {"venue": "Venue B", "count": 1},
    ]
    assert analysis["author_counts"] == [
        {"author": "Alice", "count": 2},
        {"author": "Bob", "count": 1},
    ]
    assert analysis["top_cited"][0]["title"] == "A"
    assert analysis["top_cited"][0]["citation_count"] == 10

    missing = {row["field"]: row for row in analysis["missing"]}
    assert missing["year"]["missing"] == 1
    assert missing["citation_count"]["missing"] == 1
    assert missing["citation_count"]["missing_rate"] == 0.25


def test_growing_keywords_and_keyword_pairs():
    analysis = analyze_papers(_papers())

    growing = {row["keyword"]: row for row in analysis["growing_keywords"]}
    assert growing["ml"]["recent"] == 2
    assert growing["nlp"]["recent"] == 2
    assert {(row["left"], row["right"]) for row in analysis["keyword_pairs"]} == {("ml", "nlp")}


def test_trends_zip_contains_expected_files_and_scope():
    analysis = analyze_papers(_papers())
    data = export_trends_zip(analysis, {"source": "current results", "sample_size": 4})

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        summary = archive.read("summary.csv").decode("utf-8-sig")
        years = archive.read("years.csv").decode("utf-8-sig")

    assert {
        "years.csv",
        "venues.csv",
        "authors.csv",
        "subjects.csv",
        "keywords.csv",
        "keyword_pairs.csv",
        "top_cited.csv",
        "missing.csv",
        "summary.csv",
    } <= names
    assert "current results" in summary
    assert "2024" in years


def test_trends_png_is_optional_but_valid_when_available():
    if not importlib.util.find_spec("matplotlib"):
        pytest.skip("matplotlib not installed in this test environment")
    data = export_trends_png(analyze_papers(_papers()))
    assert data.startswith(b"\x89PNG")
