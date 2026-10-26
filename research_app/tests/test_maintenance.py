import sqlite3

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
