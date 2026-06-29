"""initial schema

任务 2.2 的初始迁移：创建全部 8 张表 + 8 个索引
（含 ``api_credentials`` 软删除部分索引 ``WHERE deleted_at IS NULL``）。

Revision ID: init_schema
Revises:
Create Date: 2026-01-01 00:01:00

设计依据：
- requirements.md Req 2 / Req 3 / Req 8 / Req 11 / Req 13
- design.md「Data Models」CREATE TABLE 列表 + 8 个索引
- app/models/* ORM 模型（与本迁移须保持字段定义一致）

依赖顺序（满足外键约束 + design.md 注释关于 ``model_calls`` 必须先于 ``execution_results`` 建表）：
    users → api_credentials → agent_passports → model_calls → agent_actions
          → approvals → execution_results → audit_events
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "init_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # 0. 启用 pgcrypto 扩展：``gen_random_uuid()`` 与软删除部分索引依赖。
    #    PostgreSQL 13+ 自带 pgcrypto，无需额外安装。
    # ------------------------------------------------------------------ #
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")

    # ------------------------------------------------------------------ #
    # 1. users
    # ------------------------------------------------------------------ #
    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("primary_wallet", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False, server_default=sa.text("'user'")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("primary_wallet", name="uq_users_primary_wallet"),
    )

    # ------------------------------------------------------------------ #
    # 2. api_credentials
    # ------------------------------------------------------------------ #
    op.create_table(
        "api_credentials",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "provider", sa.Text(), nullable=False, server_default=sa.text("'HTX'")
        ),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("access_key_hash", sa.Text(), nullable=False),
        sa.Column("encrypted_access_key", sa.LargeBinary(), nullable=False),
        sa.Column("encrypted_secret_key", sa.LargeBinary(), nullable=False),
        sa.Column(
            "encryption_algorithm",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'AES-256-GCM'"),
        ),
        sa.Column(
            "permission_read",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "permission_trade",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "permission_withdraw",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "ip_whitelist_detected",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "state", sa.Text(), nullable=False, server_default=sa.text("'CREATED'")
        ),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ------------------------------------------------------------------ #
    # 3. agent_passports
    # ------------------------------------------------------------------ #
    op.create_table(
        "agent_passports",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "api_credential_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("api_credentials.id"),
            nullable=True,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("agent_type", sa.Text(), nullable=False),
        sa.Column(
            "state", sa.Text(), nullable=False, server_default=sa.text("'DRAFT'")
        ),
        sa.Column(
            "version", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column("policy_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "reputation_score",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("50"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ------------------------------------------------------------------ #
    # 4. model_calls
    # 注：必须先于 execution_results（后者外键引用 model_calls.id）。
    # ------------------------------------------------------------------ #
    op.create_table(
        "model_calls",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # 注意：这里先不设 action_id 的 FK（agent_actions 还没建），
        # 在 agent_actions 建好后再用 ALTER TABLE 加上。
        sa.Column("action_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "provider", sa.Text(), nullable=False, server_default=sa.text("'B.AI'")
        ),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_hash", sa.Text(), nullable=False),
        sa.Column("input_token_count", sa.Integer(), nullable=True),
        sa.Column("output_token_count", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "status", sa.Text(), nullable=False, server_default=sa.text("'STARTED'")
        ),
        sa.Column("raw_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ------------------------------------------------------------------ #
    # 5. agent_actions
    # ------------------------------------------------------------------ #
    op.create_table(
        "agent_actions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "passport_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_passports.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("natural_language_request", sa.Text(), nullable=False),
        sa.Column(
            "normalized_action_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "state", sa.Text(), nullable=False, server_default=sa.text("'REQUESTED'")
        ),
        sa.Column("risk_verdict", sa.Text(), nullable=True),
        sa.Column("risk_score", sa.Integer(), nullable=True),
        sa.Column("reason_codes", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column(
            "approval_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "execution_mode",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'simulation'"),
        ),
        sa.Column("policy_version_at_planning", sa.Integer(), nullable=True),
        sa.Column(
            "checkpoint_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # 现在 agent_actions 已存在，补上 model_calls.action_id 外键。
    op.create_foreign_key(
        "fk_model_calls_action_id_agent_actions",
        source_table="model_calls",
        referent_table="agent_actions",
        local_cols=["action_id"],
        remote_cols=["id"],
    )

    # ------------------------------------------------------------------ #
    # 6. approvals
    # ------------------------------------------------------------------ #
    op.create_table(
        "approvals",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "action_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_actions.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "approval_type",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'typed_confirmation'"),
        ),
        sa.Column("signed_payload", sa.Text(), nullable=True),
        sa.Column("approved", sa.Boolean(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ------------------------------------------------------------------ #
    # 7. execution_results
    # ------------------------------------------------------------------ #
    op.create_table(
        "execution_results",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "action_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_actions.id"),
            nullable=False,
        ),
        sa.Column(
            "model_call_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("model_calls.id"),
            nullable=True,
        ),
        sa.Column(
            "provider", sa.Text(), nullable=False, server_default=sa.text("'HTX'")
        ),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column(
            "request_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "response_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("provider_order_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ------------------------------------------------------------------ #
    # 8. audit_events
    # ------------------------------------------------------------------ #
    op.create_table(
        "audit_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "passport_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_passports.id"),
            nullable=True,
        ),
        sa.Column(
            "action_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_actions.id"),
            nullable=True,
        ),
        sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("event_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("previous_event_hash", sa.Text(), nullable=True),
        sa.Column("event_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ------------------------------------------------------------------ #
    # 9. 索引（design.md「Data Models」末尾 8 条 CREATE INDEX）
    # ------------------------------------------------------------------ #
    # idx_actions_passport_created：按 passport 拉最近 action（DESC 排序需 raw SQL）。
    op.execute("CREATE INDEX idx_actions_passport_created ON agent_actions (passport_id, created_at DESC);")
    # idx_actions_trace：按 trace_id 串联日志。
    op.create_index(
        "idx_actions_trace",
        "agent_actions",
        ["trace_id"],
    )
    # idx_audit_action_created：审计重放按 action_id + 时间序拉取（ASC 排序）。
    op.execute("CREATE INDEX idx_audit_action_created ON audit_events (action_id, created_at ASC);")
    # idx_audit_trace
    op.create_index(
        "idx_audit_trace",
        "audit_events",
        ["trace_id"],
    )
    # idx_credentials_user_provider：软删除部分索引（仅活跃凭证），
    # 既加速「按用户+提供方查询」，又避免与已删除条目重复。
    op.execute("CREATE INDEX idx_credentials_user_provider ON api_credentials (user_id, provider) WHERE deleted_at IS NULL;")
    # idx_passports_user_state
    op.create_index(
        "idx_passports_user_state",
        "agent_passports",
        ["user_id", "state"],
    )
    # idx_approvals_action
    op.create_index(
        "idx_approvals_action",
        "approvals",
        ["action_id"],
    )
    # idx_model_calls_action
    op.create_index(
        "idx_model_calls_action",
        "model_calls",
        ["action_id"],
    )


def downgrade() -> None:
    """反向 DROP，顺序与 upgrade 严格相反，避免外键约束失败。"""
    # 1) 索引（含部分索引）
    op.drop_index("idx_model_calls_action", table_name="model_calls")
    op.drop_index("idx_approvals_action", table_name="approvals")
    op.drop_index("idx_passports_user_state", table_name="agent_passports")
    op.execute("DROP INDEX IF EXISTS idx_credentials_user_provider;")
    op.drop_index("idx_audit_trace", table_name="audit_events")
    op.execute("DROP INDEX IF EXISTS idx_audit_action_created;")
    op.drop_index("idx_actions_trace", table_name="agent_actions")
    op.execute("DROP INDEX IF EXISTS idx_actions_passport_created;")

    # 2) 表（反向依赖序）
    op.drop_table("audit_events")
    op.drop_table("execution_results")
    op.drop_table("approvals")

    # model_calls.action_id → agent_actions 的循环外键须先解开
    op.drop_constraint(
        "fk_model_calls_action_id_agent_actions",
        "model_calls",
        type_="foreignkey",
    )
    op.drop_table("agent_actions")
    op.drop_table("model_calls")
    op.drop_table("agent_passports")
    op.drop_table("api_credentials")
    op.drop_table("users")

    # 3) 扩展：保守起见不 DROP EXTENSION，避免影响其他 schema。
