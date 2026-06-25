"""集中配置加载（pydantic-settings）。

所有运行期参数都通过 ``Settings`` 暴露，``get_settings()`` 是带 LRU 缓存的单例。
配置项与仓库根 ``.env.example`` 完全对齐，方便开发者一键替换为真实环境值。

测试时如需覆盖环境变量，可：

1. 直接在 ``conftest`` / 用例里 ``monkeypatch.setenv("JWT_SECRET", "...")``，
   再调用 ``get_settings.cache_clear()``；
2. 或者实例化 ``Settings(JWT_SECRET="...")`` 直接传参绕过环境。

设计依据：design.md「核心组件」、Req 1 / Req 9 / Req 11 / Req 15 / Req 25。
"""

from __future__ import annotations

import logging
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """全局运行期配置。

    字段全部带默认值，开发模式下不指定 ``.env`` 也能跑通 demo；
    生产/演示部署须把以 ``replace_with_*`` 开头的占位符替换为真实值。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # 允许 .env 出现未声明字段（如未来阶段的新变量），不在加载时炸掉
        extra="ignore",
        case_sensitive=True,
    )

    # ---- 认证 / JWT（Req 1） ----
    JWT_SECRET: str = Field(
        default="dev-only-insecure-jwt-secret-change-me",
        description="HS256 签名密钥；生产环境必须替换。",
    )
    JWT_ALGORITHM: str = Field(default="HS256", description="JWT 签名算法。")
    JWT_EXPIRE_HOURS: int = Field(
        default=24,
        ge=1,
        le=24,
        description="JWT 过期时长（小时），Req 1 AC4 限制 ≤ 24h。",
    )
    DEMO_WALLET: str = Field(
        default="0xA11CE00000000000000000000000000000000001",
        description="默认登录钱包地址。",
    )

    # ---- 数据库（任务 2 / docker-compose） ----
    DATABASE_URL: str = Field(
        default="postgresql+psycopg://htx_agent:htx_agent_dev_password@localhost:5432/htx_agent_passport",
        description="SQLAlchemy 连接串；测试通过 fixture 覆盖。",
    )

    # ---- 运行模式开关（Req 9 / Req 15） ----
    DEMO_MODE: bool = Field(default=True, description="Sandbox 模式总开关。")
    DEMO_REAL_TRADE: bool = Field(
        default=False,
        description="real_trade 模式必须为 true 才会真实下单（Req 9 AC5）。",
    )
    DEMO_DISABLE_EXECUTION: bool = Field(
        default=False,
        description="全局 kill switch，true 时拒绝所有非只读操作（Req 7 AC12）。",
    )

    # ---- 审计哈希链（Req 11） ----
    GENESIS_HASH: str = Field(
        default="HTX_AGENT_PASSPORT_GENESIS_V1",
        description="审计链首事件的 previous_event_hash 常量。",
    )
    AUDIT_STH_SIGNING_KEY: str = Field(
        default="",
        description="审计 Merkle 树 STH 的 HMAC 签名密钥；空时回退到 JWT_SECRET。",
    )

    # ---- 审计 STH 签发与锚定（Phase 1 / G10-G11 跟进） ----
    AUDIT_STH_INTERVAL_SECONDS: float = Field(
        default=300.0,
        ge=0.01,
        le=86400.0,
        description=(
            "周期 STH 签发的间隔（秒）。默认 5 分钟；测试可调到 0.05 让快速跑 1-2 轮就停。"
            "用 float 是为了支持亚秒级测试；生产部署应当配置整数秒数。"
        ),
    )
    AUDIT_STH_ENABLED: bool = Field(
        default=True,
        description="是否启用周期 STH 签发后台任务（默认 True；CI 可通过 env 关闭）。",
    )
    AUDIT_STH_ANCHOR_PATH: str = Field(
        default="",
        description=(
            "外部锚定 JSONL 文件的绝对路径；空时只签 STH 不写文件。"
            "生产可写到只读挂载点 / S3 Object Lock 路径 / 公共 git 仓库。"
        ),
    )
    # ---- 锚定 backend 选择（Phase 2.5 / 详见 docs/tech-research/05-...md §5.2）----
    AUDIT_STH_ANCHOR_BACKEND: str = Field(
        default="jsonl",
        description=(
            "STH 锚定 backend：``jsonl``（默认；写本地文件）/ "
            "``s3``（生产推荐；S3 Object Lock Compliance 模式）/ "
            "``null``（显式禁用锚定）。"
        ),
    )
    AUDIT_STH_ANCHOR_S3_BUCKET: str = Field(
        default="",
        description=(
            "S3 锚定桶名；``AUDIT_STH_ANCHOR_BACKEND=s3`` 时必填。"
            "桶必须预先启用 ObjectLockEnabled + Versioning。"
        ),
    )
    AUDIT_STH_ANCHOR_S3_RETENTION_YEARS: int = Field(
        default=7,
        ge=1,
        le=100,
        description=(
            "S3 Object Lock Compliance retention 期（年）。"
            "SEC 17a-4 / SOC 2 常见 7 年；金融监管常见 10-15 年。"
        ),
    )
    AUDIT_STH_ANCHOR_S3_KEY_PREFIX: str = Field(
        default="sth/",
        description="S3 对象 key 前缀（多 backend 共桶时用以隔离）。",
    )
    AUDIT_STH_ANCHOR_S3_REGION: str = Field(
        default="",
        description="AWS region；空时从环境变量 / IAM 角色读取。",
    )
    AUDIT_STH_ANCHOR_S3_ENDPOINT: str = Field(
        default="",
        description=(
            "S3 兼容服务端点（MinIO / LocalStack 测试用）；"
            "生产留空走 AWS 默认。"
        ),
    )

    # ---- G2 信息流追踪（Phase 2 / docs/tech-research/06-...md §6.2）----
    ENFORCE_MARKET_PROVENANCE: bool = Field(
        default=False,
        description=(
            "G2 信息流追踪开关：启用后 policy_engine 在 place_order 路径"
            "拒绝 ``provenance ∉ {seed, htx_real, htx_cached}`` 的 market"
            "data。默认关闭以兼容现有 fixture / 测试；生产部署应启用。"
            "前提：所有 market_snapshot 调用方都已为 entry 显式标注 provenance。"
        ),
    )

    # ---- G1 Cedar shadow evaluator（Phase 2 / docs/tech-research/06-...md §6.1）----
    CEDAR_SHADOW_ENABLED: bool = Field(
        default=False,
        description=(
            "G1 Cedar 影子评估器开关：启用后 execution_gateway / approval_service"
            "重裁决时同时跑 cedarpy 评估 PolicyDSL Step 0-3,差异写日志（不影响"
            "主路径裁决）。30 天 0 差异后再考虑切换主裁决器。"
            "默认关闭；启用前提是 ``pip install cedarpy``（已包含在 requirements.txt）。"
        ),
    )
    AUDIT_STH_SIGNING_ALGO: str = Field(
        default="hmac-sha256",
        description=(
            "STH 签名算法：``hmac-sha256``（默认，向后兼容）/ ``ed25519``（生产推荐，"
            "对外可发布公钥 + RFC 8032 标准）。改为 ed25519 时必须配置 "
            "AUDIT_STH_ED25519_PRIVATE_KEY_PATH 指向私钥文件。"
        ),
    )
    AUDIT_STH_ED25519_PRIVATE_KEY_PATH: str = Field(
        default="",
        description=(
            "Ed25519 私钥文件路径（PEM PKCS8 或 raw 32 bytes 格式）；"
            "AUDIT_STH_SIGNING_ALGO=ed25519 时必填。"
        ),
    )
    AUDIT_STH_ED25519_PUBLIC_KEY_PATH: str = Field(
        default="",
        description=(
            "Ed25519 公钥文件路径；可选，留空时由 verifier-key 端点从私钥派生。"
            "公开发布给外部审计员 / 评委用以独立验证 STH。"
        ),
    )

    # ---- 凭证保险库（Req 2 / 任务 4.1，任务 3 不直接使用） ----
    VAULT_MASTER_KEY: str = Field(
        default="",
        description="AES-256-GCM 主密钥（64 hex chars）；任务 4 才使用。",
    )
    VAULT_KEY_PROVIDER: str = Field(
        default="local",
        description="KEK provider：local（默认，从 VAULT_MASTER_KEY 派生）/ aws-kms（生产）。",
    )
    VAULT_KMS_KEY_ID: str = Field(
        default="",
        description="AWS KMS CMK ID/ARN（VAULT_KEY_PROVIDER=aws-kms 时必填）。",
    )

    # ---- B.AI Planner 适配器（Req 5 / 任务 10） ----
    BAI_API_KEY: str = Field(default="")
    BAI_API_URL: str = Field(default="https://api.b.ai/v1")
    BAI_MODEL: str = Field(
        default="deepseek-v4-flash",
        description="B.AI 模型 ID（如 deepseek-v4-flash / gpt-5.2 / claude-sonnet-4-6）。",
    )
    BAI_TIMEOUT_SECONDS: int = Field(default=30, ge=1)
    BAI_MAX_RETRIES: int = Field(default=2, ge=0)

    # ---- HTX 适配器（Req 10 / 任务 12，任务 3 不直接使用） ----
    HTX_API_URL: str = Field(default="https://api.huobi.pro")

    # ---- CORS 配置（Task 4.2）----
    CORS_ORIGINS: list[str] = Field(
        default=["http://localhost:3000", "http://localhost:3001", "http://localhost:3002", "http://localhost:8888", "http://127.0.0.1:8888"],
        description=(
            "CORS 允许的源列表。开发默认 localhost:3000；"
            "生产环境必须配置为具体域名。"
        ),
    )

    # ---- 火山引擎 TTS 语音合成（产品演示视频配音）----
    VOLCENGINE_TTS_API_KEY: str = Field(
        default="",
        description="火山引擎 API Key（从控制台 > API Key管理获取）。",
    )
    VOLCENGINE_TTS_VOICE_ID: str = Field(
        default="zh_female_vv_uranus_bigtts",
        description="默认声音 ID（speaker）。可在火山引擎控制台音色库查看。",
    )

    # ---- 启动时安全校验（Task 4.3）----
    @field_validator("JWT_SECRET")
    @classmethod
    def validate_jwt_secret(cls, v: str) -> str:
        placeholder_values = {
            "dev-only-insecure-jwt-secret-change-me",
            "replace_with_jwt_secret",
            "",
        }
        if v in placeholder_values:
            logger.warning(
                "JWT_SECRET is using a default/placeholder value. "
                "This is INSECURE for production deployments."
            )
        if len(v) < 32:
            logger.warning(
                "JWT_SECRET is shorter than 32 characters. "
                "Consider using a longer random secret."
            )
        return v

    @field_validator("VAULT_MASTER_KEY")
    @classmethod
    def validate_vault_key(cls, v: str) -> str:
        placeholder_values = {
            "replace_with_64_hex_master_key",
            "0000000000000000000000000000000000000000000000000000000000000000",
        }
        if v in placeholder_values:
            logger.warning(
                "VAULT_MASTER_KEY is using a placeholder value. "
                "Credential encryption will be INSECURE."
            )
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """带 LRU 缓存的 ``Settings`` 单例工厂。

    在测试中改变环境变量后，调用 ``get_settings.cache_clear()`` 即可强制重读。
    """
    return Settings()


__all__ = ["Settings", "get_settings"]
