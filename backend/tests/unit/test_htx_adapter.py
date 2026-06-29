"""任务 12.1 HTX 适配器单元测试（Req 10）。

覆盖矩阵
--------
1. **mock 模式行情**：
   - test_get_ticker_btcusdt_returns_seed_price
   - test_get_ticker_ethusdt_returns_seed_price
   - test_get_ticker_unknown_symbol_raises
   - test_get_ticker_case_insensitive

2. **mock 模式余额**：
   - test_get_account_balance_returns_seed_data

3. **下单限制**：
   - test_place_spot_order_in_mock_mode_raises
   - test_place_spot_order_in_real_read_mode_raises
   - test_cancel_order_in_mock_mode_raises

4. **工具元数据**：
   - test_tool_metadata_get_ticker_idempotent_concurrent_low
   - test_tool_metadata_place_spot_order_not_idempotent_not_concurrent_high

5. **错误映射**：
   - test_map_error_known_codes
   - test_map_error_unknown_defaults_to_network_error

6. **symbol 格式化**：
   - test_format_symbol_btcusdt
   - test_format_symbol_ethusdt
   - test_format_symbol_non_usdt

7. **限流器**：
   - test_rate_limiter_allows_burst
   - test_rate_limiter_basic_functionality
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.htx_adapter import (
    TOOL_METADATA,
    AccountBalance,
    HTXAdapter,
    HTXAdapterError,
    OrderResult,
    TickerResult,
    TokenBucketRateLimiter,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine synchronously for testing."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. Mock 模式行情
# ---------------------------------------------------------------------------


class TestGetTickerMock:
    """mock 模式下 get_ticker 行为。"""

    def setup_method(self):
        self.adapter = HTXAdapter(mode="mock")

    def test_get_ticker_btcusdt_returns_seed_price(self):
        """btcusdt → last=68000。"""
        result = _run(self.adapter.get_ticker("btcusdt"))
        assert isinstance(result, TickerResult)
        assert result.symbol == "btcusdt"
        assert result.last == 68000.0
        assert result.bid == 67999.0
        assert result.ask == 68001.0
        assert result.vol_24h == 1500.0

    def test_get_ticker_ethusdt_returns_seed_price(self):
        """ethusdt → last=3600。"""
        result = _run(self.adapter.get_ticker("ethusdt"))
        assert isinstance(result, TickerResult)
        assert result.symbol == "ethusdt"
        assert result.last == 3600.0
        assert result.bid == 3599.0
        assert result.ask == 3601.0

    def test_get_ticker_unknown_symbol_raises(self):
        """不存在的 symbol → HTXAdapterError。"""
        with pytest.raises(HTXAdapterError) as exc_info:
            _run(self.adapter.get_ticker("xyzusdt"))
        assert exc_info.value.code == "HTX_NETWORK_ERROR"
        assert "xyzusdt" in exc_info.value.message

    def test_get_ticker_case_insensitive(self):
        """BTCUSDT / BtcUsdt → 同样返回 68000。"""
        result_upper = _run(self.adapter.get_ticker("BTCUSDT"))
        result_mixed = _run(self.adapter.get_ticker("BtcUsdt"))
        assert result_upper.last == 68000.0
        assert result_mixed.last == 68000.0
        # symbol 内部统一小写
        assert result_upper.symbol == "btcusdt"
        assert result_mixed.symbol == "btcusdt"


# ---------------------------------------------------------------------------
# 2. Mock 模式余额
# ---------------------------------------------------------------------------


class TestGetAccountBalanceMock:
    """mock 模式下 get_account_balance 行为。"""

    def test_get_account_balance_returns_seed_data(self):
        adapter = HTXAdapter(mode="mock")
        balances = _run(adapter.get_account_balance())
        assert len(balances) == 2
        assert all(isinstance(b, AccountBalance) for b in balances)
        # USDT
        usdt = next(b for b in balances if b.currency == "usdt")
        assert usdt.available == 1000.0
        assert usdt.frozen == 0.0
        # BTC
        btc = next(b for b in balances if b.currency == "btc")
        assert btc.available == 0.01
        assert btc.frozen == 0.0


# ---------------------------------------------------------------------------
# 3. 下单限制
# ---------------------------------------------------------------------------


class TestOrderRestrictions:
    """非 real_trade 模式下下单/撤单被拒绝。"""

    def test_place_spot_order_in_mock_mode_raises(self):
        """mock 模式不允许下单。"""
        adapter = HTXAdapter(mode="mock")
        with pytest.raises(HTXAdapterError) as exc_info:
            _run(adapter.place_spot_order("btcusdt", "buy", "limit", 0.001, 68000.0))
        assert exc_info.value.code == "HTX_AUTH_FAILED"
        assert "mock" in exc_info.value.message

    def test_place_spot_order_in_real_read_mode_raises(self):
        """real_read 模式不允许下单。"""
        adapter = HTXAdapter(mode="real_read")
        with pytest.raises(HTXAdapterError) as exc_info:
            _run(adapter.place_spot_order("btcusdt", "buy", "limit", 0.001, 68000.0))
        assert exc_info.value.code == "HTX_AUTH_FAILED"
        assert "real_read" in exc_info.value.message

    def test_cancel_order_in_mock_mode_raises(self):
        """mock 模式不允许撤单。"""
        adapter = HTXAdapter(mode="mock")
        with pytest.raises(HTXAdapterError) as exc_info:
            _run(adapter.cancel_order("order-123"))
        assert exc_info.value.code == "HTX_AUTH_FAILED"
        assert "mock" in exc_info.value.message


# ---------------------------------------------------------------------------
# 4. 工具元数据
# ---------------------------------------------------------------------------


class TestToolMetadata:
    """TOOL_METADATA 标记正确。"""

    def test_tool_metadata_get_ticker_idempotent_concurrent_low(self):
        """getTicker: idempotent=True, concurrencySafe=True, riskLevel=low。"""
        meta = TOOL_METADATA["getTicker"]
        assert meta["idempotent"] is True
        assert meta["concurrencySafe"] is True
        assert meta["riskLevel"] == "low"

    def test_tool_metadata_place_spot_order_not_idempotent_not_concurrent_high(self):
        """placeSpotOrder: idempotent=False, concurrencySafe=False, riskLevel=high。"""
        meta = TOOL_METADATA["placeSpotOrder"]
        assert meta["idempotent"] is False
        assert meta["concurrencySafe"] is False
        assert meta["riskLevel"] == "high"

    def test_tool_metadata_get_account_balance(self):
        """getAccountBalance: idempotent=True, concurrencySafe=True, riskLevel=low。"""
        meta = TOOL_METADATA["getAccountBalance"]
        assert meta["idempotent"] is True
        assert meta["concurrencySafe"] is True
        assert meta["riskLevel"] == "low"

    def test_tool_metadata_cancel_order(self):
        """cancelOrder: idempotent=False, concurrencySafe=False, riskLevel=high。"""
        meta = TOOL_METADATA["cancelOrder"]
        assert meta["idempotent"] is False
        assert meta["concurrencySafe"] is False
        assert meta["riskLevel"] == "high"


# ---------------------------------------------------------------------------
# 5. 错误映射
# ---------------------------------------------------------------------------


class TestErrorMapping:
    """HTX 原始错误 → 标准错误码映射。"""

    def test_map_error_known_codes(self):
        """每个已知错误码映射正确。"""
        assert HTXAdapter.map_error("api-signature-not-valid") == "HTX_AUTH_FAILED"
        assert HTXAdapter.map_error("too-many-request") == "HTX_RATE_LIMITED"
        assert HTXAdapter.map_error("timeout") == "HTX_NETWORK_ERROR"
        assert HTXAdapter.map_error("insufficient-balance") == "HTX_INSUFFICIENT_BALANCE"
        assert HTXAdapter.map_error("order-rejected") == "HTX_ORDER_REJECTED"

    def test_map_error_unknown_defaults_to_network_error(self):
        """未知错误码默认映射为 HTX_NETWORK_ERROR。"""
        assert HTXAdapter.map_error("some-unknown-error") == "HTX_NETWORK_ERROR"
        assert HTXAdapter.map_error("") == "HTX_NETWORK_ERROR"
        assert HTXAdapter.map_error("random-stuff") == "HTX_NETWORK_ERROR"


# ---------------------------------------------------------------------------
# 6. Symbol 格式化
# ---------------------------------------------------------------------------


class TestSymbolFormatting:
    """内部小写 → UI 大写显示格式。"""

    def test_format_symbol_btcusdt(self):
        """btcusdt → BTC/USDT。"""
        assert HTXAdapter.format_symbol_for_display("btcusdt") == "BTC/USDT"

    def test_format_symbol_ethusdt(self):
        """ethusdt → ETH/USDT。"""
        assert HTXAdapter.format_symbol_for_display("ethusdt") == "ETH/USDT"

    def test_format_symbol_non_usdt(self):
        """非 USDT 结尾 → 直接大写。"""
        assert HTXAdapter.format_symbol_for_display("ethbtc") == "ETHBTC"
        assert HTXAdapter.format_symbol_for_display("dogebtc") == "DOGEBTC"


# ---------------------------------------------------------------------------
# 7. 限流器
# ---------------------------------------------------------------------------


class TestRateLimiter:
    """令牌桶限流器基本行为。"""

    def test_rate_limiter_allows_burst(self):
        """连续 100 次 acquire 不阻塞（初始满桶）。"""
        limiter = TokenBucketRateLimiter()
        start = time.time()

        async def burst():
            for _ in range(100):
                await limiter.acquire()

        _run(burst())
        elapsed = time.time() - start
        # 100 次 acquire 应该几乎瞬间完成（< 0.5s）
        assert elapsed < 0.5

    def test_rate_limiter_basic_functionality(self):
        """令牌桶基本属性：初始满桶、消耗后令牌减少。"""
        limiter = TokenBucketRateLimiter()
        assert limiter.capacity == 100
        assert limiter.refill_rate == 50.0
        # 初始 tokens = capacity
        assert limiter.tokens == 100.0

        # 消耗一个令牌
        _run(limiter.acquire())
        # tokens 应该减少（可能因为 refill 略有增加，但总体 < 100）
        assert limiter.tokens < 100.0


# ---------------------------------------------------------------------------
# 8. HTX 私有 API（balance / place_order / cancel_order）
# ---------------------------------------------------------------------------


class TestHTXPrivateAPI:
    """HTX 私有 API 调用测试，使用 httpx MockTransport。"""

    def _make_adapter(self) -> HTXAdapter:
        return HTXAdapter(
            mode="real_trade",
            access_key="test-access-key",
            secret_key="test-secret-key",
            api_url="https://api.huobi.pro",
        )

    def _mock_response(self, status_code: int, json_data: dict) -> httpx.Response:
        """构造一个 httpx.Response。"""
        return httpx.Response(
            status_code=status_code,
            json=json_data,
            request=httpx.Request("GET", "https://api.huobi.pro/test"),
        )

    def test_fetch_account_balance_success(self):
        """成功获取账户余额。"""
        adapter = self._make_adapter()
        adapter._account_id = "12345"  # 缓存 account ID

        balance_response = {
            "status": "ok",
            "data": {
                "id": 12345,
                "type": "spot",
                "state": "working",
                "list": [
                    {"currency": "usdt", "type": "trade", "balance": "1500.50"},
                    {"currency": "usdt", "type": "frozen", "balance": "100.00"},
                    {"currency": "btc", "type": "trade", "balance": "0.05"},
                    {"currency": "btc", "type": "frozen", "balance": "0.01"},
                ],
            },
        }

        async def mock_get(url, **kwargs):
            return self._mock_response(200, balance_response)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            balances = _run(adapter.get_account_balance())

        assert len(balances) == 2
        usdt = next(b for b in balances if b.currency == "usdt")
        assert usdt.available == 1500.50
        assert usdt.frozen == 100.00
        btc = next(b for b in balances if b.currency == "btc")
        assert btc.available == 0.05
        assert btc.frozen == 0.01

    def test_fetch_account_balance_api_error(self):
        """HTX 返回错误时抛异常。"""
        adapter = self._make_adapter()
        adapter._account_id = "12345"

        error_response = {
            "status": "error",
            "err-code": "api-signature-not-valid",
            "err-msg": "Signature not valid",
        }

        async def mock_get(url, **kwargs):
            return self._mock_response(200, error_response)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with pytest.raises(HTXAdapterError) as exc_info:
                _run(adapter.get_account_balance())
            assert exc_info.value.code == "HTX_AUTH_FAILED"

    def test_place_spot_order_success(self):
        """成功下单。"""
        adapter = self._make_adapter()
        adapter._account_id = "12345"

        order_response = {
            "status": "ok",
            "data": "59378",
        }

        async def mock_post(url, **kwargs):
            return self._mock_response(200, order_response)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = mock_post
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = _run(
                adapter.place_spot_order("btcusdt", "buy", "limit", 0.001, 68000.0)
            )

        assert isinstance(result, OrderResult)
        assert result.order_id == "59378"
        assert result.symbol == "btcusdt"
        assert result.side == "buy"
        assert result.status == "SUBMITTED"

    def test_place_spot_order_rejected(self):
        """下单被拒绝。"""
        adapter = self._make_adapter()
        adapter._account_id = "12345"

        error_response = {
            "status": "error",
            "err-code": "insufficient-balance",
            "err-msg": "Insufficient balance",
        }

        async def mock_post(url, **kwargs):
            return self._mock_response(200, error_response)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = mock_post
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with pytest.raises(HTXAdapterError) as exc_info:
                _run(
                    adapter.place_spot_order(
                        "btcusdt", "buy", "limit", 100.0, 68000.0
                    )
                )
            assert exc_info.value.code == "HTX_INSUFFICIENT_BALANCE"

    def test_cancel_order_success(self):
        """成功撤单。"""
        adapter = self._make_adapter()

        cancel_response = {
            "status": "ok",
            "data": "59378",
        }

        async def mock_post(url, **kwargs):
            return self._mock_response(200, cancel_response)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = mock_post
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = _run(adapter.cancel_order("59378"))

        assert isinstance(result, OrderResult)
        assert result.order_id == "59378"
        assert result.status == "CANCELLED"

    def test_cancel_order_api_error(self):
        """撤单失败。"""
        adapter = self._make_adapter()

        error_response = {
            "status": "error",
            "err-code": "order-rejected",
            "err-msg": "Order already filled",
        }

        async def mock_post(url, **kwargs):
            return self._mock_response(200, error_response)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = mock_post
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with pytest.raises(HTXAdapterError) as exc_info:
                _run(adapter.cancel_order("59378"))
            assert exc_info.value.code == "HTX_ORDER_REJECTED"

    def test_get_account_id_caches_result(self):
        """account ID 缓存后不重复请求。"""
        adapter = self._make_adapter()
        adapter._account_id = "99999"
        # 直接检查缓存，不应发起请求
        result = _run(adapter._get_account_id())
        assert result == "99999"

    def test_get_account_id_fetches_on_first_call(self):
        """首次调用时获取 account ID。"""
        adapter = self._make_adapter()

        accounts_response = {
            "status": "ok",
            "data": [
                {"id": 111, "type": "point", "state": "working"},
                {"id": 222, "type": "spot", "state": "working"},
            ],
        }

        async def mock_get(url, **kwargs):
            return self._mock_response(200, accounts_response)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = _run(adapter._get_account_id())

        assert result == "222"
        assert adapter._account_id == "222"
        # 第二次应使用缓存
        result2 = _run(adapter._get_account_id())
        assert result2 == "222"
