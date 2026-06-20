"""轻量级内存 Rate Limiter 中间件。

基于滑动窗口计数器算法，按 IP 限流。不引入额外依赖（如 slowapi），
适合 hackathon MVP 与单实例部署。多实例部署时应换用 Redis-backed limiter。

用法::

    from app.core.rate_limiter import RateLimiterMiddleware

    app.add_middleware(RateLimiterMiddleware, default_rpm=100)

也可通过 ``RateLimiterMiddleware.SENSITIVE_PATHS`` 对敏感路径设置更严格的限额。
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


@dataclass
class _Window:
    """单个 key 的滑动窗口计数器。"""

    timestamps: list[float] = field(default_factory=list)

    def count(self, now: float, window_seconds: float) -> int:
        """返回窗口内的请求数，同时清理过期记录。"""
        cutoff = now - window_seconds
        self.timestamps = [t for t in self.timestamps if t > cutoff]
        return len(self.timestamps)

    def add(self, now: float) -> None:
        self.timestamps.append(now)


class RateLimiterMiddleware(BaseHTTPMiddleware):
    """基于 IP 的内存限流中间件。

    - 全局默认：``default_rpm`` 请求/分钟 per IP
    - 敏感路径（登录、审批）：``sensitive_rpm`` 请求/分钟 per IP
    """

    SENSITIVE_PATHS: set[str] = {
        "/api/auth/demo-login",
    }

    SENSITIVE_PATH_PREFIXES: tuple[str, ...] = (
        "/api/actions/",  # 审批路径后缀 /approve
    )

    def __init__(
        self,
        app,  # noqa: ANN001 — Starlette middleware 签名
        default_rpm: int = 100,
        sensitive_rpm: int = 10,
        window_seconds: float = 60.0,
    ) -> None:
        super().__init__(app)
        self.default_rpm = default_rpm
        self.sensitive_rpm = sensitive_rpm
        self.window_seconds = window_seconds
        self._windows: dict[str, _Window] = defaultdict(_Window)

    def _get_client_ip(self, request: Request) -> str:
        """提取客户端 IP，优先 X-Forwarded-For（反向代理后）。"""
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def _is_sensitive(self, path: str) -> bool:
        if path in self.SENSITIVE_PATHS:
            return True
        # 匹配 /api/actions/{id}/approve
        if path.endswith("/approve"):
            for prefix in self.SENSITIVE_PATH_PREFIXES:
                if path.startswith(prefix):
                    return True
        return False

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # 健康检查不限流
        if request.url.path == "/health":
            return await call_next(request)

        ip = self._get_client_ip(request)
        is_sensitive = self._is_sensitive(request.url.path)
        limit = self.sensitive_rpm if is_sensitive else self.default_rpm
        key = f"{ip}:{'sensitive' if is_sensitive else 'default'}"

        now = time.monotonic()
        window = self._windows[key]
        count = window.count(now, self.window_seconds)

        if count >= limit:
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "RATE_LIMIT_EXCEEDED",
                        "message": f"Too many requests. Limit: {limit}/min.",
                        "status": 429,
                    }
                },
            )

        window.add(now)
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(max(0, limit - count - 1))
        return response
