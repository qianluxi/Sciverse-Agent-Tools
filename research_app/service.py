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
