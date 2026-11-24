"""wa-sms-bridge 集成测试套件 — Mock Fixtures。

使用 unittest.mock 完全 mock SMSBower 和 wa-app，不产生真实 HTTP 请求。
"""

import asyncio
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from app.models import PhoneInput
from app.providers.base import (
    Activation,
    SmsProviderError,
    NoNumbersError,
    BalanceError,
    SmsCancelledError,
    SmsExpiredError,
    SmsTimeoutError,
    SmsProtocolError,
)
from app.providers.smsbower import SmsBowerProvider
from app.waapi import ProbeResult, RegisterResult, WaApiClient


# ── Pytest asyncio 模式 ────────────────────────────────

def pytest_configure(config):
    """配置 pytest-asyncio 默认模式。"""
    config.option.asyncio_mode = "auto"


# ── 工厂：构建 mock 对象 ──────────────────────────────

def make_activation(
    activation_id: str = "act_123456",
    phone_number: str = "79001234567",
    country_id: int = 0,
    country_label: str = "Russian Federation",
    provider: str = "smsbower",
) -> Activation:
    """创建 Activation 实例用于测试。"""
    return Activation(
        activation_id=activation_id,
        phone_number=phone_number,
        country_id=country_id,
        country_label=country_label,
        provider=provider,
    )


def make_phone_input(
    region: str = "RU",
    phone: str = "9001234567",
    e164_number: str = "+79001234567",
    country_calling_code: str = "7",
    country_iso2: str = "ru",
) -> PhoneInput:
    """创建 PhoneInput 实例用于测试。"""
    return PhoneInput(
        region=region,
        phone=phone,
        e164_number=e164_number,
        country_calling_code=country_calling_code,
        country_iso2=country_iso2,
    )


def make_register_result(
    success: bool = True,
    wa_account_id: str = "wa_acc_111",
    status: str = "OK",
    error_message: str = "",
    registration_phase: str = "",
    request_failed: bool = False,
    login_active: bool = True,
    registered: bool = True,
    error_code: str = "",
    failure_reason: str = "",
) -> RegisterResult:
    """创建 RegisterResult（含属性控制）。"""
    raw = {
        "success": success,
        "wa_account_id": wa_account_id,
        "verification_request_id": "vreq_999",
        "status": status,
        "error_message": error_message,
        "request_failed": request_failed,
        "registration_phase": registration_phase,
    }
    if login_active:
        raw["login_state"] = {"status": "LOGIN_STATE_STATUS_ACTIVE"}
    else:
        raw["login_state"] = {"status": "LOGIN_STATE_STATUS_EXPIRED"}
    if registered:
        raw["registration"] = {"status": "REGISTRATION_STATUS_REGISTERED"}
    else:
        raw["registration"] = {"status": "REGISTRATION_STATUS_NOT_REGISTERED"}
    if error_code or failure_reason:
        message = error_message or (
            f"registration was rejected: reason={failure_reason}"
            if failure_reason else "registration failed"
        )
        raw["error"] = {"code": error_code, "message": message}
    return RegisterResult(raw)


def make_probe_result(
    success: bool = True,
    passed: bool = True,
    blocked: bool = False,
    request_failed: bool = False,
) -> ProbeResult:
    """创建 ProbeResult。"""
    raw = {
        "success": success,
        "passed": passed,
        "blocked": blocked,
        "request_failed": request_failed,
    }
    return ProbeResult(raw)


# ── AsyncMock 辅助 ────────────────────────────────────

class AsyncContextManagerMock(MagicMock):
    """模拟 async with 的 context manager。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@pytest.fixture
def orch(mock_db, mock_smsbower, mock_waapi):
    """创建编排器实例，自动注入 registry。"""
    from app.providers.registry import ProviderRegistry
    reg = ProviderRegistry()
    reg.init_provider("smsbower", mock_smsbower)
    from app.orchestrator import RegisterOrchestrator
    o = RegisterOrchestrator(reg, mock_waapi, mock_db)
    return o


# ── Fixtures ───────────────────────────────────────────


@pytest_asyncio.fixture
async def mock_db():
    """模拟 Database 层，所有方法为 AsyncMock。"""
    mock = MagicMock()

    # 数据库连接/关闭
    mock.open = AsyncMock()
    mock.close = AsyncMock()

    # 黑名单
    mock.is_blacklisted = AsyncMock(return_value=False)
    mock.add_blacklist = AsyncMock()
    mock.list_blacklist = AsyncMock(return_value=[])
    mock.remove_blacklist = AsyncMock()

    # 任务状态
    mock.create_task = AsyncMock()
    mock.update_task = AsyncMock()
    mock.get_task = AsyncMock(return_value={
        "task_id": "test_task",
        "status": "running",
        "country_id": 0,
        "country_label": "Russian Federation",
        "activation_id": "act_123456",
        "phone_number": "79001234567",
        "wa_account_id": "",
        "step": "init",
        "error": "",
        "attempts_used": 0,
    })
    mock.list_tasks = AsyncMock(return_value=[])
    mock.get_pending_tasks = AsyncMock(return_value=[])

    # 配置
    mock.get_config = AsyncMock(return_value="")
    mock.set_config = AsyncMock()
    mock.get_all_config = AsyncMock(return_value={})

    # 历史
    mock.save_history = AsyncMock()
    mock.record_registration_success = AsyncMock()
    mock.list_history = AsyncMock(return_value=[])
    mock.get_stats = AsyncMock(return_value={
        "providers": {},
        "success_count": 0,
        "cost_total": 0,
        "estimated_count": 0,
    })
    mock.health = AsyncMock(return_value=True)

    # 恢复
    mock.cancel_stale_tasks = AsyncMock(return_value=0)

    return mock


@pytest_asyncio.fixture
async def mock_smsbower():
    """模拟 SmsBowerProvider，所有 API 方法为 AsyncMock。"""
    mock = MagicMock(spec=SmsBowerProvider)
    mock.resolve_country = AsyncMock(return_value=(0, "Russia"))
    mock.request_number = AsyncMock()
    # ── 核心方法 ────────────────────────────────────
    mock.request_number = AsyncMock(return_value=make_activation())
    mock.get_sms_code = AsyncMock(return_value=("WAIT", ""))
    mock.poll_sms_code = AsyncMock(return_value="123456")
    mock.get_balance = AsyncMock(return_value=0.94)
    mock.get_countries = AsyncMock(return_value=[])
    mock.get_prices = AsyncMock(return_value=[])
    mock.normalize_phone = AsyncMock(return_value=make_phone_input())
    mock.cancel_activation = AsyncMock(return_value="ACCESS_CANCEL")
    mock.complete_activation = AsyncMock(return_value="ACCESS_READY")
    mock._safe_cancel = AsyncMock(return_value="ACCESS_CANCEL")
    mock.request_additional_sms = AsyncMock(return_value="ACCESS_READY")
    mock.close = AsyncMock()

    return mock


@pytest_asyncio.fixture
async def mock_waapi():
    """模拟 WaApiClient，所有 API 方法为 AsyncMock。"""
    mock = MagicMock(spec=WaApiClient)

    mock.probe_phone = AsyncMock(return_value=make_probe_result())
    mock.register = AsyncMock(return_value=make_register_result())
    mock.submit_otp = AsyncMock(return_value=make_register_result())
    mock.check_login_state = AsyncMock(return_value=True)
    mock.cleanup_failed_account = AsyncMock()
    mock.list_accounts = AsyncMock(return_value=[
        {"wa_account_id": "wa_acc_111", "phone": "+79001234567"},
    ])
    mock.close = AsyncMock()

    return mock


@pytest_asyncio.fixture
async def orchestrator(mock_smsbower, mock_waapi, mock_db):
    """构建 RegisterOrchestrator 实例，注入 mock 依赖。"""
    from app.providers.registry import ProviderRegistry
    reg = ProviderRegistry()
    reg.init_provider("smsbower", mock_smsbower)
    from app.orchestrator import RegisterOrchestrator
    orch = RegisterOrchestrator(reg, mock_waapi, mock_db)

    # 避免 _tasks 泄漏到其他测试
    yield orch

    # 清理
    for tid, task in list(orch._tasks.items()):
        task.cancel()
    orch._tasks.clear()


# ── 路径补丁 fixtures（在需要 patch 模块级导入时使用） ─

@pytest.fixture
def patch_httpx():
    """补丁 httpx.AsyncClient，防止意外网络请求。

    使用方式：在测试函数中声明这个 fixture（会自动应用补丁）。
    """
    with patch("httpx.AsyncClient") as mock:
        mock.return_value.get = AsyncMock(return_value=MagicMock(text=""))
        yield mock


@pytest.fixture
def patch_smsbower_http(patch_httpx):
    """补丁 SmsBowerProvider._get 使其返回可控制的值。

    可在测试中通过 mock_smsbower_get.side_effect 或 return_value 控制。
    """
    with patch.object(SmsBowerProvider, "_get", new_callable=AsyncMock) as mock_get:
        yield mock_get


@pytest.fixture
def patch_waapi_http():
    """补丁 WaApiClient._request 使其返回可控的 mock。

    可在测试中通过 mock_waapi_request.side_effect / return_value 控制。
    """
    with patch.object(WaApiClient, "_request", new_callable=AsyncMock) as mock_req:
        mock_resp = MagicMock()
        mock_resp.json = MagicMock(return_value={"success": True})
        mock_resp.text = ""
        mock_resp.status_code = 200
        mock_req.return_value = mock_resp
        yield mock_req
