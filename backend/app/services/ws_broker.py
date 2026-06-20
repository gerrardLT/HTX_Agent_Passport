"""WebSocket 实时推送 broker（Task 3 / P2）。

为 Action 状态变更提供 WebSocket 实时通知，替换前端 2s 轮询。

架构
----
- ``WSBroker``：单例管理所有 action 的订阅者（``asyncio.Queue``）
- ``publish_action_update()``：状态变更后调用，分发给所有订阅者
- WebSocket 端点验证 JWT 后接受连接，订阅 action 状态变更

用法::

    from app.services.ws_broker import broker, publish_action_update

    # 在 execution_gateway / approval_service 的状态变更后：
    await publish_action_update(action_id, {"state": "EXECUTING", ...})
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from typing import Any

logger = logging.getLogger(__name__)


class WSBroker:
    """WebSocket pub/sub broker。

    管理 ``{action_id: [asyncio.Queue]}`` 映射。
    每个 WebSocket 连接创建一个 Queue 并注册到对应 action_id。
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)

    def subscribe(self, action_id: str) -> asyncio.Queue:
        """创建一个新的订阅 Queue 并注册到 action_id。"""
        queue: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._subscribers[action_id].append(queue)
        logger.debug(
            "WS subscribe: action_id=%s, total_subscribers=%d",
            action_id,
            len(self._subscribers[action_id]),
        )
        return queue

    def unsubscribe(self, action_id: str, queue: asyncio.Queue) -> None:
        """移除订阅。"""
        subs = self._subscribers.get(action_id, [])
        if queue in subs:
            subs.remove(queue)
        if not subs:
            self._subscribers.pop(action_id, None)
        logger.debug(
            "WS unsubscribe: action_id=%s, remaining=%d",
            action_id,
            len(self._subscribers.get(action_id, [])),
        )

    async def publish(self, action_id: str, data: dict[str, Any]) -> None:
        """向 action_id 的所有订阅者推送消息。"""
        subs = self._subscribers.get(action_id, [])
        if not subs:
            return

        message = json.dumps(data, default=str)
        dead: list[asyncio.Queue] = []
        for queue in subs:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning(
                    "WS queue full for action_id=%s, dropping subscriber",
                    action_id,
                )
                dead.append(queue)

        # 清理满队列的订阅者（客户端可能已断开）
        for q in dead:
            self.unsubscribe(action_id, q)


# 全局单例
broker = WSBroker()


async def publish_action_update(
    action_id: str,
    data: dict[str, Any],
) -> None:
    """发布 action 状态更新到 WebSocket broker。

    在 ``execution_gateway.py`` 和 ``approval_service.py`` 的状态变更后调用。
    """
    await broker.publish(action_id, data)
