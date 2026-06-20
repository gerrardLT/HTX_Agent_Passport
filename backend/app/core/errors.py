"""统一错误响应（design.md 「统一错误响应」 / Req 1 AC7 / Req 15）。

约定的响应结构::

    {
      "error": {
        "code": "UNAUTHORIZED",
        "message": "missing Authorization header",
        "status": 401,
        "trace_id": "uuid",
        "details": {}
      }
    }

实现要点
--------
1. 把 FastAPI / Starlette 抛出的 :class:`HTTPException` 统一封装到上述结构。
2. 把业务自定义的 :class:`InvalidTokenError`（``ValueError`` 子类）映射为 401。
3. 把 ``RequestValidationError``（pydantic 422）映射为 ``VALIDATION_ERROR``。
4. trace_id 优先使用 ``request.state.trace_id``（后续中间件设置）；
   未设置时退化为新生成 UUID，保证响应永远带 trace_id。

注意：本模块不依赖任何路由实现；后续任务可在此扩展更多业务异常映射。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.auth import InvalidTokenError
from app.core.state_machine import IllegalStateTransition
from app.services.credentials import (
    CredentialNotFoundError,
    DuplicateCredentialError,
)
from app.services.passports import (
    PassportNotFoundError,
    PassportStateTransitionError,
)
from app.services.policy_validator import InvalidPolicyError

# HTTP 状态码 → 默认 code 映射；详细业务 code 由 detail 覆盖
_DEFAULT_CODE_BY_STATUS: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    429: "TOO_MANY_REQUESTS",
    500: "INTERNAL_ERROR",
    503: "SERVICE_UNAVAILABLE",
}


def _resolve_trace_id(request: Request) -> str:
    """从 ``request.state.trace_id`` 取值，缺失时新建 UUID。

    后续任务（13）会引入 trace 中间件给每个请求注入 trace_id；
    在那之前响应里也保证有一个 trace_id 字段，让前端联调有得显示。
    """
    trace_id = getattr(request.state, "trace_id", None)
    if isinstance(trace_id, str) and trace_id:
        return trace_id
    return str(uuid.uuid4())


def _build_error_payload(
    *,
    status: int,
    code: str,
    message: str,
    trace_id: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "status": status,
            "trace_id": trace_id,
            "details": details or {},
        }
    }


async def http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    """``HTTPException`` 统一封装。

    支持两种 ``detail`` 形态：

    1. ``str``：直接作为 ``message``，``code`` 用状态码默认值。
    2. ``dict``：可携带 ``code`` / ``message`` / ``details`` 字段，分别覆盖默认值。
       这种写法允许业务在抛异常时显式指定业务 code，而不依赖 HTTP 状态码反查。
    """
    status = exc.status_code
    default_code = _DEFAULT_CODE_BY_STATUS.get(status, "ERROR")
    detail = exc.detail

    code = default_code
    message = ""
    details: dict[str, Any] = {}

    if isinstance(detail, dict):
        code = str(detail.get("code", default_code))
        message = str(detail.get("message", "")) or default_code
        raw_details = detail.get("details", {})
        if isinstance(raw_details, dict):
            details = raw_details
    elif detail is not None:
        message = str(detail)
    else:
        message = default_code

    return JSONResponse(
        status_code=status,
        content=_build_error_payload(
            status=status,
            code=code,
            message=message,
            trace_id=_resolve_trace_id(request),
            details=details,
        ),
    )


async def invalid_token_handler(
    request: Request,
    exc: InvalidTokenError,
) -> JSONResponse:
    """:class:`InvalidTokenError` → 401 UNAUTHORIZED。"""
    return JSONResponse(
        status_code=401,
        content=_build_error_payload(
            status=401,
            code="UNAUTHORIZED",
            message=str(exc) or "invalid token",
            trace_id=_resolve_trace_id(request),
        ),
    )


async def duplicate_credential_handler(
    request: Request,
    exc: DuplicateCredentialError,
) -> JSONResponse:
    """:class:`DuplicateCredentialError` → 409 DUPLICATE_CREDENTIAL（Req 2 AC2）。

    把已存在凭证的 ``credential_id`` 写到 ``details``，方便前端引导用户去操作那条凭证。
    **不**回传 ``access_key_hash``——尽管它是哈希形态而非明文，但仍属于内部
    可链接的"可识别凭证"信息，从最小披露原则出发不暴露给客户端。
    """
    return JSONResponse(
        status_code=409,
        content=_build_error_payload(
            status=409,
            code="DUPLICATE_CREDENTIAL",
            message="credential with same access_key already exists",
            trace_id=_resolve_trace_id(request),
            details={"existing_credential_id": str(exc.existing_credential_id)},
        ),
    )


async def credential_not_found_handler(
    request: Request,
    exc: CredentialNotFoundError,
) -> JSONResponse:
    """:class:`CredentialNotFoundError` → 404 NOT_FOUND。

    "不属于本人 / 已软删除 / 不存在"统一映射 404，避免存在性侧信道
    （详见 :func:`app.services.credentials._get_owned_credential` 的 docstring）。
    """
    return JSONResponse(
        status_code=404,
        content=_build_error_payload(
            status=404,
            code="NOT_FOUND",
            message="credential not found",
            trace_id=_resolve_trace_id(request),
            details={"credential_id": str(exc.credential_id)},
        ),
    )


async def passport_not_found_handler(
    request: Request,
    exc: PassportNotFoundError,
) -> JSONResponse:
    """:class:`PassportNotFoundError` → 404 NOT_FOUND（任务 5.3）。

    "不属于本人 / 不存在 / 已 DELETED"统一映射 404，避免通过对比 404/403
    推测他人 passport_id 的存在性侧信道。
    """
    return JSONResponse(
        status_code=404,
        content=_build_error_payload(
            status=404,
            code="NOT_FOUND",
            message="passport not found",
            trace_id=_resolve_trace_id(request),
            details={"passport_id": str(exc.passport_id)},
        ),
    )


async def passport_state_transition_handler(
    request: Request,
    exc: PassportStateTransitionError,
) -> JSONResponse:
    """:class:`PassportStateTransitionError` → 409。

    业务前置条件未满足（非状态机结构错），如关联凭证 state 不在
    {READ_ONLY, TRADE_ENABLED}。``code`` 由异常 ``code`` 字段决定，
    缺省为 ``PASSPORT_STATE_INVALID``。
    """
    return JSONResponse(
        status_code=409,
        content=_build_error_payload(
            status=409,
            code=exc.code,
            message=str(exc),
            trace_id=_resolve_trace_id(request),
        ),
    )


async def invalid_policy_handler(
    request: Request,
    exc: InvalidPolicyError,
) -> JSONResponse:
    """:class:`InvalidPolicyError` → 400 POLICY_INVALID（任务 5.3）。

    Req 3 AC1 / Req 4 AC1: policy schema / 业务规则校验失败时返回 400 +
    校验错误详情，方便前端定位「哪些字段错了」。

    把 ``exc.errors`` 列表（每条带 ``path`` / ``message`` / ``validator``）
    塞进 ``details.errors``——与 :func:`validation_exception_handler` 一致的
    形态，前端可以共用同一份错误渲染器。

    注意区分 422
    -----------
    - 422 (VALIDATION_ERROR): Pydantic 请求体结构错（缺字段、类型错、互斥违反等）。
    - 400 (POLICY_INVALID): policy 字段本身的语义错（withdraw=true、未知字段等）；
      请求结构本身合法，仅 policy 内容不通过 DSL v0 校验。
    """
    return JSONResponse(
        status_code=400,
        content=_build_error_payload(
            status=400,
            code="POLICY_INVALID",
            message=str(exc),
            trace_id=_resolve_trace_id(request),
            details={"errors": exc.errors},
        ),
    )


async def illegal_state_transition_handler(
    request: Request,
    exc: IllegalStateTransition,
) -> JSONResponse:
    """:class:`IllegalStateTransition` → 409 ILLEGAL_STATE_TRANSITION。

    覆盖范围：凭证 / Passport / Action 状态机（任务 4.2 / 5.3 / 8 / 11 等）。
    异常对象自带 ``current`` / ``target`` / ``machine_name``，全部塞进 ``details``
    便于前端展示具体的"从哪个状态转哪个状态被拒了"。
    """
    return JSONResponse(
        status_code=409,
        content=_build_error_payload(
            status=409,
            code="ILLEGAL_STATE_TRANSITION",
            message=str(exc),
            trace_id=_resolve_trace_id(request),
            details={
                "current": exc.current,
                "target": exc.target,
                "machine": exc.machine_name,
            },
        ),
    )


async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """pydantic 422 → ``VALIDATION_ERROR``。

    把 ``exc.errors()`` 塞进 ``details.errors``，方便前端定位字段。
    需要清理 ``ctx`` 中不可 JSON 序列化的对象（如 ValueError 实例）。
    """
    errors = []
    for err in exc.errors():
        clean_err = dict(err)
        # ctx 可能含不可序列化的对象（如 ValueError 实例）；转为字符串表示
        if "ctx" in clean_err and isinstance(clean_err["ctx"], dict):
            clean_ctx = {}
            for k, v in clean_err["ctx"].items():
                try:
                    import json as _json
                    _json.dumps(v)
                    clean_ctx[k] = v
                except (TypeError, ValueError):
                    clean_ctx[k] = str(v)
            clean_err["ctx"] = clean_ctx
        errors.append(clean_err)

    return JSONResponse(
        status_code=422,
        content=_build_error_payload(
            status=422,
            code="VALIDATION_ERROR",
            message="request validation failed",
            trace_id=_resolve_trace_id(request),
            details={"errors": errors},
        ),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """在 :class:`FastAPI` 应用上挂载本模块的全部 handler。

    ``# type: ignore`` 抑制 starlette 的 Callable 形参泛型噪音——
    FastAPI 实际上支持把 handler 签名特化为具体的异常子类。
    """
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(InvalidTokenError, invalid_token_handler)  # type: ignore[arg-type]
    # 任务 4.2 起新增的业务异常
    app.add_exception_handler(DuplicateCredentialError, duplicate_credential_handler)  # type: ignore[arg-type]
    app.add_exception_handler(CredentialNotFoundError, credential_not_found_handler)  # type: ignore[arg-type]
    app.add_exception_handler(IllegalStateTransition, illegal_state_transition_handler)  # type: ignore[arg-type]
    # 任务 5.3 新增的 Passport 业务异常 + Policy 校验异常
    app.add_exception_handler(PassportNotFoundError, passport_not_found_handler)  # type: ignore[arg-type]
    app.add_exception_handler(PassportStateTransitionError, passport_state_transition_handler)  # type: ignore[arg-type]
    app.add_exception_handler(InvalidPolicyError, invalid_policy_handler)  # type: ignore[arg-type]


__all__ = [
    "credential_not_found_handler",
    "duplicate_credential_handler",
    "http_exception_handler",
    "illegal_state_transition_handler",
    "invalid_policy_handler",
    "invalid_token_handler",
    "passport_not_found_handler",
    "passport_state_transition_handler",
    "register_exception_handlers",
    "validation_exception_handler",
]
