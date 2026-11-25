"""外部项目接入管理：项目级 API Key CRUD、Webhook 校验测试与 Outbox 投递查询。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.schemas import CreateApiKeyRequest, UpdateApiKeyRequest, WebhookTestRequest
from app.core.auth_service import AuthService, CallerContext
from app.core.event_bus import EventBus
from app.core.url_guard import validate_webhook_url_async
from app.core.webhook_dispatcher import WebhookDispatcher
from app.db import Database
from app.dependencies import get_auth_service, get_caller, get_db, get_event_bus, get_webhook_dispatcher
from app.errors import AppError, NotFoundError

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


@router.get("/keys")
async def list_project_keys(
    caller: CallerContext = Depends(get_caller),
    db: Database = Depends(get_db),
    auth_service: AuthService = Depends(get_auth_service),
):
    AuthService.ensure_admin(caller, "仅管理员可管理项目 API 密钥")
    return {
        "api_auth_required": await auth_service.is_auth_required(),
        "items": await db.api_keys.list(),
    }


@router.post("/keys", status_code=201)
async def create_project_key(
    req: CreateApiKeyRequest,
    caller: CallerContext = Depends(get_caller),
    auth_service: AuthService = Depends(get_auth_service),
):
    AuthService.ensure_admin(caller, "仅管理员可签发项目 API 密钥")
    webhook_url = req.default_webhook_url.strip()
    if webhook_url:
        webhook_url, _ = await validate_webhook_url_async(webhook_url, resolve_dns=True)
    try:
        record, raw_key = await auth_service.create_project_key(
            name=req.name,
            project_id=req.project_id,
            scopes=req.scopes,
            default_webhook_url=webhook_url,
            webhook_secret=req.webhook_secret,
        )
    except RuntimeError as exc:
        raise AppError(str(exc), status_code=500, code="ENCRYPTION_REQUIRED") from exc

    return {
        **record,
        "api_key": raw_key,
        "notice": "密钥明文仅此一次展示，请妥善保存",
    }


@router.patch("/keys/{project_id}")
async def update_project_key(
    project_id: str,
    req: UpdateApiKeyRequest,
    caller: CallerContext = Depends(get_caller),
    db: Database = Depends(get_db),
    event_bus: EventBus = Depends(get_event_bus),
):
    AuthService.ensure_admin(caller, "仅管理员可更新项目 API 密钥")
    webhook_url = req.default_webhook_url
    if webhook_url is not None and webhook_url.strip():
        webhook_url, _ = await validate_webhook_url_async(webhook_url.strip(), resolve_dns=True)
    try:
        updated = await db.api_keys.update(
            project_id,
            name=req.name,
            enabled=req.enabled,
            scopes=req.scopes,
            default_webhook_url=webhook_url,
            webhook_secret=req.webhook_secret,
        )
    except RuntimeError as exc:
        raise AppError(str(exc), status_code=500, code="ENCRYPTION_REQUIRED") from exc
    if updated is None:
        raise NotFoundError(f"项目 '{project_id}' 不存在", code="PROJECT_NOT_FOUND")
    if req.enabled is not None or req.scopes is not None:
        event_bus.revoke_project_subscribers(project_id)
    return updated


@router.post("/keys/{project_id}/rotate")
async def rotate_project_key(
    project_id: str,
    caller: CallerContext = Depends(get_caller),
    auth_service: AuthService = Depends(get_auth_service),
    event_bus: EventBus = Depends(get_event_bus),
):
    """在现有项目身份内原子轮转密钥：保留历史订单访问权，立即作废旧密钥与已连接 SSE（修复补充 P1-03）。"""
    AuthService.ensure_admin(caller, "仅管理员可轮转项目 API 密钥")
    rotated = await auth_service.rotate_project_key(project_id)
    if rotated is None:
        raise NotFoundError(f"项目 '{project_id}' 不存在", code="PROJECT_NOT_FOUND")
    record, raw_key = rotated
    event_bus.revoke_project_subscribers(project_id)
    return {
        **record,
        "api_key": raw_key,
        "notice": "新密钥明文仅此一次展示，旧密钥及旧 SSE 长连接已立即失效",
    }


@router.delete("/keys/{project_id}")
async def delete_project_key(
    project_id: str,
    caller: CallerContext = Depends(get_caller),
    db: Database = Depends(get_db),
    event_bus: EventBus = Depends(get_event_bus),
):
    AuthService.ensure_admin(caller, "仅管理员可删除项目 API 密钥")
    deleted = await db.api_keys.delete(project_id)
    if not deleted:
        raise NotFoundError(f"项目 '{project_id}' 不存在", code="PROJECT_NOT_FOUND")
    event_bus.revoke_project_subscribers(project_id)
    return {"success": True, "project_id": project_id}


@router.post("/webhook-test")
async def test_webhook(
    req: WebhookTestRequest,
    caller: CallerContext = Depends(get_caller),
    db: Database = Depends(get_db),
    dispatcher: WebhookDispatcher = Depends(get_webhook_dispatcher),
):
    AuthService.ensure_admin(caller, "仅管理员可测试 Webhook 目标")
    secret = req.secret.strip()
    if not secret and req.project_id:
        row = await db.api_keys.get(req.project_id, include_secret=True)
        if row:
            secret = str(row.get("webhook_secret") or "")
    return await dispatcher.send_test(req.url.strip(), secret=secret, project_id=req.project_id)


@router.get("/webhooks/deliveries")
async def list_webhook_deliveries(
    project_id: str = Query(default=""),
    order_id: str = Query(default=""),
    status: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
    caller: CallerContext = Depends(get_caller),
    db: Database = Depends(get_db),
):
    """查询持久化 Webhook Outbox 投递状态（项目密钥仅可查自身项目记录）。"""
    AuthService.ensure_scope(caller, "orders:r")
    effective_project = project_id.strip() if caller.is_admin else caller.project_id
    items = await db.webhooks.list(
        project_id=effective_project,
        order_id=order_id.strip(),
        status=status.strip(),
        limit=limit,
    )
    return {"total": len(items), "items": items}
