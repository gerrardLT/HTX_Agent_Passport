"""审计哈希链：canonical_json + sha256（任务 7.1 / Req 11 AC1-3）。

本模块提供 **审计事件哈希计算的最小确定性内核**：

1. :func:`canonical_json` —— 把 ``event_json`` 序列化成跨语言、跨平台稳定的字节流；
2. :func:`compute_event_hash` —— 在该字节流之上拼接 ``previous_hash`` 与
   ``created_at_iso``，做 SHA-256 摘要；
3. :data:`GENESIS_HASH_DEFAULT` —— 链首事件的 ``previous_event_hash`` 占位常量；
4. :func:`get_genesis_hash` —— 从 ``settings.GENESIS_HASH`` 读取实际值，
   方便部署期通过环境变量替换（与 ``audit_stub`` 行为一致）。

任务 7.2 的 ``AuditWriter`` / ``verify_chain_integrity`` 都直接构建在这四件套之上，
本模块严格 **不依赖数据库 / 网络 / 模型**，是 L1 单元测试的目标。

设计依据
--------
- requirements.md Req 11 AC1：``event_hash = sha256(canonical_json(event_json) + previous_hash + created_at_iso)``。
- requirements.md Req 11 AC2：链首事件 ``previous_hash = GENESIS_HASH`` 常量。
- requirements.md Req 11 AC3：canonical_json 必须满足
    * key 字典序（递归到嵌套 dict）
    * UTF-8 编码（``ensure_ascii=False``，中文 / emoji 直接保留）
    * 无多余空白（``separators=(',', ':')``）
    * 数字使用固定精度（小数点后最多 8 位、无尾零、无科学计数法）
- design.md「反馈层：审计写入器 + 哈希链计算器」一节给出的参考实现。

跨语言稳定性约定
----------------
- ``Decimal`` 在 canonical_json 中**序列化为 JSON 字符串**（如 ``"100.5"``）而不是 JSON 数字。
  这是为了避免不同语言在浮点精度 / 格式化上的差异；接收方需按字符串解析为 Decimal。
  这一约定写入了 :func:`_decimal_to_canonical_string` 的 docstring，
  并通过单元测试 ``test_known_test_vector`` 锁定为「跨语言金标准」。
- ``set`` / ``frozenset`` 序列化为升序列表（确定性）。
- ``datetime`` / ``date`` / 任何带 ``isoformat()`` 的对象按 ISO 8601 字符串输出。
- ``NaN`` / ``+Infinity`` / ``-Infinity`` **被禁止**（``allow_nan=False``），
  与 PostgreSQL JSONB 保持一致；遇到时 ``json.dumps`` 抛 ``ValueError``。

未覆盖类型（``bytes`` / 自定义对象等）会被 ``json.dumps`` 抛 ``TypeError``——
这是有意为之，强迫上层显式转换为 base64 / hex 等可序列化形式后再写入审计事件，
避免出现"看似工作但跨语言不一致"的隐式行为。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

from app.core.config import get_settings

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
#: 链首事件的默认 ``previous_event_hash`` 占位字符串。
#:
#: 与 ``settings.GENESIS_HASH`` 的默认值保持同步，在 ``settings`` 不可用时
#: （例如纯单元测试场景仅 import 本模块）作为兜底值。生产/集成测试中应
#: 通过 :func:`get_genesis_hash` 读取，让运维可以通过环境变量切链。
GENESIS_HASH_DEFAULT: Final[str] = "HTX_AGENT_PASSPORT_GENESIS_V1"

#: 数字精度上限（小数点后位数）。Req 11 AC3 要求 ≤ 8。
_MAX_DECIMAL_PLACES: Final[int] = 8


# ---------------------------------------------------------------------------
# Decimal → 规范化字符串
# ---------------------------------------------------------------------------
def _decimal_to_canonical_string(value: Decimal) -> str:
    """将 ``Decimal`` 转为跨平台稳定的字符串（无科学计数法、无尾零、最多 8 位小数）。

    规则
    ----
    - 非有限值（``NaN`` / ``Infinity``）→ 抛 ``ValueError``。
    - 始终使用 fixed-point 格式（``format(value, 'f')``），杜绝 ``1E+2`` 这类
      跨语言不一致的输出。
    - 若小数部分超过 8 位，使用 ``ROUND_HALF_EVEN`` 量化到 8 位（与多数金融
      场景的"银行家舍入"约定一致）。
    - 移除小数点后的尾零；如果整个小数部分被移除，连小数点一起去掉。
    - ``-0`` / ``-0.000`` 等负零表示统一规范化为 ``"0"``，
      与 IEEE 754 / JSON 数字的相等性约定一致。

    Examples
    --------
    >>> _decimal_to_canonical_string(Decimal('100.50'))
    '100.5'
    >>> _decimal_to_canonical_string(Decimal('100'))
    '100'
    >>> _decimal_to_canonical_string(Decimal('0.000001'))
    '0.000001'
    >>> _decimal_to_canonical_string(Decimal('-0.0'))
    '0'
    >>> _decimal_to_canonical_string(Decimal('1.123456789'))  # 9 位 → 量化到 8 位
    '1.12345679'
    """
    if not value.is_finite():
        raise ValueError(
            f"non-finite Decimal {value!s} cannot be canonical-serialized"
        )

    # 量化到 8 位小数（仅当源精度 > 8 位时；保留原始小数位数信息以便
    # ``Decimal('100.50')`` 仍能被进一步 rstrip 成 '100.5'）。
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -_MAX_DECIMAL_PLACES:
        # ``quantize`` 默认使用当前 context 的 rounding；显式传 ROUND_HALF_EVEN
        # 让结果不依赖外部 context 设置。
        from decimal import ROUND_HALF_EVEN

        value = value.quantize(
            Decimal((0, (1,), -_MAX_DECIMAL_PLACES)),
            rounding=ROUND_HALF_EVEN,
        )

    # fixed-point 字符串（无科学计数法）
    s = format(value, "f")

    # 去掉小数部分尾零；若小数部分被全部去掉，连同小数点一起去掉
    if "." in s:
        s = s.rstrip("0").rstrip(".")

    # 处理 ``-0`` / 空串极端情况
    if s in ("", "-", "-0"):
        s = "0"

    return s


# ---------------------------------------------------------------------------
# json.dumps 的 default 回调
# ---------------------------------------------------------------------------
def _canonical_default(obj: Any) -> Any:
    """``json.dumps`` 默认序列化器：处理 stdlib JSON 不识别的类型。

    支持的类型
    ----------
    - ``Decimal``      → 经 :func:`_decimal_to_canonical_string` 转为字符串
                         （**注意**：在 JSON 中以字符串形式呈现，不是 JSON 数字）。
    - ``set`` / ``frozenset`` → 转为按字典序升序排列的列表，避免迭代顺序不稳定。
    - ``datetime`` / ``date`` → 调用 ``isoformat()`` 输出 ISO 8601 字符串。

    其他类型一律抛 ``TypeError``，强迫上层显式转换；这样可以从一开始就
    阻断"靠 ``__str__`` 兜底导致跨语言不一致"的隐式行为。
    """
    if isinstance(obj, Decimal):
        return _decimal_to_canonical_string(obj)
    if isinstance(obj, set | frozenset):
        # 排序需要可比较的元素；元素类型混杂时让 ``sorted`` 抛 TypeError，
        # 让上层显式转换。
        return sorted(obj)
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    raise TypeError(
        f"Object of type {type(obj).__name__} is not JSON serializable"
    )


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------
def canonical_json(obj: Any) -> str:
    """规范化 JSON 序列化（Req 11 AC3）。

    Parameters
    ----------
    obj : Any
        通常是 ``dict``，也允许 ``list`` / 标量；任何 :func:`_canonical_default`
        无法处理的类型会触发 ``TypeError``。

    Returns
    -------
    str
        规范化后的 JSON 字符串：
        - key 在所有层级按字典序升序排列（``sort_keys=True``，``json`` stdlib
          递归实现，因此嵌套 dict 也排序）；
        - 无多余空白（``separators=(',', ':')``）；
        - UTF-8 文本字符直接保留（``ensure_ascii=False``，中文 / emoji 不变成 ``\\uXXXX``）；
        - 不允许 ``NaN`` / ``Infinity`` / ``-Infinity``（``allow_nan=False``，
          遇到时 ``json.dumps`` 抛 ``ValueError``）。

    Notes
    -----
    - 列表元素**保留输入顺序**——只对 dict 的 key 排序，符合 JSON 语义
      （列表是有序集合，dict 是无序集合）。
    - 整数输出无 ``.0`` 后缀（``42`` 而非 ``42.0``）；浮点数沿用 Python 的
      ``repr``（如 ``1.5``、``2.0``），跨平台一致。
    """
    return json.dumps(
        obj,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_canonical_default,
        allow_nan=False,
    )


def compute_event_hash(
    event_json: dict[str, Any],
    previous_hash: str,
    created_at_iso: str,
) -> str:
    """计算审计事件哈希（Req 11 AC1）。

    ``event_hash = sha256(canonical_json(event_json) + previous_hash + created_at_iso)``

    Parameters
    ----------
    event_json : dict[str, Any]
        待哈希的事件负载。会先经过 :func:`canonical_json` 规范化。
    previous_hash : str
        前序事件的 ``event_hash``；链首事件传 :func:`get_genesis_hash` 返回值。
    created_at_iso : str
        事件创建时间的 ISO 8601 字符串（含时区，例如
        ``"2026-05-30T00:00:00+00:00"``）。调用方负责保证格式稳定，
        本函数不再做规整。

    Returns
    -------
    str
        64 个小写十六进制字符的 SHA-256 摘要。

    Notes
    -----
    - 拼接顺序固定为 ``canonical_json + previous_hash + created_at_iso``，
      与 design.md 与 PRD §14 完全对齐；任何改动都会破坏所有既有审计事件
      的链验证，等价于审计 schema 的 breaking change。
    - 整个输入串以 UTF-8 编码后送入 SHA-256；这与
      :func:`canonical_json` 的 ``ensure_ascii=False`` 一致——
      非 ASCII 字符以原始 UTF-8 字节参与哈希，跨语言时只要按 UTF-8 编码就能复算。
    """
    payload = canonical_json(event_json) + previous_hash + created_at_iso
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_genesis_hash() -> str:
    """读取 ``settings.GENESIS_HASH``；缺失时回退到 :data:`GENESIS_HASH_DEFAULT`。

    把"实际链首哈希"通过环境变量 / 配置可替换是为了支持以下场景：

    - 同一二进制部署到 staging / prod 环境时使用不同的链首常量，避免事件
      在环境间被误链；
    - 演示场景下（Req 25）保持稳定的 ``HTX_AGENT_PASSPORT_GENESIS_V1`` 值。
    """
    settings = get_settings()
    return settings.GENESIS_HASH or GENESIS_HASH_DEFAULT


__all__ = [
    "GENESIS_HASH_DEFAULT",
    "canonical_json",
    "compute_event_hash",
    "get_genesis_hash",
]
