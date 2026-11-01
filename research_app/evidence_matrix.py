from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from research_app import storage
from research_app.ai import EVIDENCE_PROMPT_VERSION, extract_evidence


DEFAULT_BATCH_SIZE = 5
DEFAULT_MAX_PAPERS = 200
DEFAULT_MAX_REQUESTS = 60
MAX_RETRIES = 2
BACKOFF_SECONDS = 2.0
TRANSIENT_MARKERS = (
    "HTTP 429",
    "HTTP 500",
    "HTTP 502",
    "HTTP 503",
    "HTTP 504",
    "无法连接",
    "timed out",
    "timeout",
)


@dataclass
class BatchRunResult:
    batches_run: int = 0
    requests: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    paused: bool = False
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "batches_run": self.batches_run,
            "requests": self.requests,
            "completed": self.completed,
            "failed": self.failed,
            "skipped": self.skipped,
            "paused": self.paused,
            "errors": list(self.errors),
        }


def unique_papers(
    papers: list[dict[str, Any]],
    *,
    max_papers: int = DEFAULT_MAX_PAPERS,
) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for paper in papers:
        paper_id = str(paper.get("paper_id") or "")
        if paper_id and paper_id not in unique:
            unique[paper_id] = paper
    if len(unique) > int(max_papers):
        raise ValueError(f"单次最多规划 {int(max_papers)} 篇文献。")
    return list(unique.values())


def plan_batches(
    project_id: str,
    task_id: str,
    papers: list[dict[str, Any]],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_papers: int = DEFAULT_MAX_PAPERS,
) -> list[dict[str, Any]]:
    batch_size = int(batch_size)
    if batch_size < 1:
        raise ValueError("批次大小至少为 1。")
    ordered = unique_papers(papers, max_papers=max_papers)
    if not ordered:
        raise ValueError("没有可提取证据的候选文献。")
    planned = []
    for index in range(0, len(ordered), batch_size):
        chunk = ordered[index : index + batch_size]
        created = storage.create_evidence_batch(
            project_id,
            task_id,
            index // batch_size,
            paper_keys=[str(paper["paper_id"]) for paper in chunk],
        )
        planned.append(
            {
                "batch_id": created["id"],
                "created": created["created"],
                "papers": chunk,
            }
        )
    return planned


def _is_transient(error_text: str) -> bool:
    return any(marker in error_text for marker in TRANSIENT_MARKERS)


def run_pending_batches(
    project_id: str,
    task_id: str,
    client: Any,
    paper_lookup: dict[str, dict[str, Any]],
    text_provider: Callable[[dict[str, Any]], str],
    *,
    retry_failed: bool = False,
    max_batches: int | None = None,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    retries: int = MAX_RETRIES,
    backoff_seconds: float = BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> BatchRunResult:
    model = str(getattr(client, "model", "") or "")
    status = storage.evidence_task_status(project_id, task_id)
    completed_papers = set(status["completed"])
    failed_papers = set(status["failed"])
    batches = storage.list_evidence_batches(project_id, task_id)
    if retry_failed:
        candidates = [
            b for b in batches if b["status"] in {"failed", "completed", "running"}
        ]
    else:
        candidates = [b for b in batches if b["status"] in {"pending", "running"}]

    result = BatchRunResult()
    for batch in candidates:
        if max_batches is not None and result.batches_run >= int(max_batches):
            result.paused = True
            break
        if result.requests >= int(max_requests):
            result.paused = True
            break

        todo = []
        for paper_key in batch["paper_keys"]:
            if retry_failed:
                if paper_key in failed_papers:
                    todo.append(paper_key)
            elif paper_key not in completed_papers and paper_key not in failed_papers:
                todo.append(paper_key)

        if not todo:
            if batch["status"] != "completed":
                storage.update_evidence_batch(batch["id"], status="completed")
            continue

        storage.update_evidence_batch(batch["id"], status="running")
        batch_requests = 0
        batch_completed = 0
        batch_failed = 0
        processed = 0
        interrupted = False

        for paper_key in todo:
            if result.requests >= int(max_requests):
                result.paused = True
                interrupted = True
                break
            paper = paper_lookup.get(paper_key) or {}
            title = str(paper.get("title") or "")
            year = paper.get("year")
            try:
                text = str(text_provider(paper) or "")
            except Exception as error:  # noqa: BLE001
                text = ""
                result.errors.append(f"{paper_key}：读取文本失败（{error}）")
            if not text:
                storage.append_evidence_version(
                    project_id,
                    paper_key,
                    task_id,
                    source="ai_raw",
                    data={},
                    model=model,
                    prompt_version=EVIDENCE_PROMPT_VERSION,
                    title=title,
                    year=year,
                    batch_id=batch["id"],
                    error="没有可用文本",
                )
                failed_papers.add(paper_key)
                batch_failed += 1
                processed += 1
                continue

            document = {
                "paper_id": paper_key,
                "title": title,
                "year": year or "",
                "text": text[:6000],
            }
            item: dict[str, Any] = {"error": "未执行", "data": None}
            for attempt in range(int(retries) + 1):
                if result.requests >= int(max_requests):
                    result.paused = True
                    interrupted = True
                    break
                result.requests += 1
                batch_requests += 1
                try:
                    item = extract_evidence(client, [document])[0]
                except Exception as error:  # noqa: BLE001
                    item = {
                        "paper_id": paper_key,
                        "title": title,
                        "year": year,
                        "data": None,
                        "error": str(error),
                        "quote_checks": [],
                    }
                if not item.get("error"):
                    break
                error_text = str(item.get("error") or "")
                if attempt < int(retries) and _is_transient(error_text):
                    if backoff_seconds:
                        sleep(float(backoff_seconds) * (2**attempt))
                    continue
                break

            if interrupted and item.get("error") == "未执行":
                break

            storage.append_evidence_version(
                project_id,
                paper_key,
                task_id,
                source="ai_raw",
                data=item.get("data") or {},
                quote_checks=item.get("quote_checks") or [],
                model=model,
                prompt_version=EVIDENCE_PROMPT_VERSION,
                title=title,
                year=year,
                batch_id=batch["id"],
                error=item.get("error"),
            )
            processed += 1
            if item.get("error"):
                failed_papers.add(paper_key)
                completed_papers.discard(paper_key)
                batch_failed += 1
                result.errors.append(f"{paper_key}：{item.get('error')}")
            else:
                completed_papers.add(paper_key)
                failed_papers.discard(paper_key)
                batch_completed += 1

        result.batches_run += 1
        result.completed += batch_completed
        result.failed += batch_failed
        if interrupted or processed < len(todo):
            new_status = "pending"
        elif batch_completed == 0 and batch_failed > 0:
            new_status = "failed"
        elif processed == 0:
            new_status = "skipped"
        else:
            new_status = "completed"
        storage.update_evidence_batch(
            batch["id"],
            status=new_status,
            request_count=int(batch.get("request_count") or 0) + batch_requests,
        )
        if result.paused:
            break

    return result


def pending_batches(
    project_id: str,
    task_id: str,
) -> dict[str, int]:
    batches = storage.list_evidence_batches(project_id, task_id)
    counts = {"pending": 0, "running": 0, "completed": 0, "failed": 0, "skipped": 0}
    for batch in batches:
        status = batch["status"]
        counts[status] = counts.get(status, 0) + 1
    return counts
