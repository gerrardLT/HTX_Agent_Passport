"""L3 Eval 框架（任务 21.1 / Req 20）。

方法论 §22「L3 Eval 驱动测试」：用结构化的 EvalCase + 断言类型 + 评分聚合，
对系统做"行为正确性"评估（区别于 L1/L2 的逐组件断言）。

核心抽象
--------
- :class:`EvalCase`：一条评估用例（输入 + 期望 + 断言类型 + 维度）。
- :class:`AssertionType`：断言类型枚举（路由 / 工具选择 / 安全 / 效率 / 质量）。
- :class:`EvalDimension`：五个评估维度（方法论 §22.2）。
- :class:`EvalResult`：单条用例结果（通过/失败 + 详情）。
- :class:`EvalReport`：聚合报告（各维度通过率 + blocker 标记）。
- :func:`run_eval_suite`：执行一组 EvalCase 并聚合成 EvalReport。

设计原则
--------
- **安全维度零容忍**（Req 20 AC4/AC7）：SAFETY 维度任一失败 → report.has_blocker=True。
- **可读报告**（Req 20 AC6）：EvalReport 输出各维度通过率 + 失败项明细。
- **确定性优先**：路由/工具选择/安全/效率维度都用确定性断言（不依赖真实 LLM），
  质量维度（LLM-as-Judge）单独隔离，避免 CI 抖动。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------
class EvalDimension(str, Enum):
    """五个评估维度（方法论 §22.2）。"""

    ROUTING = "routing"  # 路由正确性
    TOOL_SELECTION = "tool_selection"  # 工具选择
    QUALITY = "quality"  # 输出质量（LLM-as-Judge）
    SAFETY = "safety"  # 安全合规（零容忍）
    EFFICIENCY = "efficiency"  # 效率（工具调用次数约束）
    RESILIENCE = "resilience"  # 韧性（工具空结果不编造）


class AssertionType(str, Enum):
    """断言类型——决定 EvalCase 如何判定通过。"""

    EQUALS = "equals"  # actual == expected
    CONTAINS = "contains"  # expected in actual（子串/成员）
    NOT_CONTAINS = "not_contains"  # expected not in actual
    LESS_EQUAL = "less_equal"  # actual <= expected（效率上限）
    GREATER_EQUAL = "greater_equal"  # actual >= expected（质量下限）
    PREDICATE = "predicate"  # 自定义谓词函数返回 True


#: 安全相关维度——任一失败即 blocker（Req 20 AC7）。
SAFETY_DIMENSIONS: frozenset[EvalDimension] = frozenset(
    {EvalDimension.SAFETY}
)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------
@dataclass
class EvalCase:
    """一条评估用例。

    Attributes
    ----------
    name : str
        用例名（报告里用于定位）。
    dimension : EvalDimension
        所属评估维度。
    assertion : AssertionType
        断言类型。
    run : Callable[[], Any]
        执行函数——返回"实际值"（actual）。框架调用它得到 actual 后做断言。
    expected : Any
        期望值。``PREDICATE`` 断言时忽略此字段（用 predicate）。
    predicate : Callable[[Any], bool] | None
        ``PREDICATE`` 断言时的判定函数，接收 actual 返回 bool。
    description : str
        可读描述。
    """

    name: str
    dimension: EvalDimension
    assertion: AssertionType
    run: Callable[[], Any]
    expected: Any = None
    predicate: Callable[[Any], bool] | None = None
    description: str = ""


@dataclass
class EvalResult:
    """单条用例执行结果。"""

    case_name: str
    dimension: EvalDimension
    passed: bool
    actual: Any
    expected: Any
    detail: str = ""


@dataclass
class EvalReport:
    """聚合评估报告（Req 20 AC6）。"""

    results: list[EvalResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed_count(self) -> int:
        return self.total - self.passed_count

    @property
    def pass_rate(self) -> float:
        """整体通过率（0.0-1.0）；空 suite 视为 1.0。"""
        if self.total == 0:
            return 1.0
        return self.passed_count / self.total

    def dimension_pass_rate(self, dimension: EvalDimension) -> float:
        """某维度的通过率；该维度无用例时视为 1.0。"""
        subset = [r for r in self.results if r.dimension == dimension]
        if not subset:
            return 1.0
        return sum(1 for r in subset if r.passed) / len(subset)

    @property
    def failures(self) -> list[EvalResult]:
        return [r for r in self.results if not r.passed]

    @property
    def has_blocker(self) -> bool:
        """安全维度任一失败 → blocker（Req 20 AC7 零容忍）。"""
        return any(
            (not r.passed) and r.dimension in SAFETY_DIMENSIONS
            for r in self.results
        )

    def summary(self) -> str:
        """生成多维度评分报告文本（Req 20 AC6）。"""
        lines = [
            "=== L3 Eval Report ===",
            f"Total: {self.total} | Passed: {self.passed_count} | "
            f"Failed: {self.failed_count} | Pass rate: {self.pass_rate:.1%}",
            "",
            "Per-dimension pass rate:",
        ]
        for dim in EvalDimension:
            subset = [r for r in self.results if r.dimension == dim]
            if subset:
                rate = self.dimension_pass_rate(dim)
                lines.append(f"  - {dim.value}: {rate:.1%} ({len(subset)} cases)")
        if self.failures:
            lines.append("")
            lines.append("Failures:")
            for r in self.failures:
                lines.append(
                    f"  ✗ [{r.dimension.value}] {r.case_name}: "
                    f"expected={r.expected!r} actual={r.actual!r} {r.detail}"
                )
        if self.has_blocker:
            lines.append("")
            lines.append("🚫 BLOCKER: safety dimension has failures — release blocked.")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 断言执行
# ---------------------------------------------------------------------------
def _evaluate_assertion(case: EvalCase, actual: Any) -> tuple[bool, str]:
    """对单条用例的 actual 值执行断言，返回 (passed, detail)。"""
    at = case.assertion
    exp = case.expected

    if at == AssertionType.EQUALS:
        return actual == exp, ""
    if at == AssertionType.CONTAINS:
        try:
            return exp in actual, ""
        except TypeError:
            return False, f"actual {type(actual).__name__} not container"
    if at == AssertionType.NOT_CONTAINS:
        try:
            return exp not in actual, ""
        except TypeError:
            return False, f"actual {type(actual).__name__} not container"
    if at == AssertionType.LESS_EQUAL:
        return actual <= exp, ""
    if at == AssertionType.GREATER_EQUAL:
        return actual >= exp, ""
    if at == AssertionType.PREDICATE:
        if case.predicate is None:
            return False, "PREDICATE assertion but no predicate provided"
        return bool(case.predicate(actual)), ""
    return False, f"unknown assertion type {at!r}"


def run_eval_case(case: EvalCase) -> EvalResult:
    """执行单条 EvalCase。

    捕获 ``run()`` 抛出的异常并视为失败——避免单条用例崩溃中断整个 suite。
    """
    try:
        actual = case.run()
    except Exception as exc:  # noqa: BLE001 - eval 框架需兜底任何用例异常
        return EvalResult(
            case_name=case.name,
            dimension=case.dimension,
            passed=False,
            actual=f"<exception: {exc!r}>",
            expected=case.expected,
            detail="run() raised",
        )

    passed, detail = _evaluate_assertion(case, actual)
    return EvalResult(
        case_name=case.name,
        dimension=case.dimension,
        passed=passed,
        actual=actual,
        expected=case.expected,
        detail=detail,
    )


def run_eval_suite(cases: list[EvalCase]) -> EvalReport:
    """执行一组 EvalCase 并聚合成 EvalReport。"""
    report = EvalReport()
    for case in cases:
        report.results.append(run_eval_case(case))
    return report


__all__ = [
    "AssertionType",
    "EvalCase",
    "EvalDimension",
    "EvalReport",
    "EvalResult",
    "SAFETY_DIMENSIONS",
    "run_eval_case",
    "run_eval_suite",
]
