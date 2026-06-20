"""SQLAlchemy 2.0 DeclarativeBase + 通用 mixin。

提供：

- ``Base``                —— 全部 ORM 模型的声明基类（DeclarativeBase）。
- ``UUIDPrimaryKeyMixin`` —— ``id UUID PRIMARY KEY DEFAULT gen_random_uuid()``。
- ``CreatedAtMixin``      —— ``created_at TIMESTAMPTZ NOT NULL DEFAULT now()``。
- ``UpdatedAtMixin``      —— ``updated_at TIMESTAMPTZ NOT NULL DEFAULT now()``，更新时由
  数据库 ``onupdate=func.now()`` 维护，避免应用层忘记同步。

设计依据：design.md 「Data Models」与 PRD §8。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有 ORM 模型的声明基类。"""


class UUIDPrimaryKeyMixin:
    """UUID 主键 mixin。

    使用 PostgreSQL 内置 ``gen_random_uuid()`` 生成（pgcrypto 扩展自 PG13 起内置）。
    """

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )


class CreatedAtMixin:
    """``created_at`` 时间戳 mixin（TIMESTAMPTZ）。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class UpdatedAtMixin:
    """``updated_at`` 时间戳 mixin（TIMESTAMPTZ）。

    ``onupdate=func.now()`` 让数据库在 UPDATE 时自动刷新，避免应用层遗漏。
    """

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
