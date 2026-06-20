"""任务 7.1 审计哈希链的单元测试 + PBT。

覆盖维度（对应任务 7.1 验收点 / Req 11 AC1-3）
---------------------------------------------
1. canonical_json 基础语义：
   - 简单 dict 序列化、key 字典序（含嵌套）、无空白、UTF-8 直出、
     空 dict、列表顺序保留、禁止 NaN/Infinity。
2. canonical_json 数字精度：
   - int / float 输出、Decimal 去尾零、Decimal('0')、小数 / 负数边界。
3. canonical_json 跨平台稳定性 (PBT)：
   - 任意 dict round-trip、key 顺序无关、Unicode 文本稳定。
4. compute_event_hash 行为：
   - 相同输入恒返同 hash、event/prev/timestamp 任一变化都改变 hash、
     格式必须是 64 个小写 hex、genesis 链首事件可计算、
     PBT 验证不同事件产生不同哈希（碰撞抗性）。
5. **跨语言一致性回归 vector**：
   - 固定 ``(event_json, previous_hash, created_at_iso)`` → 期望 hash，
     断言永远等于这个值（跨平台 / 跨语言金标准）。
6. ``GENESIS_HASH_DEFAULT`` / ``get_genesis_hash`` 与 ``settings.GENESIS_HASH`` 对齐。

PBT 测试均标记 ``@pytest.mark.pbt``（与 ``test_vault.py`` 一致），
所有用例**不依赖数据库 / 网络 / 文件系统**，纯函数测试。
"""

from __future__ import annotations

import json
import math
import re
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.audit_chain import (
    GENESIS_HASH_DEFAULT,
    canonical_json,
    compute_event_hash,
    get_genesis_hash,
)
from app.core.config import get_settings as _get_app_settings

HEX_64 = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# 1. canonical_json 基础语义
# ---------------------------------------------------------------------------
class TestCanonicalJsonBasic:
    """Req 11 AC3：key 字典序 + UTF-8 + 无多余空白 + 列表顺序保留。"""

    def test_simple_dict_serialization(self) -> None:
        assert canonical_json({"a": 1, "b": 2}) == '{"a":1,"b":2}'

    def test_keys_sorted_alphabetically(self) -> None:
        """相同字段不同插入顺序必须产生完全相同的 JSON。"""
        d1 = {"b": 2, "a": 1, "c": 3}
        d2 = {"a": 1, "c": 3, "b": 2}
        d3 = {"c": 3, "b": 2, "a": 1}
        assert canonical_json(d1) == canonical_json(d2) == canonical_json(d3)
        assert canonical_json(d1) == '{"a":1,"b":2,"c":3}'

    def test_nested_dict_keys_sorted(self) -> None:
        """嵌套 dict 也按字典序排序（``json`` stdlib ``sort_keys=True`` 递归生效）。"""
        nested = {"outer_b": {"inner_z": 1, "inner_a": 2}, "outer_a": 0}
        result = canonical_json(nested)
        assert result == '{"outer_a":0,"outer_b":{"inner_a":2,"inner_z":1}}'

    def test_no_whitespace(self) -> None:
        """输出不含空格 / 换行 / 制表符。"""
        result = canonical_json({"a": [1, 2, 3], "b": {"c": "d"}})
        assert " " not in result
        assert "\n" not in result
        assert "\t" not in result

    def test_utf8_unicode_preserved(self) -> None:
        """中文 / emoji / 日文 必须直接输出，**不**被 ``\\uXXXX`` 转义。"""
        d = {"chinese": "中文测试", "emoji": "🚀", "japanese": "こんにちは"}
        result = canonical_json(d)
        assert "中文测试" in result
        assert "🚀" in result
        assert "こんにちは" in result
        # 仍然是合法 JSON
        assert json.loads(result) == d

    def test_empty_dict(self) -> None:
        assert canonical_json({}) == "{}"

    def test_empty_list(self) -> None:
        """``[]`` 的两种位置：作为顶层 / 作为 value。"""
        assert canonical_json([]) == "[]"
        assert canonical_json({"items": []}) == '{"items":[]}'

    def test_array_order_preserved(self) -> None:
        """JSON 列表是有序集合：list 顺序必须保留，**不**像 dict key 那样排序。"""
        d = {"items": [3, 1, 2, 5, 4]}
        result = canonical_json(d)
        assert result == '{"items":[3,1,2,5,4]}'

    def test_nested_array_order_preserved(self) -> None:
        d = {"matrix": [[3, 2, 1], [6, 5, 4]]}
        assert canonical_json(d) == '{"matrix":[[3,2,1],[6,5,4]]}'

    def test_disallow_nan(self) -> None:
        """``NaN`` 不是合法 JSON 数字（PostgreSQL JSONB 也不支持）。"""
        with pytest.raises(ValueError):
            canonical_json({"x": float("nan")})

    def test_disallow_positive_infinity(self) -> None:
        with pytest.raises(ValueError):
            canonical_json({"x": float("inf")})

    def test_disallow_negative_infinity(self) -> None:
        with pytest.raises(ValueError):
            canonical_json({"x": float("-inf")})

    def test_unsupported_type_raises_type_error(self) -> None:
        """``bytes`` 等没有显式约定的类型必须抛 TypeError，强迫上层显式转换。"""
        with pytest.raises(TypeError):
            canonical_json({"raw": b"\x00\x01"})

    def test_null_value_serialized_as_json_null(self) -> None:
        assert canonical_json({"x": None}) == '{"x":null}'

    def test_boolean_serialized_as_json_bool(self) -> None:
        assert canonical_json({"a": True, "b": False}) == '{"a":true,"b":false}'


# ---------------------------------------------------------------------------
# 2. 数字精度
# ---------------------------------------------------------------------------
class TestCanonicalJsonNumbers:
    """Req 11 AC3：数字使用固定精度（小数点后最多 8 位、无尾零、无科学计数法）。"""

    def test_integer_serialization(self) -> None:
        """整数无 ``.0`` 后缀。"""
        assert canonical_json({"n": 42}) == '{"n":42}'
        assert canonical_json({"n": 0}) == '{"n":0}'
        assert canonical_json({"n": -42}) == '{"n":-42}'

    def test_float_serialization(self) -> None:
        """普通 float 走 ``json`` 默认输出，跨平台稳定。"""
        # Python ``json.dumps(1.5) == '1.5'``、``json.dumps(2.0) == '2.0'``
        assert canonical_json({"x": 1.5}) == '{"x":1.5}'
        assert canonical_json({"x": 2.0}) == '{"x":2.0}'

    def test_decimal_strips_trailing_zeros(self) -> None:
        """``Decimal('100.50')`` → ``"100.5"``（去尾零）。"""
        result = canonical_json({"price": Decimal("100.50")})
        # Decimal 在 canonical_json 中以字符串呈现（避免跨语言 float 精度差）
        assert result == '{"price":"100.5"}'

    def test_decimal_integer_no_decimal_point(self) -> None:
        """``Decimal('100')`` → ``"100"``（无尾随小数点 / 零）。"""
        assert canonical_json({"x": Decimal("100")}) == '{"x":"100"}'

    def test_decimal_zero(self) -> None:
        """``Decimal('0')`` → ``"0"``，``Decimal('0.000')`` → ``"0"``。"""
        assert canonical_json({"x": Decimal("0")}) == '{"x":"0"}'
        assert canonical_json({"x": Decimal("0.000")}) == '{"x":"0"}'

    def test_decimal_negative_zero_normalized(self) -> None:
        """``Decimal('-0')`` / ``Decimal('-0.0')`` 都规范化为 ``"0"``。"""
        assert canonical_json({"x": Decimal("-0")}) == '{"x":"0"}'
        assert canonical_json({"x": Decimal("-0.0")}) == '{"x":"0"}'

    def test_decimal_small_number(self) -> None:
        """``Decimal('0.000001')`` 必须以 fixed-point 形式输出（**不**用 ``1E-6``）。"""
        result = canonical_json({"x": Decimal("0.000001")})
        assert result == '{"x":"0.000001"}'

    def test_decimal_negative(self) -> None:
        assert canonical_json({"x": Decimal("-1.5")}) == '{"x":"-1.5"}'
        assert canonical_json({"x": Decimal("-100.5")}) == '{"x":"-100.5"}'

    def test_decimal_eight_decimal_places(self) -> None:
        """8 位小数恰好是精度上限，应原样输出（去尾零规则不破坏有效位）。"""
        assert canonical_json({"x": Decimal("0.12345678")}) == '{"x":"0.12345678"}'

    def test_decimal_more_than_eight_places_quantized(self) -> None:
        """超过 8 位小数必须量化到 8 位（ROUND_HALF_EVEN）。"""
        # 9 位 1.123456789 → 量化到 8 位 → 1.12345679（half-even round up）
        result = canonical_json({"x": Decimal("1.123456789")})
        assert result == '{"x":"1.12345679"}'

    def test_decimal_no_scientific_notation(self) -> None:
        """大数字 / 小数字都不能输出科学计数法。"""
        assert canonical_json({"x": Decimal("123456789")}) == '{"x":"123456789"}'
        assert canonical_json({"x": Decimal("0.00000001")}) == '{"x":"0.00000001"}'

    def test_decimal_nan_disallowed(self) -> None:
        """``Decimal('NaN')`` 与 ``float('nan')`` 一致拒绝。"""
        with pytest.raises(ValueError):
            canonical_json({"x": Decimal("NaN")})

    def test_decimal_infinity_disallowed(self) -> None:
        with pytest.raises(ValueError):
            canonical_json({"x": Decimal("Infinity")})


# ---------------------------------------------------------------------------
# 3. 跨平台稳定性 PBT
# ---------------------------------------------------------------------------
# 自定义 JSON 兼容值生成器：递归构造 dict / list / str / int / float / bool / None。
# 显式排除 NaN / Infinity（``allow_nan=False``）以及超大整数（避免 hypothesis 默认
# 生成 1024 位整数让 json.dumps 慢成牛肉面）。
_JSON_PRIMITIVE = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**15), max_value=10**15),
    st.floats(allow_nan=False, allow_infinity=False, width=64),
    st.text(max_size=50),
)


def _json_compatible_dicts() -> st.SearchStrategy[dict[str, object]]:
    """生成嵌套 dict（key 必须是 str），叶节点为 JSON 原始值。"""
    return st.recursive(
        _JSON_PRIMITIVE,
        lambda children: st.one_of(
            st.lists(children, max_size=5),
            st.dictionaries(st.text(max_size=20), children, max_size=5),
        ),
        max_leaves=10,
    ).filter(lambda x: isinstance(x, dict))


@pytest.mark.pbt
class TestPropertyCanonicalJsonRoundTrip:
    """**Validates: Requirements 11**（canonical_json 是合法 JSON 且可 round-trip）。"""

    @given(d=_json_compatible_dicts())
    @settings(
        max_examples=60,
        suppress_health_check=[HealthCheck.too_slow],
    )
    def test_round_trip_dict_is_idempotent(self, d: dict[str, object]) -> None:
        """**Validates: Requirements 11**

        对任意 JSON 兼容 dict ``d``：
        ``canonical_json(json.loads(canonical_json(d))) == canonical_json(d)``。

        即"再走一轮 canonical_json 会得到相同结果"——这是序列化幂等性，
        是跨平台稳定的最基本约束。
        """
        s1 = canonical_json(d)
        # 反序列化得到的对象再次 canonical_json，结果必须不变
        parsed = json.loads(s1)
        s2 = canonical_json(parsed)
        assert s1 == s2


@pytest.mark.pbt
class TestPropertyCanonicalJsonKeyOrderIndependence:
    """**Validates: Requirements 11**（key 顺序无关性）。"""

    @given(
        keys=st.lists(
            st.text(min_size=1, max_size=10),
            min_size=2,
            max_size=8,
            unique=True,
        ),
        values=st.lists(
            _JSON_PRIMITIVE,
            min_size=2,
            max_size=8,
        ),
    )
    @settings(
        max_examples=60,
        suppress_health_check=[HealthCheck.too_slow],
    )
    def test_same_pairs_different_insert_order_same_output(
        self, keys: list[str], values: list[object]
    ) -> None:
        """**Validates: Requirements 11**

        相同 (key, value) 对集合 + 不同插入顺序 → ``canonical_json`` 必须输出相同字符串。
        这是哈希链跨平台一致性的核心：如果不同语言 / 不同运行时按不同顺序构造
        同一个 dict，规范化后的 hash 都不会变。
        """
        n = min(len(keys), len(values))
        keys = keys[:n]
        values = values[:n]
        forward = dict(zip(keys, values, strict=True))
        backward = dict(zip(reversed(keys), reversed(values), strict=True))
        # forward / backward 是同一组 (key, value) 对、但 dict 内部插入顺序不同
        assert canonical_json(forward) == canonical_json(backward)


@pytest.mark.pbt
class TestPropertyCanonicalJsonUnicodeStable:
    """**Validates: Requirements 11**（任意 Unicode 字符串作为 value 可序列化且可 round-trip）。"""

    @given(text_value=st.text(max_size=200))
    @settings(
        max_examples=60,
        suppress_health_check=[HealthCheck.too_slow],
    )
    def test_unicode_value_roundtrip(self, text_value: str) -> None:
        """**Validates: Requirements 11**

        对任意 Unicode 字符串（含 surrogate-pair / 控制字符 / emoji），
        ``json.loads(canonical_json({"v": x}))["v"] == x`` 恒成立。
        """
        d = {"v": text_value}
        result = canonical_json(d)
        # 必须是合法 JSON，并且解析回去等于原值
        assert json.loads(result) == d


# ---------------------------------------------------------------------------
# 4. compute_event_hash 行为
# ---------------------------------------------------------------------------
class TestComputeEventHash:
    """Req 11 AC1：``event_hash = sha256(canonical_json + previous_hash + created_at_iso)``。"""

    BASE_EVENT = {"event_type": "USER_LOGIN", "data": {"wallet": "0xABC"}}
    BASE_PREV = "previous_hash_placeholder"
    BASE_TS = "2026-05-30T12:00:00+00:00"

    def test_basic_hash_returns_64_hex(self) -> None:
        h = compute_event_hash(self.BASE_EVENT, self.BASE_PREV, self.BASE_TS)
        assert HEX_64.match(h), f"not 64 lowercase hex: {h!r}"

    def test_hash_deterministic(self) -> None:
        """相同输入恒返回相同 hash。"""
        h1 = compute_event_hash(self.BASE_EVENT, self.BASE_PREV, self.BASE_TS)
        h2 = compute_event_hash(self.BASE_EVENT, self.BASE_PREV, self.BASE_TS)
        h3 = compute_event_hash(self.BASE_EVENT, self.BASE_PREV, self.BASE_TS)
        assert h1 == h2 == h3

    def test_hash_changes_on_event_change(self) -> None:
        h1 = compute_event_hash(self.BASE_EVENT, self.BASE_PREV, self.BASE_TS)
        modified = {"event_type": "USER_LOGOUT", "data": {"wallet": "0xABC"}}
        h2 = compute_event_hash(modified, self.BASE_PREV, self.BASE_TS)
        assert h1 != h2

    def test_hash_changes_on_data_change(self) -> None:
        """事件 data 字段变化也必须改变 hash（哪怕 event_type 相同）。"""
        h1 = compute_event_hash(self.BASE_EVENT, self.BASE_PREV, self.BASE_TS)
        modified = {"event_type": "USER_LOGIN", "data": {"wallet": "0xDEF"}}
        h2 = compute_event_hash(modified, self.BASE_PREV, self.BASE_TS)
        assert h1 != h2

    def test_hash_changes_on_previous_change(self) -> None:
        """哈希链最关键的属性：``previous_hash`` 一变，``event_hash`` 必变。"""
        h1 = compute_event_hash(self.BASE_EVENT, self.BASE_PREV, self.BASE_TS)
        h2 = compute_event_hash(self.BASE_EVENT, "different_prev_hash", self.BASE_TS)
        assert h1 != h2

    def test_hash_changes_on_timestamp_change(self) -> None:
        h1 = compute_event_hash(self.BASE_EVENT, self.BASE_PREV, self.BASE_TS)
        h2 = compute_event_hash(
            self.BASE_EVENT, self.BASE_PREV, "2026-05-30T12:00:01+00:00"
        )
        assert h1 != h2

    def test_hash_invariant_to_dict_insertion_order(self) -> None:
        """构造 event_json 时 key 插入顺序不能影响 hash（canonical_json 已保证）。"""
        forward = {"event_type": "X", "data": {"a": 1, "b": 2}}
        backward = {"data": {"b": 2, "a": 1}, "event_type": "X"}
        h1 = compute_event_hash(forward, self.BASE_PREV, self.BASE_TS)
        h2 = compute_event_hash(backward, self.BASE_PREV, self.BASE_TS)
        assert h1 == h2

    def test_hash_genesis_chain_first_event(self) -> None:
        """链首事件用 GENESIS_HASH 作 previous，必须能正常计算（不抛错）。"""
        h = compute_event_hash(
            self.BASE_EVENT, GENESIS_HASH_DEFAULT, self.BASE_TS
        )
        assert HEX_64.match(h)

    def test_hash_with_unicode_event(self) -> None:
        """事件中含非 ASCII 字符（中文/emoji）也能稳定计算。"""
        event = {"event_type": "TASK_SUBMITTED", "data": {"text": "买入 BTC 🚀"}}
        h = compute_event_hash(event, self.BASE_PREV, self.BASE_TS)
        assert HEX_64.match(h)
        # 与 ASCII 事件不同
        ascii_event = {"event_type": "TASK_SUBMITTED", "data": {"text": "buy BTC"}}
        h2 = compute_event_hash(ascii_event, self.BASE_PREV, self.BASE_TS)
        assert h != h2


# ---------------------------------------------------------------------------
# 5. 跨语言一致性回归 vector（金标准）
# ---------------------------------------------------------------------------
class TestKnownTestVector:
    """固定的 (event_json, previous_hash, created_at_iso) → 期望 hash。

    本用例是审计哈希链的"金标准"——任何对 :func:`canonical_json` 或
    :func:`compute_event_hash` 的修改都会立刻打破这个固定向量；任何跨语言
    实现（TS / Go / Rust）也必须能复算出同一个 hash 值，否则就违反了
    Req 11 AC4 的链验证语义。

    向量值通过本仓库 Python 实现一次性产生；如需修改，必须：
    1. 同步更新 design.md 的 canonical_json 规则；
    2. 同步更新所有跨语言实现；
    3. 评估对历史审计事件的不可恢复性影响（== schema breaking change）。
    """

    def test_known_test_vector(self) -> None:
        event_json = {"event_type": "USER_LOGIN", "data": {"wallet": "0x123"}}
        previous_hash = "HTX_AGENT_PASSPORT_GENESIS_V1"
        created_at_iso = "2026-05-30T00:00:00+00:00"
        expected_hash = (
            "14eb7f9bdb8c93664fc9446756b45b7661a9ffc3ce34c36942f1a31f414c8764"
        )
        actual_hash = compute_event_hash(event_json, previous_hash, created_at_iso)
        assert actual_hash == expected_hash, (
            f"金标准向量已变化！\n"
            f"  expected: {expected_hash}\n"
            f"  actual:   {actual_hash}\n"
            f"  canonical_json={canonical_json(event_json)!r}\n"
            f"  如果是有意修改，请同步：design.md / 所有跨语言实现 / 历史事件迁移。"
        )

    def test_known_canonical_json_vector(self) -> None:
        """同一向量的 canonical_json 字符串本身也固定下来，便于跨语言对齐时调试。"""
        event_json = {"event_type": "USER_LOGIN", "data": {"wallet": "0x123"}}
        expected = '{"data":{"wallet":"0x123"},"event_type":"USER_LOGIN"}'
        assert canonical_json(event_json) == expected


# ---------------------------------------------------------------------------
# 6. PBT —— 哈希碰撞抗性
# ---------------------------------------------------------------------------
@pytest.mark.pbt
class TestPropertyHashCollisionResistance:
    """**Validates: Requirements 11**（不同事件 → 不同哈希）。

    SHA-256 的密码学碰撞概率 ≈ 2^-256，PBT 范围内（200 例）几乎不可能命中。
    本测试以"不同输入"为前提，断言 hash 不相同；命中即证明实现错误
    （例如忘记把 previous_hash 拼进哈希、event_json 被截断等）。
    """

    @given(
        events=st.lists(
            _json_compatible_dicts(),
            min_size=2,
            max_size=2,
            unique_by=lambda d: canonical_json(d),
        )
    )
    @settings(
        max_examples=50,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
    )
    def test_different_events_produce_different_hashes(
        self, events: list[dict[str, object]]
    ) -> None:
        """**Validates: Requirements 11**

        ``unique_by=canonical_json`` 保证两个 event 的规范化形式不同，
        因此它们的 hash 必须不同。"""
        e1, e2 = events
        prev = "fixed_prev_hash"
        ts = "2026-05-30T00:00:00+00:00"
        h1 = compute_event_hash(e1, prev, ts)
        h2 = compute_event_hash(e2, prev, ts)
        assert h1 != h2


@pytest.mark.pbt
class TestPropertyHashStableUnderKeyOrder:
    """**Validates: Requirements 11**（key 顺序变化不影响 hash）。"""

    @given(
        keys=st.lists(
            st.text(min_size=1, max_size=10),
            min_size=2,
            max_size=8,
            unique=True,
        ),
        values=st.lists(
            _JSON_PRIMITIVE,
            min_size=2,
            max_size=8,
        ),
        prev_hash=st.text(min_size=1, max_size=64),
        created_at_iso=st.from_regex(
            r"^20[0-9]{2}-[0-1][0-9]-[0-3][0-9]T[0-2][0-9]:[0-5][0-9]:[0-5][0-9]\+00:00$",
            fullmatch=True,
        ),
    )
    @settings(
        max_examples=50,
        suppress_health_check=[HealthCheck.too_slow],
    )
    def test_hash_invariant_under_key_reorder(
        self,
        keys: list[str],
        values: list[object],
        prev_hash: str,
        created_at_iso: str,
    ) -> None:
        """**Validates: Requirements 11**

        相同 (key, value) 对集合 + 不同插入顺序 + 相同 prev_hash + 相同 timestamp
        → 完整 ``compute_event_hash`` 输出也必须相同（key 顺序不影响哈希链）。
        """
        n = min(len(keys), len(values))
        keys = keys[:n]
        values = values[:n]
        forward = dict(zip(keys, values, strict=True))
        backward = dict(zip(reversed(keys), reversed(values), strict=True))
        h1 = compute_event_hash(forward, prev_hash, created_at_iso)
        h2 = compute_event_hash(backward, prev_hash, created_at_iso)
        assert h1 == h2


# ---------------------------------------------------------------------------
# 7. GENESIS_HASH 常量与 settings 对齐
# ---------------------------------------------------------------------------
class TestGenesisHashConstants:
    """``GENESIS_HASH_DEFAULT`` 与 ``settings.GENESIS_HASH`` 的语义。"""

    def test_default_value(self) -> None:
        assert GENESIS_HASH_DEFAULT == "HTX_AGENT_PASSPORT_GENESIS_V1"

    def test_default_is_non_empty_string(self) -> None:
        assert isinstance(GENESIS_HASH_DEFAULT, str)
        assert len(GENESIS_HASH_DEFAULT) > 0

    def test_get_genesis_hash_returns_settings_value(self) -> None:
        """``get_genesis_hash`` 必须读取 settings；测试 conftest 已固定为默认值。"""
        # conftest 中 settings_overrides 把 GENESIS_HASH 显式设为默认值
        assert get_genesis_hash() == _get_app_settings().GENESIS_HASH

    def test_get_genesis_hash_falls_back_to_default_on_empty_setting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``settings.GENESIS_HASH`` 为空字符串时回退到 :data:`GENESIS_HASH_DEFAULT`。"""
        monkeypatch.setenv("GENESIS_HASH", "")
        _get_app_settings.cache_clear()
        try:
            assert get_genesis_hash() == GENESIS_HASH_DEFAULT
        finally:
            _get_app_settings.cache_clear()

    def test_compute_event_hash_genesis_link_first_event(self) -> None:
        """链首事件: ``previous_hash = GENESIS_HASH`` 时哈希计算正常。"""
        event_json = {"event_type": "USER_LOGIN", "data": {"wallet": "0xABC"}}
        h = compute_event_hash(
            event_json, get_genesis_hash(), "2026-01-01T00:00:00+00:00"
        )
        assert HEX_64.match(h)


# ---------------------------------------------------------------------------
# 8. canonical_json 与 stdlib json.dumps 对照（互操作性）
# ---------------------------------------------------------------------------
class TestInteroperabilityWithStdlibJson:
    """canonical_json 输出必须能被 ``json.loads`` 正确解析回来（互操作性）。"""

    @pytest.mark.parametrize(
        "obj",
        [
            {},
            {"a": 1},
            {"a": 1, "b": 2.5, "c": "string", "d": True, "e": None, "f": [1, 2, 3]},
            {"nested": {"deep": {"deeper": [{"x": 1}, {"y": 2}]}}},
            {"unicode": "中文 + 日本語 + 🚀"},
        ],
    )
    def test_loads_recovers_input(self, obj: dict[str, object]) -> None:
        """除 Decimal 外的所有受支持类型，``json.loads(canonical_json(x)) == x``。"""
        s = canonical_json(obj)
        assert json.loads(s) == obj

    def test_decimal_loads_as_string(self) -> None:
        """Decimal 在 canonical_json 中以**字符串**形式出现，loads 回来仍是 str。

        这是有意为之（避免跨语言 float 精度差）；上层若需 Decimal 语义，
        必须显式 ``Decimal(s)`` 转换。
        """
        s = canonical_json({"price": Decimal("100.5")})
        parsed = json.loads(s)
        assert parsed == {"price": "100.5"}
        assert isinstance(parsed["price"], str)


# ---------------------------------------------------------------------------
# 9. 浮点边界 sanity check（防止 hypothesis 偶发 NaN/inf 漏出）
# ---------------------------------------------------------------------------
class TestFloatBoundary:
    """浮点边界：确保 ``allow_nan=False`` 在所有路径都生效。"""

    def test_nan_in_nested_dict_raises(self) -> None:
        with pytest.raises(ValueError):
            canonical_json({"outer": {"inner": float("nan")}})

    def test_inf_in_list_raises(self) -> None:
        with pytest.raises(ValueError):
            canonical_json({"items": [1.0, math.inf, 3.0]})

    def test_normal_float_passes(self) -> None:
        # 没有 NaN / inf 时不应误抛
        result = canonical_json({"x": 3.14, "y": -2.5, "z": 0.0})
        # 关键：不抛 ValueError 即通过；具体浮点格式化交给 stdlib
        assert "x" in result and "y" in result and "z" in result
