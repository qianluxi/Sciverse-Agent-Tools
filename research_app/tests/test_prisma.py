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
