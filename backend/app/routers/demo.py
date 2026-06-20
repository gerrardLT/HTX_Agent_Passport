"""预设场景路由（任务 19 / Req 25）。

提供 /api/scenarios/{scenario_id} 端点，运行预设场景并返回结果。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user, get_db
from app.models import User
from app.services.demo_scenarios import (
    run_happy_scenario,
    run_over_limit_scenario,
    run_reject_scenario,
)
from app.services.seed_data import load_seed_data

router = APIRouter(prefix="/api/scenarios", tags=["scenarios"])


@router.post("/{scenario_id}")
def run_scenario(
    scenario_id: str,
    db: Session = Depends(get_db),
    current_user: Annotated[User, Depends(get_current_user)] = None,
):
    """运行指定的预设场景。

    Parameters
    ----------
    scenario_id : str
        场景标识：happy / reject / over_limit

    Returns
    -------
    dict
        {"action_id": str, "final_state": str, "reason_codes"?: list[str]}
    """
    # 确保种子数据已加载
    seed_ids = load_seed_data(db)

    passport_id = seed_ids["passport_id"]
    user_id = seed_ids["user_id"]

    scenario_runners = {
        "happy": run_happy_scenario,
        "reject": run_reject_scenario,
        "over_limit": run_over_limit_scenario,
    }

    runner = scenario_runners.get(scenario_id)
    if runner is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown scenario: {scenario_id!r}. Available: {list(scenario_runners.keys())}",
        )

    result = runner(session=db, passport_id=passport_id, user_id=user_id)
    db.commit()

    # Convert UUID to string for JSON serialization
    return {
        "action_id": str(result["action_id"]),
        "final_state": result["final_state"],
        "reason_codes": result.get("reason_codes"),
    }


@router.post("/seed")
def load_seed(
    db: Session = Depends(get_db),
    current_user: Annotated[User, Depends(get_current_user)] = None,
):
    """手动加载种子数据（幂等）。

    Returns
    -------
    dict
        {"user_id": str, "credential_id": str, "passport_id": str}
    """
    result = load_seed_data(db)
    db.commit()
    return {
        "user_id": str(result["user_id"]),
        "credential_id": str(result["credential_id"]),
        "passport_id": str(result["passport_id"]),
    }
