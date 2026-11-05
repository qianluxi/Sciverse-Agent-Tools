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
        self.messages: list = []

    def chat(self, messages, max_tokens=2048):
        self.calls += 1
        self.messages.append(messages)
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


def _entry(paper, *, text="full text", source="full_text", error=None):
    return {
        "paper_id": paper["paper_id"],
        "candidate_key": paper["paper_id"],
        "title": paper["title"],
        "year": paper.get("year"),
        "input_source": source,
        "input_text": text if source != "none" else "",
        "input_error": error,
    }


def _freeze(project_id, task_id, entries):
    storage.save_evidence_task_inputs(project_id, task_id, entries)


def test_plan_batches_chunks_and_is_idempotent(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id, task_id = _project_task()
    papers = _papers(7)

    planned = plan_batches(project_id, task_id, papers, batch_size=3)
    assert [len(item["papers"]) for item in planned] == [3, 3, 1]
    batches = storage.list_evidence_batches(project_id, task_id)
    assert [len(batch["paper_keys"]) for batch in batches] == [3, 3, 1]

    replanned = plan_batches(project_id, task_id, papers[:3], batch_size=3)
    assert replanned[0]["created"] is False
    assert len(storage.list_evidence_batches(project_id, task_id)) == 3


def test_plan_batches_rejects_changed_paper_list(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id, task_id = _project_task()
    papers = _papers(3)
    plan_batches(project_id, task_id, papers, batch_size=3)

    try:
        plan_batches(project_id, task_id, papers[:2], batch_size=3)
    except ValueError:
        pass
    else:
        raise AssertionError("changed paper list should be rejected")


def test_run_uses_frozen_input_and_resumes(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id, task_id = _project_task()
    papers = _papers(3)
    _freeze(project_id, task_id, [_entry(paper, text="FROZEN-SOURCE") for paper in papers])
    plan_batches(project_id, task_id, papers, batch_size=2)

    client = FakeClient()
    result = run_pending_batches(
        project_id, task_id, client, backoff_seconds=0
    )
    assert result.completed == 3
    assert result.failed == 0
    assert result.requests == 3
    assert "FROZEN-SOURCE" in str(client.messages[0])
    assert pending_batches(project_id, task_id)["pending"] == 0
    assert len(storage.load_evidence_matrix(project_id)) == 3

    again = run_pending_batches(
        project_id, task_id, FakeClient(), backoff_seconds=0
    )
    assert again.requests == 0
    assert again.batches_run == 0


def test_run_retries_transient_errors_with_backoff(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id, task_id = _project_task()
    papers = _papers(1)
    _freeze(project_id, task_id, [_entry(papers[0])])
    plan_batches(project_id, task_id, papers, batch_size=1)
    sleeps = []

    result = run_pending_batches(
        project_id,
        task_id,
        FakeClient(fail_times=1),
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
    _freeze(project_id, task_id, [_entry(paper) for paper in papers])
    plan_batches(project_id, task_id, papers, batch_size=1)

    first = run_pending_batches(
        project_id,
        task_id,
        FakeClient(),
        max_requests=2,
        backoff_seconds=0,
    )
    assert first.paused is True
    assert first.completed == 2
    assert pending_batches(project_id, task_id)["pending"] > 0

    second = run_pending_batches(
        project_id, task_id, FakeClient(), backoff_seconds=0
    )
    assert second.completed == 2
    assert pending_batches(project_id, task_id)["pending"] == 0


class AlwaysFailClient:
    model = "test-model"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages, max_tokens=2048):
        self.calls += 1
        raise AIError("server error")


def test_retry_covers_failed_items_in_pending_batch(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id, task_id = _project_task()
    papers = _papers(2)
    _freeze(project_id, task_id, [_entry(paper) for paper in papers])
    plan_batches(project_id, task_id, papers, batch_size=2)

    first = run_pending_batches(
        project_id,
        task_id,
        AlwaysFailClient(),
        retries=0,
        max_requests=1,
        backoff_seconds=0,
    )
    assert first.failed == 1
    assert pending_batches(project_id, task_id)["pending"] == 1
    status = storage.evidence_task_status(project_id, task_id)
    assert status["failed"] == [papers[0]["paper_id"]]

    retried = run_pending_batches(
        project_id,
        task_id,
        FakeClient(),
        retry_failed=True,
        backoff_seconds=0,
    )
    assert retried.completed == 1
    assert storage.evidence_task_status(project_id, task_id)["failed"] == []
    assert pending_batches(project_id, task_id)["pending"] == 1

    resumed = run_pending_batches(
        project_id, task_id, FakeClient(), backoff_seconds=0
    )
    assert resumed.completed == 1
    assert pending_batches(project_id, task_id)["pending"] == 0
    assert len(storage.evidence_task_status(project_id, task_id)["completed"]) == 2


def test_batch_error_summary_survives_partial_resume(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id, task_id = _project_task()
    papers = _papers(2)
    _freeze(project_id, task_id, [_entry(paper) for paper in papers])
    plan_batches(project_id, task_id, papers, batch_size=2)

    first = run_pending_batches(
        project_id,
        task_id,
        AlwaysFailClient(),
        retries=0,
        max_requests=1,
        backoff_seconds=0,
    )
    assert first.failed == 1
    batch = storage.list_evidence_batches(project_id, task_id)[0]
    assert "server error" in (batch["error"] or "")

    second = run_pending_batches(
        project_id, task_id, FakeClient(), backoff_seconds=0
    )
    assert second.completed == 1

    batch = storage.list_evidence_batches(project_id, task_id)[0]
    assert batch["status"] == "completed"
    assert batch["completed_count"] == 1
    assert batch["failed_count"] == 1
    assert "server error" in (batch["error"] or "")
    assert storage.evidence_task_status(project_id, task_id)["failed"] == [
        papers[0]["paper_id"]
    ]


def test_partial_failure_records_counts_and_error(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id, task_id = _project_task()
    papers = _papers(2)
    _freeze(
        project_id,
        task_id,
        [
            _entry(papers[0]),
            _entry(papers[1], source="none", error="没有可用文本"),
        ],
    )
    plan_batches(project_id, task_id, papers, batch_size=2)

    result = run_pending_batches(
        project_id, task_id, FakeClient(), backoff_seconds=0
    )
    assert result.completed == 1
    assert result.failed == 1

    batch = storage.list_evidence_batches(project_id, task_id)[0]
    assert batch["status"] == "completed"
    assert batch["completed_count"] == 1
    assert batch["failed_count"] == 1
    assert "没有可用文本" in (batch["error"] or "")
    assert storage.evidence_task_status(project_id, task_id)["failed"] == [
        papers[1]["paper_id"]
    ]


def test_matrix_exports_include_batch_and_version_provenance(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id, task_id = _project_task()
    papers = _papers(1)
    _freeze(project_id, task_id, [_entry(papers[0])])
    plan_batches(project_id, task_id, papers, batch_size=1)
    run_pending_batches(
        project_id, task_id, FakeClient(), backoff_seconds=0
    )

    batch_id = storage.list_evidence_batches(project_id, task_id)[0]["id"]
    matrix = storage.load_evidence_matrix(project_id)
    csv_text = export_matrix_csv(matrix).decode("utf-8-sig")
    assert "batch_id" in csv_text
    assert "ai_batch_id" in csv_text
    assert "input_source" in csv_text
    assert str(batch_id) in csv_text
    assert task_id in csv_text

    markdown = export_matrix_markdown(matrix, task={"id": task_id}).decode("utf-8")
    assert "项目级证据矩阵" in markdown
    assert task_id in markdown
    assert str(batch_id) in markdown
    assert "全文" in markdown
