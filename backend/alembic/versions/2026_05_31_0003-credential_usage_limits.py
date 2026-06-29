"""add credential usage limits (G12 / Phase 3)

修复 G12：把 HTX API 凭证从"永久 + 无限次"升级到"可配置过期 + 每日次数上限"。

新字段（全部 nullable，向后兼容）：
- ``max_uses_per_day``：每日 HTX API 调用次数上限；NULL = 无限。
- ``current_uses_today``：当日（UTC）已用次数；UTC 0:00 由应用层 lazy 重置。
- ``last_use_at``：最近一次签名调用时间。
- ``expires_at``：凭证显式过期时间；NULL = 永久。

Revision ID: credential_usage_v1
Revises: audit_merkle_v1
Create Date: 2026-05-31 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "credential_usage_v1"
down_revision: str | None = "audit_merkle_v1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """添加 4 个 G12 字段到 ``api_credentials``。

    ``current_uses_today`` 设默认 0（NOT NULL）—— 老凭证升级后默认 0 次使用,
    与新代码逻辑一致；其他三个 nullable 默认 None = 无限制。
    """
    op.add_column(
        "api_credentials",
        sa.Column("max_uses_per_day", sa.Integer(), nullable=True),
    )
    op.add_column(
        "api_credentials",
        sa.Column(
            "current_uses_today",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "api_credentials",
        sa.Column(
            "last_use_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "api_credentials",
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("api_credentials", "expires_at")
    op.drop_column("api_credentials", "last_use_at")
    op.drop_column("api_credentials", "current_uses_today")
    op.drop_column("api_credentials", "max_uses_per_day")
