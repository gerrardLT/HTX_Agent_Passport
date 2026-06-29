"""HTX Agent Passport 后端入口。

启动方式：
    uvicorn main:app --reload --host 0.0.0.0 --port 8000

业务实现位于 ``app/`` 包内（见各子目录占位 __init__.py），
本文件仅负责实例化 FastAPI、挂载路由、注册中间件 / 异常处理器。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.rate_limiter import RateLimiterMiddleware
from app.routers import actions as actions_router
from app.routers import approvals as approvals_router
from app.routers import audit as audit_router
from app.routers import auth as auth_router
from app.routers import credentials as credentials_router
from app.routers import demo as demo_router
from app.routers import passports as passports_router
from app.routers import tts as tts_router
from app.routers import ws as ws_router
from app.services.audit_sth_scheduler import build_default_scheduler

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan：启动时拉起 STH scheduler，关停时优雅停止。

    - 把 scheduler 实例挂在 ``app.state.sth_scheduler``，便于测试用例 reach in
      或运行期诊断。
    - ``settings.AUDIT_STH_ENABLED=False`` 时跳过启动（CI / 单元测试默认走这条）。
    - 启动 / 停止异常被吞掉并写日志——后台任务的故障**不**应该让 web 层无法
      启动或无法正常退出。
    """
    settings = get_settings()
    scheduler = None
    if settings.AUDIT_STH_ENABLED:
        try:
            scheduler = build_default_scheduler()
            await scheduler.start()
            app.state.sth_scheduler = scheduler
            logger.info("STH scheduler attached to app.state.sth_scheduler")
        except Exception:  # noqa: BLE001
            logger.exception("failed to start STH scheduler; continuing without it")
            scheduler = None
            app.state.sth_scheduler = None
    else:
        app.state.sth_scheduler = None
        logger.info("STH scheduler disabled by AUDIT_STH_ENABLED=False")

    try:
        yield
    finally:
        if scheduler is not None:
            try:
                await scheduler.stop()
            except Exception:  # noqa: BLE001
                logger.exception("failed to stop STH scheduler cleanly")


def create_app() -> FastAPI:
    """工厂函数：便于测试用例独立构建 app。"""
    app = FastAPI(
        title="HTX Agent Passport API",
        description="权限、风险、审计的代理控制平面（HTX Genesis Hackathon 2026）",
        version="0.1.0",
        lifespan=lifespan,
    )

    # ---- Rate Limiting 中间件（Task 4.1）----
    app.add_middleware(RateLimiterMiddleware, default_rpm=100, sensitive_rpm=10)

    # CORS 白名单（Task 4.2）
    settings = get_settings()
    cors_origins = settings.CORS_ORIGINS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    # 安全响应头中间件（Task 4.4）
    @app.middleware("http")
    async def add_security_headers(request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if not settings.DEMO_MODE:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response

    # 统一错误响应（design.md「统一错误响应」 / Req 1 AC7）
    register_exception_handlers(app)

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        """健康检查端点，供 docker-compose / 监控使用。"""
        return {"status": "ok", "service": "htx-agent-passport-backend"}

    # ---- 路由挂载 ----
    app.include_router(auth_router.router, prefix="/api/auth", tags=["auth"])
    app.include_router(
        credentials_router.router, prefix="/api/credentials", tags=["credentials"]
    )
    app.include_router(
        passports_router.router, prefix="/api/passports", tags=["passports"]
    )

    # 预设场景路由（任务 19 / Req 25）
    app.include_router(demo_router.router, tags=["scenarios"])

    # 审批路由（任务 11 / Req 8）
    app.include_router(
        approvals_router.router, prefix="/api", tags=["approvals"]
    )

    # Action 详情与审计时间线（Phase 1 G10/G11 跟进 — 补全前端轮询 + 审计重放
    # 所需的 GET /api/actions/{id} 与 /api/actions/{id}/audit 两个端点）
    app.include_router(
        actions_router.router, prefix="/api", tags=["actions"]
    )

    # 审计 / STH / Inclusion Proof 路由（Phase 1 G10/G11 跟进）
    app.include_router(
        audit_router.router, prefix="/api/audit", tags=["audit"]
    )

    # WebSocket 路由（Task 3 / P2 — 实时推送）
    app.include_router(ws_router.router, tags=["websocket"])

    # TTS 语音合成代理（火山引擎豆包语音，产品演示视频配音）
    app.include_router(tts_router.router, prefix="/api/tts", tags=["tts"])

    # 静态文件：产品演示 HTML 页面
    demos_dir = Path(__file__).resolve().parent.parent / "design-demos"
    if demos_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(demos_dir), html=True), name="static")

    return app


app = create_app()
