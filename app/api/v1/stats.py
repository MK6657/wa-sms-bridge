"""接码订单多维统计 API（修复 P1-10 项目租户数据隔离）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.core.auth_service import AuthService, CallerContext
from app.db import Database
from app.dependencies import get_caller, get_db

router = APIRouter(prefix="/api/v1/stats", tags=["stats"])


@router.get("")
async def get_stats(
    timezone_offset_minutes: int = Query(default=0, ge=-840, le=840),
    days: int = Query(default=7, ge=1, le=90),
    project_id: str = Query(default="", description="按项目过滤（仅管理员可指定其他项目或看全局）"),
    caller: CallerContext = Depends(get_caller),
    db: Database = Depends(get_db),
):
    AuthService.ensure_scope(caller, "orders:r")
    effective_project = project_id.strip() if caller.is_admin else caller.project_id
    stats = await db.orders.get_stats(
        timezone_offset_minutes=timezone_offset_minutes,
        days=days,
        project_id=effective_project,
    )
    bl_filter = None if (caller.is_admin and not effective_project) else effective_project
    stats["blacklist_count"] = await db.blacklist.count(project_id=bl_filter)
    return stats
