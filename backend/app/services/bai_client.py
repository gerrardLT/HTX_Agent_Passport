"""B.AI HTTP 客户端（任务 10 / Req 5 / Req 14 AC5）。

本模块只封装 **B.AI HTTP 协议层**——把 ``(system, user, timeout)`` 转成一次
OpenAI-compat ``/chat/completions`` 调用，并把网络层异常 / 不可用 / 超时
**翻译成业务层异常**（:class:`BAITimeoutError` / :class:`BAIServiceUnavailableError`
/ :class:`BAIError`）。

为什么单独抽一个客户端？
------------------------
- :mod:`app.services.planner` 关注**重试 / 降级 / 审计**等业务逻辑，
  不应直接处理 ``httpx`` 的实现细节。
- 测试时可以通过 ``call_planner(..., bai_client=stub)`` 注入一个纯 Python
  stub（不涉及 HTTP），让单元测试不依赖 ``httpx.MockTransport``。
- 真实场景下若要切换到 Anthropic / 自建模型，只需提供同 :class:`BAIClient`
  接口的另一实现。

异常分类（与 Req 14 AC5「429 / 503 / 连接被拒 → MODEL_UNAVAILABLE」对齐）
-----------------------------------------------------------------------
- :class:`BAITimeoutError`  ——单次请求超时（``httpx.TimeoutException``）。
  规划器适配器看到此异常会**重试**直到 ``max_retries`` 用尽再降级。
- :class:`BAIServiceUnavailableError` —— 429 / 5xx / 网络错误。
  规划器适配器看到此异常**立即**降级，不重试。
- :class:`BAIError` —— 其它失败（200 但响应体格式错误 / 非 200 非 5xx 等）。
  规划器适配器视同「调用失败」走 MODEL_CALL_FAILED 分支。

设计依据：design.md「决策层：B.AI Planner 适配器」/ Req 5 AC4-5 / Req 14 AC5。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Final

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 异常层次
# ---------------------------------------------------------------------------
class BAIError(RuntimeError):
    """B.AI 客户端基类异常。

    所有 B.AI 相关错误都派生自本类，便于上层 ``except BAIError`` 兜底；
    具体子类用于区分「重试 / 立即降级 / 视为失败」三种处理路径。
    """


class BAITimeoutError(BAIError):
    """单次请求超时（来自 :class:`httpx.TimeoutException`）。

    规划器适配器对本异常做「最多 ``max_retries`` 次重试」处理（Req 5 AC4）；
    用尽重试后写 ``MODEL_CALL_FAILED`` 审计事件并降级到 mock planner。
    """


class BAIServiceUnavailableError(BAIError):
    """B.AI 服务不可用（429 / 5xx / 网络错误）。

    规划器适配器对本异常**立即**降级——不重试，写 ``MODEL_UNAVAILABLE``
    审计事件后调用 mock planner（Req 14 AC5）。这样 demo 演示不会因
    临时不可用陷入「重试 → 重试 → 重试」的长等待。
    """


# ---------------------------------------------------------------------------
# 响应数据类
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BAIResponse:
    """B.AI ``/chat/completions`` 响应的最小投影。

    Attributes
    ----------
    content : str
        模型生成的文本（``choices[0].message.content``）。
        **注意**：本字段是 LLM 原样输出，可能不是合法 JSON——schema 校验
        由调用方（:mod:`app.services.planner`）的 :func:`validate_action_plan_schema`
        负责。
    input_tokens : int | None
        ``usage.prompt_tokens``；若供应商未回填则为 None。
    output_tokens : int | None
        ``usage.completion_tokens``；若供应商未回填则为 None。
    latency_ms : int
        从发起请求到收到响应的总耗时（毫秒，向下取整）。
    """

    content: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int


# ---------------------------------------------------------------------------
# 内部常量
# ---------------------------------------------------------------------------
#: 默认模型名（OpenAI-compat 的 ``model`` 字段）。
#: 真实部署可通过 :class:`PlannerConfig.model` 覆盖。
DEFAULT_MODEL: Final[str] = "planner-v1"

#: 视为「服务不可用」的 HTTP 状态码——立即降级，不重试（Req 14 AC5）。
_SERVICE_UNAVAILABLE_STATUSES: Final[frozenset[int]] = frozenset({429, 502, 503, 504})


# ---------------------------------------------------------------------------
# BAIClient
# ---------------------------------------------------------------------------
class BAIClient:
    """B.AI HTTP 客户端（OpenAI-compat ``/chat/completions``）。

    用法
    ----
    生产代码不应直接 ``new BAIClient()``——通过
    :func:`app.services.planner.call_planner` 的 ``bai_client`` 参数注入即可，
    适配器会在 ``bai_client is None`` 时按 :func:`app.core.config.get_settings`
    构造默认实例。

    单元测试可以传入实现同接口的 stub（无需 :mod:`httpx`）。
    """

    def __init__(
        self,
        *,
        api_url: str | None = None,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        http_client: httpx.Client | None = None,
    ) -> None:
        """构造 BAI 客户端。

        Parameters
        ----------
        api_url : str | None, default None
            B.AI 端点 base URL（如 ``"https://api.b.ai/v1"``）。``None`` 时
            从 ``settings.BAI_API_URL`` 读取。
        api_key : str | None, default None
            Bearer token；空字符串视为「未配置」（demo 模式下允许）。
            **绝不会**写入日志或审计事件（Req 15 AC1）。
        model : str, default DEFAULT_MODEL
            发送给 B.AI 的 ``model`` 字段。
        http_client : httpx.Client | None, default None
            可选注入；提供此参数时 :meth:`close` 不会关闭它（调用方负责
            生命周期）。生产代码使用默认的内部 client。
        """
        settings = get_settings()
        self.api_url = (api_url if api_url is not None else settings.BAI_API_URL).rstrip("/")
        # 注：``api_key`` 的真实值仅在 ``_build_headers`` 中以 Bearer 形式
        # 写入 outbound HTTP header；不存任何字符串到 self 的属性
        # （除了这一引用，避免 ``__repr__`` 意外打印）。
        self._api_key = api_key if api_key is not None else settings.BAI_API_KEY
        self.model = model
        self._owned_client = http_client is None
        self._http: httpx.Client = http_client or httpx.Client()

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def chat(self, system: str, user: str, *, timeout: float) -> BAIResponse:
        """同步调用 B.AI ``/chat/completions``。

        Parameters
        ----------
        system : str
            system 消息内容（应为 :data:`app.services.planner.PLANNER_SYSTEM_PROMPT`）。
        user : str
            user 消息内容；通常是 :func:`app.services.planner._format_user_context`
            返回的 canonical JSON 文本。
        timeout : float
            单次请求超时时间（秒）。来自 :class:`PlannerConfig.timeout_seconds`。

        Returns
        -------
        BAIResponse
            响应投影（``content`` / ``input_tokens`` / ``output_tokens`` /
            ``latency_ms``）。

        Raises
        ------
        BAITimeoutError
            ``httpx.TimeoutException``——上层会重试。
        BAIServiceUnavailableError
            429 / 5xx / 网络错误——上层立即降级。
        BAIError
            响应格式异常或其它非 200 状态——上层视同失败。
        """
        url = f"{self.api_url}/chat/completions"
        headers = self._build_headers()
        body = self._build_body(system=system, user=user)

        start = time.perf_counter()
        try:
            response = self._http.post(url, json=body, headers=headers, timeout=timeout)
        except httpx.TimeoutException as exc:
            # 不在异常消息中拼接 url（避免 token 泄露到 logs；安全冗余）
            raise BAITimeoutError(f"BAI request timed out after {timeout}s") from exc
        except (httpx.ConnectError, httpx.NetworkError) as exc:
            raise BAIServiceUnavailableError(
                f"BAI network error: {exc.__class__.__name__}"
            ) from exc

        latency_ms = max(0, int((time.perf_counter() - start) * 1000))

        # ---- HTTP 状态码分类 ----
        if response.status_code in _SERVICE_UNAVAILABLE_STATUSES:
            raise BAIServiceUnavailableError(
                f"BAI returned status={response.status_code}"
            )
        if response.status_code >= 500:
            # 其它 5xx（500/501 等）也归入「服务不可用」；与 Req 14 AC5 语义一致
            raise BAIServiceUnavailableError(
                f"BAI returned status={response.status_code}"
            )
        if response.status_code != 200:
            raise BAIError(f"BAI returned non-200 status={response.status_code}")

        # ---- 解析 OpenAI-compat 响应 ----
        try:
            data: Any = response.json()
            content = data["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise BAIError(
                    f"BAI choices[0].message.content not a string: {type(content).__name__}"
                )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            # ``response.json()`` 在非 JSON body 时抛 ``json.JSONDecodeError``
            # （ValueError 子类）；KeyError / IndexError / TypeError 来自结构错误。
            raise BAIError(f"BAI response malformed: {exc!s}") from exc

        usage = data.get("usage") or {}
        return BAIResponse(
            content=content,
            input_tokens=_int_or_none(usage.get("prompt_tokens")),
            output_tokens=_int_or_none(usage.get("completion_tokens")),
            latency_ms=latency_ms,
        )

    # ------------------------------------------------------------------
    # 资源管理
    # ------------------------------------------------------------------
    def close(self) -> None:
        """关闭内部 ``httpx.Client``（仅当本实例拥有它时）。

        外部注入的 client 由调用方自行关闭；本方法不会关闭它，避免
        意外影响共享 client 的其它使用者。
        """
        if self._owned_client:
            self._http.close()

    def __enter__(self) -> "BAIClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 内部 helpers
    # ------------------------------------------------------------------
    def _build_headers(self) -> dict[str, str]:
        """构造请求头；``Authorization`` 仅在 ``api_key`` 非空时附加。"""
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _build_body(self, *, system: str, user: str) -> dict[str, Any]:
        """构造 OpenAI-compat ``/chat/completions`` 请求体。

        ``temperature=0`` 让规划结果尽量稳定（Req 5 AC1 「确定性」语义对齐）；
        ``max_tokens=1024`` 限制输出长度，避免 LLM 把整段长文 dump 进
        ActionPlan（schema 也会拒绝 actions > 3，但提前限制更省 token）。
        """
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": 1024,
        }

    def __repr__(self) -> str:
        # 显式防御：``__repr__`` 不暴露 api_key（Req 15 AC1）
        return f"BAIClient(api_url={self.api_url!r}, model={self.model!r})"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _int_or_none(value: Any) -> int | None:
    """安全把任意值转成 int；非数字时返回 None。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "DEFAULT_MODEL",
    "BAIClient",
    "BAIError",
    "BAIResponse",
    "BAIServiceUnavailableError",
    "BAITimeoutError",
]
