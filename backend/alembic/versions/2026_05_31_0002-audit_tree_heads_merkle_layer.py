"""add audit_tree_heads (Merkle STH layer)

修复 G10/G11：在线性审计哈希链之上追加 Merkle 树 + Signed Tree Head 层。
表本身只存 STH（root_hash + tree_size + signature）；Merkle 树叶子由
``audit_events.event_hash`` 派生（见 app/core/merkle.py）。

Revision ID: audit_merkle_v1
Revises: init_schema
Create Date: 2026-05-31 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "audit_merkle_v1"
down_revision: str | None = "init_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_tree_heads",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "passport_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_passports.id"),
            nullable=True,
        ),
        sa.Column("tree_size", sa.Integer(), nullable=False),
        sa.Column("root_hash", sa.Text(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    # 查询「某链最新 STH」高频路径：按 (user_id, passport_id, signed_at DESC) 索引。
    op.create_index(
        "idx_audit_tree_heads_chain_signed_at",
        "audit_tree_heads",
        ["user_id", "passport_id", sa.text("signed_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("idx_audit_tree_heads_chain_signed_at", table_name="audit_tree_heads")
    op.drop_table("audit_tree_heads")
