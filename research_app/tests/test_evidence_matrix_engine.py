from research_app.ai import AIError
from research_app.ai_review import export_matrix_csv, export_matrix_markdown
from research_app.evidence_matrix import (
    pending_batches,
    plan_batches,
    run_pending_batches,
)
import research_app.storage as storage


EVIDENCE_JSON = (
    '{"research_question":"Q","methods":"M","sample":"S",'
    '"findings":"F","limitations":"L",'
    '"evidence_quotes":[{"quote":"quote","section":"Results"}]}'
)


class FakeClient:
    model = "test-model"

    def __init__(self, *, fail_times: int = 0) -> None:
        self.calls = 0
        self.fail_times = fail_times

    def chat(self, messages, max_tokens=2048):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise AIError("HTTP 429: rate limited")
        return EVIDENCE_JSON


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "database_path", lambda: tmp_path / "workbench.sqlite3")


def _project_task():
    project_id = storage.create_screening_project("Matrix", "Q", "I", "E")
    task_id = storage.create_ai_task(
        project_id,
        kind="evidence_matrix",
        model="test-model",
        prompt_version="evidence-v1",
        input_hash="engine-hash",
    )
    return project_id, task_id


def _papers(count: int):
    return [
        {"paper_id": f"uid:{index}", "title": f"Paper {index}", "year": 2024}
        for index in range(count)
    ]


def _lookup(papers):
    return {paper["paper_id"]: paper for paper in papers}


def test_plan_batches_chunks_and_is_idempotent(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id, task_id = _project_task()
    papers = _papers(7)

    planned = plan_batches(project_id, task_id, papers, batch_size=3)
    assert [len(item["papers"]) for item in planned] == [3, 3, 1]
    batches = storage.list_evidence_batches(project_id, task_id)
    assert [len(batch["paper_keys"]) for batch in batches] == [3, 3, 1]

    replanned = plan_batches(project_id, task_id, papers[:2], batch_size=3)
    assert replanned[0]["created"] is False
    assert len(storage.list_evidence_batches(project_id, task_id)) == 3


def test_run_pending_batches_extracts_and_resumes(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id, task_id = _project_task()
    papers = _papers(3)
    plan_batches(project_id, task_id, papers, batch_size=2)

    result = run_pending_batches(
        project_id,
        task_id,
        FakeClient(),
        _lookup(papers),
        lambda paper: "full text with quote",
        backoff_seconds=0,
    )
    assert result.completed == 3
    assert result.failed == 0
    assert result.requests == 3
    assert pending_batches(project_id, task_id)["pending"] == 0
    matrix = storage.load_evidence_matrix(project_id)
    assert len(matrix) == 3

    again = run_pending_batches(
        project_id,
        task_id,
        FakeClient(),
        _lookup(papers),
        lambda paper: "full text with quote",
        backoff_seconds=0,
    )
    assert again.requests == 0
    assert again.batches_run == 0


def test_run_retries_transient_errors_with_backoff(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id, task_id = _project_task()
    papers = _papers(1)
    plan_batches(project_id, task_id, papers, batch_size=1)
    sleeps = []

    result = run_pending_batches(
        project_id,
        task_id,
        FakeClient(fail_times=1),
        _lookup(papers),
        lambda paper: "full text",
        retries=1,
        backoff_seconds=0.5,
        sleep=sleeps.append,
    )

    assert result.completed == 1
    assert result.failed == 0
    assert result.requests == 2
    assert sleeps == [0.5]


def test_run_stops_at_request_cap_and_resumes(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id, task_id = _project_task()
    papers = _papers(4)
    plan_batches(project_id, task_id, papers, batch_size=1)

    first = run_pending_batches(
        project_id,
        task_id,
        FakeClient(),
        _lookup(papers),
        lambda paper: "text",
        max_requests=2,
        backoff_seconds=0,
    )
    assert first.paused is True
    assert first.completed == 2
    assert pending_batches(project_id, task_id)["pending"] > 0

    second = run_pending_batches(
        project_id,
        task_id,
        FakeClient(),
        _lookup(papers),
        lambda paper: "text",
        backoff_seconds=0,
    )
    assert second.completed == 2
    assert pending_batches(project_id, task_id)["pending"] == 0


def test_missing_text_is_recorded_and_retryable(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id, task_id = _project_task()
    papers = _papers(1)
    plan_batches(project_id, task_id, papers, batch_size=1)

    result = run_pending_batches(
        project_id,
        task_id,
        FakeClient(),
        _lookup(papers),
        lambda paper: "",
        backoff_seconds=0,
    )
    assert result.failed == 1
    status = storage.evidence_task_status(project_id, task_id)
    assert status["failed"] == ["uid:0"]

    retried = run_pending_batches(
        project_id,
        task_id,
        FakeClient(),
        _lookup(papers),
        lambda paper: "text",
        retry_failed=True,
        backoff_seconds=0,
    )
    assert retried.completed == 1
    assert storage.evidence_task_status(project_id, task_id)["failed"] == []
    versions = storage.list_evidence_versions(project_id, "uid:0", source="ai_raw")
    assert [version["version_no"] for version in versions] == [2, 1]
    assert versions[0]["error"] is None


def test_matrix_exports_include_provenance(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id, task_id = _project_task()
    papers = _papers(1)
    plan_batches(project_id, task_id, papers, batch_size=1)
    run_pending_batches(
        project_id,
        task_id,
        FakeClient(),
        _lookup(papers),
        lambda paper: "full text",
        backoff_seconds=0,
    )

    matrix = storage.load_evidence_matrix(project_id)
    csv_bytes = export_matrix_csv(matrix).decode("utf-8-sig")
    assert "paper_id" in csv_bytes
    assert "task_id" in csv_bytes
    assert "uid:0" in csv_bytes

    markdown = export_matrix_markdown(matrix, task={"id": task_id}).decode("utf-8")
    assert "项目级证据矩阵" in markdown
    assert task_id in markdown
    assert "Paper 0" in markdown
