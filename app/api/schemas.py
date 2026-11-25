"""API v1 请求与响应 Pydantic v2 模型（含完整 OpenAPI 契约，修复 P1-05 / P2-01 / P2-05）。"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class ErrorDetailModel(BaseModel):
    code: str = Field(..., description="稳定机器可读错误码")
    message: str = Field(..., description="人类可读错误说明")
    retryable: bool = Field(default=False, description="是否建议重试")
    details: Optional[dict[str, Any]] = Field(default=None, description="可选附加诊断详情")


class ErrorEnvelopeResponse(BaseModel):
    error: ErrorDetailModel
    detail: Any = Field(..., description="兼容 FastAPI 默认字段的错误摘要")


class SmsMessageResponse(BaseModel):
    id: int
    seq: int
    code: str
    raw_text: str
    received_at: str


class OrderActivationResponse(BaseModel):
    id: int
    order_id: str
    attempt_no: int
    provider: str
    country_id: str
    service_code: str
    activation_id: str
    phone_raw: str
    phone_e164: str
    state: str
    quoted_max_price: Optional[float] = None
    actual_cost: Optional[float] = None
    estimated_cost: Optional[float] = None
    cost_status: str = "unknown"
    currency: str = ""
    currency_source: str = ""
    currency_review_required: bool = False
    refund_amount: Optional[float] = None
    refund_amount_status: str = "none"
    refund_amount_source: str = ""
    last_error: str = ""
    created_at: str = ""
    updated_at: str = ""


class OrderResponse(BaseModel):
    order_id: str
    idempotency_key: Optional[str] = None
    project_id: str
    external_ref: str = ""
    provider: str
    service: str
    service_code: str
    country_id: str
    country_label: str = ""
    country_iso2: str = ""
    country_calling_code: str = ""
    activation_id: str = ""
    phone_raw: str = ""
    phone_e164: str = ""
    phone_national: str = ""
    status: str
    version: int = 1
    settlement_state: str = "none"
    refund_state: str = "none"
    latest_code: str = ""
    code_received_at: str = ""
    code_scrubbed: bool = False
    code_count: int = 0
    max_price: Optional[float] = None
    quoted_max_price: Optional[float] = None
    cost: Optional[float] = None
    actual_cost: Optional[float] = None
    cost_status: str = "unknown"
    cost_is_estimated: bool = False
    currency: str = "USD"
    wait_timeout: int = 180
    expires_at: str = ""
    attempts_used: int = 0
    error_code: str = ""
    error_message: str = ""
    webhook_url: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    messages: list[SmsMessageResponse] = Field(default_factory=list)
    activations: list[OrderActivationResponse] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    completed_at: str = ""


class AcquireStrategyRequest(BaseModel):
    target_price: Optional[float] = Field(default=None, gt=0, description="目标价格 ($)，必须大于 0")
    price_max: Optional[float] = Field(
        default=None,
        gt=0,
        description="绝对价格上限 ($)，必须大于 0；与 target_price 同时提供时不可低于 target_price，且浮动结果受其向下截断硬性约束",
    )
    price_lock: bool = Field(
        default=False,
        description="检查目标价格档位库存后按 maxPrice<=target_price 取号（SMSBower 协议为上限语义，不保证精确固定价）",
    )
    strict_exact_price: bool = Field(
        default=False,
        description="要求平台协议原生保证并校验精确成交档位；不支持精确锁价的平台（如 SMSBower）将拒绝并返回 422",
    )
    price_float_enabled: bool = Field(default=False, description="是否按百分比放宽目标价格")
    price_float_percent: float = Field(default=0.0, ge=0, le=1000, description="价格浮动百分比 (%)")
    max_attempts: int = Field(default=1, ge=1, le=100, description="最大取号尝试次数")
    continuous_poll: bool = Field(default=False, description="持续轮询抢号直到成功或取消（返回 HTTP 202）")
    request_interval_enabled: bool = Field(default=False, description="是否启用自定义取号间隔")
    request_interval: float = Field(default=1.0, ge=0.1, le=3600, description="取号重试间隔（秒）")

    @model_validator(mode="after")
    def validate_strategy(self):
        if (self.price_lock or self.price_float_enabled or self.strict_exact_price) and not (
            self.target_price is not None and self.target_price > 0
        ):
            raise ValueError("开启锁价、精确锁价或价格浮动时，target_price 必须大于 0")
        if self.target_price is not None and self.price_max is not None and self.target_price > self.price_max:
            raise ValueError(f"target_price ({self.target_price}) 不能高于绝对价格上限 price_max ({self.price_max})")
        return self


class CreateOrderRequest(BaseModel):
    provider: str = Field(default="", description="接码平台 ID（省略时自动使用服务端配置的默认平台）")
    service: str = Field(default="whatsapp", description="服务名（如 whatsapp/telegram/google/openai）或原生服务码")
    country: str = Field(..., min_length=1, description="国家名（中/英）、ISO2、区号或平台国家 ID")
    country_fallback: list[str] = Field(default_factory=list, description="备选国家列表")
    strategy: AcquireStrategyRequest = Field(default_factory=AcquireStrategyRequest, description="价格与抢号策略")
    wait_timeout: int = Field(default=180, ge=30, le=1200, description="等待验证码超时秒数")
    skip_blacklisted: bool = Field(default=True, description="命中黑名单号码时自动退款重取")
    async_acquire: bool = Field(default=False, description="是否异步后台取号（true 时立即返回 HTTP 202 与 acquiring 状态）")
    external_ref: str = Field(default="", max_length=128, description="外部项目自定义关联 ID")
    webhook_url: str = Field(default="", max_length=512, description="订单专属 Webhook 回调地址（项目需与管理员登记目标同源）")
    metadata: dict[str, Any] = Field(default_factory=dict, description="外部项目透传元数据")


class CancelOrderRequest(BaseModel):
    blacklist: bool = Field(default=False, description="取消时是否加入黑名单（管理员写全局，项目密钥写本项目私有黑名单）")
    reason: str = Field(default="", max_length=200, description="取消/拉黑原因")


class BlacklistCreateRequest(BaseModel):
    phone: str = Field(..., min_length=3, max_length=32, description="手机号")
    provider: str = Field(default="", description="平台 ID（留空使用服务端默认平台）")
    reason: str = Field(default="", max_length=200, description="加入原因")
    scope: Literal["auto", "project", "global"] = Field(
        default="auto",
        description="作用范围：管理员默认 global，项目密钥仅允许 project",
    )


class BlacklistBatchRequest(BaseModel):
    phones: list[str] = Field(..., min_length=1, max_length=200, description="手机号列表")
    provider: str = Field(default="", description="平台 ID（留空使用服务端默认平台）")
    reason: str = Field(default="批量加入", max_length=200, description="加入原因")
    scope: Literal["auto", "project", "global"] = Field(default="auto")


class AdminLoginRequest(BaseModel):
    password: str = Field(..., min_length=1, max_length=256, description="管理员口令 (CONSOLE_ADMIN_PASSWORD)")


class CreateApiKeyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, description="接入项目名称，如 Telegram 注册机")
    project_id: str = Field(default="", max_length=48, description="可选自定义项目 ID（字母数字下划线）")
    scopes: Optional[list[str]] = Field(
        default=None,
        description="权限范围（省略时授予默认读写权限；显式传空列表 [] 则不授予任何权限）",
    )
    default_webhook_url: str = Field(default="", max_length=512, description="项目默认 HTTPS Webhook 回调地址")
    webhook_secret: str = Field(default="", max_length=128, description="可选 HMAC-SHA256 签名密钥（需配置 BRIDGE_SECRET_KEY）")

    @model_validator(mode="after")
    def validate_name(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("项目名称不能为空")
        return self


class ResolveReconciliationRequest(BaseModel):
    state: Literal["refund_confirmed", "unrefundable", "completed", "manual_resolved"] = Field(
        ..., description="人工核对后的最终状态"
    )
    note: str = Field(default="", max_length=300, description="人工核对备注/上游账单流水号")
    refund_amount: Optional[float] = Field(
        default=None, ge=0, description="可选：经上游账单核实的实际退款金额（未提供时记为未知）"
    )
    currency: Optional[str] = Field(
        default=None, max_length=16, description="可选：人工核对确认的币种代码（如 USD / RUB / EUR）"
    )


class UpdateApiKeyRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    enabled: Optional[bool] = None
    scopes: Optional[list[str]] = None
    default_webhook_url: Optional[str] = Field(default=None, max_length=512)
    webhook_secret: Optional[str] = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_optional_name(self):
        if self.name is not None:
            self.name = self.name.strip()
            if not self.name:
                raise ValueError("项目名称不能为空")
        return self


class WebhookTestRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=512, description="目标 Webhook URL")
    secret: str = Field(default="", max_length=128, description="可选签名密钥")
    project_id: str = Field(default="", max_length=48, description="可选项目 ID（复用其保存的签名密钥）")


class SettingsUpdateRequest(BaseModel):
    smsbower_api_key: Optional[str] = None
    smsbower_base_url: Optional[str] = None
    smsbower_default_country: Optional[int] = Field(default=None, ge=0)
    five_sim_api_key: Optional[str] = None
    hero_sms_api_key: Optional[str] = None
    hero_sms_base_url: Optional[str] = None
    api_auth_required: Optional[bool] = None
    sms_code_retention_hours: Optional[int] = Field(default=None, ge=-1, le=8760)
    sms_code_mask_in_history: Optional[bool] = None
