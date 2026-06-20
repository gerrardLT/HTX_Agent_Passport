"""B.AI HTTP 客户端单元测试（任务 10 / Req 5 / Req 14 / Req 15）。

仅覆盖 :class:`BAIClient` 自身的 HTTP 协议层翻译——把 ``httpx`` 行为
转成业务异常 + :class:`BAIResponse`。Planner 适配器层的逻辑（重试 /
降级 / 审计）由 ``test_planner.py`` 用 stub 客户端覆盖，避免每条测试
都启动 :class:`httpx.MockTransport`。

测试策略
--------
用 :class:`httpx.MockTransport` 注入响应——这是 ``httpx`` 官方推荐的
单元测试方式，不依赖网络、不需要真实端点。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.services.bai_client import (
    DEFAULT_MODEL,
    BAIClient,
    BAIError,
    BAIResponse,
    BAIServiceUnavailableError,
    BAITimeoutError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _ok_body(content: str = '{"version":"0.1"}') -> dict:
    return {
        "id": "chatcmpl-test",
        "model": "planner-v1",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }


def _make_client(handler) -> BAIClient:
    """构造一个走 MockTransport 的 BAIClient。"""
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    return BAIClient(
        api_url="https://stub.b.ai/v1",
        api_key="test-api-key",
        http_client=http,
    )


# ---------------------------------------------------------------------------
# 1. 成功路径
# ---------------------------------------------------------------------------
class TestBAIClientSuccess:
    """正常 200 响应解析。"""

    def test_chat_returns_response_with_content_and_tokens(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ok_body('{"version":"0.1"}'))

        with _make_client(handler) as client:
            response = client.chat("system", "user", timeout=5.0)

        assert isinstance(response, BAIResponse)
        assert response.content == '{"version":"0.1"}'
        assert response.input_tokens == 100
        assert response.output_tokens == 50
        assert response.latency_ms >= 0

    def test_chat_passes_messages_correctly(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, json=_ok_body())

        with _make_client(handler) as client:
            client.chat("SYSTEM_MSG", "USER_MSG", timeout=5.0)

        body = captured["body"]
        assert body["model"] == DEFAULT_MODEL
        assert body["messages"][0] == {"role": "system", "content": "SYSTEM_MSG"}
        assert body["messages"][1] == {"role": "user", "content": "USER_MSG"}
        assert body["temperature"] == 0
        # Bearer token 注入到 Authorization header
        assert captured["headers"]["authorization"] == "Bearer test-api-key"

    def test_chat_omits_authorization_when_no_api_key(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, json=_ok_body())

        transport = httpx.MockTransport(handler)
        http = httpx.Client(transport=transport)
        client = BAIClient(api_url="https://stub.b.ai/v1", api_key="", http_client=http)
        try:
            client.chat("s", "u", timeout=5.0)
        finally:
            client.close()

        # 无 token 时不附加 authorization header
        assert "authorization" not in captured["headers"]

    def test_chat_handles_missing_usage_field(self) -> None:
        """B.AI 没回填 usage → input_tokens / output_tokens 为 None。"""
        body = _ok_body()
        del body["usage"]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        with _make_client(handler) as client:
            response = client.chat("s", "u", timeout=5.0)

        assert response.content == '{"version":"0.1"}'
        assert response.input_tokens is None
        assert response.output_tokens is None


# ---------------------------------------------------------------------------
# 2. 超时（Req 5 AC4 触发器）
# ---------------------------------------------------------------------------
class TestBAIClientTimeout:
    """**Validates: Requirements 5**（AC4：超时翻译为 BAITimeoutError）。"""

    def test_timeout_raises_bai_timeout_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("simulated read timeout")

        with _make_client(handler) as client:
            with pytest.raises(BAITimeoutError) as exc_info:
                client.chat("s", "u", timeout=1.0)

        # 异常消息含 timeout 字样但不含 api_key
        msg = str(exc_info.value)
        assert "timed out" in msg.lower()
        assert "test-api-key" not in msg


# ---------------------------------------------------------------------------
# 3. 服务不可用（Req 14 AC5）
# ---------------------------------------------------------------------------
class TestBAIClientServiceUnavailable:
    """**Validates: Requirements 14**（AC5：429/503 → MODEL_UNAVAILABLE）。"""

    @pytest.mark.parametrize("status", [429, 502, 503, 504])
    def test_known_unavailable_statuses_raise(self, status: int) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, text="upstream error")

        with _make_client(handler) as client:
            with pytest.raises(BAIServiceUnavailableError):
                client.chat("s", "u", timeout=5.0)

    def test_500_treated_as_service_unavailable(self) -> None:
        """其它 5xx（500/501）也归入 service_unavailable，与 Req 14 AC5 协同。"""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="internal error")

        with _make_client(handler) as client:
            with pytest.raises(BAIServiceUnavailableError):
                client.chat("s", "u", timeout=5.0)

    def test_connect_error_raises_service_unavailable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        with _make_client(handler) as client:
            with pytest.raises(BAIServiceUnavailableError):
                client.chat("s", "u", timeout=5.0)


# ---------------------------------------------------------------------------
# 4. 响应格式错误
# ---------------------------------------------------------------------------
class TestBAIClientMalformedResponse:
    """格式错误的响应抛 :class:`BAIError`（非 timeout / 非 unavailable）。"""

    def test_non_json_body_raises_bai_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="not a json {{{")

        with _make_client(handler) as client:
            with pytest.raises(BAIError):
                client.chat("s", "u", timeout=5.0)

    def test_missing_choices_raises_bai_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": "test"})

        with _make_client(handler) as client:
            with pytest.raises(BAIError):
                client.chat("s", "u", timeout=5.0)

    def test_non_string_content_raises_bai_error(self) -> None:
        body = _ok_body()
        body["choices"][0]["message"]["content"] = {"oops": "not a string"}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        with _make_client(handler) as client:
            with pytest.raises(BAIError):
                client.chat("s", "u", timeout=5.0)

    def test_400_raises_bai_error_not_unavailable(self) -> None:
        """4xx 非 429 → :class:`BAIError`（不重试也不视作不可用）。"""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "bad request"})

        with _make_client(handler) as client:
            with pytest.raises(BAIError) as exc_info:
                client.chat("s", "u", timeout=5.0)
            assert not isinstance(exc_info.value, BAIServiceUnavailableError)


# ---------------------------------------------------------------------------
# 5. 安全：__repr__ 不泄露 api_key
# ---------------------------------------------------------------------------
class TestBAIClientSecureRepr:
    """**Validates: Requirements 15**（AC1：API 密钥绝不出现在日志输出）。"""

    def test_repr_does_not_contain_api_key(self) -> None:
        client = BAIClient(api_url="https://x.b.ai/v1", api_key="SECRET-API-KEY-XYZ")
        repr_str = repr(client)
        assert "SECRET-API-KEY-XYZ" not in repr_str
        assert "Bearer" not in repr_str
        client.close()

    def test_str_does_not_contain_api_key(self) -> None:
        client = BAIClient(api_url="https://x.b.ai/v1", api_key="ANOTHER-SECRET")
        assert "ANOTHER-SECRET" not in str(client)
        client.close()
