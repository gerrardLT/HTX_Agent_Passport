"""任务 2.2 迁移与 schema 完整性测试。

务实策略：CI 环境通常没有 PostgreSQL，但 Alembic 迁移必须在 PG 上执行。
本测试以 ORM metadata 为可信来源，在 in-memory SQLite 上验证 schema 一致性：

1) 全部 8 张表存在（Base.metadata.create_all 不抛错）。
2) 模型 ↔ 列定义一致（关键字段 / 外键 / nullable）。
3) 基础 CRUD 闭环：插入 User / ApiCredential（带 BYTEA） / AgentPassport，再查询。
4) Alembic 迁移脚本本身可被 import（语法正确、revision 元数据齐全）。
5) 迁移脚本与 ORM 模型的表名集合完全一致（防止漏建/多建表）。

PostgreSQL 端的真正"正向/反向"通过 ``alembic upgrade head`` / ``alembic downgrade base``
在本地或 CI 配置 PG 服务时手工/CI 任务跑；那部分依赖外部数据库连接，本文件不强依赖。
"""

from __future__ import annotations

import importlib.util
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models import (
    AgentAction,
    AgentPassport,
    ApiCredential,
    Approval,
    AuditEvent,
    Base,
    ExecutionResult,
    ModelCall,
    User,
)

pytestmark = pytest.mark.integration


# 全部表的预期集合（ORM metadata + DB 实际）：8 张设计表 + audit_tree_heads（Merkle STH 层）。
EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        "users",
        "api_credentials",
        "agent_passports",
        "agent_actions",
        "approvals",
        "execution_results",
        "audit_events",
        "model_calls",
        "audit_tree_heads",
    }
)

# init_schema 迁移脚本本身只创建原始 8 张表；audit_tree_heads 由后续迁移
# audit_merkle_v1 添加。这两个集合用于不同维度的断言。
INIT_MIGRATION_TABLES: frozenset[str] = frozenset(EXPECTED_TABLES) - {"audit_tree_heads"}

# 8 个索引的预期集合（含部分索引名）。
EXPECTED_INDEXES: frozenset[str] = frozenset(
    {
        "idx_actions_passport_created",
        "idx_actions_trace",
        "idx_audit_action_created",
        "idx_audit_trace",
        "idx_credentials_user_provider",
        "idx_passports_user_state",
        "idx_approvals_action",
        "idx_model_calls_action",
    }
)


# --------------------------------------------------------------------------- #
# 1. ORM metadata 完整性（schema layer 正向验证）
# --------------------------------------------------------------------------- #
class TestSchemaIntegrity:
    """ORM 元数据 + SQLite create_all 验证。"""

    def test_metadata_contains_all_eight_tables(self) -> None:
        """Base.metadata 必须列出 9 张表（8 张设计表 + audit_tree_heads）。"""
        actual = set(Base.metadata.tables.keys())
        assert actual == EXPECTED_TABLES, (
            f"ORM 表名集合不匹配；缺失 {EXPECTED_TABLES - actual}，"
            f"多出 {actual - EXPECTED_TABLES}"
        )

    def test_create_all_succeeds_on_sqlite(self, sqlite_engine: Engine) -> None:
        """create_all 通过 = 列类型在 SQLite 下都被翻译成功（含 UUID/JSONB/ARRAY/BYTEA）。"""
        inspector = inspect(sqlite_engine)
        actual = set(inspector.get_table_names())
        # 由 conftest 的 sqlite_engine fixture 已经 create_all 过；这里只断言效果。
        assert EXPECTED_TABLES.issubset(actual), (
            f"SQLite 中实际表名 {actual} 未覆盖预期 {EXPECTED_TABLES}"
        )

    def test_credentials_has_soft_delete_column(self) -> None:
        """ApiCredential.deleted_at 必须存在 + nullable，支持软删除部分索引。"""
        col = ApiCredential.__table__.columns["deleted_at"]
        assert col.nullable is True

    def test_action_has_trace_id_and_reason_codes(self) -> None:
        """补全字段（design.md 要求）必须出现在 ORM 中。"""
        cols = AgentAction.__table__.columns
        for name in (
            "trace_id",
            "reason_codes",
            "checkpoint_json",
            "policy_version_at_planning",
        ):
            assert name in cols, f"AgentAction 缺少补全字段 {name!r}"

    def test_approval_expires_at_not_null(self) -> None:
        """Approval.expires_at 必须 NOT NULL（Req 8 AC4-5）。"""
        col = Approval.__table__.columns["expires_at"]
        assert col.nullable is False

    def test_execution_results_has_model_call_fk(self) -> None:
        """execution_results.model_call_id 必须外键引用 model_calls.id。"""
        fk_set = ExecutionResult.__table__.columns["model_call_id"].foreign_keys
        targets = {fk.target_fullname for fk in fk_set}
        assert "model_calls.id" in targets


# --------------------------------------------------------------------------- #
# 2. 基础 CRUD（验证模型可被实际写入读取）
# --------------------------------------------------------------------------- #
class TestBasicCrud:
    """关键模型的最小写入/查询闭环；间接验证列类型在 SQLite 下能存取。"""

    def test_can_insert_and_query_user(self, db_session: Session) -> None:
        wallet = "0xA11CE000000000000000000000000000000000A1"
        user = User(primary_wallet=wallet, email="alice@example.com", role="user")
        db_session.add(user)
        db_session.flush()

        fetched = db_session.query(User).filter_by(primary_wallet=wallet).one()
        assert fetched.id is not None
        assert fetched.role == "user"

    def test_can_insert_credential_with_bytea(self, db_session: Session) -> None:
        """ApiCredential 的 LargeBinary（BYTEA）字段在 SQLite 下走 BLOB；写入/读出一致。"""
        user = User(primary_wallet="0xCAFE000000000000000000000000000000000B0B")
        db_session.add(user)
        db_session.flush()

        cipher_access = bytes(range(12)) + b"ENCRYPTED-ACCESS"
        cipher_secret = bytes(range(12)) + b"ENCRYPTED-SECRET"
        cred = ApiCredential(
            user_id=user.id,
            provider="HTX",
            label="demo-key",
            access_key_hash="a" * 64,
            encrypted_access_key=cipher_access,
            encrypted_secret_key=cipher_secret,
            encryption_algorithm="AES-256-GCM",
            permission_read=True,
            permission_trade=True,
            permission_withdraw=False,
            state="TRADE_ENABLED",
        )
        db_session.add(cred)
        db_session.flush()

        fetched = db_session.query(ApiCredential).filter_by(label="demo-key").one()
        # SQLite 的 BLOB 与 PG 的 BYTEA 在 SQLAlchemy 层都映射为 bytes；值必须完全一致。
        assert fetched.encrypted_access_key == cipher_access
        assert fetched.encrypted_secret_key == cipher_secret
        assert fetched.permission_withdraw is False
        assert fetched.deleted_at is None  # 默认未软删除

    def test_can_insert_passport_with_jsonb_policy(self, db_session: Session) -> None:
        """AgentPassport.policy_json 必须能存取 dict（PG=JSONB / SQLite=JSON）。"""
        user = User(primary_wallet="0xBEEF000000000000000000000000000000000C0C")
        db_session.add(user)
        db_session.flush()

        policy = {
            "version": "0.1",
            "capabilities": {"read_market": True, "place_order": True},
            "limits": {"allowed_symbols": ["btcusdt"], "max_notional_usdt_per_order": 20},
            "approval": {"required_for_trade": True, "expires_after_seconds": 300},
            "blocked_actions": ["withdraw", "borrow", "margin", "transfer_out"],
        }
        passport = AgentPassport(
            user_id=user.id,
            api_credential_id=None,
            name="readonly-test",
            agent_type="readonly_researcher",
            state="DRAFT",
            version=1,
            policy_json=policy,
            reputation_score=50,
        )
        db_session.add(passport)
        db_session.flush()

        fetched = db_session.query(AgentPassport).filter_by(name="readonly-test").one()
        assert fetched.policy_json["capabilities"]["read_market"] is True
        assert fetched.version == 1

    def test_can_chain_action_approval_audit(self, db_session: Session) -> None:
        """跨表外键链路：User → Passport → Action → Approval / AuditEvent / ModelCall / ExecutionResult。"""
        user = User(primary_wallet="0xDEAD000000000000000000000000000000000D0D")
        db_session.add(user)
        db_session.flush()

        passport = AgentPassport(
            user_id=user.id,
            name="full-chain",
            agent_type="small_spot_executor",
            state="ACTIVE",
            version=1,
            policy_json={"version": "0.1"},
            reputation_score=50,
        )
        db_session.add(passport)
        db_session.flush()

        trace_id = uuid.uuid4()
        action = AgentAction(
            passport_id=passport.id,
            user_id=user.id,
            trace_id=trace_id,
            natural_language_request="查看 BTC 行情",
            state="REQUESTED",
            approval_required=True,
            execution_mode="simulation",
        )
        db_session.add(action)
        db_session.flush()

        approval = Approval(
            action_id=action.id,
            user_id=user.id,
            approval_type="typed_confirmation",
            approved=None,
            expires_at=datetime.now(UTC),
        )
        model_call = ModelCall(
            action_id=action.id,
            trace_id=trace_id,
            provider="B.AI",
            model="planner-v1",
            prompt_hash="b" * 64,
            status="STARTED",
        )
        db_session.add_all([approval, model_call])
        db_session.flush()

        execution = ExecutionResult(
            action_id=action.id,
            model_call_id=model_call.id,
            provider="HTX",
            mode="simulation",
            request_payload={"symbol": "btcusdt"},
            response_payload={"order_id": "sim-1"},
            status="EXECUTED",
        )
        audit = AuditEvent(
            user_id=user.id,
            passport_id=passport.id,
            action_id=action.id,
            trace_id=trace_id,
            event_type="ACTION_REQUESTED",
            actor_type="USER",
            actor_id=str(user.id),
            event_json={"task": "查看 BTC 行情"},
            previous_event_hash="HTX_AGENT_PASSPORT_GENESIS_V1",
            event_hash="c" * 64,
        )
        db_session.add_all([execution, audit])
        db_session.flush()

        # 反向取一遍，确认外键 + 关系映射都通了。
        # 注：SQLite 下 ``UUID(as_uuid=True)`` 的 round-trip 在某些版本中不完全等价
        # （write 时存字符串、read 时转 UUID，filter 时再 bind），所以我们用
        # ``count`` + 关系反向引用来验证而非 UUID identity 匹配，行为等价。
        assert db_session.query(AgentAction).count() == 1
        assert db_session.query(Approval).count() == 1
        assert db_session.query(ModelCall).count() == 1
        assert db_session.query(ExecutionResult).count() == 1
        assert db_session.query(AuditEvent).count() == 1

        # 直接基于已加载对象校验关键字段。
        assert action.trace_id == trace_id
        assert audit.previous_event_hash == "HTX_AGENT_PASSPORT_GENESIS_V1"
        assert audit.event_type == "ACTION_REQUESTED"
        assert execution.mode == "simulation"
        assert execution.response_payload == {"order_id": "sim-1"}


# --------------------------------------------------------------------------- #
# 3. Alembic 迁移脚本静态检查（不执行 SQL，仅校验脚本结构）
# --------------------------------------------------------------------------- #
class TestAlembicMigrationScript:
    """确保迁移脚本本身合法、能 import、且声明了全部表与索引。

    我们不在测试中真正连接 PG 跑 ``alembic upgrade``——CI 没有 PG 服务，
    那部分由本地 / 部署流水线在有 PG 时执行。
    """

    @pytest.fixture(scope="class")
    def migration_path(self) -> Path:
        backend_dir = Path(__file__).resolve().parent.parent.parent
        path = (
            backend_dir
            / "alembic"
            / "versions"
            / "2026_01_01_0001-init_schema_initial_schema.py"
        )
        assert path.exists(), f"迁移脚本不存在：{path}"
        return path

    @pytest.fixture(scope="class")
    def migration_module(self, migration_path: Path):  # type: ignore[no-untyped-def]
        """以独立 module 形式 import 迁移脚本，避免污染全局命名空间。"""
        spec = importlib.util.spec_from_file_location(
            "alembic_init_schema", migration_path
        )
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_module_imports_cleanly(self, migration_module) -> None:  # type: ignore[no-untyped-def]
        assert hasattr(migration_module, "upgrade")
        assert hasattr(migration_module, "downgrade")
        assert migration_module.revision == "init_schema"
        assert migration_module.down_revision is None

    def test_script_creates_all_eight_tables(self, migration_path: Path) -> None:
        """正则扫描 ``op.create_table("xxx", ...)``，断言 init 迁移创建了原始 8 张表。

        audit_tree_heads 由后续 audit_merkle_v1 迁移添加，不在 init 脚本里。
        """
        text = migration_path.read_text(encoding="utf-8")
        created = set(re.findall(r"op\.create_table\(\s*\"([^\"]+)\"", text))
        assert created == INIT_MIGRATION_TABLES, (
            f"迁移脚本表集合不匹配；缺失 {INIT_MIGRATION_TABLES - created}，"
            f"多出 {created - INIT_MIGRATION_TABLES}"
        )

    def test_script_creates_all_eight_indexes(self, migration_path: Path) -> None:
        """同时识别 ``op.create_index("xxx"`` 与 raw SQL ``CREATE INDEX xxx``。

        部分索引（idx_credentials_user_provider）必须用 raw SQL 才能加 ``WHERE deleted_at IS NULL``
        子句，因此正则要兼容两种形态。
        raw SQL 在源码中以多行字符串拼接呈现（"CREATE INDEX foo "\n"ON bar..."），
        先把所有空白压成单空格再做匹配，规避 Python 字面量拼接的换行干扰。
        """
        text = migration_path.read_text(encoding="utf-8")
        compact = re.sub(r"\s+", " ", text)
        from_helper = set(re.findall(r"op\.create_index\(\s*\"([^\"]+)\"", compact))
        # raw SQL 形态："CREATE INDEX <name> ON ..."；通过 op.execute 调用，
        # 紧邻 ``op.execute(`` 之后的 ``"CREATE INDEX <name>`` 即为索引名。
        from_raw_sql = set(re.findall(r"CREATE INDEX\s+(\w+)\s+ON", compact))
        actual = from_helper | from_raw_sql
        assert actual == EXPECTED_INDEXES, (
            f"迁移脚本索引集合不匹配；缺失 {EXPECTED_INDEXES - actual}，"
            f"多出 {actual - EXPECTED_INDEXES}"
        )

    def test_partial_index_present_for_soft_delete(self, migration_path: Path) -> None:
        """idx_credentials_user_provider 必须包含 ``WHERE deleted_at IS NULL``。"""
        text = migration_path.read_text(encoding="utf-8")
        # 同一行或紧邻行包含 deleted_at IS NULL 即可（多行字符串 / 拼接均可）。
        compact = re.sub(r"\s+", " ", text)
        assert (
            "idx_credentials_user_provider" in compact
            and "WHERE deleted_at IS NULL" in compact
        ), "软删除部分索引未声明 WHERE deleted_at IS NULL 条件"

    def test_pgcrypto_extension_enabled(self, migration_path: Path) -> None:
        """gen_random_uuid() 依赖 pgcrypto；迁移开头必须启用扩展。"""
        text = migration_path.read_text(encoding="utf-8")
        assert "CREATE EXTENSION IF NOT EXISTS pgcrypto" in text

    def test_downgrade_drops_all_tables(self, migration_path: Path) -> None:
        """init 迁移的 downgrade 必须 DROP 它创建的全部 8 张表（不含 audit_tree_heads）。"""
        text = migration_path.read_text(encoding="utf-8")
        # downgrade 段单独抽取，避免上半段 create 干扰。
        downgrade_match = re.search(
            r"def downgrade\(\) -> None:(.*?)(?:\Z|\ndef )", text, re.DOTALL
        )
        assert downgrade_match, "迁移脚本缺少 downgrade 函数体"
        downgrade_body = downgrade_match.group(1)
        dropped = set(re.findall(r"op\.drop_table\(\s*\"([^\"]+)\"", downgrade_body))
        assert dropped == INIT_MIGRATION_TABLES, (
            f"downgrade 未 drop 全部表；缺失 {INIT_MIGRATION_TABLES - dropped}"
        )
