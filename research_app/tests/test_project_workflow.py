import json
import sqlite3

import pytest

import research_app.storage as storage


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "database_path", lambda: tmp_path / "workbench.sqlite3")


def _project():
    return storage.create_screening_project("Review", "Question", "Include", "Exclude")


def _paper(identifier: str, title: str) -> dict:
    return {
        "title": title,
        "doi": f"10.1000/{identifier}",
        "unique_id": f"paper:{identifier}",
        "publication_published_year": 2024,
    }


def test_search_runs_are_idempotent_and_listed(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = _project()
    papers = [_paper("a", "Paper A"), _paper("b", "Paper B")]
    params = {"query": "topic", "page": 1, "page_size": 20}

    first = storage.save_search_run(project_id, "structured", params, papers, 2)
    second = storage.save_search_run(project_id, "structured", params, papers, 2)
    reordered = storage.save_search_run(
        project_id,
        "structured",
        params,
        list(reversed(papers)),
        2,
    )

    assert first["created"] is True
    assert second["created"] is False
    assert reordered["created"] is False
    assert first["id"] == second["id"] == reordered["id"]
    runs = storage.list_search_runs(project_id)
    assert len(runs) == 1
    assert runs[0]["params"] == params
    assert runs[0]["result_count"] == 2
    assert runs[0]["snapshot_count"] == 2


def test_same_page_with_different_total_creates_new_snapshot(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = _project()
    papers = [_paper("a", "Paper A")]
    params = {"query": "topic", "page": 1, "page_size": 20}

    first = storage.save_search_run(project_id, "structured", params, papers, 200)
    second = storage.save_search_run(project_id, "structured", params, papers, 235)

    assert first["id"] != second["id"]
    runs = storage.list_search_runs(project_id)
    assert {run["result_count"] for run in runs} == {200, 235}


def test_reimport_same_run_does_not_change_project_stats(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = _project()
    papers = [_paper("a", "Paper A"), _paper("b", "Paper B")]
    run = storage.save_search_run(
        project_id,
        "structured",
        {"query": "topic", "page": 1, "page_size": 20},
        papers,
        2,
    )

    first = storage.add_screening_candidates(project_id, papers, source_run_id=run["id"])
    stats_before = storage.screening_stats(project_id)
    second = storage.add_screening_candidates(project_id, papers, source_run_id=run["id"])
    stats_after = storage.screening_stats(project_id)

    assert first == {"identified": 2, "added": 2, "duplicates": 0, "conflicts": 0}
    assert second == {"identified": 0, "added": 0, "duplicates": 0, "conflicts": 0}
    assert stats_before == stats_after
    with storage._connection() as connection:
        source_count = connection.execute(
            """SELECT COUNT(*) FROM screening_candidate_sources
               WHERE project_id = ? AND run_id = ?""",
            (project_id, run["id"]),
        ).fetchone()[0]
    assert source_count == 2


def test_cross_page_sources_are_kept_per_paper(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = _project()
    paper_a = _paper("a", "Paper A")
    paper_b = _paper("b", "Paper B")
    run_a = storage.save_search_run(
        project_id,
        "structured",
        {"query": "topic", "page": 1, "page_size": 1},
        [paper_a],
        2,
    )
    run_b = storage.save_search_run(
        project_id,
        "structured",
        {"query": "topic", "page": 2, "page_size": 1},
        [paper_b],
        2,
    )

    storage.add_screening_candidates(
        project_id,
        [paper_a, paper_b],
        source_run_ids=[run_a["id"], run_b["id"]],
    )

    with storage._connection() as connection:
        sources = {
            row["candidate_key"]: row["run_id"]
            for row in connection.execute(
                """SELECT candidate_key, run_id FROM screening_candidate_sources
                   WHERE project_id = ?""",
                (project_id,),
            )
        }
    assert sources["doi:10.1000/a"] == run_a["id"]
    assert sources["doi:10.1000/b"] == run_b["id"]


def test_cross_project_source_creates_manual_snapshot(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    source_project = _project()
    target_project = storage.create_screening_project("Target", "Q2", "I", "E")
    papers = [_paper("a", "Paper A")]
    old_run = storage.save_search_run(
        source_project,
        "structured",
        {"query": "old", "page": 1, "page_size": 20},
        papers,
        1,
    )

    result = storage.add_screening_candidates(
        target_project,
        papers,
        source_run_id=old_run["id"],
    )

    assert result["manual_source_created"] is True
    runs = storage.list_search_runs(target_project)
    assert len(runs) == 1
    assert runs[0]["params"] == {"source": "manual_selection"}
    with storage._connection() as connection:
        source = connection.execute(
            """SELECT run_id FROM screening_candidate_sources
               WHERE project_id = ?""",
            (target_project,),
        ).fetchone()
    assert source["run_id"] == runs[0]["id"]


def test_candidate_sources_and_project_overview(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = _project()
    papers = [_paper("a", "Paper A"), _paper("b", "Paper B")]
    run = storage.save_search_run(
        project_id,
        "structured",
        {"query": "topic", "page": 1, "page_size": 20},
        papers,
        2,
    )

    result = storage.add_screening_candidates(
        project_id,
        papers,
        source_run_id=run["id"],
    )

    assert result == {"identified": 2, "added": 2, "duplicates": 0, "conflicts": 0}
    with storage._connection() as connection:
        sources = connection.execute(
            """SELECT candidate_key, run_id FROM screening_candidate_sources
               WHERE project_id = ? ORDER BY candidate_key""",
            (project_id,),
        ).fetchall()
    assert len(sources) == 2
    assert {row["run_id"] for row in sources} == {run["id"]}

    overview = storage.project_overview(project_id)
    assert overview["search_runs"] == 1
    assert overview["unique"] == 2
    assert overview["pending"] == 2
    assert overview["latest_search"]["id"] == run["id"]


def test_conflict_candidate_cannot_save_decision(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = _project()
    storage.add_screening_candidates(
        project_id,
        [
            {"title": "A", "doi": "10.1000/a", "unique_id": "paper:a"},
            {"title": "B", "doi": "10.1000/b", "unique_id": "paper:b"},
        ],
    )
    storage.add_screening_candidates(
        project_id,
        [{"title": "Conflict", "doi": "10.1000/a", "unique_id": "paper:b"}],
    )
    candidates, _ = storage.list_screening_candidates(project_id, page_size=10)
    conflict = next(item for item in candidates if item["candidate_key"].startswith("conflict:"))

    with pytest.raises(ValueError, match="身份冲突"):
        storage.set_screening_decision(project_id, conflict["candidate_key"], "included")


def test_conflict_candidate_keeps_source_run(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = _project()
    storage.add_screening_candidates(
        project_id,
        [
            {"title": "A", "doi": "10.1000/a", "unique_id": "paper:a"},
            {"title": "B", "doi": "10.1000/b", "unique_id": "paper:b"},
        ],
    )
    conflict_paper = {"title": "Conflict", "doi": "10.1000/a", "unique_id": "paper:b"}
    run = storage.save_search_run(
        project_id,
        "structured",
        {"query": "conflict", "page": 1, "page_size": 20},
        [conflict_paper],
        1,
    )

    storage.add_screening_candidates(
        project_id,
        [conflict_paper],
        source_run_id=run["id"],
    )

    with storage._connection() as connection:
        conflict_key = connection.execute(
            """SELECT candidate_key FROM screening_candidates
               WHERE project_id = ? AND candidate_key LIKE 'conflict:%'""",
            (project_id,),
        ).fetchone()["candidate_key"]
        source = connection.execute(
            """SELECT run_id FROM screening_candidate_sources
               WHERE project_id = ? AND candidate_key = ?""",
            (project_id, conflict_key),
        ).fetchone()
    assert source["run_id"] == run["id"]


def test_field_scan_results_and_report_persist(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = _project()
    task_id = storage.create_ai_task(
        project_id,
        kind="field_scan",
        model="test-model",
        prompt_version="field-scan-v1",
        input_hash="scan-hash",
    )
    storage.save_evidence_items(
        project_id,
        task_id,
        [
            {
                "paper_id": "paper:a",
                "title": "Classic paper",
                "year": 2002,
                "data": {
                    "category": "foundational",
                    "core_summary": "经典研究。",
                    "rationale": "早期工作。",
                    "confidence": "high",
                    "evidence_quotes": [],
                },
                "reviewed": True,
                "reviewer_note": "checked",
                "model": "test-model",
                "prompt_version": "field-scan-v1",
            }
        ],
    )
    storage.save_scan_report(
        project_id,
        task_id,
        [{"theme": "剪力墙", "summary": "主题", "paper_ids": ["paper:a"]}],
        [{"gap": "缺少长期数据", "evidence": "样本有限", "paper_ids": ["paper:a"]}],
        aggregation_error="summary failed",
    )

    saved = storage.load_latest_scan(project_id)

    assert saved is not None
    assert saved["items"][0]["data"]["category"] == "foundational"
    assert saved["items"][0]["reviewed"] is True
    assert saved["themes"][0]["theme"] == "剪力墙"
    assert saved["gaps"][0]["gap"] == "缺少长期数据"
    assert saved["aggregation_error"] == "summary failed"


def test_ai_screening_suggestions_and_human_review_are_audited(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = _project()
    storage.add_screening_candidates(
        project_id,
        [
            {
                "title": "Eligible study",
                "unique_id": "paper:a",
                "abstract": "Randomized trial of the intervention.",
            }
        ],
    )
    task_id = storage.create_ai_task(
        project_id,
        kind="screening",
        model="test-model",
        prompt_version="screening-v1",
        input_hash="screening-hash",
    )
    storage.save_screening_suggestions(
        project_id,
        task_id,
        [
            {
                "candidate_key": "uid:paper:a",
                "paper_id": "paper:a",
                "title": "Eligible study",
                "year": 2024,
                "suggestion": "include",
                "criteria": ["研究类型符合"],
                "rationale": "Randomized trial",
                "confidence": "high",
                "evidence_quotes": [
                    {"quote": "Randomized trial", "section": "摘要"}
                ],
                "quote_checks": [
                    {"quote": "Randomized trial", "section": "摘要", "verified": True}
                ],
            }
        ],
    )

    loaded = storage.load_latest_screening_suggestions(project_id)
    assert loaded is not None
    assert loaded["task"]["kind"] == "screening"
    assert loaded["suggestions"][0]["criteria"] == ["研究类型符合"]
    assert loaded["suggestions"][0]["review_status"] == "pending"
    candidates = storage.list_all_screening_candidates(project_id)
    assert candidates[0]["abstract"] == "Randomized trial of the intervention."

    storage.set_screening_decision(
        project_id,
        "uid:paper:a",
        "included",
        ai_task_id=task_id,
        ai_action="accepted",
    )
    storage.review_screening_suggestion(
        project_id,
        task_id,
        "uid:paper:a",
        "accepted",
        review_decision="included",
        note="checked",
    )

    loaded = storage.load_latest_screening_suggestions(project_id)
    assert loaded["suggestions"][0]["review_status"] == "accepted"
    assert loaded["suggestions"][0]["review_decision"] == "included"
    assert loaded["suggestions"][0]["review_note"] == "checked"
    audit = storage.list_screening_audit(project_id)
    assert audit[0]["new_status"] == "included"
    assert audit[0]["ai_task_id"] == task_id
    assert audit[0]["ai_action"] == "accepted"
    manifest = json.loads(storage.project_manifest(project_id))
    assert manifest["summary"]["ai_screening_suggestions_exported"] == 1
    assert manifest["ai_screening_suggestions"][0]["review_status"] == "accepted"


def test_apply_screening_review_rolls_back_all_writes_on_event_failure(
    monkeypatch, tmp_path
):
    _isolate(monkeypatch, tmp_path)
    project_id = _project()
    storage.add_screening_candidates(
        project_id,
        [{"title": "Paper A", "unique_id": "paper:a"}],
    )
    task_id = storage.create_ai_task(
        project_id,
        kind="screening",
        model="test-model",
        prompt_version="screening-v2",
        input_hash="atomic-hash",
    )
    storage.save_screening_suggestions(
        project_id,
        task_id,
        [
            {
                "candidate_key": "uid:paper:a",
                "paper_id": "paper:a",
                "title": "Paper A",
                "suggestion": "include",
                "criteria": [],
                "rationale": "test",
                "confidence": "high",
                "evidence_quotes": [],
                "quote_checks": [],
                "invalid_criteria": [],
            }
        ],
    )
    with storage._connection() as connection:
        connection.execute(
            """CREATE TRIGGER fail_review_event
               BEFORE INSERT ON ai_screening_review_events
               BEGIN SELECT RAISE(ABORT, 'forced event failure'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError):
        storage.apply_screening_review(
            project_id,
            task_id,
            "uid:paper:a",
            "included",
            note="should roll back",
        )

    candidates = storage.list_all_screening_candidates(project_id)
    assert candidates[0]["status"] == "pending"
    saved = storage.load_latest_screening_suggestions(project_id)
    assert saved["suggestions"][0]["review_status"] == "pending"
    assert storage.list_screening_audit(project_id) == []
    assert storage.list_screening_review_events(project_id) == []


def test_screening_snapshot_freezes_gold_and_review_events_are_append_only(
    monkeypatch, tmp_path
):
    _isolate(monkeypatch, tmp_path)
    project_id = _project()
    storage.add_screening_candidates(
        project_id,
        [{"title": "Paper A", "unique_id": "paper:a"}],
    )
    task_id = storage.create_ai_task(
        project_id,
        kind="screening_benchmark",
        model="test-model",
        prompt_version="screening-v2",
        input_hash="snapshot-hash",
    )
    snapshot = storage.create_screening_snapshot(
        project_id,
        task_id,
        kind="screening_benchmark",
        criteria={"question": "Q", "items": []},
        items=[{"candidate_key": "uid:paper:a", "title": "Paper A"}],
        gold={"uid:paper:a": "included"},
    )
    storage.set_screening_decision(
        project_id,
        "uid:paper:a",
        "excluded",
        reason="其他",
    )

    loaded = storage.load_screening_snapshot(project_id, task_id)
    assert loaded["gold"] == {"uid:paper:a": "included"}
    assert loaded["dataset_hash"] == snapshot["dataset_hash"]

    evaluation = {"total": 1, "metrics": {"include_recall": 1.0}}
    storage.save_screening_snapshot_evaluation(task_id, evaluation)
    assert (
        storage.load_screening_snapshot(project_id, task_id)["evaluation"]
        == evaluation
    )

    storage.append_screening_review_event(
        project_id,
        task_id,
        "uid:paper:a",
        ai_suggestion="include",
        review_status="accepted",
        review_decision="included",
        status_before="pending",
        status_after="included",
        note="first",
    )
    storage.append_screening_review_event(
        project_id,
        task_id,
        "uid:paper:a",
        ai_suggestion="include",
        review_status="rejected",
        review_decision=None,
        status_before="included",
        status_after="included",
        note="second",
    )
    events = storage.list_screening_review_events(project_id, task_id=task_id)
    assert [event["note"] for event in events] == ["second", "first"]


def test_ai_evidence_persists_raw_and_final_versions(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = _project()
    task_id = storage.create_ai_task(
        project_id,
        kind="evidence",
        model="test-model",
        prompt_version="evidence-v1",
        input_hash="input-hash",
    )
    raw = {"findings": "AI original", "evidence_quotes": [{"quote": "quote"}]}
    edited = {"findings": "Human edited", "evidence_quotes": [{"quote": "quote"}]}

    storage.save_evidence_items(
        project_id,
        task_id,
        [
            {
                "paper_id": "paper:a",
                "title": "Paper A",
                "year": 2024,
                "data": raw,
                "raw_data": raw,
                "quote_checks": [{"quote": "quote", "verified": True}],
                "reviewed": False,
                "model": "test-model",
                "prompt_version": "evidence-v1",
            }
        ],
    )
    storage.save_evidence_items(
        project_id,
        task_id,
        [
            {
                "paper_id": "paper:a",
                "title": "Paper A",
                "year": 2024,
                "data": edited,
                "raw_data": raw,
                "quote_checks": [{"quote": "quote", "verified": True}],
                "reviewed": False,
                "model": "test-model",
                "prompt_version": "evidence-v1",
            }
        ],
    )
    with storage._connection() as connection:
        pending = connection.execute(
            """SELECT final_json, reviewed_json FROM evidence_items
               WHERE project_id = ? AND task_id = ? AND paper_id = ?""",
            (project_id, task_id, "paper:a"),
        ).fetchone()
    assert json.loads(pending["final_json"]) == {}
    assert json.loads(pending["reviewed_json"])["findings"] == "Human edited"
    unreviewed = storage.load_latest_evidence(project_id)
    assert unreviewed["items"][0]["data"]["findings"] == "Human edited"
    assert unreviewed["items"][0]["reviewed"] is False

    storage.save_evidence_items(
        project_id,
        task_id,
        [
            {
                "paper_id": "paper:a",
                "title": "Paper A",
                "year": 2024,
                "data": edited,
                "raw_data": raw,
                "quote_checks": [{"quote": "quote", "verified": True}],
                "reviewed": True,
                "reviewer_note": "checked",
                "model": "test-model",
                "prompt_version": "evidence-v1",
            }
        ],
    )

    saved = storage.load_latest_evidence(project_id)
    assert saved is not None
    item = saved["items"][0]
    assert item["raw_data"]["findings"] == "AI original"
    assert item["data"]["findings"] == "Human edited"
    assert item["reviewed"] is True
    assert item["reviewer_note"] == "checked"
    overview = storage.project_overview(project_id)
    assert overview["ai_tasks"] == 1
    assert overview["evidence_items"] == 1


def test_project_manifest_includes_source_traceability(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = _project()
    paper = _paper("a", "Paper A")
    run = storage.save_search_run(
        project_id,
        "structured",
        {"query": "topic", "page": 1, "page_size": 20},
        [paper],
        1,
    )
    storage.add_screening_candidates(project_id, [paper], source_run_id=run["id"])

    manifest = json.loads(storage.project_manifest(project_id))
    candidate = manifest["candidates"][0]
    assert candidate["sources"][0]["run_id"] == run["id"]
    assert manifest["search_runs"][0]["id"] == run["id"]
    assert manifest["summary"]["unique"] == 1


def test_project_manifest_exports_all_runs_and_tasks(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = _project()
    paper = _paper("a", "Paper A")
    first_run = None
    for index in range(101):
        run = storage.save_search_run(
            project_id,
            "structured",
            {"query": "topic", "page": index + 1, "page_size": 1},
            [paper],
            101,
        )
        if first_run is None:
            first_run = run
    for _ in range(101):
        storage.create_ai_task(
            project_id,
            kind="evidence",
            model="m",
            prompt_version="v",
            input_hash="h",
        )
    storage.add_screening_candidates(
        project_id,
        [paper],
        source_run_id=first_run["id"],
    )

    manifest = json.loads(storage.project_manifest(project_id))

    assert manifest["summary"]["complete"] is True
    assert manifest["summary"]["search_runs_exported"] == 101
    assert manifest["summary"]["ai_tasks_exported"] == 101
    run_ids = {run["id"] for run in manifest["search_runs"]}
    for candidate in manifest["candidates"]:
        for source in candidate["sources"]:
            assert source["run_id"] in run_ids


def test_project_statistics_are_audited_and_rebuilt_from_sources(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = _project()
    papers = [_paper("a", "Paper A"), _paper("b", "Paper B")]
    run = storage.save_search_run(
        project_id,
        "structured",
        {"query": "topic", "page": 1, "page_size": 20},
        papers,
        2,
    )
    storage.add_screening_candidates(project_id, papers, source_run_id=run["id"])
    with storage._connection() as connection:
        connection.execute(
            """UPDATE screening_projects
               SET identified_count = 99, duplicate_count = 99 WHERE id = ?""",
            (project_id,),
        )

    report = storage.inspect_project_statistics(project_id)[0]
    assert report["needs_review"] is True
    assert report["can_rebuild"] is True
    assert report["estimated_identified"] == 2
    assert report["estimated_duplicates"] == 0

    result = storage.rebuild_project_statistics(project_id, apply=True)

    assert result["applied"] is True
    assert result["backup_path"]
    with sqlite3.connect(result["backup_path"]) as backup:
        old = backup.execute(
            "SELECT identified_count, duplicate_count FROM screening_projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert old == (99, 99)
    stats = storage.screening_stats(project_id)
    assert stats["identified"] == 2
    assert stats["duplicates"] == 0
    assert stats["unique"] == 2


def test_legacy_project_without_import_events_cannot_rebuild(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = _project()
    storage.add_screening_candidates(project_id, [_paper("a", "Paper A")])
    with storage._connection() as connection:
        connection.execute(
            "UPDATE screening_projects SET stats_tracked = 0 WHERE id = ?",
            (project_id,),
        )

    report = storage.inspect_project_statistics(project_id)[0]

    assert report["needs_review"] is True
    assert report["can_rebuild"] is False
    with pytest.raises(ValueError, match="无法精确重建"):
        storage.rebuild_project_statistics(project_id, apply=True)


def test_statistics_rebuild_uses_import_events_for_multi_source_duplicates(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = _project()
    paper = _paper("a", "Paper A")
    runs = []
    for page in (1, 2, 3):
        runs.append(
            storage.save_search_run(
                project_id,
                "structured",
                {"query": "topic", "page": page, "page_size": 1},
                [paper],
                3,
            )
        )
    storage.save_search_run(
        project_id,
        "structured",
        {"query": "topic", "page": 4, "page_size": 1},
        [_paper("d", "Unused D")],
        3,
    )
    for run in runs:
        storage.add_screening_candidates(project_id, [paper], source_run_id=run["id"])

    stats = storage.screening_stats(project_id)
    assert stats["identified"] == 3
    assert stats["duplicates"] == 2
    report = storage.inspect_project_statistics(project_id)[0]
    assert report["can_rebuild"] is True
    assert report["estimated_identified"] == 3
    assert report["estimated_duplicates"] == 2
    assert report["needs_review"] is False

    result = storage.rebuild_project_statistics(project_id, apply=True)
    assert result["backup_path"] is None
    assert storage.screening_stats(project_id)["identified"] == 3
    assert storage.screening_stats(project_id)["duplicates"] == 2


def test_delete_project_cascades_all_project_data(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = _project()
    other_id = storage.create_screening_project("Other", "Q2", "I", "E")
    papers = [_paper("a", "Paper A")]
    run = storage.save_search_run(
        project_id,
        "structured",
        {"query": "topic", "page": 1, "page_size": 20},
        papers,
        1,
    )
    storage.add_screening_candidates(project_id, papers, source_run_id=run["id"])
    task_id = storage.create_ai_task(
        project_id,
        kind="evidence",
        model="m",
        prompt_version="v",
        input_hash="h",
    )
    storage.save_evidence_items(
        project_id,
        task_id,
        [{"paper_id": "paper:a", "data": {"findings": "ok"}}],
    )

    assert storage.delete_screening_project(project_id) is True
    assert storage.delete_screening_project(project_id) is False
    assert [project["id"] for project in storage.list_screening_projects()] == [other_id]
    with storage._connection() as connection:
        for table in (
            "screening_candidates",
            "screening_candidate_identifiers",
            "screening_candidate_sources",
            "screening_duplicates",
            "screening_audit",
            "search_runs",
            "ai_task_runs",
            "evidence_items",
            "project_import_events",
            "ai_scan_reports",
            "ai_screening_suggestions",
            "ai_screening_snapshots",
            "ai_screening_review_events",
        ):
            count = connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]
            assert count == 0


def test_schema_v10_tables_are_created(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _project()
    with storage._connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        report_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(ai_scan_reports)")
        }
        candidate_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(screening_candidates)")
        }
        audit_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(screening_audit)")
        }
        suggestion_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(ai_screening_suggestions)"
            )
        }
    assert {
        "search_runs",
        "screening_candidate_sources",
        "ai_task_runs",
        "evidence_items",
        "project_import_events",
        "ai_scan_reports",
        "ai_screening_suggestions",
        "ai_screening_snapshots",
        "ai_screening_review_events",
    } <= tables
    assert "aggregation_error" in report_columns
    assert "abstract" in candidate_columns
    assert {"ai_task_id", "ai_action"} <= audit_columns
    assert "invalid_criteria_json" in suggestion_columns
