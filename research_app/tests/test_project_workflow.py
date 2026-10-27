import json

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
    stats = storage.screening_stats(project_id)
    assert stats["identified"] == 2
    assert stats["duplicates"] == 0
    assert stats["unique"] == 2


def test_project_statistics_without_sources_cannot_rebuild(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = _project()
    storage.add_screening_candidates(project_id, [_paper("a", "Paper A")])

    report = storage.inspect_project_statistics(project_id)[0]

    assert report["needs_review"] is True
    assert report["can_rebuild"] is False
    with pytest.raises(ValueError, match="不能自动重建"):
        storage.rebuild_project_statistics(project_id, apply=True)


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
        ):
            count = connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]
            assert count == 0


def test_schema_v5_tables_are_created(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _project()
    with storage._connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {
        "search_runs",
        "screening_candidate_sources",
        "ai_task_runs",
        "evidence_items",
    } <= tables
