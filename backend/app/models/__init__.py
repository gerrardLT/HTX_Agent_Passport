"""SQLAlchemy ORM 模型包（任务 2.1）。

实现 PRD §8 + design.md 「Data Models」定义的 8 张表：

- ``users``              —— ``User``
- ``api_credentials``    —— ``ApiCredential``（含 ``encryption_algorithm`` / ``deleted_at``）
- ``agent_passports``    —— ``AgentPassport``
- ``agent_actions``      —— ``AgentAction``（含 ``trace_id`` / ``reason_codes`` /
                                              ``checkpoint_json`` / ``policy_version_at_planning``）
- ``approvals``          —— ``Approval``（含 ``expires_at``，``approved`` 允许 NULL）
- ``model_calls``        —— ``ModelCall``
- ``execution_results``  —— ``ExecutionResult``（含 ``model_call_id`` 外键 → ``model_calls.id``）
- ``audit_events``       —— ``AuditEvent``

导入顺序：被引用的表先导入（``ModelCall`` 早于 ``ExecutionResult``），
确保 Alembic ``autogenerate`` 推断的建表 DDL 顺序正确，避免延迟外键约束的麻烦。

所有模型共享 ``Base``（``DeclarativeBase``），可通过 ``Base.metadata`` 一次性建表。

辅助常量/枚举类位于 ``app.models.enums``：
- ``PassportState`` / ``ActionState`` / ``CredentialState`` / ``AuditEventType``
"""

from __future__ import annotations

from app.models.action import AgentAction
from app.models.approval import Approval
from app.models.audit import AuditEvent
from app.models.audit_tree_head import AuditTreeHead

# 基础设施
from app.models.base import (
    Base,
    CreatedAtMixin,
    UpdatedAtMixin,
    UUIDPrimaryKeyMixin,
)
from app.models.credential import ApiCredential

# 状态/事件常量
from app.models.enums import (
    ActionState,
    AuditEventType,
    CredentialState,
    PassportState,
)
from app.models.execution import ExecutionResult
from app.models.model_call import ModelCall
from app.models.passport import AgentPassport

# ORM 模型
# 顺序：先 User，再凭证 / 护照，再 ModelCall，再 Action，
# 最后 Approval / ExecutionResult / AuditEvent。
from app.models.user import User

__all__ = [
    # 基础
    "Base",
    "CreatedAtMixin",
    "UpdatedAtMixin",
    "UUIDPrimaryKeyMixin",
    # 状态/事件常量
    "ActionState",
    "AuditEventType",
    "CredentialState",
    "PassportState",
    # ORM 模型
    "User",
    "ApiCredential",
    "AgentPassport",
    "ModelCall",
    "AgentAction",
    "Approval",
    "ExecutionResult",
    "AuditEvent",
    "AuditTreeHead",
]
