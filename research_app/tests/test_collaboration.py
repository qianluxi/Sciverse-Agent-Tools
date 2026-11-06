import threading

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
        project_id,
        "uid:paper:a",
        ctx["reviewer_one"],
        "included",
        expected_revision=1,
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


def _add_ai_suggestions(project_id):
    task_id = storage.create_ai_task(
        project_id,
        kind="screening",
        model="test-model",
        prompt_version="screening-v2",
        input_hash="collab-ai",
    )
    storage.save_screening_suggestions(
        project_id,
        task_id,
        [
            {
                "candidate_key": "uid:paper:b",
                "paper_id": "paper:b",
                "title": "Paper B",
                "suggestion": "include",
                "criteria": [],
                "rationale": "",
                "confidence": "high",
                "evidence_quotes": [],
                "quote_checks": [],
                "invalid_criteria": [],
            },
            {
                "candidate_key": "uid:paper:c",
                "paper_id": "paper:c",
                "title": "Paper C",
                "suggestion": "uncertain",
                "criteria": [],
                "rationale": "",
                "confidence": "low",
                "evidence_quotes": [],
                "quote_checks": [],
                "invalid_criteria": [],
            },
            {
                "candidate_key": "uid:paper:d",
                "paper_id": "paper:d",
                "title": "Paper D",
                "suggestion": "exclude",
                "criteria": [],
                "rationale": "",
                "confidence": "low",
                "evidence_quotes": [],
                "quote_checks": [],
                "invalid_criteria": [],
            },
        ],
    )
    return task_id


def test_ai_priority_queue_and_provenance(monkeypatch, tmp_path):
    ctx = _setup(monkeypatch, tmp_path)
    project_id = ctx["project_id"]
    task_id = _add_ai_suggestions(project_id)
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

    priority = storage.ai_priority_candidates(project_id)
    assert [item["candidate_key"] for item in priority] == [
        "uid:paper:a",
        "uid:paper:b",
        "uid:paper:d",
        "uid:paper:c",
    ]
    assert priority[0]["priority_rank"] == 0
    assert priority[1]["ai_suggestion"] == "include"
    assert priority[1]["ai_task_id"] == task_id
    assert priority[2]["ai_suggestion"] == "exclude"
    assert priority[3]["ai_suggestion"] == "uncertain"

    storage.set_blind_decision(
        project_id,
        "uid:paper:b",
        ctx["reviewer_one"],
        "included",
        ai_task_id=task_id,
        ai_suggestion="include",
        ai_confidence="high",
        ai_assisted=True,
        expected_revision=0,
    )
    decision = next(
        item
        for item in storage.list_blind_decisions(
            project_id, reviewer_id=ctx["reviewer_one"]
        )
        if item["candidate_key"] == "uid:paper:b"
    )
    assert decision["ai_assisted"] == 1
    assert decision["ai_suggestion"] == "include"
    assert decision["ai_confidence"] == "high"

    # Re-saving identical content without AI args keeps provenance.
    storage.set_blind_decision(
        project_id,
        "uid:paper:b",
        ctx["reviewer_one"],
        "included",
        expected_revision=1,
    )
    decision = next(
        item
        for item in storage.list_blind_decisions(
            project_id, reviewer_id=ctx["reviewer_one"]
        )
        if item["candidate_key"] == "uid:paper:b"
    )
    assert decision["ai_assisted"] == 1

    # Changing the decision manually clears AI provenance.
    storage.set_blind_decision(
        project_id,
        "uid:paper:b",
        ctx["reviewer_one"],
        "excluded",
        reason="主题不相关",
        expected_revision=1,
    )
    decision = next(
        item
        for item in storage.list_blind_decisions(
            project_id, reviewer_id=ctx["reviewer_one"]
        )
        if item["candidate_key"] == "uid:paper:b"
    )
    assert decision["ai_assisted"] == 0
    assert decision["ai_suggestion"] == ""


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


def test_reviewer_pin_authentication(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    reviewer = storage.create_reviewer("Pinned", "reviewer", "2468")
    assert storage.verify_reviewer_pin(reviewer, "2468") is True
    assert storage.verify_reviewer_pin(reviewer, "0000") is False
    assert storage.verify_reviewer_pin(reviewer, "") is False
    legacy = storage.create_reviewer("Legacy", "reviewer")
    assert storage.verify_reviewer_pin(legacy, "anything") is False
    storage.set_reviewer_pin(legacy, "1357")
    assert storage.verify_reviewer_pin(legacy, "1357") is True


def test_concurrent_first_submission_has_single_winner(monkeypatch, tmp_path):
    ctx = _setup(monkeypatch, tmp_path)
    project_id = ctx["project_id"]
    reviewer = ctx["reviewer_one"]
    barrier = threading.Barrier(2)
    results = []

    def worker(status, reason):
        barrier.wait()
        try:
            storage.set_blind_decision(
                project_id,
                "uid:paper:a",
                reviewer,
                status,
                reason=reason,
                expected_revision=0,
            )
            results.append("ok")
        except ValueError:
            results.append("rejected")

    threads = [
        threading.Thread(target=worker, args=("included", None)),
        threading.Thread(
            target=worker, args=("excluded", "主题不相关")
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == ["ok", "rejected"]
    decisions = storage.list_blind_decisions(
        project_id, reviewer_id=reviewer
    )
    assert len(decisions) == 1
    assert decisions[0]["revision"] == 1
    assert len(storage.list_blind_audit(project_id)) == 1


def test_reason_conflict_requires_coordinator_choice(monkeypatch, tmp_path):
    ctx = _setup(monkeypatch, tmp_path)
    project_id = ctx["project_id"]
    storage.set_blind_decision(
        project_id,
        "uid:paper:a",
        ctx["reviewer_one"],
        "excluded",
        reason="主题不相关",
    )
    storage.set_blind_decision(
        project_id,
        "uid:paper:a",
        ctx["reviewer_two"],
        "excluded",
        reason="研究类型不符",
    )
    summary = {
        item["candidate_key"]: item
        for item in storage.blind_screening_summary(project_id)
    }
    assert summary["uid:paper:a"]["state"] == "reason_conflict"

    with pytest.raises(ValueError):
        storage.confirm_blind_agreement(
            project_id, "uid:paper:a", ctx["coordinator"]
        )
    storage.confirm_blind_agreement(
        project_id,
        "uid:paper:a",
        ctx["coordinator"],
        reason="其他",
    )
    candidates = {
        item["candidate_key"]: item
        for item in storage.list_all_screening_candidates(project_id)
    }
    assert candidates["uid:paper:a"]["status"] == "excluded"
    assert candidates["uid:paper:a"]["exclusion_reason"] == "其他"


def test_reviewer_priority_hides_global_blind_state(monkeypatch, tmp_path):
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
    rows = storage.reviewer_priority_candidates(
        project_id, ctx["reviewer_one"]
    )
    assert rows
    assert all("blind_state" not in row for row in rows)
    assert all("decisions" not in row for row in rows)
