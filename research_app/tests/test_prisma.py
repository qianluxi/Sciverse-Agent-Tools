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
    assert counts["fulltext"]["assessed"] == 2
    assert counts["fulltext"]["included"] == 1
    assert counts["fulltext"]["excluded"] == 1
    assert counts["fulltext"]["excluded_reasons"] == [
        {"reason": "研究对象不符", "count": 1}
    ]
    assert counts["included_in_review"] == 1
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
