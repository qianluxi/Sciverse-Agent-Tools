import hashlib
import json
import sqlite3

import pytest

import research_app.storage as storage


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "database_path", lambda: tmp_path / "workbench.sqlite3")


def _project_task():
    project_id = storage.create_screening_project("Matrix", "Q", "I", "E")
    task_id = storage.create_ai_task(
        project_id,
        kind="evidence",
        model="test-model",
        prompt_version="evidence-v1",
        input_hash="matrix-hash",
    )
    return project_id, task_id


def test_evidence_batch_creation_is_idempotent(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id, task_id = _project_task()

    first = storage.create_evidence_batch(
        project_id, task_id, 0, paper_keys=["uid:a", "uid:b"]
    )
    again = storage.create_evidence_batch(
        project_id, task_id, 0, paper_keys=["uid:a", "uid:b"]
    )

    assert first["created"] is True
    assert again["created"] is False
    assert again["id"] == first["id"]
    batches = storage.list_evidence_batches(project_id, task_id)
    assert len(batches) == 1
    assert batches[0]["status"] == "pending"
    assert batches[0]["paper_keys"] == ["uid:a", "uid:b"]

    with pytest.raises(ValueError):
        storage.create_evidence_batch(project_id, task_id, 0, paper_keys=["uid:a"])


def test_evidence_batch_tracks_status_and_timestamps(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id, task_id = _project_task()
    batch = storage.create_evidence_batch(project_id, task_id, 0)

    storage.update_evidence_batch(batch["id"], status="running")
    running = storage.list_evidence_batches(project_id, task_id)[0]
    assert running["status"] == "running"
    assert running["started_at"]
    assert running["finished_at"] is None

    storage.update_evidence_batch(
        batch["id"], status="completed", request_count=3
    )
    done = storage.list_evidence_batches(project_id, task_id)[0]
    assert done["request_count"] == 3
    assert done["finished_at"]

    storage.update_evidence_batch(batch["id"], status="running")
    resumed = storage.list_evidence_batches(project_id, task_id)[0]
    assert resumed["status"] == "running"
    assert resumed["finished_at"] is None

    with pytest.raises(ValueError):
        storage.update_evidence_batch(batch["id"], status="unknown")


def test_append_evidence_version_is_append_only(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id, task_id = _project_task()

    first = storage.append_evidence_version(
        project_id,
        "uid:a",
        task_id,
        source="ai_raw",
        data={"sample_size": 10},
        model="test-model",
        prompt_version="evidence-v1",
        title="Paper A",
        year=2024,
    )
    duplicate = storage.append_evidence_version(
        project_id,
        "uid:a",
        task_id,
        source="ai_raw",
        data={"sample_size": 10},
        model="test-model",
        prompt_version="evidence-v1",
        title="Paper A",
        year=2024,
    )
    revised = storage.append_evidence_version(
        project_id,
        "uid:a",
        task_id,
        source="ai_raw",
        data={"sample_size": 12},
        model="test-model",
        prompt_version="evidence-v1",
    )

    assert first["version_no"] == 1 and first["created"] is True
    assert duplicate["version_no"] == 1 and duplicate["created"] is False
    assert revised["version_no"] == 2 and revised["created"] is True

    history = storage.list_evidence_versions(project_id, "uid:a", source="ai_raw")
    assert [item["version_no"] for item in history] == [2, 1]
    assert history[0]["data"] == {"sample_size": 12}

    human = storage.append_evidence_version(
        project_id,
        "uid:a",
        task_id,
        source="human_corrected",
        data={"sample_size": 12},
        reviewer_note="checked",
    )
    assert human["version_no"] == 1

    with pytest.raises(ValueError):
        storage.append_evidence_version(
            project_id, "uid:a", task_id, source="unknown", data={}
        )


def test_load_evidence_matrix_returns_latest_per_source(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id, task_id = _project_task()

    storage.append_evidence_version(
        project_id,
        "uid:a",
        task_id,
        source="ai_raw",
        data={"v": 1},
        title="Paper A",
        year=2024,
    )
    storage.append_evidence_version(
        project_id, "uid:a", task_id, source="ai_raw", data={"v": 2}
    )
    storage.append_evidence_version(
        project_id, "uid:a", task_id, source="human_corrected", data={"v": 3}
    )
    storage.append_evidence_version(
        project_id, "uid:b", task_id, source="ai_raw", data={"v": 9}, title="Paper B"
    )

    matrix = storage.load_evidence_matrix(project_id)
    by_paper = {entry["paper_id"]: entry for entry in matrix}
    assert set(by_paper) == {"uid:a", "uid:b"}

    paper_a = by_paper["uid:a"]
    assert paper_a["title"] == "Paper A"
    assert paper_a["versions"]["ai_raw"]["version_no"] == 2
    assert paper_a["versions"]["ai_raw"]["data"] == {"v": 2}
    assert paper_a["versions"]["human_corrected"]["data"] == {"v": 3}
    assert "final_adopted" not in paper_a["versions"]
    assert by_paper["uid:b"]["versions"]["ai_raw"]["data"] == {"v": 9}


def test_evidence_matrix_can_write_on_locked_project(monkeypatch, tmp_path):
    # 冻结保护针对基准金标准与候选，只追加的证据版本不影响冻结数据，允许继续写入。
    _isolate(monkeypatch, tmp_path)
    project_id, task_id = _project_task()
    storage.set_screening_project_lock(project_id, True, reason="frozen")

    batch = storage.create_evidence_batch(project_id, task_id, 0)
    assert batch["created"] is True
    version = storage.append_evidence_version(
        project_id, "uid:a", task_id, source="ai_raw", data={"v": 1}
    )
    assert version["version_no"] == 1


def test_append_evidence_version_saves_note_only_changes(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id, task_id = _project_task()

    first = storage.append_evidence_version(
        project_id, "uid:a", task_id, source="ai_raw", data={"v": 1},
        reviewer_note="first",
    )
    second = storage.append_evidence_version(
        project_id, "uid:a", task_id, source="ai_raw", data={"v": 1},
        reviewer_note="second",
    )

    assert first["version_no"] == 1
    assert second["version_no"] == 2
    history = storage.list_evidence_versions(project_id, "uid:a", source="ai_raw")
    assert [item["reviewer_note"] for item in history] == ["second", "first"]


def test_append_evidence_version_validates_task_and_batch(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id, task_id = _project_task()
    batch = storage.create_evidence_batch(
        project_id, task_id, 0, paper_keys=["uid:a"]
    )

    with pytest.raises(ValueError):
        storage.append_evidence_version(
            project_id, "uid:b", task_id, source="ai_raw", data={},
            batch_id=batch["id"],
        )
    with pytest.raises(ValueError):
        storage.append_evidence_version(
            project_id, "uid:a", task_id, source="ai_raw", data={}, batch_id=9999
        )

    screening_task = storage.create_ai_task(
        project_id,
        kind="screening",
        model="test-model",
        prompt_version="screening-v2",
        input_hash="screening-hash",
    )
    with pytest.raises(ValueError):
        storage.append_evidence_version(
            project_id, "uid:a", screening_task, source="ai_raw", data={}
        )


def test_list_ai_tasks_filters_by_kind(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id, task_id = _project_task()
    second = storage.create_ai_task(
        project_id,
        kind="evidence_matrix",
        model="test-model",
        prompt_version="evidence-v1",
        input_hash="second",
    )
    storage.create_ai_task(
        project_id,
        kind="screening",
        model="test-model",
        prompt_version="screening-v2",
        input_hash="screening",
    )

    tasks = storage.list_ai_tasks_by_kind(project_id, "evidence_matrix")
    task_ids = {item["id"] for item in tasks}
    assert second in task_ids
    assert task_id not in task_ids
    assert all(item["kind"] == "evidence_matrix" for item in tasks)


def test_v14_missing_error_column_migrates_to_latest(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id, task_id = _project_task()
    storage.append_evidence_version(
        project_id, "uid:a", task_id, source="ai_raw", data={"v": 1},
        title="Paper A",
    )
    db_path = tmp_path / "workbench.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute("ALTER TABLE evidence_item_versions DROP COLUMN error")
        connection.execute("PRAGMA user_version = 14")

    storage.get_screening_project(project_id)

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 17
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(evidence_item_versions)"
            )
        }
        assert "error" in columns
        row = connection.execute(
            """SELECT paper_id, data_json, version_no, error
               FROM evidence_item_versions"""
        ).fetchone()
        assert row[0] == "uid:a"
        assert json.loads(row[1]) == {"v": 1}
        assert row[2] == 1
        assert row[3] is None

    # Running initialization again must be a no-op.
    storage.get_screening_project(project_id)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 17


def test_task_inputs_are_frozen(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id, task_id = _project_task()
    entry = {
        "paper_id": "uid:a",
        "candidate_key": "uid:a",
        "title": "Paper A",
        "year": 2024,
        "doc_id": "doc:1",
        "source_run_ids": ["run:1"],
        "input_source": "full_text",
        "input_text": "frozen text",
    }

    first = storage.save_evidence_task_inputs(project_id, task_id, [entry])
    assert first == {"saved": 1, "skipped": 0}

    same = storage.save_evidence_task_inputs(project_id, task_id, [entry])
    assert same == {"saved": 0, "skipped": 1}

    changed = {**entry, "input_text": "changed text"}
    with pytest.raises(ValueError):
        storage.save_evidence_task_inputs(project_id, task_id, [changed])

    inputs = storage.list_evidence_task_inputs(project_id, task_id)
    assert len(inputs) == 1
    assert inputs[0]["input_text"] == "frozen text"
    assert inputs[0]["source_run_ids"] == ["run:1"]
    assert inputs[0]["input_text_hash"] == hashlib.sha256(
        b"frozen text"
    ).hexdigest()
    assert (
        storage.get_evidence_task_input(project_id, task_id, "uid:a")["input_source"]
        == "full_text"
    )
