"""Alembic 迁移环境（任务 2.2）。

职责：
1. 从环境变量 ``DATABASE_URL`` 读取数据库连接（不把生产密钥提交到 alembic.ini）。
2. 导入 ``app.models`` 触发全部 ORM 类注册到 ``Base.metadata``，让 ``--autogenerate`` 能识别全部 8 张表。
3. 同时支持 offline（生成 SQL 文件）与 online（直接连库执行）两种模式。

设计选择：使用同步引擎 —— 迁移操作罕见且对延迟不敏感，无需 asyncpg。
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# ---------------------------------------------------------------------------
# 1. 把 backend/ 加到 sys.path，确保 ``app.models`` 可被导入
# ---------------------------------------------------------------------------
# alembic 默认 cwd 为 alembic.ini 所在目录（即 backend/），但子进程或测试
# 场景下未必如此；显式 prepend 更稳。
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# Load .env file if present (for local development)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(_BACKEND_DIR / ".env")

# ---------------------------------------------------------------------------
# 2. 加载 ORM 模型 → 填充 Base.metadata
# ---------------------------------------------------------------------------
# ``app.models.__init__`` 会按依赖顺序导入 8 张表的 ORM 类，
# 此 import 一次性把所有 Table 对象注册到 ``Base.metadata``。
from app.models import Base  # noqa: E402

target_metadata = Base.metadata

# ---------------------------------------------------------------------------
# 3. Alembic Config 与日志
# ---------------------------------------------------------------------------
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 优先使用环境变量；alembic.ini 中的 sqlalchemy.url 是空字符串占位。
_db_url = os.environ.get("DATABASE_URL")
if _db_url:
    config.set_main_option("sqlalchemy.url", _db_url)


# ---------------------------------------------------------------------------
# 4. 迁移执行入口
# ---------------------------------------------------------------------------
def run_migrations_offline() -> None:
    """生成 SQL 脚本而不连接数据库（``alembic upgrade --sql``）。"""
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError(
            "DATABASE_URL 未设置且 alembic.ini sqlalchemy.url 为空，"
            "无法在 offline 模式生成迁移 SQL。"
        )
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """使用真实连接执行迁移（``alembic upgrade head``）。"""
    section = config.get_section(config.config_ini_section) or {}
    if not section.get("sqlalchemy.url"):
        raise RuntimeError(
            "DATABASE_URL 未设置；在线迁移需要可连接的 PostgreSQL。"
        )

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
