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

    assert result == {"identified": 2, "added": 1, "duplicates": 1, "conflicts": 0}
    duplicates = storage.list_screening_duplicates(project_id)
    assert duplicates[0]["duplicate_of"] == "doi:10.1000/same"
    assert duplicates[0]["basis"] == "unique_id 相同"


def test_weak_candidate_gets_metadata_without_losing_review_state(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = storage.create_screening_project("Review", "Q", "Include", "Exclude")
    storage.add_screening_candidates(project_id, [{"title": "Weak paper"}])
    candidates, _ = storage.list_screening_candidates(project_id)
    key = candidates[0]["candidate_key"]
    storage.set_screening_decision(project_id, key, "included", notes="Keep this decision")
    audit_before = storage.list_screening_audit(project_id)

    result = storage.add_screening_candidates(
        project_id,
        [
            {
                "title": "Weak paper",
                "doi": "10.1000/weak",
                "unique_id": "paper:weak",
                "publication_published_year": 2024,
            }
        ],
    )

    assert result == {"identified": 1, "added": 0, "duplicates": 1, "conflicts": 0}
    candidates, _ = storage.list_screening_candidates(project_id)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["candidate_key"] == key
    assert candidate["doi"] == "10.1000/weak"
    assert candidate["paper_id"] == "paper:weak"
    assert candidate["year"] == 2024
    assert candidate["status"] == "included"
    assert candidate["notes"] == "Keep this decision"
    assert storage.list_screening_audit(project_id) == audit_before


def test_metadata_completion_is_order_independent(monkeypatch, tmp_path):
    weak = {"title": "Order paper", "publication_published_year": None}
    strong = {
        "title": "Order paper",
        "doi": "10.1000/order",
        "unique_id": "paper:order",
        "publication_published_year": 2024,
    }

    def import_order(papers, directory):
        _isolate(monkeypatch, tmp_path / directory)
        project_id = storage.create_screening_project("Review", "Q", "Include", "Exclude")
        storage.add_screening_candidates(project_id, papers)
        candidates, total = storage.list_screening_candidates(project_id)
        return candidates[0], total

    first, first_total = import_order([weak, strong], "forward")
    second, second_total = import_order([strong, weak], "reverse")

    assert first_total == 1
    assert second_total == 1
    assert first["candidate_key"] == "title:orderpaper"
    assert second["candidate_key"] == "doi:10.1000/order"
    for field in ("title", "doi", "paper_id", "year"):
        assert first[field] == second[field]


def test_conflicting_strong_identifiers_are_recorded_without_remapping(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = storage.create_screening_project("Review", "Q", "Include", "Exclude")
    storage.add_screening_candidates(
        project_id,
        [
            {"title": "A", "doi": "10.1000/a", "unique_id": "paper:a"},
            {"title": "B", "doi": "10.1000/b", "unique_id": "paper:b"},
        ],
    )

    result = storage.add_screening_candidates(
        project_id,
        [{"title": "Conflict", "doi": "10.1000/a", "unique_id": "paper:b"}],
    )

    assert result == {"identified": 1, "added": 1, "duplicates": 0, "conflicts": 1}
    duplicates = storage.list_screening_duplicates(project_id)
    assert "强标识符冲突" in duplicates[0]["basis"]
    assert "doi:10.1000/a" in duplicates[0]["duplicate_of"]
    assert "doi:10.1000/b" in duplicates[0]["duplicate_of"]
    with storage._connection() as connection:
        mappings = {
            row["identifier_key"]: row["candidate_key"]
            for row in connection.execute(
                "SELECT identifier_key, candidate_key FROM screening_candidate_identifiers WHERE project_id = ?",
                (project_id,),
            )
        }
    assert mappings["doi:10.1000/a"] == "doi:10.1000/a"
    assert mappings["doi:10.1000/b"] == "doi:10.1000/b"


def test_conflicting_unregistered_ids_are_not_aliased(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = storage.create_screening_project("Review", "Q", "Include", "Exclude")
    storage.add_screening_candidates(
        project_id,
        [{"title": "Paper A", "doi": "10.1000/a", "unique_id": "paper:A"}],
    )

    result = storage.add_screening_candidates(
        project_id,
        [{"title": "Paper A copy", "doi": "10.1000/a", "unique_id": "paper:B"}],
    )

    assert result == {"identified": 1, "added": 1, "duplicates": 0, "conflicts": 1}
    with storage._connection() as connection:
        mappings = {
            row["identifier_key"]: row["candidate_key"]
            for row in connection.execute(
                "SELECT identifier_key, candidate_key FROM screening_candidate_identifiers WHERE project_id = ?",
                (project_id,),
            )
        }
    assert "uid:paper:B" not in mappings
    assert mappings["doi:10.1000/a"] == "doi:10.1000/a"
    duplicates = storage.list_screening_duplicates(project_id)
    assert "uid" in duplicates[0]["basis"]
    assert "paper:B" in duplicates[0]["basis"]

    later = storage.add_screening_candidates(
        project_id,
        [{"title": "Paper B only", "unique_id": "paper:B"}],
    )
    assert later == {"identified": 1, "added": 1, "duplicates": 0, "conflicts": 0}


def test_conflicting_unregistered_doi_is_not_aliased(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = storage.create_screening_project("Review", "Q", "Include", "Exclude")
    storage.add_screening_candidates(
        project_id,
        [{"title": "Paper A", "doi": "10.1000/a", "unique_id": "paper:A"}],
    )

    result = storage.add_screening_candidates(
        project_id,
        [{"title": "Paper A copy", "doi": "10.1000/b", "unique_id": "paper:A"}],
    )

    assert result == {"identified": 1, "added": 1, "duplicates": 0, "conflicts": 1}
    with storage._connection() as connection:
        mappings = {
            row["identifier_key"]: row["candidate_key"]
            for row in connection.execute(
                "SELECT identifier_key, candidate_key FROM screening_candidate_identifiers WHERE project_id = ?",
                (project_id,),
            )
        }
    assert "doi:10.1000/b" not in mappings

    later = storage.add_screening_candidates(
        project_id,
        [{"title": "Paper B only", "doi": "10.1000/b"}],
    )
    assert later == {"identified": 1, "added": 1, "duplicates": 0, "conflicts": 0}


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

    assert result == {"identified": 2, "added": 2, "duplicates": 0, "conflicts": 0}


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

    assert result == {"identified": 4, "added": 3, "duplicates": 1, "conflicts": 0}
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

    assert result == {"identified": 1, "added": 0, "duplicates": 1, "conflicts": 0}
    assert storage.screening_stats(project_id)["unique"] == 1


def test_screening_queue_can_keep_stable_title_order_after_saving(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = storage.create_screening_project("Review", "Q", "Include", "Exclude")
    storage.add_screening_candidates(project_id, [_paper(2), _paper(1)])

    before, _ = storage.list_screening_candidates(
        project_id, page_size=10, order_by="title"
    )
    storage.set_screening_decision(project_id, "doi:10.1000/study-2", "included")
    after, _ = storage.list_screening_candidates(
        project_id, page_size=10, order_by="title"
    )

    assert [candidate["title"] for candidate in before] == ["Study 1", "Study 2"]
    assert [candidate["title"] for candidate in after] == ["Study 1", "Study 2"]
