"""可选插件：whatsapp_wa 自动注册集成（默认关闭，全路由接入统一鉴权，修复 P1-02）。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.adapters import create_wa_adapter
from app.adapters.whatsapp_wa import WaApiError
from app.config import env
from app.core.auth_service import AuthService, CallerContext
from app.core.url_guard import validate_wa_app_url
from app.dependencies import get_caller
from app.errors import UpstreamError
from app.models import RegisterRequest
from app.orchestrator import RegisterOrchestrator
from app.plugins.base import IntegrationPlugin
from app.service_codes import canonical_service

logger = logging.getLogger(__name__)


class WaConfigUpdate(BaseModel):
    wa_app_url: str


class WhatsappWaPlugin(IntegrationPlugin):
    plugin_id = "whatsapp_wa"
    display_name = "WhatsApp 自动注册插件 (wa-app)"
    description = "可选插件：对接外部 wa-app 完成 WhatsApp 自动探测、注册与提交 OTP（默认关闭，不影响独立接码核心）。"

    def __init__(self, *, enabled: bool = False):
        super().__init__(enabled=enabled)
        self.waapi: Any = None
        self.orch: Optional[RegisterOrchestrator] = None
        self._db: Any = None
        self._router = self._build_router()

    async def on_startup(self, *, db: Any, registry: Any) -> None:
        self._db = db
        wa_app_url = await db.get_config("wa_app_url", env.wa_app_url)
        self.waapi = create_wa_adapter(env.wa_adapter, wa_app_url or env.wa_app_url)
        self.waapi.configure_auth(
            auth_type=env.wa_app_auth_type,
            token=env.wa_app_api_token,
            username=env.wa_app_api_username,
            password=env.wa_app_api_password,
        )
        self.orch = RegisterOrchestrator(registry, self.waapi, db)
        stale_count = await self.orch.recover_stale_tasks()
        if stale_count:
            logger.warning("whatsapp_wa 插件启动恢复 %d 个残留任务", stale_count)
        self.orch.start_background_tasks()

    async def on_shutdown(self) -> None:
        if self.orch is not None:
            await self.orch.shutdown()
            self.orch = None
        if self.waapi is not None:
            await self.waapi.close()
            self.waapi = None

    async def health(self) -> dict[str, Any]:
        if not self.waapi:
            return {"ok": False, "error": "wa-app 客户端未启动"}
        try:
            raw = await asyncio.wait_for(self.waapi.health(), timeout=2.5)
            wa_ok = bool(raw.get("ok", True)) if isinstance(raw, dict) else True
            return {"ok": wa_ok, "url": self.waapi._base_url, "detail": raw}
        except asyncio.TimeoutError:
            return {"ok": False, "url": self.waapi._base_url, "error": "wa-app 健康检查超时"}
        except Exception as exc:
            return {"ok": False, "url": self.waapi._base_url, "error": str(exc)}

    def get_router(self) -> APIRouter:
        return self._router

    def _require_enabled(self) -> None:
        if not self.enabled or not self._started or not self.orch or not self.waapi:
            raise HTTPException(
                status_code=503,
                detail="whatsapp_wa 插件未启用，请设置 ENABLE_WA_PLUGIN=true 后重启服务",
            )

    def _build_router(self) -> APIRouter:
        r = APIRouter(prefix="/api/v1/plugins/whatsapp_wa", tags=["plugin:whatsapp_wa"])

        @r.get("/health")
        async def wa_plugin_health(caller: CallerContext = Depends(get_caller)):
            AuthService.ensure_scope(caller, "wa:r")
            if not self.enabled:
                return {"enabled": False, "ok": False, "status": "disabled"}
            return {"enabled": True, **(await self.health())}

        @r.post("/register")
        async def wa_plugin_register(req: RegisterRequest, caller: CallerContext = Depends(get_caller)):
            AuthService.ensure_scope(caller, "wa:w")
            self._require_enabled()
            assert self.orch is not None
            if canonical_service(req.service) != "whatsapp":
                raise HTTPException(400, "whatsapp_wa 插件仅支持 WhatsApp 自动注册")
            task_id = f"task_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
            return await self.orch.start_registration(
                task_id=task_id,
                country=req.country or "52",
                country_fallback=req.country_fallback,
                max_attempts=req.max_attempts,
                price_max=req.price_max,
                wait_timeout=req.wait_timeout,
                provider_name=req.provider or "smsbower",
                service="whatsapp",
                target_price=req.target_price,
                price_lock_enabled=req.price_lock_enabled,
                price_float_enabled=req.price_float_enabled,
                price_float_percent=req.price_float_percent,
                continuous_poll=req.continuous_poll,
                target_success_count=req.target_success_count,
                request_interval_enabled=req.request_interval_enabled,
                request_interval=req.request_interval,
                proxy=req.proxy or "",
            )

        @r.get("/tasks")
        async def wa_plugin_tasks(caller: CallerContext = Depends(get_caller)):
            AuthService.ensure_scope(caller, "wa:r")
            self._require_enabled()
            assert self.orch is not None
            return await self.orch.list_tasks()

        @r.get("/tasks/{task_id}")
        async def wa_plugin_get_task(task_id: str, caller: CallerContext = Depends(get_caller)):
            AuthService.ensure_scope(caller, "wa:r")
            self._require_enabled()
            assert self.orch is not None
            task = await self.orch.get_task(task_id)
            if not task:
                raise HTTPException(404, f"任务 {task_id} 不存在")
            return task

        @r.post("/tasks/{task_id}/stop")
        async def wa_plugin_stop_task(task_id: str, caller: CallerContext = Depends(get_caller)):
            AuthService.ensure_scope(caller, "wa:w")
            self._require_enabled()
            assert self.orch is not None
            task = await self.orch.stop_task(task_id)
            if not task:
                raise HTTPException(404, f"任务 {task_id} 不存在")
            return task

        @r.get("/accounts")
        async def wa_plugin_accounts(caller: CallerContext = Depends(get_caller)):
            AuthService.ensure_scope(caller, "wa:r")
            self._require_enabled()
            try:
                return {"accounts": await self.waapi.list_accounts()}
            except WaApiError as exc:
                raise UpstreamError(str(exc), code="WA_API_ERROR") from exc

        @r.get("/history")
        async def wa_plugin_history(caller: CallerContext = Depends(get_caller)):
            AuthService.ensure_scope(caller, "wa:r")
            self._require_enabled()
            return {"entries": await self._db.list_history()}

        @r.put("/config")
        async def wa_plugin_update_config(body: WaConfigUpdate, caller: CallerContext = Depends(get_caller)):
            AuthService.ensure_admin(caller, "仅管理员可修改 wa-app 上游地址")
            self._require_enabled()
            url = validate_wa_app_url(body.wa_app_url)
            await self._db.set_config("wa_app_url", url)
            self.waapi.set_base_url(url)
            return {"success": True, "wa_app_url": url}

        return r
