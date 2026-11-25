"""核心接码订单 API（修复 P1-01 Webhook SSRF 授权、P1-10 黑名单隔离与 P2-05 完整 OpenAPI 契约）。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, Response

from app.api.schemas import (
    CancelOrderRequest,
    CreateOrderRequest,
    ErrorEnvelopeResponse,
    OrderListResponse,
    OrderResponse,
    ResolveReconciliationRequest,
)
from app.core.auth_service import AuthService, CallerContext
from app.core.order_service import AcquireOptions, OrderService
from app.core.url_guard import validate_webhook_url
from app.db import Database
from app.dependencies import get_auth_service, get_caller, get_db, get_order_service
from app.errors import NotFoundError

router = APIRouter(prefix="/api/v1/orders", tags=["orders"])

ORDER_CREATE_RESPONSES = {
    201: {"model": OrderResponse, "description": "同步取号成功，已分配号码并进入 waiting_code"},
    202: {"model": OrderResponse, "description": "异步/持续轮询抢号已受理，进入 acquiring"},
    401: {"model": ErrorEnvelopeResponse, "description": "鉴权失败或缺少有效凭据"},
    402: {"model": ErrorEnvelopeResponse, "description": "接码平台余额不足"},
    403: {"model": ErrorEnvelopeResponse, "description": "缺少 orders:rw 权限或 Webhook 目标未授权"},
    409: {"model": ErrorEnvelopeResponse, "description": "无可用号码、黑名单退款待确认或幂等键参数冲突"},
    422: {"model": ErrorEnvelopeResponse, "description": "请求参数或价格策略校验失败"},
    502: {"model": ErrorEnvelopeResponse, "description": "上游接码平台错误或购买结果不确定"},
}


@router.post("", status_code=201, response_model=OrderResponse, responses=ORDER_CREATE_RESPONSES)
async def create_order(
    req: CreateOrderRequest,
    response: Response,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key", max_length=128),
    caller: CallerContext = Depends(get_caller),
    order_service: OrderService = Depends(get_order_service),
):
    AuthService.ensure_scope(caller, "orders:w")

    # P1-01 / v6 复核 P1-06: 路由层仅做纯 URL 语法校验；项目同源策略与外部 DNS 仅适用于新建订单，
    # 推迟到幂等查表未命中之后执行，确保管理员更改默认回调域名后，已成功订单的原样幂等重试不会被 403 拒绝！
    explicit_webhook = req.webhook_url.strip()
    if explicit_webhook:
        order_webhook, _ = validate_webhook_url(
            explicit_webhook,
            enforce_project_origin=False,
            resolve_dns=False,
        )
    else:
        order_webhook = caller.default_webhook_url

    strategy = AcquireOptions(
        target_price=req.strategy.target_price,
        price_max=req.strategy.price_max,
        price_lock=req.strategy.price_lock,
        strict_exact_price=req.strategy.strict_exact_price,
        price_float_enabled=req.strategy.price_float_enabled,
        price_float_percent=req.strategy.price_float_percent,
        max_attempts=req.strategy.max_attempts,
        continuous_poll=req.strategy.continuous_poll,
        request_interval_enabled=req.strategy.request_interval_enabled,
        request_interval=req.strategy.request_interval,
    )
    order = await order_service.create_order(
        country=req.country,
        service=req.service,
        provider_name=req.provider,
        country_fallback=req.country_fallback,
        strategy=strategy,
        wait_timeout=req.wait_timeout,
        skip_blacklisted=req.skip_blacklisted,
        async_acquire=req.async_acquire,
        project_id=caller.project_id,
        external_ref=req.external_ref,
        idempotency_key=(idempotency_key or "").strip() or None,
        webhook_url=order_webhook,
        explicit_webhook_url=explicit_webhook,
        validate_webhook_dns=bool(explicit_webhook),
        allowed_project_webhook_url=caller.default_webhook_url,
        enforce_project_webhook_origin=bool(explicit_webhook) and (not caller.is_admin),
        metadata=req.metadata,
    )
    if (
        order.get("async_acquire")
        or int(order.get("idempotency_http_status") or 0) == 202
        or order.get("status") == "acquiring"
    ):
        response.status_code = 202
    return order


@router.get("", response_model=OrderListResponse)
async def list_orders(
    status: str = Query(default="", description="状态筛选，多个以逗号分隔"),
    active_only: bool = Query(default=False, description="仅看进行中的订单"),
    project_id: str = Query(default="", description="按项目 ID 筛选（仅管理员可查其他项目）"),
    service: str = Query(default="", description="按服务筛选"),
    provider: str = Query(default="", description="按平台筛选"),
    search: str = Query(default="", description="搜索订单号/手机号/国家/external_ref"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=200),
    caller: CallerContext = Depends(get_caller),
    db: Database = Depends(get_db),
):
    AuthService.ensure_scope(caller, "orders:r")
    effective_project = project_id if caller.is_admin else caller.project_id
    return await db.orders.list(
        status=status,
        active_only=active_only,
        project_id=effective_project,
        service=service,
        provider=provider,
        search=search,
        page=page,
        page_size=page_size,
    )


@router.get("/reconciliation/pending")
async def list_pending_reconciliation(
    caller: CallerContext = Depends(get_caller),
    order_service: OrderService = Depends(get_order_service),
):
    """管理员人工核对入口：列出待核对的不确定购买/待退款激活与待核对完成状态的订单（修复复核 P1-01）。"""
    AuthService.ensure_admin(caller, "仅管理员可查看待核对对账队列")
    return await order_service.list_reconciliation_items()


@router.post("/reconciliation/{activation_row_id}/resolve")
async def resolve_reconciliation_item(
    activation_row_id: int,
    req: ResolveReconciliationRequest,
    caller: CallerContext = Depends(get_caller),
    order_service: OrderService = Depends(get_order_service),
):
    """管理员人工结清某条结果未知的激活记录（修复复核 P1-01）。"""
    AuthService.ensure_admin(caller, "仅管理员可核销待对账激活")
    return await order_service.resolve_activation_manually(
        activation_row_id,
        state=req.state,
        note=req.note,
        refund_amount=req.refund_amount,
        currency=req.currency,
    )


@router.get("/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: str,
    caller: CallerContext = Depends(get_caller),
    order_service: OrderService = Depends(get_order_service),
):
    AuthService.ensure_scope(caller, "orders:r")
    order = await order_service.get_order(order_id)
    if not caller.is_admin and order.get("project_id") != caller.project_id:
        raise NotFoundError(f"订单 {order_id} 不存在", code="ORDER_NOT_FOUND")
    return order


@router.get("/{order_id}/wait", response_model=OrderResponse)
async def wait_order_code(
    order_id: str,
    timeout: int = Query(default=60, ge=1, le=300, description="最长阻塞等待秒数"),
    after_seq: int = Query(default=0, ge=0, description="等待序号大于该值的新短信"),
    caller: CallerContext = Depends(get_caller),
    auth_service: AuthService = Depends(get_auth_service),
    order_service: OrderService = Depends(get_order_service),
):
    AuthService.ensure_scope(caller, "orders:r")
    order = await order_service.get_order(order_id)
    if not caller.is_admin and order.get("project_id") != caller.project_id:
        raise NotFoundError(f"订单 {order_id} 不存在", code="ORDER_NOT_FOUND")
    return await order_service.wait_for_code(
        order_id,
        timeout=timeout,
        after_seq=after_seq,
        caller=caller,
        auth_service=auth_service,
    )


@router.post("/{order_id}/resend", response_model=OrderResponse)
async def resend_order_sms(
    order_id: str,
    caller: CallerContext = Depends(get_caller),
    order_service: OrderService = Depends(get_order_service),
):
    AuthService.ensure_scope(caller, "orders:w")
    order = await order_service.get_order(order_id)
    if not caller.is_admin and order.get("project_id") != caller.project_id:
        raise NotFoundError(f"订单 {order_id} 不存在", code="ORDER_NOT_FOUND")
    return await order_service.resend_sms(order_id)


@router.post("/{order_id}/complete", response_model=OrderResponse)
async def complete_order(
    order_id: str,
    caller: CallerContext = Depends(get_caller),
    order_service: OrderService = Depends(get_order_service),
):
    AuthService.ensure_scope(caller, "orders:w")
    order = await order_service.get_order(order_id)
    if not caller.is_admin and order.get("project_id") != caller.project_id:
        raise NotFoundError(f"订单 {order_id} 不存在", code="ORDER_NOT_FOUND")
    return await order_service.complete_order(order_id)


@router.post("/{order_id}/cancel", response_model=OrderResponse)
async def cancel_order(
    order_id: str,
    body: Optional[CancelOrderRequest] = None,
    caller: CallerContext = Depends(get_caller),
    order_service: OrderService = Depends(get_order_service),
):
    AuthService.ensure_scope(caller, "orders:w")
    order = await order_service.get_order(order_id)
    if not caller.is_admin and order.get("project_id") != caller.project_id:
        raise NotFoundError(f"订单 {order_id} 不存在", code="ORDER_NOT_FOUND")

    req = body or CancelOrderRequest()
    if req.blacklist:
        AuthService.ensure_scope(caller, "blacklist:w")

    # P1-10: 管理员拉黑写入平台全局黑名单；项目密钥仅写入该项目私有黑名单，不影响其他项目
    bl_project = "" if caller.is_admin else caller.project_id
    return await order_service.cancel_order(
        order_id,
        blacklist=req.blacklist,
        reason=req.reason,
        blacklist_project_id=bl_project,
    )


@router.delete("/{order_id}/code")
async def scrub_order_code(
    order_id: str,
    caller: CallerContext = Depends(get_caller),
    order_service: OrderService = Depends(get_order_service),
):
    AuthService.ensure_scope(caller, "orders:w")
    order = await order_service.get_order(order_id)
    if not caller.is_admin and order.get("project_id") != caller.project_id:
        raise NotFoundError(f"订单 {order_id} 不存在", code="ORDER_NOT_FOUND")
    await order_service.scrub_order_code(order_id)
    return {"success": True, "order_id": order_id}
