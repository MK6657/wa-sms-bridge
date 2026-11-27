"""SMS Hub (wa-sms-bridge v1.0.0) FastAPI 应用入口。

独立运行 SMS 接码核心与标准对外 API（/api/v1/*），默认仅激活 SMSBower，
原 wa-app 自动注册编排作为可选插件按 ENABLE_WA_PLUGIN 开关加载，
按需通过 ENABLE_LEGACY_API=true 挂载受管理员鉴权保护的 v0.2.0 兼容路由。
"""

from __future__ import annotations

import asyncio
import gzip
import logging
import os
import shutil
from contextlib import AsyncExitStack, asynccontextmanager
from logging.handlers import MemoryHandler, RotatingFileHandler
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.v1 import v1_router
from app.config import APP_VERSION, env
from app.core.auth_service import AuthService
from app.core.event_bus import EventBus
from app.core.order_service import OrderService
from app.core.pricing_service import PricingService
from app.core.retention_service import RetentionService
from app.core.webhook_dispatcher import WebhookDispatcher
from app.db import Database
from app.dependencies import AppContainer, require_admin_caller
from app.errors import register_exception_handlers
from app.log_redaction import redact_text
from app.plugins import PluginManager
from app.plugins.whatsapp_wa import WhatsappWaPlugin
from app.providers.five_sim import FiveSimProvider
from app.providers.hero_sms import HeroSmsProvider
from app.providers.registry import ProviderRegistry, registry as global_registry
from app.providers.smsbower import SmsBowerProvider

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%H:%M:%S"


class SecretRedactionFilter(logging.Filter):
    """Redact common token/query-secret/code patterns before records reach handlers."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = ()
        return True


class RedactingFormatter(logging.Formatter):
    """确保消息正文与异常堆栈 (exc_info / stack_info) 全部经过敏感信息脱敏。"""

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


_SECRET_FILTER = SecretRedactionFilter()
_FILE_BUFFER_HANDLER: Optional[MemoryHandler] = None
_CURRENT_LOG_PATH: Optional[Path] = None

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format=LOG_FORMAT,
    datefmt=LOG_DATE_FORMAT,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
for _handler in logging.getLogger().handlers:
    _handler.addFilter(_SECRET_FILTER)
    _handler.setFormatter(RedactingFormatter(LOG_FORMAT, LOG_DATE_FORMAT))


def _gzip_rotated_log(source: str, dest: str) -> None:
    with open(source, "rb") as src, gzip.open(dest, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst)
    os.remove(source)


def close_file_logging(handler: Optional[MemoryHandler] = None) -> None:
    """刷新、从根 logger 移除并关闭归属的缓冲及底层文件处理器，释放 Windows 文件句柄（修复 v6 复核 P2-03）。"""
    global _FILE_BUFFER_HANDLER
    root = logging.getLogger()
    targets: list[logging.Handler] = []
    if handler is not None:
        targets.append(handler)
    else:
        for h in list(root.handlers):
            if getattr(h, "_bridge_file_handler", False):
                targets.append(h)
        if _FILE_BUFFER_HANDLER is not None and _FILE_BUFFER_HANDLER not in targets:
            targets.append(_FILE_BUFFER_HANDLER)

    for h in targets:
        try:
            root.removeHandler(h)
        except Exception:
            pass
        inner_target = getattr(h, "target", None)
        try:
            h.flush()
        except Exception:
            pass
        if isinstance(h, MemoryHandler):
            try:
                h.setTarget(None)  # type: ignore[arg-type]
            except Exception:
                pass
        try:
            h.close()
        except Exception:
            pass
        if inner_target is not None:
            try:
                inner_target.flush()
            except Exception:
                pass
            try:
                inner_target.close()
            except Exception:
                pass
        if _FILE_BUFFER_HANDLER is h:
            _FILE_BUFFER_HANDLER = None


def configure_file_logging(data_dir: str) -> tuple[Path, MemoryHandler]:
    """为当前应用生命周期配置独立文件日志处理器；若已有旧桥接处理器则先安全关闭释放再按新目录重建。"""
    global _FILE_BUFFER_HANDLER, _CURRENT_LOG_PATH
    log_path = Path(os.environ.get("BRIDGE_LOG_FILE") or Path(data_dir) / "bridge.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    close_file_logging()

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=int(os.environ.get("BRIDGE_LOG_MAX_BYTES", str(2 * 1024 * 1024))),
        backupCount=int(os.environ.get("BRIDGE_LOG_BACKUPS", "5")),
        encoding="utf-8",
        delay=True,
    )
    target_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    file_handler.namer = lambda name: f"{name}.gz"
    file_handler.rotator = _gzip_rotated_log
    file_handler.setFormatter(RedactingFormatter(LOG_FORMAT, LOG_DATE_FORMAT))
    file_handler.setLevel(target_level)
    file_handler.addFilter(_SECRET_FILTER)

    buffer_handler = MemoryHandler(
        capacity=int(os.environ.get("BRIDGE_LOG_BUFFER_CAPACITY", "100")),
        flushLevel=getattr(logging, os.environ.get("BRIDGE_LOG_FLUSH_LEVEL", "WARNING").upper(), logging.WARNING),
        target=file_handler,
        flushOnClose=True,
    )
    buffer_handler.setLevel(target_level)
    buffer_handler.addFilter(_SECRET_FILTER)
    buffer_handler._bridge_file_handler = True  # type: ignore[attr-defined]
    root_logger = logging.getLogger()
    if root_logger.level == logging.NOTSET or root_logger.level > target_level:
        root_logger.setLevel(target_level)
    root_logger.addHandler(buffer_handler)
    _FILE_BUFFER_HANDLER = buffer_handler
    _CURRENT_LOG_PATH = log_path
    return log_path, buffer_handler


def get_current_log_path() -> Optional[Path]:
    return _CURRENT_LOG_PATH


def flush_file_logging() -> None:
    if _FILE_BUFFER_HANDLER:
        _FILE_BUFFER_HANDLER.flush()
    for handler in logging.getLogger().handlers:
        if getattr(handler, "_bridge_file_handler", False):
            handler.flush()


def _missing_api_key_error(error: Exception) -> bool:
    return "API Key 未配置" in str(error)


def _init_active_providers(
    reg: ProviderRegistry,
    db: Database,
    active_names: list[str],
    default_name: str,
) -> None:
    """仅初始化配置中激活的平台，并严格遵从 DEFAULT_PROVIDER 配置（修复 P2-01）。"""

    async def get_smsbower_key() -> str:
        return await db.config.get("smsbower_api_key", "")

    async def get_smsbower_base_url() -> str:
        return await db.config.get("smsbower_base_url", "") or env.smsbower_base_url

    async def get_five_sim_key() -> str:
        return await db.config.get("five_sim_api_key", "")

    async def get_hero_sms_key() -> str:
        return await db.config.get("hero_sms_api_key", "")

    async def get_hero_sms_base_url() -> str:
        return await db.config.get("hero_sms_base_url", "")

    factories = {
        "smsbower": lambda: SmsBowerProvider(
            get_smsbower_key,
            base_url_getter=get_smsbower_base_url,
            country_cache_ttl=float(env.country_cache_ttl_seconds),
        ),
        "5sim": lambda: FiveSimProvider(
            get_five_sim_key,
            country_cache_ttl=float(env.country_cache_ttl_seconds),
        ),
        "herosms": lambda: HeroSmsProvider(
            get_hero_sms_key,
            base_url_getter=get_hero_sms_base_url,
            country_cache_ttl=float(env.country_cache_ttl_seconds),
        ),
    }

    for name in active_names:
        factory = factories.get(name)
        if not factory:
            logger.warning("忽略未知 ACTIVE_PROVIDERS 项: %s", name)
            continue
        try:
            reg.init_provider(name, factory())
        except Exception as exc:
            logger.warning("Provider '%s' 初始化失败: %s", name, exc)

    preferred = (default_name or "").strip().lower()
    if preferred and reg.is_active(preferred):
        reg.set_default(preferred)
    elif reg.list_providers():
        reg.set_default(reg.list_providers()[0])


def create_app(
    *,
    db_path: Optional[str] = None,
    active_providers: Optional[list[str]] = None,
    default_provider: Optional[str] = None,
    enable_wa_plugin: Optional[bool] = None,
    enable_legacy_api: Optional[bool] = None,
    registry_instance: Optional[ProviderRegistry] = None,
    skip_startup_probe: bool = False,
) -> FastAPI:
    """创建可测试、可隔离实例化的 FastAPI 应用。"""
    env.reload()

    wa_enabled = env.enable_wa_plugin if enable_wa_plugin is None else bool(enable_wa_plugin)
    legacy_enabled = (
        (env.enable_legacy_api if enable_legacy_api is None else bool(enable_legacy_api))
        and wa_enabled
    )
    wa_plugin = WhatsappWaPlugin(enabled=wa_enabled)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        env.reload()
        for _uv_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            for _uv_handler in logging.getLogger(_uv_name).handlers:
                _uv_handler.addFilter(_SECRET_FILTER)
                _uv_handler.setFormatter(RedactingFormatter(LOG_FORMAT, LOG_DATE_FORMAT))
        effective_db_path = db_path or env.db_path
        data_dir = str(Path(effective_db_path).parent) if db_path and effective_db_path != ":memory:" else env.bridge_data_dir
        async def _safe_close(label: str, coro_fn) -> None:
            try:
                await coro_fn()
            except Exception:
                logger.warning("关闭资源 '%s' 异常", label, exc_info=True)

        async with AsyncExitStack() as stack:
            if data_dir and effective_db_path != ":memory:":
                os.makedirs(data_dir, exist_ok=True)
                log_path, log_handler = configure_file_logging(data_dir)
                stack.callback(close_file_logging, log_handler)
                logger.info("SMS Hub 启动中... db=%s log_file=%s", effective_db_path, log_path)
            else:
                stack.callback(flush_file_logging)

            db = Database(effective_db_path)
            stack.push_async_callback(_safe_close, "database", db.close)
            await db.open()
            application.state.db = db

            seed_key = os.environ.get("SMSBOWER_API_KEY", "").strip()
            if seed_key and not await db.config.get("smsbower_api_key", ""):
                try:
                    await db.config.set("smsbower_api_key", seed_key, encrypt=True)
                    logger.info("已从环境变量写入初始 SMSBOWER_API_KEY")
                except RuntimeError as exc:
                    logger.warning("写入初始 SMSBOWER_API_KEY 跳过: %s", exc)

            resolved_default = (default_provider or env.default_provider).strip().lower()
            reg = registry_instance if registry_instance is not None else (
                global_registry if db_path is None else ProviderRegistry(default=resolved_default)
            )
            if registry_instance is None:
                reg.reset()
                providers_to_activate = list(active_providers) if active_providers is not None else list(env.active_providers)
                if resolved_default and resolved_default not in providers_to_activate:
                    providers_to_activate.append(resolved_default)
                _init_active_providers(reg, db, providers_to_activate, resolved_default)

            async def _close_providers() -> None:
                for pname, prov in reg.all_instances().items():
                    try:
                        await prov.close()
                    except Exception:
                        logger.warning("关闭 provider '%s' 异常", pname, exc_info=True)

            stack.push_async_callback(_close_providers)

            event_bus = EventBus()
            stack.callback(event_bus.close_all)
            pricing_service = PricingService(reg, cache_ttl=float(env.price_cache_ttl_seconds))
            webhook_dispatcher = WebhookDispatcher(db, timeout_seconds=env.webhook_timeout_seconds)
            stack.push_async_callback(_safe_close, "webhook_dispatcher", webhook_dispatcher.shutdown)

            order_service = OrderService(
                db,
                reg,
                event_bus,
                on_outbox_enqueued=webhook_dispatcher.notify,
                on_orders_scrubbed=webhook_dispatcher.cancel_code_deliveries_for_orders,
            )
            stack.push_async_callback(_safe_close, "order_service", order_service.shutdown)

            auth_service = AuthService(db)
            retention_service = RetentionService(db, order_service)
            stack.push_async_callback(_safe_close, "retention_service", retention_service.shutdown)

            plugin_manager = PluginManager()
            plugin_manager.register(wa_plugin)
            stack.push_async_callback(_safe_close, "plugin_manager", plugin_manager.shutdown_all)

            recovered_orders = await order_service.recover_on_startup()
            if recovered_orders:
                logger.info("启动恢复处理了 %d 笔遗留接码订单", recovered_orders)

            recovered_wh = await webhook_dispatcher.recover_and_start()
            if recovered_wh:
                logger.info("启动恢复续送 %d 条遗留 Webhook 投递", recovered_wh)

            retention_service.start()
            await plugin_manager.startup_all(db=db, registry=reg)

            if legacy_enabled and wa_plugin.is_started:
                from app.router_api import _init as legacy_router_init
                legacy_router_init(wa_plugin.orch, reg, wa_plugin.waapi, db)

            container = AppContainer(
                db=db,
                registry=reg,
                event_bus=event_bus,
                pricing_service=pricing_service,
                order_service=order_service,
                auth_service=auth_service,
                webhook_dispatcher=webhook_dispatcher,
                retention_service=retention_service,
                plugin_manager=plugin_manager,
            )
            application.state.container = container

            if not skip_startup_probe:
                for pname, prov in reg.all_instances().items():
                    try:
                        bal = await asyncio.wait_for(prov.get_balance(), timeout=3.0)
                        logger.info("活跃平台 %s 就绪，余额: $%.2f", pname, bal)
                    except Exception as exc:
                        if _missing_api_key_error(exc):
                            logger.info("平台 %s 尚未配置 API Key，请在控制台《系统配置》填写", pname)
                        else:
                            logger.warning("平台 %s 初始连通测试失败: %s", pname, exc)

            logger.info(
                "SMS Hub 就绪 (default_provider=%s, active_providers=%s, wa_plugin=%s, legacy_api=%s)",
                reg.default_provider,
                reg.list_providers(),
                wa_plugin.is_started,
                legacy_enabled,
            )
            try:
                yield
            finally:
                logger.info("SMS Hub 正在优雅关闭...")
        logger.info("SMS Hub 已关闭")

    application = FastAPI(
        title="SMS Hub API",
        description="独立、轻量、可扩展的 SMS 接码服务与对外集成 API（当前主活平台：SMSBower）",
        version=APP_VERSION,
        lifespan=lifespan,
    )

    cors_list = list(dict.fromkeys([*env.admin_trusted_origins, *env.cors_origins]))
    if cors_list:
        allow_creds = bool(env.admin_trusted_origins) and "*" not in env.admin_trusted_origins and not env.cors_origins
        application.add_middleware(
            CORSMiddleware,
            allow_origins=env.admin_trusted_origins if allow_creds else cors_list,
            allow_credentials=allow_creds,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @application.middleware("http")
    async def security_headers_middleware(request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    register_exception_handlers(application)
    application.include_router(v1_router)
    application.include_router(wa_plugin.get_router())

    # P2-04: 可选挂载受管理员鉴权保护的 v0.2.0 兼容路由（跳过 /api/health 避免冲突）
    if legacy_enabled:
        from app.router_api import router as legacy_router
        from fastapi import APIRouter

        filtered_legacy = APIRouter()
        for route in legacy_router.routes:
            if getattr(route, "path", "") == "/api/health":
                continue
            filtered_legacy.routes.append(route)
        application.include_router(
            filtered_legacy,
            tags=["legacy:v0.2"],
            dependencies=[Depends(require_admin_caller)],
            include_in_schema=False,
        )

    web_dist = Path(__file__).parent / "web_dist"
    assets_dir = web_dist / "assets"
    if assets_dir.is_dir():
        application.mount("/assets", StaticFiles(directory=str(assets_dir)), name="web_assets")

    @application.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        if full_path.startswith(("api/", "docs", "redoc", "openapi.json")):
            raise HTTPException(status_code=404, detail="Not Found")
        if web_dist.is_dir():
            target = (web_dist / full_path).resolve()
            if full_path and target.is_file() and web_dist.resolve() in target.parents:
                return FileResponse(target)
            index_file = web_dist / "index.html"
            if index_file.is_file():
                return FileResponse(index_file)
        raise HTTPException(
            status_code=404,
            detail="前端静态资源未构建，请在 frontend/ 目录运行 npm run build",
        )

    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=os.environ.get("BRIDGE_BIND_HOST", "127.0.0.1"), port=9090, reload=True)
