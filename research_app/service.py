from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from sciverse import AgentToolsClient


_MAX_RETRIES = 2
_MAX_AUTOMATIC_RETRY_AFTER = 5.0
_REQUEST_TIMEOUT_SECONDS = 30.0


def _retry_delay(error: httpx.HTTPStatusError, attempt: int) -> float | None:
    status = error.response.status_code
    if status == 429:
        retry_after = error.response.headers.get("retry-after")
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    delay = 1.0 * (2**attempt)
            delay = max(0.0, delay)
            return delay if delay <= _MAX_AUTOMATIC_RETRY_AFTER else None
        return 1.0 * (2**attempt)
    if status in {500, 502, 503, 504}:
        return 0.25 * (2**attempt)
    return None


async def _call_api(token: str, action: str, kwargs: dict[str, Any]) -> Any:
    async with AgentToolsClient(token=token, timeout=_REQUEST_TIMEOUT_SECONDS) as client:
        method = getattr(client, action)
        for attempt in range(_MAX_RETRIES + 1):
            try:
                return await method(**kwargs)
            except httpx.HTTPStatusError as error:
                if attempt == _MAX_RETRIES:
                    raise
                delay = _retry_delay(error, attempt)
                if delay is None:
                    raise
                await asyncio.sleep(delay)


def call_api(token: str, action: str, **kwargs: Any) -> Any:
    """Run one SDK action; keep its async client within this event-loop lifetime."""
    return asyncio.run(_call_api(token, action, kwargs))


def enrich_semantic_hits(
    token: str,
    hits: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    """Attach paper metadata to semantic chunks via their doc_id.

    Semantic search returns chunks with doc_id but no unique_id. The metadata
    endpoint can batch-filter by doc_id, so this adds unique_id/doi/year/venue
    without changing the chunk identity, score, or offset.
    """
    doc_ids = list(dict.fromkeys(str(hit["doc_id"]) for hit in hits if hit.get("doc_id")))
    metadata: dict[str, dict[str, Any]] = {}
    requests = 0
    for start in range(0, len(doc_ids), 50):
        chunk = doc_ids[start : start + 50]
        requests += 1
        result = call_api(
            token,
            "search_papers",
            filters_advanced=[{"field": "doc_id", "operator": "FILTER_OP_IN", "value": chunk}],
            page=1,
            page_size=50,
        )
        for paper in result.get("results", []):
            doc_id = paper.get("doc_id")
            if doc_id:
                metadata[str(doc_id)] = paper

    enriched: list[dict[str, Any]] = []
    matched = 0
    for hit in hits:
        merged = dict(hit)
        paper = metadata.get(str(hit.get("doc_id") or ""))
        if paper:
            matched += 1
            for field in (
                "unique_id",
                "doi",
                "publication_published_year",
                "publication_venue_name_unified",
                "publication_venue_type",
                "citation_count",
                "is_content_accessible",
                "metadata_type",
            ):
                if paper.get(field) is not None:
                    merged[field] = paper[field]
            if paper.get("title") and not merged.get("title"):
                merged["title"] = paper["title"]
        enriched.append(merged)
    return enriched, requests, matched
