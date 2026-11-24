"""FastAPI 应用入口。"""

import logging
import os
import asyncio
import gzip
import shutil
from contextlib import asynccontextmanager
from logging.handlers import MemoryHandler, RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import env
from app.db import Database
from app.providers.smsbower import SmsBowerProvider
from app.providers.five_sim import FiveSimProvider
from app.providers.hero_sms import HeroSmsProvider
from app.providers.registry import registry
from app.adapters import create_wa_adapter
from app.orchestrator import RegisterOrchestrator
from app.log_redaction import redact_text

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%H:%M:%S"


class SecretRedactionFilter(logging.Filter):
    """Redact common token/query-secret patterns before records reach handlers."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = ()
        return True


_SECRET_FILTER = SecretRedactionFilter()
_FILE_BUFFER_HANDLER: MemoryHandler | None = None


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


def _gzip_rotated_log(source: str, dest: str):
    with open(source, "rb") as src, gzip.open(dest, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst)
    os.remove(source)


def configure_file_logging() -> Path:
    """Attach a local rotating log file for post-mortem diagnostics."""
    global _FILE_BUFFER_HANDLER
    log_path = Path(os.environ.get("BRIDGE_LOG_FILE") or Path(env.bridge_data_dir) / "bridge.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, "_bridge_file_handler", False):
            return log_path

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=int(os.environ.get("BRIDGE_LOG_MAX_BYTES", str(2 * 1024 * 1024))),
        backupCount=int(os.environ.get("BRIDGE_LOG_BACKUPS", "5")),
        encoding="utf-8",
        delay=True,
    )
    file_handler.namer = lambda name: f"{name}.gz"
    file_handler.rotator = _gzip_rotated_log
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    file_handler.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))
    file_handler.addFilter(_SECRET_FILTER)

    buffer_handler = MemoryHandler(
        capacity=int(os.environ.get("BRIDGE_LOG_BUFFER_CAPACITY", "100")),
        flushLevel=getattr(logging, os.environ.get("BRIDGE_LOG_FLUSH_LEVEL", "WARNING").upper(), logging.WARNING),
        target=file_handler,
        flushOnClose=True,
    )
    buffer_handler.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))
    buffer_handler.addFilter(_SECRET_FILTER)
    buffer_handler._bridge_file_handler = True
    root.addHandler(buffer_handler)
    _FILE_BUFFER_HANDLER = buffer_handler
    return log_path


def flush_file_logging():
    if _FILE_BUFFER_HANDLER:
        _FILE_BUFFER_HANDLER.flush()


def _missing_api_key_error(error: Exception) -> bool:
    return "API Key 未配置" in str(error)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动 → 运行 → 关闭。"""
    # ── 启动 ────────────────────────────────────────────
    # 确保数据目录存在
    os.makedirs(env.bridge_data_dir, exist_ok=True)
    log_path = configure_file_logging()
    logger.info("wa-sms-bridge 启动中... log_file=%s", log_path)

    # 数据库
    db = Database(env.db_path)
    await db.open()

    # 检查是否有种子 API Key（环境变量），写入 SQLite
    seed_key = os.environ.get("SMSBOWER_API_KEY", "")
    if seed_key:
        existing = await db.get_config("smsbower_api_key", "")
        if not existing:
            logger.info("从环境变量写入初始 SMSBOWER_API_KEY")
            await db.set_config("smsbower_api_key", seed_key, encrypt=True)

    # Provider（API Key 从 SQLite 配置表读取，支持运行时热更新）
    async def get_smsbower_key():
        return await db.get_config("smsbower_api_key", "")

    async def get_smsbower_base_url():
        return await db.get_config("smsbower_base_url", "")

    async def get_five_sim_key():
        return await db.get_config("five_sim_api_key", "")

    async def get_hero_sms_key():
        return await db.get_config("hero_sms_api_key", "")

    async def get_hero_sms_base_url():
        return await db.get_config("hero_sms_base_url", "")

    # 创建 provider（各自独立 try/except，避免一个失败阻塞其他）
    smsbower_provider = five_sim_provider = hero_sms_provider = None
    try:
        smsbower_provider = SmsBowerProvider(get_smsbower_key, base_url_getter=get_smsbower_base_url)
        registry.init_provider("smsbower", smsbower_provider)
    except Exception as e:
        logger.warning("SMSBower provider 初始化失败: %s", e)
    try:
        five_sim_provider = FiveSimProvider(get_five_sim_key)
        registry.init_provider("5sim", five_sim_provider)
    except Exception as e:
        logger.warning("5sim provider 初始化失败: %s", e)
    try:
        hero_sms_provider = HeroSmsProvider(get_hero_sms_key, base_url_getter=get_hero_sms_base_url)
        registry.init_provider("herosms", hero_sms_provider)
    except Exception as e:
        logger.warning("HeroSMS provider 初始化失败: %s", e)

    # wa-app API 客户端。优先使用 bridge 自己持久化的上游地址。
    wa_app_url = await db.get_config("wa_app_url", env.wa_app_url)
    waapi = create_wa_adapter(env.wa_adapter, wa_app_url or env.wa_app_url)
    waapi.configure_auth(
        auth_type=env.wa_app_auth_type,
        token=env.wa_app_api_token,
        username=env.wa_app_api_username,
        password=env.wa_app_api_password,
    )

    # 编排器
    orch = RegisterOrchestrator(registry, waapi, db)
    stale_count = await orch.recover_stale_tasks()
    if stale_count:
        logger.warning("启动时已检查并处理 %d 个残留任务", stale_count)
    orch.start_background_tasks()

    # 注入 router 依赖
    from app.router_api import _init as router_init
    router_init(orch, registry, waapi, db)

    # 挂载静态文件
    static_dir = Path(__file__).parent / "web"
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="web")
        logger.info("管理界面已挂载: %s", static_dir)

    app.state.db = db
    app.state.waapi = waapi
    app.state.orch = orch

    # 连接测试
    try:
        bal = await smsbower_provider.get_balance()
        logger.info("SMSBower 余额: $%.2f", bal)
    except Exception as e:
        if _missing_api_key_error(e):
            logger.info("SMSBower 未配置 API Key，跳过初始连接测试")
        else:
            logger.warning("SMSBower 初始连接测试失败: %s", e)

    # 测试 5sim/HeroSMS（仅在有 Key 时）
    for name, prov in [("5sim", five_sim_provider), ("HeroSMS", hero_sms_provider)]:
        try:
            bal = await prov.get_balance()
            logger.info("%s 余额: $%.2f", name, bal)
        except Exception as e:
            if _missing_api_key_error(e):
                logger.info("%s 未配置 API Key，跳过连接测试", name)
            else:
                logger.warning("%s 连接测试失败: %s", name, e)

    try:
        health = await asyncio.wait_for(waapi.health(), timeout=2.5)
        logger.info("wa-app 连接正常: %s", health)
    except asyncio.TimeoutError:
        logger.warning("wa-app 连接测试超时（bridge 继续启动）")
    except Exception as e:
        logger.warning("wa-app 连接测试失败: %s", e)

    logger.info("wa-sms-bridge 就绪")
    yield

    # ── 关闭 ────────────────────────────────────────────
    logger.info("wa-sms-bridge 关闭中...")
    # 1. 先停止所有编排任务
    try:
        await orch.shutdown()
    except Exception:
        logger.warning("编排器关闭异常", exc_info=True)
    # 2. 关闭 provider（此时没有任务在运行）
    for name, prov in registry.all_instances().items():
        try:
            await prov.close()
        except Exception:
            logger.warning("关闭 provider '%s' 异常", name, exc_info=True)
    # 3. 关闭 wa-app 客户端
    try:
        await waapi.close()
    except Exception:
        logger.warning("wa-api 客户端关闭异常", exc_info=True)
    # 4. 关闭数据库
    try:
        await db.close()
    except Exception:
        logger.warning("数据库关闭异常", exc_info=True)
    logger.info("wa-sms-bridge 已关闭")
    flush_file_logging()


app = FastAPI(
    title="wa-sms-bridge",
    version="0.2.0",
    lifespan=lifespan,
)

from app.router_api import router as api_router
app.include_router(api_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=9090, reload=True)
