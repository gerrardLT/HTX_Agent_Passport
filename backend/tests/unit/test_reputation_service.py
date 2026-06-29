"""任务 15 声誉服务单元测试。

覆盖维度（Req 24 AC1-4）：
1. EXECUTED → +2
2. AUTO_REJECTED → -3
3. EXECUTION_FAILED → -5
4. 分数不低于 0
5. 分数不高于 100
6. 未知 outcome 不变
7. 写入 REPUTATION_UPDATED 审计事件
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentPassport, AuditEvent, User
from app.models.enums import AuditEventType
from app.services.reputation_service import ReputationService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_user(db_session: Session, wallet: str = "0xREP_TEST_0001") -> User:
    """创建测试用户。"""
    user = User(primary_wallet=wallet)
    db_session.add(user)
    db_session.flush()
    return user


def _make_passport(
    db_session: Session, user: User, reputation_score: int = 50
) -> AgentPassport:
    """创建测试 passport。"""
    passport = AgentPassport(
        user_id=user.id,
        name="test-passport",
        agent_type="trader",
        state="ACTIVE",
        policy_json={
            "version": "0.1",
            "capabilities": {"read_market": True},
            "limits": {"allowed_symbols": ["btcusdt"]},
            "approval": {"required_for_trade": True},
            "blocked_actions": [],
        },
        reputation_score=reputation_score,
    )
    db_session.add(passport)
    db_session.flush()
    return passport


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestReputationService:
    def test_executed_increases_score(self, db_session: Session):
        """EXECUTED → +2。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user, reputation_score=50)
        svc = ReputationService(db_session)

        new_score = svc.update_reputation(passport.id, "EXECUTED")

        assert new_score == 52
        db_session.refresh(passport)
        assert passport.reputation_score == 52

    def test_auto_rejected_decreases_score(self, db_session: Session):
        """AUTO_REJECTED → -3。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user, reputation_score=50)
        svc = ReputationService(db_session)

        new_score = svc.update_reputation(passport.id, "AUTO_REJECTED")

        assert new_score == 47
        db_session.refresh(passport)
        assert passport.reputation_score == 47

    def test_execution_failed_decreases_score(self, db_session: Session):
        """EXECUTION_FAILED → -5。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user, reputation_score=50)
        svc = ReputationService(db_session)

        new_score = svc.update_reputation(passport.id, "EXECUTION_FAILED")

        assert new_score == 45
        db_session.refresh(passport)
        assert passport.reputation_score == 45

    def test_score_clamped_at_0(self, db_session: Session):
        """分数不低于 0。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user, reputation_score=2)
        svc = ReputationService(db_session)

        new_score = svc.update_reputation(passport.id, "EXECUTION_FAILED")  # -5

        assert new_score == 0
        db_session.refresh(passport)
        assert passport.reputation_score == 0

    def test_score_clamped_at_100(self, db_session: Session):
        """分数不高于 100。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user, reputation_score=99)
        svc = ReputationService(db_session)

        new_score = svc.update_reputation(passport.id, "EXECUTED")  # +2

        assert new_score == 100
        db_session.refresh(passport)
        assert passport.reputation_score == 100

    def test_unknown_outcome_no_change(self, db_session: Session):
        """未知结果不变。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user, reputation_score=50)
        svc = ReputationService(db_session)

        new_score = svc.update_reputation(passport.id, "SOME_UNKNOWN_OUTCOME")

        assert new_score == 50
        db_session.refresh(passport)
        assert passport.reputation_score == 50

    def test_writes_reputation_updated_audit(self, db_session: Session):
        """写入 REPUTATION_UPDATED 审计事件。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user, reputation_score=50)
        svc = ReputationService(db_session)

        svc.update_reputation(passport.id, "EXECUTED")

        # 查询审计事件
        events = (
            db_session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.REPUTATION_UPDATED
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        evt = events[0]
        assert evt.user_id == user.id
        assert evt.passport_id == passport.id
        assert evt.actor_type == "SYSTEM"
        assert evt.event_json["data"]["previous_score"] == 50
        assert evt.event_json["data"]["new_score"] == 52
        assert evt.event_json["data"]["delta"] == 2
        assert evt.event_json["data"]["reason"] == "EXECUTED"
