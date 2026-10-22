from __future__ import annotations

import httpx


def _safe_response_text(response: httpx.Response) -> str:
    text = response.text[:2000]
    authorization = response.request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token:
            text = text.replace(token, "[REDACTED]")
        text = text.replace(authorization, "Bearer [REDACTED]")
    return text


def describe_error(error: Exception) -> tuple[str, str | None]:
    if isinstance(error, httpx.HTTPStatusError):
        response = error.response
        status = response.status_code
        messages = {
            400: "检索条件有误，请检查关键词或筛选条件。",
            401: "Token 无效或已过期，请检查 Sciverse API Token。",
            403: "当前账号没有权限访问该字段或文献资源，请检查筛选条件和账号权限。",
            429: "请求过于频繁，已达到服务限流。请稍后重试。",
        }
        if status >= 500:
            message = "Sciverse 服务暂时不可用，请稍后重试。"
        else:
            message = messages.get(status, f"服务请求失败（HTTP {status}）。")
        retry_after = response.headers.get("retry-after")
        if status == 429 and retry_after:
            message = f"{message} 服务端建议等待：{retry_after}。"
        return message, _safe_response_text(response)
    if isinstance(error, httpx.TimeoutException):
        return "请求超时，请检查网络连接后重试。", None
    if isinstance(error, httpx.TransportError):
        return "无法连接 Sciverse 服务，请检查网络连接。", None
    return f"请求失败（{type(error).__name__}），请稍后重试。", None
