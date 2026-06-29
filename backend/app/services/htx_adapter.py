"""HTX 适配器（任务 12.1 / Req 10）。

封装 HTX 公共行情客户端与私有签名客户端，提供 getTicker / getAccountBalance /
placeSpotOrder / cancelOrder 接口。支持 mock / real_read / real_trade 三种模式。

设计要点（方法论 §12 工具设计 5 原则）：
- 每个接口标记 idempotent / concurrencySafe / riskLevel 元数据
- symbol 内部统一小写，返回给前端时转大写显示格式
- 标准错误码映射（HTX_*）
- 令牌桶限流（100 req/2s）
- HMAC-SHA256 签名（时间戳容差 ±60s）
- mock 模式返回种子价格（Req 25 demo seed）
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 种子行情数据（Req 25 demo seed）
# ---------------------------------------------------------------------------
#: 种子行情价格数据（不含 as_of）——用于测试和内部引用。
_SEED_PRICES: Final[dict[str, dict[str, Any]]] = {
    "btcusdt": {
        "last": 68000.0,
        "bid": 67999.0,
        "ask": 68001.0,
        "vol_24h": 1500.0,
    },
    "ethusdt": {
        "last": 3600.0,
        "bid": 3599.0,
        "ask": 3601.0,
        "vol_24h": 25000.0,
    },
}

#: 向后兼容的静态引用——**仅用于测试**。测试中需要固定 as_of 的场景
#: 应直接使用此常量或自行构造 snapshot dict。
#: 生产 / 运行时代码应调用 :func:`get_fresh_seed_market_data` 获取
#: 带当前时间戳的快照，避免 stale_price_check 误判过期。
SEED_MARKET_DATA: Final[dict[str, dict[str, Any]]] = {
    sym: {
        **prices,
        "as_of": "2024-06-15T12:00:00+00:00",
        "provenance": "seed",
    }
    for sym, prices in _SEED_PRICES.items()
}


def get_fresh_seed_market_data() -> dict[str, dict[str, Any]]:
    """返回带**当前 UTC 时间戳**的种子行情快照。

    每次调用生成新的 ``as_of`` 字段，确保 ``stale_price_check`` 模块
    不会因种子数据过期而阻断合法操作。

    ``provenance`` 字段固定为 ``"seed"``——种子数据始终视为可信来源
    （与 G2 信息流追踪兼容）。

    Returns
    -------
    dict[str, dict[str, Any]]
        ``{symbol: {last, bid, ask, vol_24h, as_of, provenance}}``
    """
    now_iso = datetime.now(UTC).isoformat()
    return {
        sym: {
            **prices,
            "as_of": now_iso,
            "provenance": "seed",
        }
        for sym, prices in _SEED_PRICES.items()
    }

# ---------------------------------------------------------------------------
# 工具元数据（方法论 §12 工具设计 5 原则）
# ---------------------------------------------------------------------------
TOOL_METADATA: Final[dict[str, dict[str, Any]]] = {
    "getTicker": {"idempotent": True, "concurrencySafe": True, "riskLevel": "low"},
    "getAccountBalance": {"idempotent": True, "concurrencySafe": True, "riskLevel": "low"},
    "placeSpotOrder": {"idempotent": False, "concurrencySafe": False, "riskLevel": "high"},
    "cancelOrder": {"idempotent": False, "concurrencySafe": False, "riskLevel": "high"},
}

# ---------------------------------------------------------------------------
# 标准错误码映射
# ---------------------------------------------------------------------------
HTX_ERROR_MAP: Final[dict[str, str]] = {
    "api-signature-not-valid": "HTX_AUTH_FAILED",
    "too-many-request": "HTX_RATE_LIMITED",
    "timeout": "HTX_NETWORK_ERROR",
    "insufficient-balance": "HTX_INSUFFICIENT_BALANCE",
    "order-rejected": "HTX_ORDER_REJECTED",
}


# ---------------------------------------------------------------------------
# Ticker 合理性范围（修复 G5：工具返回值不可信处理）
# ---------------------------------------------------------------------------
#: 各 symbol 的 last price 合理范围（USDT 计价）。
#:
#: 设计依据：见 `docs/tech-research/06-phase2-policy-and-injection.md` G5 节。
#: 这是"防御纵深"层——即便 HTX 公共 API 被中间人攻击 / 网络异常返回离谱
#: 价格，本检查也能在 ticker 进入 `market_snapshot` 之前拦下。
#:
#: 范围放宽（BTC: $1k-$1M）覆盖 10 年内合理波动；超出此范围视为脏数据,
#: 调用方走"种子价格 fallback + 写 MARKET_DATA_ANOMALY 审计事件"路径。
#:
#: 生产应升级为 3σ-from-rolling-avg 的动态范围（需要历史价格存储）。
TICKER_PRICE_RANGES: Final[dict[str, tuple[float, float]]] = {
    "btcusdt": (1_000.0, 1_000_000.0),
    "ethusdt": (10.0, 100_000.0),
    "solusdt": (1.0, 10_000.0),
    "dogeusdt": (0.0001, 100.0),
}


def validate_ticker_sanity(
    symbol: str, last: float, bid: float, ask: float
) -> tuple[bool, str]:
    """检查 ticker 数值合理性（修复 G5）。

    检查项
    ------
    1. **正数性**：last/bid/ask 必须 > 0（HTX API 偶尔返回 0 表示数据缺失）。
    2. **bid <= last <= ask**：基本市场结构（如违反说明数据已损坏 / 中间人攻击）。
    3. **历史合理范围**：last 在 :data:`TICKER_PRICE_RANGES` 内（防离群值）。
    4. **bid-ask spread 限制**：(ask - bid) / last < 5%（防 spread 异常宽）。

    Returns
    -------
    (ok, reason)
        ``(True, "")`` 通过；``(False, reason)`` 不通过。reason 字符串供
        审计事件 ``MARKET_DATA_ANOMALY`` 的 detail 字段使用。

    Notes
    -----
    本函数纯函数（无 I/O），可被任何调用方使用——不限于 HTXAdapter。
    Policy Engine 的 stale-price 检查也可以选择性使用本函数做"价格异常加强"。
    """
    if last <= 0 or bid <= 0 or ask <= 0:
        return False, f"non-positive price (last={last}, bid={bid}, ask={ask})"
    if not (bid <= last <= ask):
        return False, (
            f"price ordering invalid (bid={bid}, last={last}, ask={ask}): "
            "expected bid <= last <= ask"
        )
    sym_lower = symbol.lower()
    range_ = TICKER_PRICE_RANGES.get(sym_lower)
    if range_ is not None:
        lo, hi = range_
        if not (lo <= last <= hi):
            return False, (
                f"price out of historical range for {sym_lower}: "
                f"last={last} not in [{lo}, {hi}]"
            )
    # spread 检查：放在最后，先确认数值合法
    spread = ask - bid
    if last > 0 and (spread / last) > 0.05:
        return False, (
            f"bid-ask spread too wide for {sym_lower}: "
            f"spread={spread} ({spread/last:.2%} of last)"
        )
    return True, ""


# ---------------------------------------------------------------------------
# 异常类
# ---------------------------------------------------------------------------
class HTXAdapterError(Exception):
    """HTX 适配器错误基类。"""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(f"[{code}] {message}")


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------
@dataclass
class TickerResult:
    """行情结果。"""

    symbol: str
    last: float
    bid: float
    ask: float
    vol_24h: float = 0.0


@dataclass
class AccountBalance:
    """账户余额。"""

    currency: str
    available: float
    frozen: float


@dataclass
class OrderResult:
    """订单结果。"""

    order_id: str
    symbol: str
    side: str
    status: str
    filled_amount: float = 0.0
    filled_price: float = 0.0


# ---------------------------------------------------------------------------
# 令牌桶限流器（100 req/2s）
# ---------------------------------------------------------------------------
@dataclass
class TokenBucketRateLimiter:
    """令牌桶限流器。

    capacity=100, refill_rate=50 tokens/s → 100 req/2s 窗口。
    """

    capacity: int = 100
    refill_rate: float = 50.0  # tokens per second (100/2s)
    tokens: float = field(default=100.0, init=False)
    last_refill: float = field(default_factory=time.time, init=False)

    async def acquire(self) -> None:
        """获取一个令牌；无令牌时等待直到可用。"""
        while True:
            now = time.time()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last_refill = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
            await asyncio.sleep(0.02)  # 20ms 后重试


# ---------------------------------------------------------------------------
# HTX 适配器
# ---------------------------------------------------------------------------
class HTXAdapter:
    """HTX 适配器（Req 10）。

    支持三种模式：
    - mock: 返回种子数据，不依赖外部网络
    - real_read: 调用真实 HTX 公共行情 API（私有端点不可用）
    - real_trade: 调用真实 HTX 公共 + 私有 API
    """

    def __init__(
        self,
        mode: Literal["mock", "real_read", "real_trade"] = "mock",
        access_key: str = "",
        secret_key: str = "",
        api_url: str = "https://api.huobi.pro",
    ):
        self.mode = mode
        self.access_key = access_key
        self.secret_key = secret_key
        self.api_url = api_url
        self.rate_limiter = TokenBucketRateLimiter()
        self._account_id: str | None = None  # 缓存 HTX account ID

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    async def get_ticker(self, symbol: str) -> TickerResult:
        """获取行情（idempotent=True, concurrencySafe=True, riskLevel=low）。

        修复 G5（工具返回值不可信处理）：调用 :func:`validate_ticker_sanity`
        校验返回值合理性，不通过抛 ``HTX_NETWORK_ERROR``——让 Policy Engine
        的反幻觉检查路径接管（symbol 不进 market_snapshot → PLAN_HALLUCINATION）。
        """
        symbol = symbol.lower()
        if self.mode == "mock":
            data = SEED_MARKET_DATA.get(symbol)
            if data is None:
                raise HTXAdapterError(
                    "HTX_NETWORK_ERROR",
                    f"symbol {symbol} not found in seed data",
                )
            last = float(data["last"])
            bid = float(data["bid"])
            ask = float(data["ask"])
            ok, reason = validate_ticker_sanity(symbol, last, bid, ask)
            if not ok:
                raise HTXAdapterError(
                    "HTX_NETWORK_ERROR",
                    f"ticker sanity check failed for {symbol}: {reason}",
                )
            return TickerResult(
                symbol=symbol,
                last=last,
                bid=bid,
                ask=ask,
                vol_24h=data.get("vol_24h", 0.0),
            )
        await self.rate_limiter.acquire()
        # 真实 HTX 公共行情 API 调用
        return await self._fetch_real_ticker(symbol)

    async def get_account_balance(self) -> list[AccountBalance]:
        """获取账户余额（idempotent=True, concurrencySafe=True, riskLevel=low）。"""
        if self.mode == "mock":
            return [
                AccountBalance(currency="usdt", available=1000.0, frozen=0.0),
                AccountBalance(currency="btc", available=0.01, frozen=0.0),
            ]
        await self.rate_limiter.acquire()
        return await self._fetch_account_balance()

    async def place_spot_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        amount: float,
        price: float | None = None,
    ) -> OrderResult:
        """下单（idempotent=False, concurrencySafe=False, riskLevel=high）。"""
        symbol = symbol.lower()
        if self.mode != "real_trade":
            raise HTXAdapterError(
                "HTX_AUTH_FAILED",
                f"place_spot_order not allowed in mode={self.mode}",
            )
        await self.rate_limiter.acquire()
        return await self._fetch_place_order(symbol, side, order_type, amount, price)

    async def cancel_order(self, order_id: str) -> OrderResult:
        """撤单（idempotent=False, concurrencySafe=False, riskLevel=high）。"""
        if self.mode != "real_trade":
            raise HTXAdapterError(
                "HTX_AUTH_FAILED",
                f"cancel_order not allowed in mode={self.mode}",
            )
        await self.rate_limiter.acquire()
        return await self._fetch_cancel_order(order_id)

    # ------------------------------------------------------------------
    # 真实 HTX 公共行情调用
    # ------------------------------------------------------------------

    async def _fetch_real_ticker(self, symbol: str) -> TickerResult:
        """调用 HTX 公共行情 API 获取实时 ticker。

        端点：``GET /market/detail/merged?symbol={symbol}``
        文档：https://huobiapi.github.io/docs/spot/v1/en/#get-merged-ticker

        HTX 返回的 ``tick`` 字段包含 ``close``（last）、``bid[0]``、``ask[0]``、
        ``vol`` 等字段。本方法将其标准化为 :class:`TickerResult` 并通过
        :func:`validate_ticker_sanity` 校验合理性。

        Raises
        ------
        HTXAdapterError
            网络错误 / API 返回异常 / ticker 合理性校验失败。
        """
        url = f"{self.api_url}/market/detail/merged"
        params = {"symbol": symbol}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException:
            raise HTXAdapterError(
                "HTX_NETWORK_ERROR",
                f"HTX ticker request timed out for {symbol}",
                retryable=True,
            )
        except httpx.ConnectError as exc:
            raise HTXAdapterError(
                "HTX_NETWORK_ERROR",
                f"HTX connection failed: {exc}",
                retryable=True,
            )
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code == 429:
                raise HTXAdapterError(
                    "HTX_RATE_LIMITED",
                    f"HTX rate limited (429) for {symbol}",
                    retryable=True,
                )
            raise HTXAdapterError(
                "HTX_NETWORK_ERROR",
                f"HTX HTTP {status_code} for {symbol}",
                retryable=True,
            )
        except Exception as exc:
            raise HTXAdapterError(
                "HTX_NETWORK_ERROR",
                f"unexpected error fetching ticker for {symbol}: {exc}",
                retryable=True,
            )

        # 解析 HTX 响应
        if data.get("status") != "ok":
            err_msg = data.get("err-msg", "unknown error")
            mapped = self.map_error(data.get("err-code", ""))
            raise HTXAdapterError(
                mapped,
                f"HTX API error for {symbol}: {err_msg}",
                retryable=False,
            )

        tick = data.get("tick", {})
        last = float(tick.get("close", 0))
        bid = float(tick.get("bid", [0])[0]) if tick.get("bid") else 0.0
        ask = float(tick.get("ask", [0])[0]) if tick.get("ask") else 0.0
        vol_24h = float(tick.get("vol", 0))

        # 合理性校验（G5 防御纵深）
        ok, reason = validate_ticker_sanity(symbol, last, bid, ask)
        if not ok:
            logger.warning(
                "HTX ticker sanity check failed for %s: %s",
                symbol,
                reason,
            )
            raise HTXAdapterError(
                "HTX_NETWORK_ERROR",
                f"ticker sanity check failed for {symbol}: {reason}",
            )

        return TickerResult(
            symbol=symbol,
            last=last,
            bid=bid,
            ask=ask,
            vol_24h=vol_24h,
        )

    # ------------------------------------------------------------------
    # 真实 HTX 私有 API 调用
    # ------------------------------------------------------------------

    async def _get_account_id(self) -> str:
        """获取 HTX spot account ID（缓存）。\n\n        端点：``GET /v1/account/accounts``\n        HTX 返回多个 account，我们取 type=spot 的第一个。
        """
        if self._account_id:
            return self._account_id

        path = "/v1/account/accounts"
        signed_params = self._sign_request("GET", path, {})
        url = f"{self.api_url}{path}?{urlencode(signed_params)}"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            raise HTXAdapterError(
                "HTX_NETWORK_ERROR",
                f"failed to fetch account list: {exc}",
                retryable=True,
            )

        if data.get("status") != "ok":
            raise HTXAdapterError(
                self.map_error(data.get("err-code", "")),
                f"HTX account list error: {data.get('err-msg', 'unknown')}",
            )

        accounts = data.get("data", [])
        for acct in accounts:
            if acct.get("type") == "spot" and acct.get("state") == "working":
                self._account_id = str(acct["id"])
                return self._account_id

        raise HTXAdapterError(
            "HTX_NETWORK_ERROR",
            "no active spot account found",
        )

    async def _fetch_account_balance(self) -> list[AccountBalance]:
        """调用 HTX 私有 API 获取账户余额。\n\n        端点：``GET /v1/account/accounts/{account-id}/balance``\n        解析 ``list`` 字段，只返回 type=trade 的余额（非 frozen）。
        """
        account_id = await self._get_account_id()
        path = f"/v1/account/accounts/{account_id}/balance"
        signed_params = self._sign_request("GET", path, {})
        url = f"{self.api_url}{path}?{urlencode(signed_params)}"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException:
            raise HTXAdapterError(
                "HTX_NETWORK_ERROR",
                "HTX balance request timed out",
                retryable=True,
            )
        except Exception as exc:
            raise HTXAdapterError(
                "HTX_NETWORK_ERROR",
                f"failed to fetch balance: {exc}",
                retryable=True,
            )

        if data.get("status") != "ok":
            raise HTXAdapterError(
                self.map_error(data.get("err-code", "")),
                f"HTX balance error: {data.get('err-msg', 'unknown')}",
            )

        balances: list[AccountBalance] = []
        for item in data.get("data", {}).get("list", []):
            if item.get("type") == "trade":
                balances.append(
                    AccountBalance(
                        currency=item.get("currency", "").lower(),
                        available=float(item.get("balance", 0)),
                        frozen=0.0,
                    )
                )
            elif item.get("type") == "frozen":
                # 匹配已有的 currency，更新 frozen 字段
                currency = item.get("currency", "").lower()
                existing = next(
                    (b for b in balances if b.currency == currency), None
                )
                if existing:
                    existing.frozen = float(item.get("balance", 0))
                else:
                    balances.append(
                        AccountBalance(
                            currency=currency,
                            available=0.0,
                            frozen=float(item.get("balance", 0)),
                        )
                    )
        return balances

    async def _fetch_place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        amount: float,
        price: float | None = None,
    ) -> OrderResult:
        """调用 HTX 私有 API 下单。\n\n        端点：``POST /v1/order/orders/place``\n        参数：symbol, side(buy/sell), type(limit/market), amount, price\n        """
        account_id = await self._get_account_id()
        path = "/v1/order/orders/place"

        # HTX order type 映射
        htx_type = order_type
        if order_type in ("limit", "buy-limit", "sell-limit"):
            htx_type = f"{side}-limit" if "-" not in order_type else order_type
        elif order_type in ("market", "buy-market", "sell-market"):
            htx_type = f"{side}-market" if "-" not in order_type else order_type

        body: dict[str, Any] = {
            "account-id": account_id,
            "symbol": symbol,
            "type": htx_type,
            "amount": str(amount),
        }
        if price is not None and "limit" in htx_type:
            body["price"] = str(price)

        signed_params = self._sign_request("POST", path, {})
        url = f"{self.api_url}{path}?{urlencode(signed_params)}"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=body)
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException:
            raise HTXAdapterError(
                "HTX_NETWORK_ERROR",
                "HTX place order timed out",
                retryable=True,
            )
        except Exception as exc:
            raise HTXAdapterError(
                "HTX_NETWORK_ERROR",
                f"failed to place order: {exc}",
                retryable=False,
            )

        if data.get("status") != "ok":
            err_code = data.get("err-code", "")
            raise HTXAdapterError(
                self.map_error(err_code),
                f"HTX order rejected: {data.get('err-msg', err_code)}",
            )

        order_id = str(data.get("data", ""))
        return OrderResult(
            order_id=order_id,
            symbol=symbol,
            side=side,
            status="SUBMITTED",
        )

    async def _fetch_cancel_order(self, order_id: str) -> OrderResult:
        """调用 HTX 私有 API 撤单。\n\n        端点：``POST /v1/order/orders/{order-id}/submitcancel``\n        """
        path = f"/v1/order/orders/{order_id}/submitcancel"
        signed_params = self._sign_request("POST", path, {})
        url = f"{self.api_url}{path}?{urlencode(signed_params)}"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url)
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException:
            raise HTXAdapterError(
                "HTX_NETWORK_ERROR",
                "HTX cancel order timed out",
                retryable=True,
            )
        except Exception as exc:
            raise HTXAdapterError(
                "HTX_NETWORK_ERROR",
                f"failed to cancel order: {exc}",
                retryable=False,
            )

        if data.get("status") != "ok":
            err_code = data.get("err-code", "")
            raise HTXAdapterError(
                self.map_error(err_code),
                f"HTX cancel rejected: {data.get('err-msg', err_code)}",
            )

        return OrderResult(
            order_id=order_id,
            symbol="",
            side="",
            status="CANCELLED",
        )

    # ------------------------------------------------------------------
    # 签名
    # ------------------------------------------------------------------

    def _sign_request(self, method: str, path: str, params: dict[str, str]) -> dict[str, str]:
        """HMAC-SHA256 签名（时间戳容差 ±60s）。"""
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        params_to_sign = {
            "AccessKeyId": self.access_key,
            "SignatureMethod": "HmacSHA256",
            "SignatureVersion": "2",
            "Timestamp": timestamp,
            **params,
        }
        sorted_params = "&".join(f"{k}={v}" for k, v in sorted(params_to_sign.items()))
        # 签名 payload 需要 host（不含 scheme）
        host = self.api_url.replace("https://", "").replace("http://", "")
        payload = f"{method}\n{host}\n{path}\n{sorted_params}"
        signature = hmac.new(
            self.secret_key.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        params_to_sign["Signature"] = signature
        return params_to_sign

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def format_symbol_for_display(symbol: str) -> str:
        """内部小写 → UI 大写显示格式（如 btcusdt → BTC/USDT）。"""
        s = symbol.upper()
        if s.endswith("USDT"):
            return f"{s[:-4]}/USDT"
        return s

    @staticmethod
    def map_error(raw_error: str) -> str:
        """映射 HTX 原始错误到标准错误码。"""
        return HTX_ERROR_MAP.get(raw_error, "HTX_NETWORK_ERROR")
