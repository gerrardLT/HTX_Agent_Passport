"""Cedar 影子裁决器（修复 G1 / Phase 2 PoC）。

把 PolicyDSL v0 的核心裁决重写为 Cedar 策略，作为 :func:`evaluate_policy`
的影子评估器：在主裁决路径之外并行跑一遍，差异只写日志不影响业务路径。
跑 30 天 0 差异后再考虑替换主裁决器。

设计依据
--------
``docs/tech-research/06-...md`` §6.1 + 多源交叉验证（AWS Cedar 论文 / Natoma
对比 / Microsoft Agent Governance Toolkit）。Cedar 优势：

1. **形式化分析**：``cedarpy.validate_policies`` + Cedar Analysis 可证
   "策略 A 蕴含策略 B"，让"策略变更不破坏现有授权"成为可验证陈述。
2. **强类型**：schema 校验在策略部署时即捕获 typo / 类型失配。
3. **故意不支持正则 / HTTP 调用**：与"零信任 PEP 不应有动态外部调用"
   的安全愿景吻合。

为何只是 PoC
-----------
- Cedar 无法表达"daily_history 累加 + 动态时间窗口"的全部 Step 5/6/7 检查
  （Cedar 是策略语言，不是计算语言）；这些 check 仍由 Python 主路径处理,
  Cedar 只覆盖 Step 1-3（blocked_actions / capabilities / allowed_symbols）。
- 30 天观察期：通过 ``cedar_shadow_log_difference`` 记录 main vs Cedar 的
  verdict 差异；任何差异都是"主路径 bug"或"Cedar 翻译 bug"的信号。
- 30 天 0 差异后才考虑切换主路径——切换时仍保留双跑窗口。

目前覆盖的检查
--------------
✅ Step 0  kill switch（global_config.demo_disable_execution）
✅ Step 1  blocked_actions
✅ Step 2  capabilities
✅ Step 3  allowed_symbols
❌ Step 4-7 / 反幻觉 / G2 provenance（保留给主裁决器；Cedar 不评估这些）

差异语义
--------
Cedar 决定 ``Allow / Deny``,主路径决定 ``ALLOW / REQUIRE_APPROVAL / REJECT``。
等价映射：

- 主路径 ``REJECT`` → Cedar 应当 ``Deny``（任一 forbid 触发）。
- 主路径 ``ALLOW`` / ``REQUIRE_APPROVAL`` → Cedar 应当 ``Allow``。

不一致时记 ``CEDAR_SHADOW_DIVERGENCE`` 日志,便于 30 天观察统计。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final, Literal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CedarShadowResult:
    """Cedar 影子评估结果。

    Attributes
    ----------
    decision : Literal["Allow", "Deny", "Skipped"]
        Cedar 引擎决定。``"Skipped"`` 表示 Cedar 未运行（cedarpy 未安装 /
        action_type 不在 PoC 范围 / 配置关闭）。
    reasons : tuple[str, ...]
        Cedar diagnostics.reasons 中触发的 policy id（仅在 Allow/Deny 时填充）。
    error : str | None
        Cedar 引擎抛出的错误（如 schema 不匹配）；None 表示正常完成。
    """

    decision: Literal["Allow", "Deny", "Skipped"]
    reasons: tuple[str, ...] = ()
    error: str | None = None


# ---------------------------------------------------------------------------
# Cedar Schema 与策略（与 PolicyDSL v0 Step 0-3 对齐）
# ---------------------------------------------------------------------------
# Schema：实体 + actions + 上下文。
#
# 设计要点：
# - 每个 action_type（read_market / read_account / place_order / cancel_order）
#   声明为独立的 ``Action`` 实体；Cedar 的 ``action`` 概念天然映射 PolicyDSL 的
#   ``capabilities`` 字段名。
# - ``Passport`` 实体携带 ``capabilities`` / ``blocked_action_types`` / ``allowed_symbols``
#   作为属性,let 策略读取。
# - ``GlobalEnv`` 单例携带 ``kill_switch``,policy 依此 forbid 写操作。
# - context 携带 ``symbol``（小写）,policy 用以做 allowed_symbols 检查。

CEDAR_SCHEMA_JSON: Final[dict[str, Any]] = {
    "HTX": {
        "entityTypes": {
            "User": {"shape": {"type": "Record", "attributes": {}}},
            "Passport": {
                "shape": {
                    "type": "Record",
                    "attributes": {
                        # 5 个 capability 开关
                        "cap_read_market": {"type": "Boolean"},
                        "cap_read_account": {"type": "Boolean"},
                        "cap_place_order": {"type": "Boolean"},
                        "cap_cancel_order": {"type": "Boolean"},
                        # blocked_actions 元素列表（小写 enum 字符串）
                        "blocked_action_types": {
                            "type": "Set",
                            "element": {"type": "String"},
                        },
                        "allowed_symbols": {
                            "type": "Set",
                            "element": {"type": "String"},
                        },
                    },
                }
            },
            "GlobalEnv": {
                "shape": {
                    "type": "Record",
                    "attributes": {"kill_switch": {"type": "Boolean"}},
                }
            },
        },
        "actions": {
            "read_market": {
                "appliesTo": {
                    "principalTypes": ["User"],
                    "resourceTypes": ["Passport"],
                    "context": {
                        "type": "Record",
                        "attributes": {"symbol": {"type": "String"}},
                    },
                }
            },
            "read_account": {
                "appliesTo": {
                    "principalTypes": ["User"],
                    "resourceTypes": ["Passport"],
                    "context": {
                        "type": "Record",
                        "attributes": {"symbol": {"type": "String"}},
                    },
                }
            },
            "place_order": {
                "appliesTo": {
                    "principalTypes": ["User"],
                    "resourceTypes": ["Passport"],
                    "context": {
                        "type": "Record",
                        "attributes": {"symbol": {"type": "String"}},
                    },
                }
            },
            "cancel_order": {
                "appliesTo": {
                    "principalTypes": ["User"],
                    "resourceTypes": ["Passport"],
                    "context": {
                        "type": "Record",
                        "attributes": {"symbol": {"type": "String"}},
                    },
                }
            },
        },
    }
}


# ---------------------------------------------------------------------------
# Cedar 策略（与 PolicyDSL v0 Step 0-3 一一对应）
# ---------------------------------------------------------------------------
# Cedar 默认拒绝（implicit deny）；写一条全局 permit 让符合规则的 action 通过,
# 然后用 forbid 拦截违规。
#
# 顺序：forbid 优先于 permit（Cedar 标准语义）—— 任何 forbid 触发即 Deny,
# 无论 permit 是否同时匹配。

CEDAR_POLICIES: Final[str] = """
// === Step 0: kill switch ===
// 写操作（place_order / cancel_order）在 kill_switch=true 时拒。
// read_market / read_account 不受 kill_switch 影响（与 PolicyDSL 一致）。
forbid (
    principal,
    action in [HTX::Action::"place_order", HTX::Action::"cancel_order"],
    resource
)
when {
    HTX::GlobalEnv::"global".kill_switch
};

// === Step 1: blocked_actions ===
// 当 action_type 在 passport.blocked_action_types 中时拒。
// PolicyDSL 的 enum 包含 withdraw/borrow/margin/transfer_out/unknown_tool_call,
// ActionPlan schema 实际只允许 read_*/place_/cancel_/no_op；本检查是深度防御。
//
// 注意：Cedar action_type 是 "place_order" 等枚举字符串；blocked_action_types
// 中的元素也是字符串。我们把 action 名嵌入 context.action_type_str 字段。
//
// （为简化 schema，此规则当前只覆盖 place_order / cancel_order——
// 与主裁决器 _make_reject 的 BLOCKED_ACTION_PLACE_ORDER / CANCEL_ORDER 一致。）
forbid (
    principal,
    action == HTX::Action::"place_order",
    resource
)
when {
    resource.blocked_action_types.contains("place_order")
};

forbid (
    principal,
    action == HTX::Action::"cancel_order",
    resource
)
when {
    resource.blocked_action_types.contains("cancel_order")
};

// === Step 2: capabilities ===
// 4 个 forbid 各自检查对应 capability 是否未授予。
forbid (
    principal,
    action == HTX::Action::"read_market",
    resource
)
unless { resource.cap_read_market };

forbid (
    principal,
    action == HTX::Action::"read_account",
    resource
)
unless { resource.cap_read_account };

forbid (
    principal,
    action == HTX::Action::"place_order",
    resource
)
unless { resource.cap_place_order };

forbid (
    principal,
    action == HTX::Action::"cancel_order",
    resource
)
unless { resource.cap_cancel_order };

// === Step 3: allowed_symbols（仅对 place_order / cancel_order）===
// read_market / read_account 也带 symbol，但 PolicyDSL Step 3 对所有
// 带 symbol 的 action 都校验——这里同样覆盖。
forbid (
    principal,
    action,
    resource
)
when {
    !resource.allowed_symbols.contains(context.symbol)
};

// === 默认 permit ===
// 通过所有 forbid → Allow。Cedar 的 default deny 不会自动放行,需要一条
// permit 兜底。
permit (principal, action, resource);
"""


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def shadow_evaluate(
    *,
    action: dict[str, Any],
    policy: dict[str, Any],
    kill_switch: bool = False,
) -> CedarShadowResult:
    """运行 Cedar 影子评估（覆盖 PolicyDSL Step 0-3）。

    Parameters
    ----------
    action : dict[str, Any]
        ActionPlan v0 单 action dict（type / symbol / 等）。
    policy : dict[str, Any]
        Passport.policy_json（与 evaluate_policy 同款）。
    kill_switch : bool
        全局 kill switch 开关（``global_config.demo_disable_execution``）。

    Returns
    -------
    CedarShadowResult
        ``decision ∈ {Allow, Deny, Skipped}``。
        Skipped 表示 Cedar 未运行（cedarpy 缺失 / 不在 PoC 范围 / 错误吞掉）。

    Notes
    -----
    本函数**不抛异常**——任何 Cedar 引擎错误都吞为 Skipped + error 字段。
    主路径绝不依赖本函数的结果，只用差异统计驱动持续改进。
    """
    try:
        import cedarpy
    except ImportError:
        return CedarShadowResult(
            decision="Skipped",
            error="cedarpy not installed",
        )

    action_type = action.get("type")
    if action_type == "no_op":
        # PoC 范围不覆盖 no_op：主路径 always ALLOW，Cedar 也 always Allow。
        return CedarShadowResult(decision="Allow", reasons=())

    if action_type not in {"read_market", "read_account", "place_order", "cancel_order"}:
        return CedarShadowResult(
            decision="Skipped",
            error=f"action_type {action_type!r} out of PoC scope",
        )

    symbol = (action.get("symbol") or "").lower()

    # 构造 entities
    capabilities: dict[str, Any] = policy.get("capabilities", {})
    limits: dict[str, Any] = policy.get("limits", {})
    blocked: list[str] = list(policy.get("blocked_actions", []))
    allowed_symbols: list[str] = [
        s.lower() for s in limits.get("allowed_symbols", []) if isinstance(s, str)
    ]

    entities: list[dict[str, Any]] = [
        {
            "uid": {"type": "HTX::User", "id": "u"},
            "attrs": {},
            "parents": [],
        },
        {
            "uid": {"type": "HTX::Passport", "id": "p"},
            "attrs": {
                "cap_read_market": bool(capabilities.get("read_market", False)),
                "cap_read_account": bool(capabilities.get("read_account", False)),
                "cap_place_order": bool(capabilities.get("place_order", False)),
                "cap_cancel_order": bool(capabilities.get("cancel_order", False)),
                "blocked_action_types": blocked,
                "allowed_symbols": allowed_symbols,
            },
            "parents": [],
        },
        {
            "uid": {"type": "HTX::GlobalEnv", "id": "global"},
            "attrs": {"kill_switch": bool(kill_switch)},
            "parents": [],
        },
    ]

    request = {
        "principal": 'HTX::User::"u"',
        "action": f'HTX::Action::"{action_type}"',
        "resource": 'HTX::Passport::"p"',
        "context": {"symbol": symbol},
    }

    try:
        result = cedarpy.is_authorized(
            request=request,
            policies=CEDAR_POLICIES,
            entities=entities,
            schema=CEDAR_SCHEMA_JSON,
        )
    except Exception as exc:  # noqa: BLE001 — Cedar 错误吞 + 报告
        return CedarShadowResult(
            decision="Skipped",
            error=f"cedarpy error: {exc}",
        )

    # decision 是 cedarpy.Decision 枚举；str() 返回 "Decision.Allow" / "Decision.Deny"
    decision_str = str(result.decision).split(".")[-1]
    reasons = tuple(getattr(result.diagnostics, "reason", []) or [])
    if decision_str == "Allow":
        return CedarShadowResult(decision="Allow", reasons=reasons)
    if decision_str == "Deny":
        return CedarShadowResult(decision="Deny", reasons=reasons)
    return CedarShadowResult(
        decision="Skipped",
        error=f"unexpected Cedar decision: {decision_str}",
    )


# ---------------------------------------------------------------------------
# 差异比对
# ---------------------------------------------------------------------------
def cedar_decision_matches_main(
    *,
    cedar_decision: Literal["Allow", "Deny", "Skipped"],
    main_verdict: Literal["ALLOW", "REQUIRE_APPROVAL", "REJECT"],
    main_reason_codes: tuple[str, ...],
) -> bool:
    """判断 Cedar shadow 决定与主路径裁决是否一致（在 PoC 覆盖的范围内）。

    映射规则
    --------
    - Cedar ``Skipped`` → 一律 True（Cedar 不参与判定）
    - Cedar ``Allow``   → 主路径必须是 ALLOW 或 REQUIRE_APPROVAL
                            （Cedar 不区分这两个；REQUIRE_APPROVAL 在 Cedar
                             层是 Allow——审批是上层调度的语义）。
    - Cedar ``Deny``    → 主路径必须是 REJECT,且 reason_code 应当是 Cedar
                            覆盖范围内的（EXECUTION_DISABLED / BLOCKED_ACTION_* /
                            CAPABILITY_NOT_GRANTED / SYMBOL_NOT_ALLOWED）。

    若 Cedar Deny 但主路径 REJECT 用了 Cedar 范围外的 reason（如
    LIMIT_MAX_NOTIONAL_EXCEEDED）——视为不匹配（Cedar 不应在那种场景 Deny）。
    """
    if cedar_decision == "Skipped":
        return True

    cedar_covers = {
        "EXECUTION_DISABLED",
        "BLOCKED_ACTION_PLACE_ORDER",
        "BLOCKED_ACTION_CANCEL_ORDER",
        "BLOCKED_ACTION_UNKNOWN_TOOL_CALL",
        "BLOCKED_ACTION_WITHDRAW",
        "BLOCKED_ACTION_BORROW",
        "BLOCKED_ACTION_MARGIN",
        "BLOCKED_ACTION_TRANSFER_OUT",
        "CAPABILITY_NOT_GRANTED",
        "SYMBOL_NOT_ALLOWED",
    }
    main_in_cedar_scope = bool(set(main_reason_codes) & cedar_covers)

    if cedar_decision == "Allow":
        # Cedar 放行 → 主路径不应在 Cedar 覆盖范围内拒绝
        if main_verdict == "REJECT" and main_in_cedar_scope:
            return False
        return True

    if cedar_decision == "Deny":
        if main_verdict != "REJECT":
            return False
        return main_in_cedar_scope

    return True  # 未知值兜底


def log_cedar_shadow_difference(
    *,
    cedar_result: CedarShadowResult,
    main_verdict: str,
    main_reason_codes: tuple[str, ...],
    action: dict[str, Any],
) -> None:
    """有差异时记日志（专用 logger / 30 天观察期统计依据）。

    日志结构化便于后续 grep / metrics 聚合：

    - ``CEDAR_SHADOW_DIVERGENCE``：决定不一致（Cedar Allow 主 REJECT 等）
    - ``CEDAR_SHADOW_ERROR``：Cedar 引擎抛错（schema/policy 翻译 bug）
    - ``CEDAR_SHADOW_MATCH``（仅 DEBUG 级别）：决定一致

    生产部署可把这些日志接入 metrics 系统（Prometheus counter）做自动监控。
    """
    if cedar_result.error:
        logger.warning(
            "CEDAR_SHADOW_ERROR action_type=%s error=%s",
            action.get("type"), cedar_result.error,
        )
        return

    if not cedar_decision_matches_main(
        cedar_decision=cedar_result.decision,
        main_verdict=main_verdict,  # type: ignore[arg-type]
        main_reason_codes=main_reason_codes,
    ):
        logger.warning(
            "CEDAR_SHADOW_DIVERGENCE cedar=%s main=%s codes=%s symbol=%s type=%s",
            cedar_result.decision,
            main_verdict,
            main_reason_codes,
            action.get("symbol"),
            action.get("type"),
        )
    else:
        logger.debug(
            "CEDAR_SHADOW_MATCH cedar=%s main=%s",
            cedar_result.decision, main_verdict,
        )


__all__ = [
    "CEDAR_POLICIES",
    "CEDAR_SCHEMA_JSON",
    "CedarShadowResult",
    "cedar_decision_matches_main",
    "log_cedar_shadow_difference",
    "shadow_evaluate",
]
