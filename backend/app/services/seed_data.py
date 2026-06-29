"""Demo 种子数据（任务 19 / Req 25 / PRD §17）。

提供种子常量和 load_seed_data 函数，在 Demo 模式启动时自动加载
预设用户、凭证、护照，确保演示无外部依赖。
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from sqlalchemy.orm import Session

from app.models import AgentPassport, ApiCredential, User
from app.models.enums import CredentialState, PassportState

# ---------------------------------------------------------------------------
# 种子常量（PRD §17）
# ---------------------------------------------------------------------------
SEED_USER_WALLET: Final[str] = "0xA11CE00000000000000000000000000000000001"

SEED_CREDENTIAL_LABEL: Final[str] = "Demo HTX Key"

SEED_PASSPORT_NAME: Final[str] = "Genesis Small Spot Agent"

#: 种子行情数据（PRD §17 / Req 25 demo seed）。
#:
#: ``as_of`` 字段（修复 G16 / Req 16 AC2）：
#: 标记 snapshot 抓取时间，给 :func:`stale_price_check` 在审批/执行重裁决时
#: 判定时效性。这里取一个**静态时间戳**——种子数据本就不"实时"，把它定在
#: 与项目其他 fixture 一致的 ``2024-06-15T12:00:00+00:00``，让单元测试直接
#: 依赖 SEED_MARKET_DATA 时可显式传 ``now`` 控制是否触发"过期"分支。
SEED_MARKET_DATA: Final[dict[str, dict[str, Any]]] = {
    "btcusdt": {
        "last": 68000.0,
        "bid": 67999.0,
        "ask": 68001.0,
        "as_of": "2024-06-15T12:00:00+00:00",
        # G2 / Phase 2：trust label——种子数据视为可信。
        "provenance": "seed",
    },
    "ethusdt": {
        "last": 3600.0,
        "bid": 3599.0,
        "ask": 3601.0,
        "as_of": "2024-06-15T12:00:00+00:00",
        "provenance": "seed",
    },
}

SEED_TASKS: Final[dict[str, str]] = {
    "happy": "查看 BTC/USDT 并准备一个 10 USDT 的限价买入单，仅当它在我的策略范围内。",
    "reject": "立即将我所有的 USDT 提现到这个地址。",
    "over_limit": "现在买入 500 USDT 的 BTC。",
}

SEED_POLICY: Final[dict[str, Any]] = {
    "version": "0.1",
    "capabilities": {
        "read_market": True,
        "read_account": True,
        "place_order": True,
        "cancel_order": True,
        "withdraw": False,
    },
    "limits": {
        "allowed_symbols": ["btcusdt", "ethusdt"],
        "max_notional_usdt_per_order": 20,
        "max_daily_notional_usdt": 100,
        "max_orders_per_day": 10,
    },
    "approval": {
        "required_for_trade": True,
        "required_for_policy_change": True,
        "expires_after_seconds": 300,
    },
    "blocked_actions": ["withdraw", "borrow", "margin", "transfer_out", "unknown_tool_call"],
}


# ---------------------------------------------------------------------------
# 加载函数
# ---------------------------------------------------------------------------
def load_seed_data(session: Session) -> dict[str, uuid.UUID]:
    """加载种子数据到数据库（幂等：已存在则跳过）。

    Returns
    -------
    dict[str, uuid.UUID]
        {"user_id": ..., "credential_id": ..., "passport_id": ...}
    """
    from sqlalchemy import select

    # 1. 用户
    user = session.execute(
        select(User).where(User.primary_wallet == SEED_USER_WALLET)
    ).scalar_one_or_none()
    if user is None:
        user = User(primary_wallet=SEED_USER_WALLET)
        session.add(user)
        session.flush()

    # 2. 凭证（mock 加密：直接用 b"DEMO" 占位）
    cred = session.execute(
        select(ApiCredential).where(
            ApiCredential.user_id == user.id,
            ApiCredential.label == SEED_CREDENTIAL_LABEL,
            ApiCredential.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if cred is None:
        cred = ApiCredential(
            user_id=user.id,
            label=SEED_CREDENTIAL_LABEL,
            access_key_hash="demo_access_key_hash_" + "0" * 44,
            encrypted_access_key=b"DEMO_ENCRYPTED_ACCESS",
            encrypted_secret_key=b"DEMO_ENCRYPTED_SECRET",
            encryption_algorithm="AES-256-GCM",
            permission_read=True,
            permission_trade=True,
            permission_withdraw=False,
            state=CredentialState.TRADE_ENABLED,
        )
        session.add(cred)
        session.flush()

    # 3. 护照
    passport = session.execute(
        select(AgentPassport).where(
            AgentPassport.user_id == user.id,
            AgentPassport.name == SEED_PASSPORT_NAME,
        )
    ).scalar_one_or_none()
    if passport is None:
        passport = AgentPassport(
            user_id=user.id,
            api_credential_id=cred.id,
            name=SEED_PASSPORT_NAME,
            agent_type="small_spot_executor",
            state=PassportState.ACTIVE,
            version=1,
            policy_json=SEED_POLICY,
            reputation_score=50,
        )
        session.add(passport)
        session.flush()

    session.commit()

    return {
        "user_id": user.id,
        "credential_id": cred.id,
        "passport_id": passport.id,
    }


__all__ = [
    "SEED_CREDENTIAL_LABEL",
    "SEED_MARKET_DATA",
    "SEED_PASSPORT_NAME",
    "SEED_POLICY",
    "SEED_TASKS",
    "SEED_USER_WALLET",
    "load_seed_data",
]
