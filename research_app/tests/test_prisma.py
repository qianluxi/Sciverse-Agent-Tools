import sqlite3

import pytest

import research_app.storage as storage


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "database_path", lambda: tmp_path / "workbench.sqlite3")


def _project_with_candidates(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = storage.create_screening_project("Review", "Q", "I", "E")
    storage.add_screening_candidates(
        project_id,
        [
            {"title": "Paper A", "unique_id": "paper:a", "abstract": "a"},
            {"title": "Paper B", "unique_id": "paper:b", "abstract": "b"},
            {"title": "Paper C", "unique_id": "paper:c", "abstract": "c"},
            {"title": "Paper D", "unique_id": "paper:d", "abstract": "d"},
        ],
    )
    return project_id


def test_prisma_counts_separates_title_abstract_and_fulltext(monkeypatch, tmp_path):
    project_id = _project_with_candidates(monkeypatch, tmp_path)
    storage.set_screening_decision(project_id, "uid:paper:a", "included")
    storage.set_screening_decision(project_id, "uid:paper:b", "included")
    storage.set_screening_decision(
        project_id, "uid:paper:c", "excluded", reason="主题不相关"
    )
    storage.set_fulltext_decision(project_id, "uid:paper:a", "included")
    storage.set_fulltext_decision(
        project_id,
        "uid:paper:b",
        "excluded",
        reason="研究对象不符",
        notes="wrong population",
    )

    counts = storage.prisma_counts(project_id)

    assert counts["identified"] == 4
    assert counts["duplicates_removed"] == 0
    assert counts["unique_records"] == 4
    assert counts["title_abstract"] == {
        "screened": 4,
        "pending": 1,
        "included": 2,
        "excluded": 1,
    }
    assert counts["fulltext"]["not_sought"] == 0
    assert counts["fulltext"]["sought"] == 2
    assert counts["fulltext"]["not_retrieved"] == 0
    assert counts["fulltext"]["retrieved_pending"] == 0
    assert counts["fulltext"]["assessed"] == 2
    assert counts["fulltext"]["included"] == 1
    assert counts["fulltext"]["excluded"] == 1
    assert counts["fulltext"]["partition_total"] == 2
    assert counts["fulltext"]["conservation_ok"] is True
    assert counts["fulltext"]["excluded_reasons"] == [
        {"reason": "研究对象不符", "count": 1}
    ]
    assert counts["included_in_review"] == 1
    assert counts["included_reports"] == 1
    assert counts["included_studies"] is None
    assert counts["study_level_supported"] is False
    assert any("待定" in warning for warning in counts["warnings"])


def test_prisma_warns_when_fulltext_stage_missing(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = storage.create_screening_project("Review", "Q", "I", "E")
    storage.add_screening_candidates(
        project_id, [{"title": "Paper A", "unique_id": "paper:a"}]
    )
    storage.set_screening_decision(project_id, "uid:paper:a", "included")

    counts = storage.prisma_counts(project_id)

    assert counts["fulltext"]["not_sought"] == 1
    assert counts["fulltext"]["sought"] == 0
    assert counts["included_in_review"] == 0
    assert any("尚未记录全文阶段" in warning for warning in counts["warnings"])


def test_fulltext_status_partition_and_conservation(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = storage.create_screening_project("Review", "Q", "I", "E")
    storage.add_screening_candidates(
        project_id,
        [
            {"title": f"Paper {index}", "unique_id": f"paper:{index}"}
            for index in range(5)
        ],
    )
    for index in range(5):
        storage.set_screening_decision(
            project_id, f"uid:paper:{index}", "included"
        )

    # A: no record (not_sought); B: not_retrieved; C: retrieved_pending;
    # D: included; E: excluded with reason.
    storage.set_fulltext_decision(project_id, "uid:paper:1", "not_retrieved")
    storage.set_fulltext_decision(
        project_id, "uid:paper:2", "retrieved_pending"
    )
    storage.set_fulltext_decision(project_id, "uid:paper:3", "included")
    storage.set_fulltext_decision(
        project_id, "uid:paper:4", "excluded", reason="研究对象不符"
    )

    counts = storage.prisma_counts(project_id)
    fulltext = counts["fulltext"]

    assert fulltext["not_sought"] == 1
    assert fulltext["not_retrieved"] == 1
    assert fulltext["retrieved_pending"] == 1
    assert fulltext["included"] == 1
    assert fulltext["excluded"] == 1
    assert fulltext["sought"] == 4
    assert fulltext["assessed"] == 2
    assert fulltext["partition_total"] == 5
    assert fulltext["conservation_ok"] is True
    assert counts["included_in_review"] == 1
    assert fulltext["excluded_reasons"] == [
        {"reason": "研究对象不符", "count": 1}
    ]
    assert any("待资格评估" in warning for warning in counts["warnings"])
    assert any("尚未记录全文阶段" in warning for warning in counts["warnings"])


def test_not_retrieved_is_not_counted_as_fulltext_exclusion(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = storage.create_screening_project("Review", "Q", "I", "E")
    storage.add_screening_candidates(
        project_id, [{"title": "Paper A", "unique_id": "paper:a"}]
    )
    storage.set_screening_decision(project_id, "uid:paper:a", "included")
    storage.set_fulltext_decision(project_id, "uid:paper:a", "not_retrieved")

    counts = storage.prisma_counts(project_id)
    fulltext = counts["fulltext"]

    assert fulltext["not_retrieved"] == 1
    assert fulltext["excluded"] == 0
    assert fulltext["assessed"] == 0
    assert fulltext["excluded_reasons"] == []
    assert "无法获取全文" not in {
        item["reason"] for item in fulltext["excluded_reasons"]
    }
    with pytest.raises(ValueError):
        storage.set_fulltext_decision(
            project_id, "uid:paper:a", "excluded", reason="无法获取全文"
        )


V16_FULLTEXT_SCHEMA = """
CREATE TABLE screening_projects (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, question TEXT NOT NULL,
    inclusion_criteria TEXT NOT NULL, exclusion_criteria TEXT NOT NULL,
    identified_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    stats_tracked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE screening_fulltext (
    project_id TEXT NOT NULL, candidate_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'not_sought'
        CHECK (status IN ('not_sought', 'not_retrieved', 'included', 'excluded')),
    exclusion_reason TEXT, notes TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, candidate_key)
);
CREATE TABLE screening_fulltext_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
    candidate_key TEXT NOT NULL, old_status TEXT, new_status TEXT NOT NULL,
    old_reason TEXT, new_reason TEXT, old_notes TEXT, new_notes TEXT,
    changed_at TEXT NOT NULL
);
PRAGMA user_version = 16;
"""


def test_v16_fulltext_migrates_to_v17(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    path = tmp_path / "workbench.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(V16_FULLTEXT_SCHEMA)
        connection.execute(
            """INSERT INTO screening_projects(
                   id, name, question, inclusion_criteria, exclusion_criteria,
                   created_at, updated_at)
               VALUES ('p1', 'P', 'Q', 'I', 'E', 't', 't')"""
        )
        connection.executemany(
            """INSERT INTO screening_fulltext(
                   project_id, candidate_key, status, exclusion_reason, notes, updated_at)
               VALUES ('p1', ?, ?, ?, '', 't')""",
            [
                ("uid:included", "included", None),
                ("uid:excluded", "excluded", "研究对象不符"),
                ("uid:missing", "excluded", "无法获取全文"),
            ],
        )
        connection.executemany(
            """INSERT INTO screening_fulltext_audit(
                   project_id, candidate_key, old_status, new_status,
                   old_reason, new_reason, changed_at)
               VALUES ('p1', ?, 'not_sought', ?, NULL, NULL, 't')""",
            [
                ("uid:included", "included"),
                ("uid:excluded", "excluded"),
            ],
        )

    storage.get_screening_project("p1")

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 20
        rows = {
            row[0]: (row[1], row[2])
            for row in connection.execute(
                """SELECT candidate_key, status, exclusion_reason
                   FROM screening_fulltext"""
            )
        }
        assert rows["uid:included"] == ("included", None)
        assert rows["uid:excluded"] == ("excluded", "研究对象不符")
        assert rows["uid:missing"] == ("not_retrieved", None)
        assert connection.execute(
            "SELECT COUNT(*) FROM screening_fulltext_audit"
        ).fetchone()[0] == 3
        migration = connection.execute(
            """SELECT new_status, new_notes FROM screening_fulltext_audit
               WHERE candidate_key = 'uid:missing'
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        assert migration[0] == "not_retrieved"
        assert "Schema v17" in migration[1]

    storage.get_screening_project("p1")
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 20
        assert connection.execute(
            "SELECT COUNT(*) FROM screening_fulltext"
        ).fetchone()[0] == 3
        assert connection.execute(
            "SELECT COUNT(*) FROM screening_fulltext_audit"
        ).fetchone()[0] == 3


def test_legacy_stale_fulltext_cannot_be_reactivated(monkeypatch, tmp_path):
    project_id = _project_with_candidates(monkeypatch, tmp_path)
    key = "uid:paper:a"
    storage.set_screening_decision(project_id, key, "included")
    storage.set_fulltext_decision(project_id, key, "included")

    # Simulate a legacy database where title/abstract was later excluded while
    # the fulltext record was left in place.
    with storage._connection() as connection:
        connection.execute(
            """UPDATE screening_candidates SET status = 'excluded'
               WHERE project_id = ? AND candidate_key = ?""",
            (project_id, key),
        )

    stale = storage.list_stale_fulltext_decisions(project_id)
    assert len(stale) == 1
    assert stale[0]["candidate_key"] == key
    assert stale[0]["status"] == "included"

    # Notes-only change while the status is unchanged is still allowed.
    assert (
        storage.set_screening_decision(
            project_id, key, "excluded", reason="主题不相关", notes="legacy"
        )
        is True
    )

    with pytest.raises(ValueError):
        storage.set_screening_decision(project_id, key, "included")

    assert storage.reset_fulltext_decision(project_id, key) is True
    assert storage.list_stale_fulltext_decisions(project_id) == []
    assert storage.set_screening_decision(project_id, key, "included") is True
    assert storage.get_fulltext_decision(project_id, key) is None

    counts = storage.prisma_counts(project_id)
    assert counts["included_reports"] == 0
    assert counts["fulltext"]["not_sought"] == 1
    audit = storage.list_fulltext_audit(project_id)
    assert audit[0]["new_status"] == "not_sought"


def test_title_abstract_change_requires_fulltext_reset(monkeypatch, tmp_path):
    project_id = _project_with_candidates(monkeypatch, tmp_path)
    storage.set_screening_decision(project_id, "uid:paper:a", "included")
    storage.set_fulltext_decision(project_id, "uid:paper:a", "included")

    with pytest.raises(ValueError):
        storage.set_screening_decision(
            project_id, "uid:paper:a", "excluded", reason="主题不相关"
        )
    with pytest.raises(ValueError):
        storage.set_screening_decision(project_id, "uid:paper:a", "pending")

    assert storage.reset_fulltext_decision(project_id, "uid:paper:a") is True
    assert storage.get_fulltext_decision(project_id, "uid:paper:a") is None
    audit = storage.list_fulltext_audit(project_id)
    assert audit[0]["old_status"] == "included"
    assert audit[0]["new_status"] == "not_sought"

    assert (
        storage.set_screening_decision(
            project_id, "uid:paper:a", "excluded", reason="主题不相关"
        )
        is True
    )
    counts = storage.prisma_counts(project_id)
    assert counts["title_abstract"]["excluded"] == 1
    assert counts["fulltext"]["not_sought"] == 0
    assert counts["included_reports"] == 0


def test_fulltext_excluded_blocks_title_abstract_pending(monkeypatch, tmp_path):
    project_id = _project_with_candidates(monkeypatch, tmp_path)
    storage.set_screening_decision(project_id, "uid:paper:b", "included")
    storage.set_fulltext_decision(
        project_id, "uid:paper:b", "excluded", reason="研究对象不符"
    )

    with pytest.raises(ValueError):
        storage.set_screening_decision(project_id, "uid:paper:b", "pending")

    storage.reset_fulltext_decision(project_id, "uid:paper:b")
    assert (
        storage.set_screening_decision(project_id, "uid:paper:b", "pending")
        is True
    )


def test_fulltext_decision_guards_and_audit(monkeypatch, tmp_path):
    project_id = _project_with_candidates(monkeypatch, tmp_path)
    storage.set_screening_decision(project_id, "uid:paper:a", "included")

    with pytest.raises(ValueError):
        storage.set_fulltext_decision(project_id, "uid:paper:b", "included")
    with pytest.raises(ValueError):
        storage.set_fulltext_decision(
            project_id, "uid:paper:a", "excluded"
        )

    assert (
        storage.set_fulltext_decision(project_id, "uid:paper:a", "included")
        is True
    )
    assert (
        storage.set_fulltext_decision(project_id, "uid:paper:a", "included")
        is False
    )
    decision = storage.get_fulltext_decision(project_id, "uid:paper:a")
    assert decision["status"] == "included"
    audit = storage.list_fulltext_audit(project_id)
    assert len(audit) == 1
    assert audit[0]["new_status"] == "included"

    assert (
        storage.set_fulltext_decision(
            project_id, "uid:paper:a", "excluded", reason="数据不完整"
        )
        is True
    )
    audit = storage.list_fulltext_audit(project_id)
    assert len(audit) == 2
    assert audit[0]["old_status"] == "included"
    assert audit[0]["new_status"] == "excluded"
