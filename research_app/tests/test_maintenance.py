import os
import sqlite3
import stat

import pytest

import research_app.storage as storage


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "database_path", lambda: tmp_path / "workbench.sqlite3")


def _seed_aliased_candidate():
    project_id = storage.create_screening_project("Review", "Q", "Include", "Exclude")
    storage.add_screening_candidates(
        project_id,
        [{"title": "Paper A", "doi": "10.1000/a", "unique_id": "paper:A"}],
    )
    candidates, _ = storage.list_screening_candidates(project_id)
    candidate_key = candidates[0]["candidate_key"]
    storage.set_screening_decision(
        project_id,
        candidate_key,
        "included",
        notes="Keep this decision",
    )
    with storage._connection() as connection:
        connection.execute(
            """INSERT INTO screening_candidate_identifiers(
                   project_id, identifier_key, candidate_key
               ) VALUES (?, ?, ?)""",
            (project_id, "uid:paper:B", candidate_key),
        )
    return project_id, candidate_key


def test_inspect_and_repair_old_bad_alias_preserves_review_state(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id, candidate_key = _seed_aliased_candidate()
    audit_before = storage.list_screening_audit(project_id)

    report = storage.inspect_identifier_integrity()
    assert report["counts"] == {"conflicts": 1, "ambiguous": 0, "orphans": 0}
    conflict = report["conflicts"][0]
    assert conflict["type"] == "uid"
    assert conflict["canonical"] == "paper:A"
    assert conflict["aliases"] == ["paper:B"]

    result = storage.repair_identifier_integrity(backup=True)

    assert result["removed_aliases"] == 1
    assert result["backup_path"]
    assert (tmp_path / "workbench.sqlite3").exists()
    backup_path = result["backup_path"]
    with sqlite3.connect(backup_path) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    candidates, _ = storage.list_screening_candidates(project_id)
    candidate = candidates[0]
    assert candidate["candidate_key"] == candidate_key
    assert candidate["status"] == "included"
    assert candidate["notes"] == "Keep this decision"
    assert storage.list_screening_audit(project_id) == audit_before

    later = storage.add_screening_candidates(
        project_id,
        [{"title": "Paper B", "unique_id": "paper:B"}],
    )
    assert later == {"identified": 1, "added": 1, "duplicates": 0, "conflicts": 0}


def test_single_uid_alias_without_canonical_uid_is_ambiguous(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = storage.create_screening_project("Review", "Q", "Include", "Exclude")
    storage.add_screening_candidates(
        project_id,
        [{"title": "Paper A", "doi": "10.1000/a"}],
    )
    candidates, _ = storage.list_screening_candidates(project_id)
    candidate_key = candidates[0]["candidate_key"]
    with storage._connection() as connection:
        connection.execute(
            """INSERT INTO screening_candidate_identifiers(
                   project_id, identifier_key, candidate_key
               ) VALUES (?, ?, ?)""",
            (project_id, "uid:paper:B", candidate_key),
        )

    report = storage.inspect_identifier_integrity()
    assert report["counts"] == {"conflicts": 0, "ambiguous": 1, "orphans": 0}
    ambiguous = report["ambiguous"][0]
    assert ambiguous["type"] == "uid"
    assert ambiguous["values"] == ["paper:B"]
    assert "缺少该类型规范标识" in ambiguous["reason"]

    result = storage.repair_identifier_integrity(backup=True)

    assert result["removed_aliases"] == 0
    assert result["backup_path"] is None
    with storage._connection() as connection:
        mapping = connection.execute(
            """SELECT candidate_key FROM screening_candidate_identifiers
               WHERE project_id = ? AND identifier_key = ?""",
            (project_id, "uid:paper:B"),
        ).fetchone()
    assert mapping["candidate_key"] == candidate_key


def test_normal_database_has_no_integrity_false_positives(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = storage.create_screening_project("Review", "Q", "Include", "Exclude")
    storage.add_screening_candidates(
        project_id,
        [{"title": "Paper A", "doi": "10.1000/a", "unique_id": "paper:A"}],
    )

    report = storage.inspect_identifier_integrity()

    assert report["supported"] is True
    assert report["counts"] == {"conflicts": 0, "ambiguous": 0, "orphans": 0}
    result = storage.repair_identifier_integrity(backup=True)
    assert result["removed_aliases"] == 0
    assert result["removed_orphans"] == 0
    assert result["backup_path"] is None


def test_ambiguous_aliases_are_reported_but_not_removed(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = storage.create_screening_project("Review", "Q", "Include", "Exclude")
    storage.add_screening_candidates(project_id, [{"title": "Weak title"}])
    candidates, _ = storage.list_screening_candidates(project_id)
    candidate_key = candidates[0]["candidate_key"]
    with storage._connection() as connection:
        for uid in ("paper:A", "paper:B"):
            connection.execute(
                """INSERT INTO screening_candidate_identifiers(
                       project_id, identifier_key, candidate_key
                   ) VALUES (?, ?, ?)""",
                (project_id, f"uid:{uid}", candidate_key),
            )

    report = storage.inspect_identifier_integrity()
    assert report["counts"] == {"conflicts": 0, "ambiguous": 1, "orphans": 0}
    assert report["ambiguous"][0]["type"] == "uid"
    assert report["ambiguous"][0]["values"] == ["paper:A", "paper:B"]

    result = storage.repair_identifier_integrity(backup=True)

    assert result["removed_aliases"] == 0
    assert result["backup_path"] is None
    with storage._connection() as connection:
        count = connection.execute(
            """SELECT COUNT(*) FROM screening_candidate_identifiers
               WHERE project_id = ? AND candidate_key = ? AND identifier_key LIKE 'uid:%'""",
            (project_id, candidate_key),
        ).fetchone()[0]
    assert count == 2


def test_inspect_is_read_only_for_old_schema_and_upgrade_creates_backup(monkeypatch, tmp_path):
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

    report = storage.inspect_identifier_integrity()

    assert report["supported"] is False
    assert report["schema_version"] == 1
    assert "upgrade" in report["message"]
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "screening_candidate_identifiers" not in tables
    with pytest.raises(ValueError, match="upgrade"):
        storage.repair_identifier_integrity()

    result = storage.upgrade_database()

    assert result["upgraded"] is True
    assert result["from_version"] == 1
    assert result["to_version"] == 7
    assert result["backup_path"]
    with sqlite3.connect(result["backup_path"]) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
    assert storage.inspect_identifier_integrity()["supported"] is True


def test_backup_permission_failure_aborts_without_leaving_backup(monkeypatch, tmp_path):
    path = tmp_path / "workbench.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sample (value TEXT)")

    class PosixOS:
        name = "posix"

    monkeypatch.setattr(storage, "os", PosixOS)
    monkeypatch.setattr(
        storage.Path,
        "chmod",
        lambda self, mode: (_ for _ in ()).throw(OSError("denied")),
    )

    with pytest.raises(RuntimeError, match="权限"):
        storage._backup_database(path)

    assert not list(tmp_path.glob("workbench.sqlite3.bak-*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX 文件权限测试")
def test_backup_permissions_are_restricted(monkeypatch, tmp_path):
    path = tmp_path / "workbench.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sample (value TEXT)")

    backup = storage._backup_database(path)

    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_orphan_identifier_is_reported_and_removed(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = storage.create_screening_project("Review", "Q", "Include", "Exclude")
    with storage._connection() as connection:
        connection.execute(
            """INSERT INTO screening_candidate_identifiers(
                   project_id, identifier_key, candidate_key
               ) VALUES (?, ?, ?)""",
            (project_id, "uid:paper:orphan", "missing-candidate"),
        )

    report = storage.inspect_identifier_integrity()
    assert report["counts"] == {"conflicts": 0, "ambiguous": 0, "orphans": 1}

    result = storage.repair_identifier_integrity(backup=True)

    assert result["removed_orphans"] == 1
    assert result["backup_path"]
    with storage._connection() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM screening_candidate_identifiers WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0]
    assert count == 0
