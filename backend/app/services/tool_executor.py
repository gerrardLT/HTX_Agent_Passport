"""并发分批执行器（任务 12.2 / Req 10 AC8 / 方法论 §10）。

只读工具可并发执行，写操作必须串行执行。

设计要点：
- partition：连续只读工具合并为并发批次，写操作单独串行
- execute_batch：并发批次用 asyncio.gather，串行批次逐个执行
- Property 8：placeSpotOrder/cancelOrder 永远在 concurrent=False 的批次中且每批只有 1 个 call
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Coroutine

from app.services.htx_adapter import TOOL_METADATA


@dataclass
class ToolCall:
    """工具调用描述。"""

    name: str
    params: dict[str, Any]


@dataclass
class ToolResult:
    """工具执行结果。"""

    name: str
    params: dict[str, Any]
    result: Any = None
    error: str | None = None
    success: bool = True


@dataclass
class Batch:
    """执行批次。"""

    calls: list[ToolCall]
    concurrent: bool


class ToolExecutor:
    """并发分批执行器（方法论 §10 / Req 10 AC8）。

    只读工具（concurrencySafe=True）可并发执行；
    写操作（concurrencySafe=False）必须串行执行，每个单独一批。
    """

    def partition(self, calls: list[ToolCall]) -> list[Batch]:
        """将工具调用列表分为并发/串行批次。

        规则：
        - 连续的 concurrencySafe=True 工具合并为一个并发批次
        - concurrencySafe=False 的工具单独一个串行批次（每批只有 1 个 call）
        """
        batches: list[Batch] = []
        current_concurrent: list[ToolCall] = []

        for call in calls:
            meta = TOOL_METADATA.get(call.name, {})
            if meta.get("concurrencySafe", False):
                current_concurrent.append(call)
            else:
                # 先把之前积累的并发调用刷出去
                if current_concurrent:
                    batches.append(Batch(calls=current_concurrent, concurrent=True))
                    current_concurrent = []
                # 写操作单独一批
                batches.append(Batch(calls=[call], concurrent=False))

        # 尾部剩余的并发调用
        if current_concurrent:
            batches.append(Batch(calls=current_concurrent, concurrent=True))

        return batches

    async def execute_batch(
        self,
        calls: list[ToolCall],
        executor_fn: Callable[..., Coroutine[Any, Any, Any]],
    ) -> list[ToolResult]:
        """按分批策略执行工具调用列表。

        - 并发批次用 asyncio.gather 并行执行
        - 串行批次逐个执行
        - 某个调用失败不影响其他调用
        """
        batches = self.partition(calls)
        results: list[ToolResult] = []

        for batch in batches:
            if batch.concurrent:
                batch_results = await asyncio.gather(
                    *[self._execute_one(call, executor_fn) for call in batch.calls],
                    return_exceptions=True,
                )
                for call, res in zip(batch.calls, batch_results):
                    if isinstance(res, Exception):
                        results.append(
                            ToolResult(
                                name=call.name,
                                params=call.params,
                                error=str(res),
                                success=False,
                            )
                        )
                    else:
                        results.append(res)
            else:
                for call in batch.calls:
                    result = await self._execute_one(call, executor_fn)
                    results.append(result)

        return results

    async def _execute_one(
        self,
        call: ToolCall,
        executor_fn: Callable[..., Coroutine[Any, Any, Any]],
    ) -> ToolResult:
        """执行单个工具调用，捕获异常。"""
        try:
            result = await executor_fn(call.name, call.params)
            return ToolResult(name=call.name, params=call.params, result=result, success=True)
        except Exception as e:
            return ToolResult(name=call.name, params=call.params, error=str(e), success=False)
