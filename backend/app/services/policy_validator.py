"""Policy DSL v0 校验服务（任务 5.1 / Req 4）。

本模块把"原始 dict → 已校验的 :class:`PolicyDSLv0`"这一过程封装成单一入口
:func:`validate_policy_dsl`。校验分三层：

1. **JSON Schema 严格校验**（:data:`POLICY_DSL_V0_SCHEMA`）
   - 结构性约束、const、enum、pattern、minimum/maximum 全覆盖。
   - 每个 object 都 ``additionalProperties: false`` → Req 4 AC8 第一道闸。
2. **Pydantic 二次校验**（:class:`PolicyDSLv0`）
   - 兜底捕获 schema 漏写或类型擦除问题（如 number/integer 边界微差）。
   - ``extra='forbid'`` → Req 4 AC8 第二道闸；二者构成"深度防御"。
3. **业务规则**
   - withdraw 必须为 false（Req 4 AC2，即便 const 已校验也再断言一次）。
   - allowed_symbols 转小写归一化（Req 4 AC3）；返回的 PolicyDSLv0 内是小写。
   - allowed_time_utc 跨午夜（Req 4 AC4）只标注属性，不抛错（22:00→02:00 合法）。
   - allowed_time_utc 给了对象就必须同时给 start+end（PRD 未硬性 require，
     但缺一意义模糊）。
   - blocked_actions 中元素必须在 :data:`BLOCKED_ACTIONS_ENUM` 中；schema
     已枚举，这里防御性再判一次。

错误模型
--------
:class:`InvalidPolicyError` 继承 ``ValueError``，便于上层既能精确捕获也能
``except ValueError`` 兜底。``errors`` 字段是结构化错误列表，每条带 ``path``
（指向出错的字段路径，用 dotted/index 形式）+ ``message`` + ``validator``。
路由层（任务 5.3）会把它转成 HTTP 422 + ``code="POLICY_INVALID"``。

性能注记
--------
``Draft202012Validator`` 实例可被复用（线程安全、无状态）。这里用模块级
:func:`functools.lru_cache` 缓存一份 validator，避免每次 validate 都重新
解析 schema。jsonschema 库内部已经做了 schema reference 解析与编译。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError as PydanticValidationError

from app.schemas.policy import (
    BLOCKED_ACTIONS_ENUM,
    POLICY_DSL_V0_SCHEMA,
    PolicyDSLv0,
)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class InvalidPolicyError(ValueError):
    """Policy DSL v0 校验失败的统一异常类型。

    继承 ``ValueError`` 让上层既可精确捕获 ``InvalidPolicyError`` 也可
    宽松 ``except ValueError``。``errors`` 字段是结构化错误列表，
    给前端展示"哪些字段错了"用：

    每个 error item 形如::

        {
          "path": "limits.max_slippage_bps",   # dotted path; 数组用 [i]
          "message": "501 is greater than the maximum of 500",
          "validator": "maximum",              # jsonschema validator 名
        }

    Pydantic 二次校验阶段抛出的错误也会规范成同样 shape，便于上层统一处理。
    """

    def __init__(self, message: str, errors: list[dict[str, Any]] | None = None) -> None:
        self.errors: list[dict[str, Any]] = errors or []
        super().__init__(message)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _get_validator() -> Draft202012Validator:
    """模块级缓存的 jsonschema validator。

    Draft202012Validator 是线程安全的，可被多个并发请求共享。lru_cache 的好处：
    - 避免每次 validate 都重新编译 schema（schema 是常量字典，编译开销不小）。
    - 测试场景下也能命中同一份 validator，便于"修改 schema 字典"被测试热加载。

    Notes
    -----
    Draft202012 是 PRD §9.1 ``$schema`` 声明的版本；不允许使用 Draft7 等旧版
    避免约束语义差异（特别是 ``unevaluatedProperties`` 与 ``$dynamicRef``）。
    """
    Draft202012Validator.check_schema(POLICY_DSL_V0_SCHEMA)
    return Draft202012Validator(POLICY_DSL_V0_SCHEMA)


def _format_jsonschema_error(err: JsonSchemaValidationError) -> dict[str, Any]:
    """把单个 jsonschema ValidationError 规范成 dict。

    ``err.absolute_path`` 是 ``deque`` 形式的字段路径，含字符串 key 与整数
    数组下标；用 ``[i]`` 与 ``.`` 拼成 jq-friendly 的 dotted path。
    """
    parts: list[str] = []
    for p in err.absolute_path:
        if isinstance(p, int):
            parts.append(f"[{p}]")
        else:
            if parts:
                parts.append(f".{p}")
            else:
                parts.append(str(p))
    path = "".join(parts) if parts else "<root>"
    return {
        "path": path,
        "message": err.message,
        "validator": err.validator,
    }


def _format_pydantic_error(err: PydanticValidationError) -> list[dict[str, Any]]:
    """把 Pydantic ValidationError 规范成 errors 列表。

    Pydantic ``err.errors()`` 返回的每个 dict 已有 ``loc`` (元组) + ``msg`` +
    ``type``；映射到 InvalidPolicyError.errors 的 path/message/validator 三键。
    """
    items: list[dict[str, Any]] = []
    for item in err.errors():
        loc = item.get("loc", ())
        # loc 可能含 int (list 索引) 与 str (字段名)
        parts: list[str] = []
        for p in loc:
            if isinstance(p, int):
                parts.append(f"[{p}]")
            else:
                if parts:
                    parts.append(f".{p}")
                else:
                    parts.append(str(p))
        items.append(
            {
                "path": "".join(parts) if parts else "<root>",
                "message": item.get("msg", ""),
                "validator": item.get("type", "pydantic"),
            }
        )
    return items


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------
def normalize_symbol_list(symbols: list[str]) -> list[str]:
    """归一化 symbol 列表：strip + 小写 + 去重保序（Req 4 AC3）。

    设计要点
    --------
    - 保序：``["BTCUSDT", "btcusdt", "ETHUSDT"]`` → ``["btcusdt", "ethusdt"]``，
      首次出现的位置决定最终顺序，便于"用户在 UI 中拖拽排序的偏好"被保留。
    - 去空白：策略 JSON 经常被人手敲入，``" btcusdt "`` 这种粗心输入会因
      字符串比较失败导致 SYMBOL_NOT_ALLOWED 误报；这里主动 strip。
    - 全部小写：HTX 内部一律小写（design.md「HTX 适配器」「symbol 内部小写、
      UI 大写」），与策略比较时也在小写域。
    - 不抛错：空字符串项在 Pydantic 已被 ``min_length=1`` 拒掉；本函数仍会
      跳过 ``""`` 防御性兼容（不进入返回列表）。

    Parameters
    ----------
    symbols : list[str]
        原始 symbol 列表，元素可能含大写字母 / 前后空白 / 重复。

    Returns
    -------
    list[str]
        归一化后的有序去重列表，全部小写。

    Examples
    --------
    >>> normalize_symbol_list(["BTCUSDT", " ethusdt ", "BtcUsdt"])
    ['btcusdt', 'ethusdt']
    """
    seen: set[str] = set()
    result: list[str] = []
    for raw in symbols:
        norm = raw.strip().lower()
        if not norm:
            # Pydantic 应已拦下空字符串；这里再防御一道
            continue
        if norm in seen:
            continue
        seen.add(norm)
        result.append(norm)
    return result


def is_cross_midnight(start: str, end: str) -> bool:
    """判断 ``allowed_time_utc`` 是否为跨午夜区间（Req 4 AC4）。

    Parameters
    ----------
    start, end : str
        形如 ``"22:00"`` / ``"02:00"`` 的 HH:MM 字符串；调用方需保证两者
        都已通过 schema 的 pattern 校验。

    Returns
    -------
    bool
        ``True`` 表示 ``start > end``（跨午夜，如 22:00→02:00）；
        ``False`` 表示同日内（含 ``start == end`` 退化情况）。

    Notes
    -----
    Policy Engine（任务 8 ``is_within_time_window``）会用此函数决定窗口
    判定方向；本任务只提供工具不做判定本身。
    """
    return start > end


def validate_policy_dsl(
    raw: dict[str, Any], *, allow_unknown: bool = False
) -> PolicyDSLv0:
    """校验 Policy DSL v0 并返回强类型实例。

    流程
    ----
    1. **JSON Schema 校验**：用 :func:`_get_validator` 收集 *所有* 错误（不是
       fail-fast），让前端一次性看到全部问题。
       - 若 ``allow_unknown=True``：临时构造一份去掉 ``additionalProperties:
         false`` 的 schema 副本（仅顶层与一级子节生效），其它约束保持原状。
    2. **Pydantic 二次校验**：把 ``PolicyDSLv0(**raw)`` 当作"类型擦除后的
       第二道闸"。schema 通过但 Pydantic 失败的情况罕见，主要兜底 schema
       未来漏写。``allow_unknown=True`` 时 Pydantic 也会 ``extra='ignore'``。
    3. **业务规则**：
       a) ``capabilities.withdraw`` 必须为 ``False``（Req 4 AC2，防御性二次）。
       b) ``limits.allowed_symbols`` 用 :func:`normalize_symbol_list` 重写
          （Req 4 AC3）。
       c) ``limits.allowed_time_utc`` 给了对象就必须同时给 start+end。
       d) ``blocked_actions`` 中所有元素须在
          :data:`app.schemas.policy.BLOCKED_ACTIONS_ENUM` 中（schema 已校验，
          这里再断言）。

    Parameters
    ----------
    raw : dict[str, Any]
        原始策略字典（一般来自 HTTP body 或种子数据 JSON 文件）。
    allow_unknown : bool, default False
        是否允许未知字段。生产环境必须保持 ``False``（Req 4 AC8）；
        ``True`` 仅供"开发模式临时调通新字段"使用，调用方有责任只在
        ``settings.DEMO_MODE`` 等开发环境下传 ``True``。

    Returns
    -------
    PolicyDSLv0
        已校验且 ``allowed_symbols`` 已归一化的实例。可直接 ``model_dump()``
        入库或交给 Policy Engine。

    Raises
    ------
    InvalidPolicyError
        任一阶段失败；``errors`` 包含所有错误条目。
    """
    if not isinstance(raw, dict):
        raise InvalidPolicyError(
            "policy must be a JSON object",
            errors=[
                {
                    "path": "<root>",
                    "message": f"expected object, got {type(raw).__name__}",
                    "validator": "type",
                }
            ],
        )

    # ---- 1. JSON Schema 阶段 ----
    if allow_unknown:
        # 临时副本：去掉所有层级的 additionalProperties: false，让"开发模式"
        # 接受未知字段。这里只关心"未知字段"被放行，其它约束（const/enum/
        # pattern/minimum/maximum/required）依然生效。
        schema = _strip_additional_properties_false(POLICY_DSL_V0_SCHEMA)
        validator = Draft202012Validator(schema)
    else:
        validator = _get_validator()

    schema_errors = sorted(validator.iter_errors(raw), key=lambda e: list(e.absolute_path))
    if schema_errors:
        formatted = [_format_jsonschema_error(e) for e in schema_errors]
        raise InvalidPolicyError(
            f"policy DSL v0 schema validation failed with {len(formatted)} error(s)",
            errors=formatted,
        )

    # ---- 2. Pydantic 二次校验 ----
    # allow_unknown 下临时把 extra 设为 ignore 是不可行的（ConfigDict 在类定义时
    # 固化）；此时直接绕过 Pydantic（schema 已是宽松版），把 raw 转成 dict 后续业务
    # 校验自行处理。否则严格走 Pydantic。
    if allow_unknown:
        # 仅基于 raw dict 提取已知字段构造 PolicyDSLv0 —— 未知字段在 schema 阶段
        # 已被放行，这里要保留它们到结果对象之外（model_dump 不含未知字段，
        # 这是允许的：未知字段不参与业务决策）。
        known: dict[str, Any] = {
            k: raw[k]
            for k in (
                "version",
                "capabilities",
                "limits",
                "approval",
                "blocked_actions",
            )
            if k in raw
        }
        # 子节内部的"未知字段"也要剥掉，避免 Pydantic extra=forbid 报错。
        known = _project_known_fields(known)
        try:
            policy = PolicyDSLv0(**known)
        except PydanticValidationError as err:
            raise InvalidPolicyError(
                "policy pydantic validation failed",
                errors=_format_pydantic_error(err),
            ) from err
    else:
        try:
            policy = PolicyDSLv0(**raw)
        except PydanticValidationError as err:
            raise InvalidPolicyError(
                "policy pydantic validation failed",
                errors=_format_pydantic_error(err),
            ) from err

    # ---- 3. 业务规则 ----
    business_errors: list[dict[str, Any]] = []

    # 3a. withdraw 必须 False（Req 4 AC2，防御性 — 即便 const 已通过）
    if policy.capabilities.withdraw is not False:
        business_errors.append(
            {
                "path": "capabilities.withdraw",
                "message": "withdraw must be false in MVP",
                "validator": "withdraw_const",
            }
        )

    # 3b. allowed_symbols 归一化（Req 4 AC3）
    policy.limits.allowed_symbols = normalize_symbol_list(policy.limits.allowed_symbols)
    # 归一化之后再次确认非空（normalize 会跳过空字符串，理论上 Pydantic 已拦）
    if not policy.limits.allowed_symbols:
        business_errors.append(
            {
                "path": "limits.allowed_symbols",
                "message": "allowed_symbols cannot be empty after normalization",
                "validator": "minItems",
            }
        )

    # 3c. allowed_time_utc 完整性
    if policy.limits.allowed_time_utc is not None:
        atu = policy.limits.allowed_time_utc
        if atu.start is None or atu.end is None:
            business_errors.append(
                {
                    "path": "limits.allowed_time_utc",
                    "message": "allowed_time_utc must contain both start and end",
                    "validator": "required",
                }
            )
        # 跨午夜不算错，仅作为属性可在外部通过 is_cross_midnight 查询。

    # 3d. blocked_actions enum 防御
    for idx, item in enumerate(policy.blocked_actions):
        if item not in BLOCKED_ACTIONS_ENUM:
            business_errors.append(
                {
                    "path": f"blocked_actions[{idx}]",
                    "message": f"{item!r} is not a valid blocked action",
                    "validator": "enum",
                }
            )

    if business_errors:
        raise InvalidPolicyError(
            f"policy business-rule validation failed with {len(business_errors)} error(s)",
            errors=business_errors,
        )

    return policy


# ---------------------------------------------------------------------------
# 内部：allow_unknown 模式辅助
# ---------------------------------------------------------------------------
def _strip_additional_properties_false(value: Any) -> Any:
    """递归克隆 schema 并移除所有 ``additionalProperties: false``。

    "开发模式接受未知字段"只对 object 节点生效；array items 等不受影响。
    返回新结构（深拷贝），不修改原 schema 常量。

    返回类型以 ``Any`` 标注：递归遍历会同时处理 dict / list / 标量，
    类型在递归边界上变化频繁，统一 ``Any`` 可避免大量 cast。
    """
    if isinstance(value, dict):
        new: dict[str, Any] = {}
        for k, v in value.items():
            if k == "additionalProperties" and v is False:
                continue
            new[k] = _strip_additional_properties_false(v)
        return new
    if isinstance(value, list):
        return [_strip_additional_properties_false(item) for item in value]
    return value


# 已知子节里的字段白名单——只用于 allow_unknown=True 路径，避免 Pydantic
# extra='forbid' 报错。维护成本低：每次往 schema 加新字段时同步追加到这里。
_KNOWN_FIELDS: dict[str, set[str]] = {
    "capabilities": {
        "read_market",
        "read_account",
        "place_order",
        "cancel_order",
        "withdraw",
    },
    "limits": {
        "allowed_symbols",
        "max_notional_usdt_per_order",
        "max_daily_notional_usdt",
        "max_orders_per_day",
        "allowed_order_types",
        "max_slippage_bps",
        "allowed_time_utc",
    },
    "limits.allowed_time_utc": {"start", "end"},
    "approval": {
        "required_for_trade",
        "required_for_policy_change",
        "expires_after_seconds",
    },
}


def _project_known_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """allow_unknown=True 下把 raw 投影到"仅含已知字段"的 dict。

    用于 Pydantic 解析阶段——schema 阶段已宽松接受未知字段，但 Pydantic
    模型仍是 ``extra='forbid'``；为避免无谓的 ValidationError，主动投影。
    """
    out: dict[str, Any] = {}
    for top_key in ("version", "capabilities", "limits", "approval", "blocked_actions"):
        if top_key not in raw:
            continue
        value = raw[top_key]
        if top_key == "capabilities" and isinstance(value, dict):
            out[top_key] = {
                k: v for k, v in value.items() if k in _KNOWN_FIELDS["capabilities"]
            }
        elif top_key == "limits" and isinstance(value, dict):
            limits_proj = {
                k: v for k, v in value.items() if k in _KNOWN_FIELDS["limits"]
            }
            atu = limits_proj.get("allowed_time_utc")
            if isinstance(atu, dict):
                limits_proj["allowed_time_utc"] = {
                    k: v
                    for k, v in atu.items()
                    if k in _KNOWN_FIELDS["limits.allowed_time_utc"]
                }
            out[top_key] = limits_proj
        elif top_key == "approval" and isinstance(value, dict):
            out[top_key] = {k: v for k, v in value.items() if k in _KNOWN_FIELDS["approval"]}
        else:
            out[top_key] = value
    return out


__all__ = [
    "InvalidPolicyError",
    "is_cross_midnight",
    "normalize_symbol_list",
    "validate_policy_dsl",
]
