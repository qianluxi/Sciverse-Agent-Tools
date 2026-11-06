import pytest

import research_app.storage as storage


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "database_path", lambda: tmp_path / "workbench.sqlite3")


def _setup(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = storage.create_screening_project("Review", "Q", "I", "E")
    storage.add_screening_candidates(
        project_id,
        [
            {"title": "Paper A", "unique_id": "paper:a"},
            {"title": "Paper B", "unique_id": "paper:b"},
            {"title": "Paper C", "unique_id": "paper:c"},
            {"title": "Paper D", "unique_id": "paper:d"},
        ],
    )
    reviewer_one = storage.create_reviewer("Alice", "reviewer")
    reviewer_two = storage.create_reviewer("Bob", "reviewer")
    coordinator = storage.create_reviewer("Carol", "coordinator")
    viewer = storage.create_reviewer("Dave", "viewer")
    return {
        "project_id": project_id,
        "reviewer_one": reviewer_one,
        "reviewer_two": reviewer_two,
        "coordinator": coordinator,
        "viewer": viewer,
    }


def test_reviewer_roles_and_duplicate_names(monkeypatch, tmp_path):
    ctx = _setup(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        storage.create_reviewer("Alice", "reviewer")
    with pytest.raises(ValueError):
        storage.create_reviewer("Eve", "unknown")

    with pytest.raises(ValueError):
        storage.set_blind_decision(
            ctx["project_id"], "uid:paper:a", ctx["viewer"], "included"
        )


def test_blind_decisions_detect_conflict_and_agreement(monkeypatch, tmp_path):
    ctx = _setup(monkeypatch, tmp_path)
    project_id = ctx["project_id"]

    storage.set_blind_decision(
        project_id, "uid:paper:a", ctx["reviewer_one"], "included"
    )
    storage.set_blind_decision(
        project_id,
        "uid:paper:a",
        ctx["reviewer_two"],
        "excluded",
        reason="主题不相关",
    )
    storage.set_blind_decision(
        project_id, "uid:paper:b", ctx["reviewer_one"], "included"
    )
    storage.set_blind_decision(
        project_id, "uid:paper:b", ctx["reviewer_two"], "included"
    )

    summary = {
        item["candidate_key"]: item
        for item in storage.blind_screening_summary(project_id)
    }
    assert summary["uid:paper:a"]["state"] == "conflict"
    assert summary["uid:paper:b"]["state"] == "agreed"
    assert summary["uid:paper:c"]["state"] == "unreviewed"
    assert summary["uid:paper:a"]["fingerprint"]


def test_blind_decision_revision_concurrency(monkeypatch, tmp_path):
    ctx = _setup(monkeypatch, tmp_path)
    project_id = ctx["project_id"]

    first = storage.set_blind_decision(
        project_id, "uid:paper:a", ctx["reviewer_one"], "included"
    )
    assert first == {"revision": 1, "created": True}
    same = storage.set_blind_decision(
        project_id, "uid:paper:a", ctx["reviewer_one"], "included"
    )
    assert same == {"revision": 1, "created": False}
    with pytest.raises(ValueError):
        storage.set_blind_decision(
            project_id,
            "uid:paper:a",
            ctx["reviewer_one"],
            "excluded",
            reason="主题不相关",
            expected_revision=0,
        )
    updated = storage.set_blind_decision(
        project_id,
        "uid:paper:a",
        ctx["reviewer_one"],
        "excluded",
        reason="主题不相关",
        expected_revision=1,
    )
    assert updated["revision"] == 2


def test_coordinator_resolves_conflict_with_audit(monkeypatch, tmp_path):
    ctx = _setup(monkeypatch, tmp_path)
    project_id = ctx["project_id"]
    storage.set_blind_decision(
        project_id, "uid:paper:a", ctx["reviewer_one"], "included"
    )
    storage.set_blind_decision(
        project_id,
        "uid:paper:a",
        ctx["reviewer_two"],
        "excluded",
        reason="主题不相关",
    )
    fingerprint = next(
        item["fingerprint"]
        for item in storage.blind_screening_summary(project_id)
        if item["candidate_key"] == "uid:paper:a"
    )

    with pytest.raises(ValueError):
        storage.resolve_blind_conflict(
            project_id,
            "uid:paper:a",
            ctx["reviewer_one"],
            "included",
            expected_fingerprint=fingerprint,
        )

    storage.resolve_blind_conflict(
        project_id,
        "uid:paper:a",
        ctx["coordinator"],
        "included",
        notes="coordinator decision",
        expected_fingerprint=fingerprint,
    )

    candidates = {
        item["candidate_key"]: item
        for item in storage.list_all_screening_candidates(project_id)
    }
    assert candidates["uid:paper:a"]["status"] == "included"
    audit = storage.list_screening_audit(project_id)
    assert audit[0]["actor"] == ctx["coordinator"]
    resolutions = storage.list_conflict_resolutions(project_id)
    assert resolutions[0]["resolution_type"] == "conflict"
    assert resolutions[0]["final_status"] == "included"
    assert resolutions[0]["coordinator_id"] == ctx["coordinator"]


def test_stale_fingerprint_is_rejected(monkeypatch, tmp_path):
    ctx = _setup(monkeypatch, tmp_path)
    project_id = ctx["project_id"]
    storage.set_blind_decision(
        project_id, "uid:paper:d", ctx["reviewer_one"], "included"
    )
    storage.set_blind_decision(
        project_id,
        "uid:paper:d",
        ctx["reviewer_two"],
        "excluded",
        reason="主题不相关",
    )
    fingerprint = next(
        item["fingerprint"]
        for item in storage.blind_screening_summary(project_id)
        if item["candidate_key"] == "uid:paper:d"
    )
    storage.set_blind_decision(
        project_id,
        "uid:paper:d",
        ctx["reviewer_two"],
        "excluded",
        reason="主题不相关",
        notes="changed after review",
        expected_revision=1,
    )

    with pytest.raises(ValueError):
        storage.resolve_blind_conflict(
            project_id,
            "uid:paper:d",
            ctx["coordinator"],
            "included",
            expected_fingerprint=fingerprint,
        )


def test_confirm_agreement_and_fulltext_guard(monkeypatch, tmp_path):
    ctx = _setup(monkeypatch, tmp_path)
    project_id = ctx["project_id"]

    storage.set_blind_decision(
        project_id, "uid:paper:b", ctx["reviewer_one"], "included"
    )
    storage.set_blind_decision(
        project_id, "uid:paper:b", ctx["reviewer_two"], "included"
    )
    storage.confirm_blind_agreement(
        project_id, "uid:paper:b", ctx["coordinator"]
    )
    candidates = {
        item["candidate_key"]: item
        for item in storage.list_all_screening_candidates(project_id)
    }
    assert candidates["uid:paper:b"]["status"] == "included"

    storage.set_screening_decision(project_id, "uid:paper:c", "included")
    storage.set_fulltext_decision(project_id, "uid:paper:c", "included")
    storage.set_blind_decision(
        project_id, "uid:paper:c", ctx["reviewer_one"], "included"
    )
    storage.set_blind_decision(
        project_id,
        "uid:paper:c",
        ctx["reviewer_two"],
        "excluded",
        reason="主题不相关",
    )
    with pytest.raises(ValueError):
        storage.resolve_blind_conflict(
            project_id,
            "uid:paper:c",
            ctx["coordinator"],
            "excluded",
            reason="主题不相关",
        )


def test_blind_audit_is_append_only(monkeypatch, tmp_path):
    ctx = _setup(monkeypatch, tmp_path)
    project_id = ctx["project_id"]
    storage.set_blind_decision(
        project_id, "uid:paper:a", ctx["reviewer_one"], "included"
    )
    storage.set_blind_decision(
        project_id,
        "uid:paper:a",
        ctx["reviewer_one"],
        "excluded",
        reason="主题不相关",
        expected_revision=1,
    )
    audit = storage.list_blind_audit(project_id)
    assert len(audit) == 2
    assert audit[0]["old_status"] == "included"
    assert audit[0]["new_status"] == "excluded"
    assert audit[0]["reviewer_name"] == "Alice"
