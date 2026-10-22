import httpx
import pytest

import research_app.service as service


def _http_error(status: int, headers: dict[str, str] | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.example/search")
    response = httpx.Response(status, headers=headers, text="fixture error", request=request)
    return httpx.HTTPStatusError("request failed", request=request, response=response)


class FakeClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.entered = False
        self.exited = False
        self.options = {}

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *_args):
        self.exited = True
        return None

    def __getattr__(self, _action):
        async def call(**_kwargs):
            self.calls += 1
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        return call


def _install_fake(monkeypatch, outcomes):
    client = FakeClient(outcomes)
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    def fake_client(**kwargs):
        client.options = kwargs
        return client

    monkeypatch.setattr(service, "AgentToolsClient", fake_client)
    monkeypatch.setattr(service.asyncio, "sleep", fake_sleep)
    return client, delays


@pytest.mark.parametrize("status", [400, 401, 403])
def test_non_retryable_http_errors_are_not_retried(monkeypatch, status):
    error = _http_error(status)
    client, delays = _install_fake(monkeypatch, [error])

    with pytest.raises(httpx.HTTPStatusError):
        service.call_api("test-token", "search_papers", query="x")

    assert client.calls == 1
    assert delays == []


def test_rate_limit_retries_after_server_delay(monkeypatch):
    client, delays = _install_fake(
        monkeypatch, [_http_error(429, {"Retry-After": "2"}), {"hits": []}]
    )

    result = service.call_api("test-token", "semantic_search", query="x")

    assert result == {"hits": []}
    assert client.calls == 2
    assert delays == [2.0]
    assert client.options == {"token": "test-token", "timeout": 30.0}
    assert client.entered and client.exited


def test_rate_limit_with_long_retry_after_is_not_retried_early(monkeypatch):
    client, delays = _install_fake(monkeypatch, [_http_error(429, {"Retry-After": "30"})])

    with pytest.raises(httpx.HTTPStatusError):
        service.call_api("test-token", "search_papers", query="x")

    assert client.calls == 1
    assert delays == []


def test_transient_server_errors_use_bounded_retries(monkeypatch):
    client, delays = _install_fake(
        monkeypatch,
        [_http_error(503), _http_error(502), _http_error(500)],
    )

    with pytest.raises(httpx.HTTPStatusError):
        service.call_api("test-token", "search_papers", query="x")

    assert client.calls == 3
    assert delays == [0.25, 0.5]


def test_timeout_is_not_retried(monkeypatch):
    client, delays = _install_fake(monkeypatch, [httpx.ReadTimeout("timed out")])

    with pytest.raises(httpx.ReadTimeout):
        service.call_api("test-token", "read_content", doc_id="doc-1")

    assert client.calls == 1
    assert delays == []
