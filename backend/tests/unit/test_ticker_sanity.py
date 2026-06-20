"""Ticker 合理性校验测试（修复 G5：工具返回值不可信处理）。

**Validates: G5 — 防御纵深，HTX 公共 API 异常返回不影响主路径**

覆盖项：
1. 正向：合理 ticker 通过
2. 反向：负数 / 零价格被拒
3. 反向：bid > ask 被拒（数据损坏）
4. 反向：last < bid 或 last > ask 被拒（结构违反）
5. 反向：超历史范围（BTC 价 < $1k 或 > $1M）被拒
6. 反向：spread 过宽（> 5%）被拒
7. 未在 TICKER_PRICE_RANGES 的 symbol 跳过范围检查（仅做基本结构检查）
"""

from __future__ import annotations

import pytest

from app.services.htx_adapter import (
    HTXAdapter,
    HTXAdapterError,
    validate_ticker_sanity,
)


# ===========================================================================
# 1. validate_ticker_sanity 纯函数测试
# ===========================================================================
class TestValidateTickerSanity:
    def test_valid_btc_ticker_passes(self) -> None:
        ok, reason = validate_ticker_sanity("btcusdt", 68000, 67999, 68001)
        assert ok
        assert reason == ""

    def test_valid_eth_ticker_passes(self) -> None:
        ok, _ = validate_ticker_sanity("ethusdt", 3600, 3599, 3601)
        assert ok

    def test_zero_last_rejected(self) -> None:
        ok, reason = validate_ticker_sanity("btcusdt", 0, 100, 200)
        assert not ok
        assert "non-positive" in reason

    def test_negative_bid_rejected(self) -> None:
        ok, reason = validate_ticker_sanity("btcusdt", 68000, -1, 68001)
        assert not ok
        assert "non-positive" in reason

    def test_bid_greater_than_ask_rejected(self) -> None:
        """bid > ask 是市场结构违反（订单簿损坏）。"""
        ok, reason = validate_ticker_sanity("btcusdt", 68000, 68500, 67500)
        assert not ok
        assert "ordering invalid" in reason

    def test_last_below_bid_rejected(self) -> None:
        ok, reason = validate_ticker_sanity("btcusdt", 67000, 67500, 68000)
        assert not ok

    def test_last_above_ask_rejected(self) -> None:
        ok, _ = validate_ticker_sanity("btcusdt", 68500, 67500, 68000)
        assert not ok

    def test_btc_below_historical_range_rejected(self) -> None:
        """BTC < $1000 视为离群值（历史最低 ~$3k 在 2018）。"""
        ok, reason = validate_ticker_sanity("btcusdt", 500, 499, 501)
        assert not ok
        assert "out of historical range" in reason

    def test_btc_above_historical_range_rejected(self) -> None:
        """BTC > $1M 视为离群值（防中间人攻击注入虚高价）。"""
        ok, _ = validate_ticker_sanity("btcusdt", 5_000_000, 4_999_999, 5_000_001)
        assert not ok

    def test_spread_too_wide_rejected(self) -> None:
        """bid-ask spread > 5% 视为流动性异常。

        构造 spread 10%：bid=64000, ask=72000, last=68000, spread=8000=11.7%。
        """
        ok, reason = validate_ticker_sanity("btcusdt", 68000, 64000, 72000)
        assert not ok
        assert "spread" in reason

    def test_spread_at_5_percent_boundary_passes(self) -> None:
        """spread 接近 5% 边界但不超过 → 通过。"""
        # spread = 4% of last
        last = 100.0
        spread = 4.0
        bid = last - spread / 2
        ask = last + spread / 2
        ok, _ = validate_ticker_sanity("btcusdt", last, bid, ask)
        # last=100 < $1000 BTC 范围下限 → 仍会被范围检查拦下，
        # 改用范围内的值
        ok, _ = validate_ticker_sanity("btcusdt", 50000, 49000, 51000)
        assert ok

    def test_unknown_symbol_skips_range_check(self) -> None:
        """未在 TICKER_PRICE_RANGES 的 symbol（如 newcoin）→ 仅做基本结构检查。"""
        ok, _ = validate_ticker_sanity("newcoinusdt", 0.5, 0.499, 0.501)
        assert ok


# ===========================================================================
# 2. HTXAdapter.get_ticker 集成测试
# ===========================================================================
class TestGetTickerSanityIntegration:
    """**Validates: 工具调用集成路径**——HTX adapter 在 mock 模式下用 sanity 校验。"""

    @pytest.mark.asyncio
    async def test_seed_data_passes_sanity(self) -> None:
        """SEED_MARKET_DATA 的种子值应通过 sanity（生产部署 baseline）。"""
        adapter = HTXAdapter(mode="mock")
        ticker = await adapter.get_ticker("btcusdt")
        assert ticker.symbol == "btcusdt"
        assert ticker.last == 68000.0

    @pytest.mark.asyncio
    async def test_corrupted_seed_data_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """模拟 HTX 公共 API 返回零价 → 适配器抛 HTX_NETWORK_ERROR。"""
        from app.services import htx_adapter

        # 临时把 SEED_MARKET_DATA 的 btcusdt 改为非法
        monkeypatch.setitem(
            htx_adapter.SEED_MARKET_DATA,
            "btcusdt",
            {"last": 0, "bid": 0, "ask": 0, "vol_24h": 0, "as_of": "2024-06-15T12:00:00+00:00"},
        )
        adapter = HTXAdapter(mode="mock")
        with pytest.raises(HTXAdapterError, match="sanity check failed"):
            await adapter.get_ticker("btcusdt")

    @pytest.mark.asyncio
    async def test_extreme_price_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """模拟 HTX 中间人攻击注入虚高价 → 拒绝。"""
        from app.services import htx_adapter

        monkeypatch.setitem(
            htx_adapter.SEED_MARKET_DATA,
            "btcusdt",
            {"last": 9_999_999, "bid": 9_999_998, "ask": 10_000_000, "vol_24h": 0, "as_of": "2024-06-15T12:00:00+00:00"},
        )
        adapter = HTXAdapter(mode="mock")
        with pytest.raises(HTXAdapterError, match="sanity check failed"):
            await adapter.get_ticker("btcusdt")
