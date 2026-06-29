"""WebSocket 路由（Task 3 / P2）。

提供 ``ws://host/ws/actions/{action_id}?token=JWT`` 端点。
连接建立后订阅 action 状态变更，实时推送 JSON 消息。

安全
----
- JWT token 通过 query parameter 传递（WebSocket 握手不支持自定义 header）
- 验证失败立即关闭连接（code=4001）
- 连接期间持续验证 action 归属（通过 action_id 查询）
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.core.auth import InvalidTokenError, decode_access_token
from app.core.database import get_db_session
from app.models import AgentAction
from app.services.ws_broker import broker

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws/actions/{action_id}")
async def action_ws(websocket: WebSocket, action_id: str, token: str = "") -> None:
    """WebSocket 端点：订阅 action 状态变更。

    协议
    ----
    1. 客户端连接：``ws://host/ws/actions/{action_id}?token=JWT``
    2. 服务端验证 JWT → 验证 action 归属 → accept 连接
    3. 服务端持续推送 JSON 消息：``{"action_id": "...", "state": "...", ...}``
    4. 客户端断开时自动取消订阅
    """
    # ---- JWT 验证 ----
    if not token:
        await websocket.close(code=4001, reason="missing token")
        return

    try:
        token_payload = decode_access_token(token)
        user_id = token_payload.sub
    except (InvalidTokenError, Exception):
        await websocket.close(code=4001, reason="token verification failed")
        return

    # ---- Action 归属验证 ----
    try:
        action_uuid = UUID(action_id)
    except ValueError:
        await websocket.close(code=4002, reason="invalid action_id")
        return

    try:
        session_gen = get_db_session()
        db = next(session_gen)
        try:
            action = db.execute(
                select(AgentAction)
                .where(AgentAction.id == action_uuid)
                .where(AgentAction.user_id == UUID(user_id))
            ).scalar_one_or_none()

            if action is None:
                await websocket.close(code=4002, reason="action not found")
                return
        finally:
            with contextlib.suppress(StopIteration):
                next(session_gen)
    except Exception:
        logger.exception("WS action lookup failed")
        await websocket.close(code=4500, reason="internal error")
        return

    # ---- 接受连接并订阅 ----
    await websocket.accept()
    queue = broker.subscribe(action_id)

    try:
        # 发送初始状态
        await websocket.send_json({
            "type": "connected",
            "action_id": action_id,
            "state": str(action.state),
        })

        # 持续等待消息并推送
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=30.0)
                await websocket.send_text(message)
            except TimeoutError:
                # 心跳：每 30s 发送 ping 保持连接
                await websocket.send_json({"type": "ping"})
    except WebSocketDisconnect:
        logger.debug("WS client disconnected: action_id=%s", action_id)
    except Exception:
        logger.exception("WS error: action_id=%s", action_id)
    finally:
        broker.unsubscribe(action_id, queue)
