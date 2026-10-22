import httpx
import pytest

from research_app.errors import describe_error


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, "检索条件有误"),
        (401, "Token 无效"),
        (403, "没有权限"),
        (429, "限流"),
        (500, "暂时不可用"),
        (502, "暂时不可用"),
        (503, "暂时不可用"),
        (504, "暂时不可用"),
    ],
)
def test_http_errors_have_user_friendly_messages(status, expected):
    request = httpx.Request("POST", "https://api.example/search")
    response = httpx.Response(status, text="fixture error", request=request)
    error = httpx.HTTPStatusError("request failed", request=request, response=response)

    message, details = describe_error(error)

    assert expected in message
    assert details == "fixture error"


def test_rate_limit_message_includes_retry_after():
    request = httpx.Request("POST", "https://api.example/search")
    response = httpx.Response(429, headers={"Retry-After": "3"}, request=request)
    error = httpx.HTTPStatusError("rate limited", request=request, response=response)

    message, _ = describe_error(error)

    assert "3" in message


def test_timeout_and_transport_errors_have_safe_messages():
    timeout_message, timeout_details = describe_error(httpx.ReadTimeout("slow"))
    network_message, network_details = describe_error(httpx.ConnectError("offline"))

    assert "超时" in timeout_message
    assert timeout_details is None
    assert "网络连接" in network_message
    assert network_details is None


def test_unexpected_error_does_not_expose_exception_text():
    message, details = describe_error(RuntimeError("secret-token-value"))

    assert "secret-token-value" not in message
    assert details is None


def test_validation_error_message_is_shown_to_user():
    message, details = describe_error(ValueError("Token 包含控制字符，请重新复制。"))

    assert "Token 包含控制字符" in message
    assert details is None


def test_http_error_details_redact_bearer_token():
    request = httpx.Request(
        "POST",
        "https://api.example/search",
        headers={"Authorization": "Bearer private-token"},
    )
    response = httpx.Response(400, text="bad request: private-token", request=request)
    error = httpx.HTTPStatusError("request failed", request=request, response=response)

    _, details = describe_error(error)

    assert details == "bad request: [REDACTED]"
    assert "private-token" not in details
