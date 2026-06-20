"""审批/执行延迟后 market snapshot 时效与 slippage 重校验（修复 G16）。

LangGraph 社区点名的 HITL 头号失败模式：human approval 延迟后市场价格已变,
若仅复核 policy 版本变化（已实现）而不复核 market snapshot 时效与价格偏差,
则用户可能"按 1 小时前的报价"提交订单——尤其在加密市场剧烈波动时这是
真实资金风险。Req 16 AC2 已定义 ``max_slippage_bps`` 但此前仅在规划阶段使用,
未覆盖"延迟提交"场景。

设计为纯函数（无 I/O）：调用方负责把 ``market_snapshot`` 传进来。这样：

1. 测试时直接构造 snapshot dict，不需要 mock HTX 适配器；
2. ``policy_engine`` 保持纯函数性质（不被本模块"污染" I/O 调用）；
3. 上游可灵活选择 snapshot 来源（执行网关默认从 ``SEED_MARKET_DATA`` / 缓存 /
   实时拉取）。

为何独立成模块而不是塞进 ``policy_engine.evaluate_policy``？
-------------------------------------------------------
- ``evaluate_policy`` 已被 PBT 当作"确定性裁决核心"——把"snapshot 时效"
  这种**带时间维度**的判断混进去会让 Property 1（同输入恒同输出）的语义
  变味：同样 ``(action, policy, snapshot)`` 在不同 ``now`` 下结果不同。
- G16 的检查只在**重裁决（审批 / 执行延迟提交）路径**触发；初次裁决
  时无延迟问题。两个路径职责分离更清晰。
- reason_codes 已加入 :data:`policy_engine.REASON_CODES` 仅作"契约登记"，
  实际产生它们的是本模块。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

#: 默认 snapshot 时效阈值（秒）。审批/执行延迟超过此值要求刷新。
#:
#: 60 秒是"开发体验优先"的合理默认：足够让本地手测和 CI 不被频繁打断,
#: 又能在压测/真实交易场景下有效拦截过期报价。生产前可由环境变量覆盖
#: （后续若需要，把它读 ``settings`` 即可——目前不引入是为了让本模块
#: 保持纯函数性质，方便测试任意构造时间点）。
DEFAULT_SNAPSHOT_FRESHNESS_SECONDS: Final[int] = 60

#: 仅 place_order 触发 stale-price 检查；其他 action 类型直接放行。
#:
#: read_market / read_account：天然不带 limit_price，且只读不会造成资金风险。
#: cancel_order：引用已存在订单 id，与当前价无关。
#: no_op：什么都不做。
_PRICE_CHECKED_ACTION_TYPES: Final[frozenset[str]] = frozenset({"place_order"})


@dataclass(frozen=True)
class StalePriceCheckResult:
    """重校验结果（不可变 / 可哈希）。

    Attributes
    ----------
    ok : bool
        ``True`` = 可继续执行；``False`` = 阻断（拒绝/要求重新审批）。
    reason_code : str | None
        阻断时的原因码（``"MARKET_SNAPSHOT_STALE"`` /
        ``"MARKET_SLIPPAGE_EXCEEDED"``）；``ok=True`` 时为 ``None``。
    detail : dict[str, Any]
        详细信息（写入审计 ``event_data``）：``snapshot_age_seconds`` /
        ``expected_price`` / ``actual_price`` / ``deviation_bps`` /
        ``threshold_bps`` 等。
    """

    ok: bool
    reason_code: str | None
    detail: dict[str, Any]


def _parse_as_of(raw: Any) -> datetime | None:
    """把 snapshot.as_of 字段解析成带 UTC 的 ``datetime``。

    支持
    ----
    - ``datetime`` 实例：tz-aware 直接用；naive 视为 UTC。
    - ``str`` ISO 8601：尝试 ``datetime.fromisoformat``；末尾 ``Z`` 兼容
      转 ``+00:00``。

    任何无法解析的输入返回 ``None``——上层据此走"无法判定时效"分支
    （保守跳过时效检查）。
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
    if isinstance(raw, str):
        s = raw.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return None


def check_market_snapshot_freshness_and_slippage(
    *,
    action: dict[str, Any],
    policy: dict[str, Any],
    market_snapshot: dict[str, dict[str, Any]],
    now: datetime,
    snapshot_freshness_seconds: int = DEFAULT_SNAPSHOT_FRESHNESS_SECONDS,
) -> StalePriceCheckResult:
    """检查 snapshot 时效 + ``limit_price`` slippage（Req 16 AC2 / G16）。

    跳过条件
    --------
    - ``action.type`` 不是 ``place_order`` → 跳过（read/cancel/no_op 不需要价格校验）。
    - ``market_snapshot`` 中没有该 symbol → 跳过（``PLAN_HALLUCINATION``
      由 :func:`policy_engine.evaluate_policy` 处理，本模块不重复拦截）。
    - ``max_slippage_bps`` 未配置 → 跳过 slippage 检查（但仍做时效检查）。
    - ``limit_price`` 为 ``None`` / 0 → 跳过 slippage 检查（market 单不在此处控制滑点）。

    时效检查
    --------
    若 snapshot 含 ``as_of`` 字段（ISO 8601 字符串或 ``datetime``），
    且 ``now - as_of > snapshot_freshness_seconds`` → 阻断
    ``MARKET_SNAPSHOT_STALE``。这是**严格策略**：过期就阻断，无论价格是否
    实际变了——因为我们不知道"当前真实价格"，只能要求刷新。

    若 snapshot 不含 ``as_of`` → 视为"无法判定时效"，**保守策略：跳过时效
    检查**（不阻断，因为这可能只是种子数据/旧数据格式）。这与"开发时不愿
    被频繁打断"的工程权衡一致；生产前应通过 HTX 实时接口确保 ``as_of``
    始终存在。

    Slippage 检查
    -------------
    若 ``max_slippage_bps`` 已配置 + ``limit_price`` 已给 + snapshot 中有
    ``last`` 价::

        deviation_bps = abs(limit_price - last) / last * 10000

    若 ``deviation_bps > max_slippage_bps`` → ``MARKET_SLIPPAGE_EXCEEDED``。

    Parameters
    ----------
    action : dict[str, Any]
        已规范化的 action dict（symbol 已小写）。
    policy : dict[str, Any]
        Passport.policy_json；从 ``limits.max_slippage_bps`` 读阈值。
    market_snapshot : dict[str, dict]
        ``{symbol_lowercase: {"last": price, "as_of": iso8601 | datetime, ...}}``。
    now : datetime
        当前 UTC 时间。强制显式传入以保证测试可重放（与 ``evaluate_policy``
        的 PBT 习惯一致）。naive datetime 视为 UTC。
    snapshot_freshness_seconds : int, default :data:`DEFAULT_SNAPSHOT_FRESHNESS_SECONDS`
        过期阈值；调用方可按需放宽（如压力测试）。

    Returns
    -------
    StalePriceCheckResult
        ``ok=True`` 可继续；``ok=False`` 由调用方决定拒绝或要求重新审批。
    """
    # 防御：naive now → 视为 UTC（与 audit_writer / policy_engine 同语义）
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    # ---- 跳过条件 1：action.type 不需价格校验 ----
    action_type = action.get("type")
    if action_type not in _PRICE_CHECKED_ACTION_TYPES:
        return StalePriceCheckResult(
            ok=True,
            reason_code=None,
            detail={"skipped": "action_type_not_price_checked", "action_type": action_type},
        )

    # ---- 跳过条件 2：snapshot 缺该 symbol ----
    # 这条由 policy_engine 的 PLAN_HALLUCINATION 拦截，本模块不重复处理；
    # 但要"跳过"而非"阻断"，否则在 PLAN_HALLUCINATION 之前先被本模块拦下,
    # 拦截原因会被掩盖。
    symbol = action.get("symbol")
    if not isinstance(symbol, str) or symbol not in market_snapshot:
        return StalePriceCheckResult(
            ok=True,
            reason_code=None,
            detail={"skipped": "symbol_not_in_snapshot", "symbol": symbol},
        )

    snapshot_entry = market_snapshot[symbol]

    # ---- 时效检查 ----
    as_of_dt = _parse_as_of(snapshot_entry.get("as_of"))
    if as_of_dt is not None:
        age_seconds = (now - as_of_dt).total_seconds()
        if age_seconds > snapshot_freshness_seconds:
            return StalePriceCheckResult(
                ok=False,
                reason_code="MARKET_SNAPSHOT_STALE",
                detail={
                    "symbol": symbol,
                    "snapshot_as_of": as_of_dt.isoformat(),
                    "now": now.isoformat(),
                    "snapshot_age_seconds": age_seconds,
                    "freshness_threshold_seconds": snapshot_freshness_seconds,
                },
            )
    # else：as_of 缺失 / 解析失败——保守跳过时效检查，继续做 slippage 检查

    # ---- Slippage 检查 ----
    limits: dict[str, Any] = policy.get("limits", {}) or {}
    max_slippage_bps_raw = limits.get("max_slippage_bps")
    if max_slippage_bps_raw is None:
        # 未配置 → 跳过 slippage（但时效检查上面已通过/已跳过）
        return StalePriceCheckResult(
            ok=True,
            reason_code=None,
            detail={
                "skipped": "max_slippage_bps_not_configured",
                "symbol": symbol,
            },
        )

    limit_price_raw = action.get("limit_price")
    if limit_price_raw in (None, 0, 0.0):
        # market 单或显式 0 → 不在此处控制滑点
        return StalePriceCheckResult(
            ok=True,
            reason_code=None,
            detail={
                "skipped": "limit_price_unset",
                "symbol": symbol,
                "limit_price": limit_price_raw,
            },
        )

    last_raw = snapshot_entry.get("last")
    if last_raw is None or float(last_raw) <= 0.0:
        # snapshot 没 last 价或异常值 → 保守跳过（不阻断，避免 snapshot 格式
        # 缺陷误伤合法订单；真正的 PLAN_HALLUCINATION 由 policy_engine 处理）
        return StalePriceCheckResult(
            ok=True,
            reason_code=None,
            detail={
                "skipped": "snapshot_last_price_unavailable",
                "symbol": symbol,
                "snapshot_last": last_raw,
            },
        )

    last_price = float(last_raw)
    limit_price = float(limit_price_raw)
    threshold_bps = int(max_slippage_bps_raw)

    # 偏差用绝对值——买高 / 卖低都算偏离（不区分方向，与 max_slippage_bps
    # 的语义一致）。1 bp = 0.01%，所以 10000 倍。
    deviation_bps = abs(limit_price - last_price) / last_price * 10000.0

    if deviation_bps > threshold_bps:
        return StalePriceCheckResult(
            ok=False,
            reason_code="MARKET_SLIPPAGE_EXCEEDED",
            detail={
                "symbol": symbol,
                "limit_price": limit_price,
                "snapshot_last": last_price,
                "deviation_bps": deviation_bps,
                "threshold_bps": threshold_bps,
                "snapshot_as_of": as_of_dt.isoformat() if as_of_dt else None,
                "now": now.isoformat(),
            },
        )

    return StalePriceCheckResult(
        ok=True,
        reason_code=None,
        detail={
            "symbol": symbol,
            "limit_price": limit_price,
            "snapshot_last": last_price,
            "deviation_bps": deviation_bps,
            "threshold_bps": threshold_bps,
            "snapshot_as_of": as_of_dt.isoformat() if as_of_dt else None,
        },
    )


__all__ = [
    "DEFAULT_SNAPSHOT_FRESHNESS_SECONDS",
    "StalePriceCheckResult",
    "check_market_snapshot_freshness_and_slippage",
]
