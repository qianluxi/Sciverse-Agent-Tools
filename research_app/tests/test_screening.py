import sqlite3

import pytest

import research_app.storage as storage
from research_app.screening import (
    EXCLUSION_REASONS,
    candidate_key,
    duplicate_basis,
    normalize_title,
)


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "database_path", lambda: tmp_path / "workbench.sqlite3")


def _paper(index: int) -> dict:
    return {
        "title": f"Study {index}",
        "doi": f"10.1000/study-{index}",
        "unique_id": f"paper:{index}",
        "publication_published_year": 2020 + index % 5,
    }


def test_candidate_key_prefers_doi_and_reports_basis():
    key = candidate_key({"doi": "https://doi.org/10.1000/X", "title": "Ignored"})
    assert key == "doi:10.1000/x"
    assert duplicate_basis(key) == "DOI 相同"


def test_normalize_title_keeps_non_latin_scripts():
    assert normalize_title("Привет, мир!") == "приветмир"
    assert normalize_title("日本語のタイトル") == "日本語のタイトル"
    assert normalize_title("한국어 제목") == "한국어제목"
    assert normalize_title("A/B: Test") == "abtest"


def test_cross_identifier_dedup_uses_doi_and_unique_id(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = storage.create_screening_project("Review", "Q", "Include", "Exclude")

    result = storage.add_screening_candidates(
        project_id,
        [
            {
                "title": "Same work",
                "doi": "10.1000/same",
                "unique_id": "paper:42",
            },
            {
                "title": "Same work",
                "unique_id": "paper:42",
            },
        ],
    )

    assert result == {"identified": 2, "added": 1, "duplicates": 1}
    duplicates = storage.list_screening_duplicates(project_id)
    assert duplicates[0]["duplicate_of"] == "doi:10.1000/same"
    assert duplicates[0]["basis"] == "unique_id 相同"


def test_conflicting_strong_ids_do_not_merge_by_title(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = storage.create_screening_project("Review", "Q", "Include", "Exclude")

    result = storage.add_screening_candidates(
        project_id,
        [
            {"title": "Same title", "doi": "10.1000/one", "unique_id": "paper:one"},
            {"title": "Same title", "doi": "10.1000/two", "unique_id": "paper:two"},
        ],
    )

    assert result == {"identified": 2, "added": 2, "duplicates": 0}


def test_foreign_keys_are_enabled_and_cascade(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = storage.create_screening_project("Review", "Q", "Include", "Exclude")
    storage.add_screening_candidates(project_id, [_paper(1)])

    with storage._connection() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        connection.execute("DELETE FROM screening_projects WHERE id = ?", (project_id,))
        remaining = connection.execute(
            "SELECT COUNT(*) FROM screening_candidates WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0]
    assert remaining == 0


def test_project_candidates_dedup_stats_and_audit(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = storage.create_screening_project(
        "Review", "Does intervention X work?", "Randomized trials", "Not about X"
    )

    papers = [_paper(index) for index in range(3)]
    papers.append({"title": "Study 0", "doi": "DOI:10.1000/study-0"})
    result = storage.add_screening_candidates(project_id, papers)

    assert result == {"identified": 4, "added": 3, "duplicates": 1}
    stats = storage.screening_stats(project_id)
    assert stats == {
        "identified": 4,
        "duplicates": 1,
        "unique": 3,
        "pending": 3,
        "included": 0,
        "excluded": 0,
        "screened": 0,
    }
    duplicates = storage.list_screening_duplicates(project_id)
    assert duplicates[0]["basis"] == "DOI 相同"

    candidates, total = storage.list_screening_candidates(project_id, page_size=2)
    assert total == 3
    assert len(candidates) == 2
    key = candidates[0]["candidate_key"]

    with pytest.raises(ValueError, match="排除原因"):
        storage.set_screening_decision(project_id, key, "excluded")

    assert storage.set_screening_decision(project_id, key, "included", notes="Meets criteria")
    assert (
        storage.set_screening_decision(project_id, key, "included", notes="Meets criteria") is False
    )
    assert storage.set_screening_decision(
        project_id, key, "excluded", reason=EXCLUSION_REASONS[0], notes="Wrong population"
    )
    stats = storage.screening_stats(project_id)
    assert stats["included"] == 0
    assert stats["excluded"] == 1
    assert stats["screened"] == 1

    audit = storage.list_screening_audit(project_id)
    assert len(audit) == 2
    assert audit[0]["old_status"] == "included"
    assert audit[0]["new_status"] == "excluded"
    assert audit[0]["new_reason"] == EXCLUSION_REASONS[0]


def test_fifty_candidate_fixture_with_missing_fields_and_duplicates(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = storage.create_screening_project("PRISMA", "Question?", "Include", "Exclude")

    papers = [_paper(index) for index in range(45)]
    papers.extend(
        [
            {"title": "Missing metadata", "year": None},
            {"title": "Missing metadata", "year": None},
            {"title": "Duplicate DOI", "doi": "10.1000/study-1"},
            {"title": "Duplicate DOI 2", "doi": "https://doi.org/10.1000/study-2"},
            {"title": "No identifiers"},
        ]
    )
    result = storage.add_screening_candidates(project_id, papers)

    assert result["identified"] == 50
    assert result["added"] == 47
    assert result["duplicates"] == 3
    stats = storage.screening_stats(project_id)
    assert stats["identified"] == 50
    assert stats["duplicates"] == 3
    assert stats["unique"] == 47
    assert stats["pending"] == 47


def test_v1_database_migrates_to_screening_tables(monkeypatch, tmp_path):
    path = tmp_path / "workbench.sqlite3"
    monkeypatch.setattr(storage, "database_path", lambda: path)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE search_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mode TEXT NOT NULL,
                params_json TEXT NOT NULL,
                result_count INTEGER NOT NULL,
                searched_at TEXT NOT NULL
            );
            CREATE TABLE saved_searches (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                mode TEXT NOT NULL,
                params_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            PRAGMA user_version = 1;
            """
        )

    assert storage.list_screening_projects() == []
    assert storage.list_saved_searches() == []


def test_v2_database_migrates_identifiers_for_cross_id_dedup(monkeypatch, tmp_path):
    path = tmp_path / "workbench.sqlite3"
    monkeypatch.setattr(storage, "database_path", lambda: path)
    project_id = storage.create_screening_project("Review", "Q", "Include", "Exclude")
    storage.add_screening_candidates(
        project_id,
        [{"title": "Paper", "doi": "10.1000/paper", "unique_id": "paper:1"}],
    )
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE screening_candidate_identifiers")
        connection.execute("PRAGMA user_version = 2")

    result = storage.add_screening_candidates(
        project_id,
        [{"title": "Paper", "unique_id": "paper:1"}],
    )

    assert result == {"identified": 1, "added": 0, "duplicates": 1}
    assert storage.screening_stats(project_id)["unique"] == 1
