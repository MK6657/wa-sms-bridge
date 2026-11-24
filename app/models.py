"""Pydantic 数据模型。"""

from pydantic import BaseModel, Field, model_validator
from typing import Optional


class RegisterRequest(BaseModel):
    country: str = Field(default="", description="主要国家名/ID，如 russia / 0")
    country_fallback: list = Field(default_factory=list, description="备用国家 ID/名列表")
    service: str = Field(default="whatsapp", description="服务用途")
    max_attempts: int = Field(default=3, ge=1, le=100, description="最大尝试次数（仅计实际发码）")
    price_max: Optional[float] = Field(default=None, ge=0, description="价格上限 $")
    target_price: Optional[float] = Field(default=None, ge=0, description="目标价格 $")
    price_lock_enabled: bool = Field(default=False, description="锁定目标价格档位")
    price_float_enabled: bool = Field(default=False, description="是否启用价格浮动")
    price_float_percent: float = Field(default=0, ge=0, le=1000, description="价格浮动百分比")
    continuous_poll: bool = Field(default=False, description="是否一直轮询直到成功/停止")
    target_success_count: int = Field(default=1, ge=1, le=1000, description="本任务目标成功注册数量")
    request_interval_enabled: bool = Field(default=False, description="是否启用取号间隔")
    request_interval: float = Field(default=1.0, ge=0.1, le=3600, description="取号间隔秒数")
    wait_timeout: int = Field(default=180, ge=30, le=600, description="等码超时秒数")
    proxy: str = Field(default="", description="代理地址，如 socks5://127.0.0.1:1080")
    provider: str = Field(default="smsbower", description="SMS 平台: smsbower / 5sim / herosms")

    @model_validator(mode="after")
    def validate_attempt_budget(self):
        if not self.continuous_poll and self.target_success_count > self.max_attempts:
            raise ValueError("未开启一直轮询时，尝试次数不能小于目标成功次数")
        if (self.price_lock_enabled or self.price_float_enabled) and not (
            self.target_price is not None and self.target_price > 0
        ):
            raise ValueError("开启锁定价格或价格浮动时，目标价格必须大于 0")
        return self


class TaskInfo(BaseModel):
    task_id: str
    status: str  # running / completed / failed / cancelled
    country_id: str
    country_label: str
    phone: str = ""
    wa_account_id: str = ""
    step: str = ""
    error: str = ""
    created_at: str = ""
    updated_at: str = ""


class ProviderBalance(BaseModel):
    balance: float
    currency: str = "USD"


class ConfigUpdate(BaseModel):
    smsbower_api_key: Optional[str] = None
    smsbower_base_url: Optional[str] = None
    smsbower_default_country: Optional[int] = None
    five_sim_api_key: Optional[str] = None
    hero_sms_api_key: Optional[str] = None
    hero_sms_base_url: Optional[str] = None
    wa_app_url: Optional[str] = None


class ConfigResponse(BaseModel):
    wa_app_url: str
    smsbower_base_url: str
    hero_sms_base_url: str = ""
    smsbower_default_country: int
    smsbower_api_key_masked: str = ""
    five_sim_api_key_masked: str = ""
    hero_sms_api_key_masked: str = ""
    wa_app_auth_type: str


class BlacklistEntry(BaseModel):
    id: int = 0
    phone: str
    reason: str = ""
    created_at: str = ""


class PhoneInput(BaseModel):
    """wa-app PhoneInput 结构，与 phoneFromAction 兼容。"""
    region: str = ""
    phone: str = ""
    e164_number: str = ""
    country_calling_code: str = ""
    country_iso2: str = ""
