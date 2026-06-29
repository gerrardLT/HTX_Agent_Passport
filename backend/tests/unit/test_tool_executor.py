"""并发分批执行器单元测试 + PBT（任务 12.2 / Req 10 AC8）。

覆盖：
1. test_partition_all_concurrent：全部只读 → 1 个并发批次
2. test_partition_all_serial：全部写操作 → 每个单独一批
3. test_partition_mixed：读-写-读 → 3 个批次（并发-串行-并发）
4. test_partition_empty：空列表 → 空批次
5. test_execute_batch_concurrent_calls_run_together：并发批次中的调用确实并行
6. test_execute_batch_serial_calls_run_sequentially：串行批次中的调用顺序执行
7. test_execute_batch_error_handling：某个调用失败不影响其他
8. PBT — Property 8：placeSpotOrder/cancelOrder 不并发
"""

from __future__ import annotations

import asyncio
import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.tool_executor import ToolCall, ToolExecutor


@pytest.fixture
def executor() -> ToolExecutor:
    return ToolExecutor()


# ---------------------------------------------------------------------------
# 单元测试：partition
# ---------------------------------------------------------------------------


class TestPartition:
    """ToolExecutor.partition 分批逻辑测试。"""

    def test_partition_all_concurrent(self, executor: ToolExecutor) -> None:
        """全部只读工具 → 1 个并发批次。"""
        calls = [
            ToolCall(name="getTicker", params={"symbol": "btcusdt"}),
            ToolCall(name="getAccountBalance", params={}),
            ToolCall(name="getTicker", params={"symbol": "ethusdt"}),
        ]
        batches = executor.partition(calls)

        assert len(batches) == 1
        assert batches[0].concurrent is True
        assert len(batches[0].calls) == 3

    def test_partition_all_serial(self, executor: ToolExecutor) -> None:
        """全部写操作 → 每个单独一批（concurrent=False）。"""
        calls = [
            ToolCall(name="placeSpotOrder", params={"symbol": "btcusdt", "side": "buy"}),
            ToolCall(name="cancelOrder", params={"order_id": "123"}),
            ToolCall(name="placeSpotOrder", params={"symbol": "ethusdt", "side": "sell"}),
        ]
        batches = executor.partition(calls)

        assert len(batches) == 3
        for batch in batches:
            assert batch.concurrent is False
            assert len(batch.calls) == 1

    def test_partition_mixed(self, executor: ToolExecutor) -> None:
        """读-写-读 → 3 个批次（并发-串行-并发）。"""
        calls = [
            ToolCall(name="getTicker", params={"symbol": "btcusdt"}),
            ToolCall(name="getAccountBalance", params={}),
            ToolCall(name="placeSpotOrder", params={"symbol": "btcusdt", "side": "buy"}),
            ToolCall(name="getTicker", params={"symbol": "ethusdt"}),
        ]
        batches = executor.partition(calls)

        assert len(batches) == 3
        # 第一批：2 个只读并发
        assert batches[0].concurrent is True
        assert len(batches[0].calls) == 2
        # 第二批：1 个写操作串行
        assert batches[1].concurrent is False
        assert len(batches[1].calls) == 1
        assert batches[1].calls[0].name == "placeSpotOrder"
        # 第三批：1 个只读并发
        assert batches[2].concurrent is True
        assert len(batches[2].calls) == 1

    def test_partition_empty(self, executor: ToolExecutor) -> None:
        """空列表 → 空批次。"""
        batches = executor.partition([])
        assert batches == []


# ---------------------------------------------------------------------------
# 单元测试：execute_batch
# ---------------------------------------------------------------------------


class TestExecuteBatch:
    """ToolExecutor.execute_batch 执行逻辑测试。"""

    @pytest.mark.asyncio
    async def test_execute_batch_concurrent_calls_run_together(
        self, executor: ToolExecutor
    ) -> None:
        """并发批次中的调用确实并行执行（总耗时接近单次而非累加）。"""
        call_times: list[float] = []

        async def slow_executor(name: str, params: dict) -> str:
            call_times.append(time.monotonic())
            await asyncio.sleep(0.1)  # 100ms
            return f"result_{name}"

        calls = [
            ToolCall(name="getTicker", params={"symbol": "btcusdt"}),
            ToolCall(name="getAccountBalance", params={}),
            ToolCall(name="getTicker", params={"symbol": "ethusdt"}),
        ]

        start = time.monotonic()
        results = await executor.execute_batch(calls, slow_executor)
        elapsed = time.monotonic() - start

        assert len(results) == 3
        assert all(r.success for r in results)
        # 并发执行：总耗时应接近 100ms 而非 300ms
        assert elapsed < 0.25  # 宽松阈值，避免 CI 抖动

    @pytest.mark.asyncio
    async def test_execute_batch_serial_calls_run_sequentially(
        self, executor: ToolExecutor
    ) -> None:
        """串行批次中的调用顺序执行（总耗时为累加）。"""
        execution_order: list[str] = []

        async def tracking_executor(name: str, params: dict) -> str:
            execution_order.append(name)
            await asyncio.sleep(0.05)  # 50ms
            return f"result_{name}"

        calls = [
            ToolCall(name="placeSpotOrder", params={"symbol": "btcusdt", "side": "buy"}),
            ToolCall(name="cancelOrder", params={"order_id": "123"}),
        ]

        start = time.monotonic()
        results = await executor.execute_batch(calls, tracking_executor)
        elapsed = time.monotonic() - start

        assert len(results) == 2
        assert all(r.success for r in results)
        # 串行执行：顺序保持
        assert execution_order == ["placeSpotOrder", "cancelOrder"]
        # 串行执行：总耗时应接近 100ms（2 × 50ms）
        assert elapsed >= 0.09

    @pytest.mark.asyncio
    async def test_execute_batch_error_handling(self, executor: ToolExecutor) -> None:
        """某个调用失败不影响其他调用。"""

        async def failing_executor(name: str, params: dict) -> str:
            if name == "getAccountBalance":
                raise RuntimeError("connection timeout")
            return f"ok_{name}"

        calls = [
            ToolCall(name="getTicker", params={"symbol": "btcusdt"}),
            ToolCall(name="getAccountBalance", params={}),
            ToolCall(name="getTicker", params={"symbol": "ethusdt"}),
        ]

        results = await executor.execute_batch(calls, failing_executor)

        assert len(results) == 3
        # 第一个和第三个成功
        assert results[0].success is True
        assert results[0].result == "ok_getTicker"
        # 第二个失败
        assert results[1].success is False
        assert "connection timeout" in results[1].error
        # 第三个仍然成功
        assert results[2].success is True
        assert results[2].result == "ok_getTicker"


# ---------------------------------------------------------------------------
# PBT — Property 8：placeSpotOrder/cancelOrder 不并发
# ---------------------------------------------------------------------------

# 策略：生成包含 placeSpotOrder/cancelOrder 的随机 ToolCall 列表
TOOL_NAMES = ["getTicker", "getAccountBalance", "placeSpotOrder", "cancelOrder"]

tool_call_strategy = st.builds(
    ToolCall,
    name=st.sampled_from(TOOL_NAMES),
    params=st.fixed_dictionaries({"symbol": st.sampled_from(["btcusdt", "ethusdt", "xrpusdt"])}),
)

# 确保列表中至少包含一个写操作
tool_calls_with_writes = st.lists(
    tool_call_strategy, min_size=1, max_size=20
).filter(lambda calls: any(c.name in ("placeSpotOrder", "cancelOrder") for c in calls))


class TestProperty8:
    """PBT — Property 8：placeSpotOrder/cancelOrder 永远在 concurrent=False 的批次中且每批只有 1 个 call。

    **Validates: Requirements 10.8**
    """

    @given(calls=tool_calls_with_writes)
    @settings(max_examples=200, deadline=None)
    def test_write_operations_never_concurrent(self, calls: list[ToolCall]) -> None:
        """随机生成包含 placeSpotOrder/cancelOrder 的 ToolCall 列表，
        partition 后这些调用永远在 concurrent=False 的批次中且每批只有 1 个 call。
        """
        executor = ToolExecutor()
        batches = executor.partition(calls)

        for batch in batches:
            for call in batch.calls:
                if call.name in ("placeSpotOrder", "cancelOrder"):
                    # 写操作必须在非并发批次中
                    assert batch.concurrent is False, (
                        f"{call.name} found in concurrent batch: {batch}"
                    )
                    # 写操作批次只有 1 个 call
                    assert len(batch.calls) == 1, (
                        f"{call.name} batch has {len(batch.calls)} calls, expected 1"
                    )
