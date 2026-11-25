"""号码黑名单管理 API（修复 P1-10 分项目隔离 & P2-01 默认平台贯通）。"""

from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.api.schemas import BlacklistBatchRequest, BlacklistCreateRequest
from app.core.auth_service import AuthService, CallerContext
from app.db import Database
from app.dependencies import get_caller, get_db, get_registry
from app.errors import AppError, ForbiddenError
from app.providers.registry import ProviderRegistry

router = APIRouter(prefix="/api/v1/blacklist", tags=["blacklist"])


def _resolve_target_project(caller: CallerContext, scope: str, project_id: Optional[str] = None) -> str:
    if scope == "global":
        if not caller.is_admin and not caller.has_scope("blacklist:global"):
            raise ForbiddenError("写入平台共享全局黑名单仅限管理员或拥有 blacklist:global 权限的密钥")
        return ""
    if caller.is_admin:
        if project_id is not None:
            return project_id.strip()
        if scope not in {"auto", "project", "all"}:
            return scope.strip()
        return ""
    return caller.project_id


@router.get("")
async def list_blacklist(
    provider: str = Query(default="", description="按平台过滤"),
    scope: str = Query(default="auto", description="auto / project / global / all（仅管理员可用 global/all）"),
    project_id: str = Query(default="", description="管理员按指定 project_id 过滤"),
    limit: int = Query(default=200, ge=1, le=1000),
    caller: CallerContext = Depends(get_caller),
    db: Database = Depends(get_db),
):
    AuthService.ensure_scope(caller, "blacklist:r")
    project_filter: Optional[str]
    project_only = False
    if caller.is_admin:
        clean_pid = project_id.strip()
        if clean_pid:
            project_filter = clean_pid
        elif scope == "global":
            project_filter = ""
        elif scope == "project":
            project_filter = None
            project_only = True
        elif scope in {"all", "auto"}:
            project_filter = None
        else:
            project_filter = scope.strip()
    else:
        if scope == "global" and not caller.has_scope("blacklist:global"):
            raise ForbiddenError("无权读取平台全局黑名单")
        project_filter = "" if scope == "global" else caller.project_id

    entries = await db.blacklist.list(
        provider=provider,
        limit=limit,
        project_id=project_filter,
        project_only=project_only,
    )
    return {
        "scope": "global" if project_filter == "" else ("project" if (project_only or project_filter is not None) else "all"),
        "total": len(entries),
        "entries": entries,
    }


@router.post("", status_code=201)
async def add_blacklist(
    req: BlacklistCreateRequest,
    caller: CallerContext = Depends(get_caller),
    db: Database = Depends(get_db),
    registry: ProviderRegistry = Depends(get_registry),
):
    AuthService.ensure_scope(caller, "blacklist:w")
    raw_phone = req.phone.strip()
    digits = re.sub(r"\D", "", raw_phone)
    if len(digits) < 5:
        raise AppError("手机号格式无效（至少包含 5 位数字）", status_code=422, code="VALIDATION_ERROR")
    phone = f"+{digits}" if raw_phone.startswith("+") else digits
    provider = req.provider.strip() or registry.default_provider
    target_project = _resolve_target_project(caller, req.scope)
    inserted = await db.blacklist.add(phone, reason=req.reason.strip(), provider=provider, project_id=target_project)
    return {
        "success": True,
        "added": 1 if inserted else 0,
        "phone": phone,
        "provider": provider,
        "scope": "global" if not target_project else "project",
        "project_id": target_project,
    }


@router.post("/batch", status_code=201)
async def add_blacklist_batch(
    req: BlacklistBatchRequest,
    caller: CallerContext = Depends(get_caller),
    db: Database = Depends(get_db),
    registry: ProviderRegistry = Depends(get_registry),
):
    AuthService.ensure_scope(caller, "blacklist:w")
    provider = req.provider.strip() or registry.default_provider
    target_project = _resolve_target_project(caller, req.scope)
    added = 0
    for raw in req.phones:
        raw_phone = str(raw or "").strip()
        digits = re.sub(r"\D", "", raw_phone)
        if len(digits) < 5:
            continue
        phone = f"+{digits}" if raw_phone.startswith("+") else digits
        if await db.blacklist.add(phone, reason=req.reason.strip(), provider=provider, project_id=target_project):
            added += 1
    return {
        "success": True,
        "added": added,
        "provider": provider,
        "scope": "global" if not target_project else "project",
    }


@router.delete("")
async def remove_blacklist(
    phone: str = Query(..., min_length=1),
    provider: str = Query(default=""),
    scope: str = Query(default="auto"),
    project_id: Optional[str] = Query(default=None, description="管理员指定要移除的项目黑名单 project_id（空串表示全局）"),
    caller: CallerContext = Depends(get_caller),
    db: Database = Depends(get_db),
    registry: ProviderRegistry = Depends(get_registry),
):
    AuthService.ensure_scope(caller, "blacklist:w")
    prov = provider.strip() or registry.default_provider
    target_project = _resolve_target_project(caller, scope, project_id=project_id)
    deleted = await db.blacklist.remove(phone.strip(), provider=prov, project_id=target_project)
    return {"success": True, "deleted": deleted, "project_id": target_project}
