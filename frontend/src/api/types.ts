export interface ApiErrorShape {
  code: string;
  message: string;
  retryable?: boolean;
  details?: Record<string, unknown>;
}

export interface AuthStatusResponse {
  password_required: boolean;
  api_auth_required: boolean;
  direct_loopback: boolean;
  authenticated: boolean;
  is_admin: boolean;
  project_id: string;
  can_bootstrap_local_session: boolean;
}

export interface HealthResponse {
  ok: boolean;
  service: string;
  version: string;
  db: boolean;
  default_provider: string;
  default_country?: number;
  active_providers: string[];
  provider_configured: Record<string, boolean>;
  plugins: Record<string, { enabled: boolean; started?: boolean; ok?: boolean; status?: string; error?: string }>;
}

export interface ProviderDescriptor {
  id: string;
  label: string;
  tier: "primary" | "legacy" | "custom";
  active: boolean;
  configured: boolean;
  default_base_url: string;
  capabilities: {
    supports_resend: boolean;
    supports_price_lock: boolean;
    supports_custom_service: boolean;
  };
  note: string;
}

export interface ServiceOption {
  id: string;
  label: string;
  code: string;
  popular: boolean;
}

export interface CountryOption {
  id: number | string;
  name: string;
  name_zh?: string;
  cc?: string;
  iso?: string;
  aliases?: string[];
}

export interface PriceEntry {
  cost: number;
  count: number;
  in_stock: boolean;
}

export interface PriceSnapshot {
  provider: string;
  country: string;
  country_label: string;
  service: string;
  service_code: string;
  entries: PriceEntry[];
  low: number | null;
  middle: number | null;
  high: number | null;
  stock: number;
  available: boolean;
  cached?: boolean;
}

export interface SmsMessage {
  id: number;
  seq: number;
  code: string;
  raw_text: string;
  received_at: string;
}

export type OrderStatus =
  | "acquiring"
  | "waiting_code"
  | "code_received"
  | "waiting_next_code"
  | "completing"
  | "cancelling"
  | "completed"
  | "cancelled"
  | "expired"
  | "failed"
  | "compensation_pending";

export interface SmsOrder {
  order_id: string;
  idempotency_key?: string | null;
  project_id: string;
  external_ref: string;
  provider: string;
  service: string;
  service_code: string;
  country_id: string;
  country_label: string;
  country_iso2: string;
  country_calling_code: string;
  activation_id: string;
  phone_raw: string;
  phone_e164: string;
  phone_national: string;
  status: OrderStatus;
  settlement_state?: string;
  refund_state?: string;
  latest_code: string;
  code_scrubbed: boolean;
  code_count: number;
  max_price: number | null;
  cost: number | null;
  actual_cost?: number | null;
  cost_status?: string;
  cost_is_estimated: boolean;
  currency: string;
  wait_timeout: number;
  expires_at: string;
  attempts_used: number;
  error_code: string;
  error_message: string;
  webhook_url: string;
  metadata: Record<string, unknown>;
  messages: SmsMessage[];
  created_at: string;
  updated_at: string;
  completed_at: string;
}

export interface OrderListResponse {
  items: SmsOrder[];
  total: number;
  page: number;
  page_size: number;
}

export interface CreateOrderPayload {
  provider?: string;
  service: string;
  country: string;
  country_fallback?: string[];
  strategy?: {
    target_price?: number;
    price_max?: number;
    price_lock?: boolean;
    price_float_enabled?: boolean;
    price_float_percent?: number;
    max_attempts?: number;
    continuous_poll?: boolean;
    request_interval_enabled?: boolean;
    request_interval?: number;
  };
  wait_timeout?: number;
  skip_blacklisted?: boolean;
  async_acquire?: boolean;
  external_ref?: string;
  webhook_url?: string;
}

export interface CurrencyBreakdown {
  currency: string;
  actual_cost_total: number;
  refunded_amount_total: number;
  net_actual_cost_total: number;
  estimated_cost_total: number;
  cost_total: number;
  unknown_cost_count: number;
  refund_amount_unknown_count?: number;
}

export interface FxConversionSummary {
  target_currency: string;
  as_of_date: string;
  rates: Record<string, number>;
  actual_cost_total: number;
  refunded_amount_total: number;
  net_actual_cost_total: number;
  estimated_cost_total: number;
  cost_total: number;
}

export interface FinancialAggregateFields {
  currency?: string | null;
  is_multi_currency?: boolean;
  actual_cost_total?: number | null;
  refunded_amount_total?: number | null;
  refund_amount_unknown_count?: number;
  net_actual_cost_total?: number | null;
  estimated_cost_total?: number | null;
  unknown_cost_count?: number;
  cost_total?: number | null;
  by_currency?: CurrencyBreakdown[];
  currency_review_count?: number;
  currency_review_by_currency?: CurrencyBreakdown[];
  fx_conversion?: FxConversionSummary | null;
}

export interface StatsResponse {
  timezone_offset_minutes: number;
  window_days: number;
  active_orders: number;
  blacklist_count: number;
  today: FinancialAggregateFields & {
    total: number;
    received_count: number;
    completed_count: number;
    completed_without_code_count?: number;
    unreceived_paid_count?: number;
    refunded_count: number;
    refund_confirmed_count?: number;
    refund_pending_count?: number;
    unconfirmed_refund_count?: number;
    success_rate: number;
    cost_total: number | null;
  };
  by_project: Array<
    FinancialAggregateFields & {
      project_id: string;
      total: number;
      received_count: number;
      cost_total: number | null;
    }
  >;
  by_service: Array<
    FinancialAggregateFields & {
      service: string;
      service_code: string;
      total: number;
      received_count: number;
      cost_total: number | null;
    }
  >;
}

export interface BlacklistEntry {
  id: number;
  phone: string;
  provider: string;
  project_id?: string;
  reason: string;
  created_at: string;
}

export interface ProjectApiKey extends FinancialAggregateFields {
  project_id: string;
  name: string;
  key_prefix: string;
  scopes: string[];
  default_webhook_url: string;
  has_webhook_secret: boolean;
  enabled: boolean;
  last_used_at: string;
  created_at: string;
  order_count?: number;
  received_count?: number;
  api_key?: string;
}

export interface SettingsResponse {
  active_providers: string[];
  default_provider: string;
  provider_catalog: ProviderDescriptor[];
  smsbower_base_url: string;
  smsbower_default_country: number;
  smsbower_api_key_masked: string;
  five_sim_api_key_masked: string;
  hero_sms_api_key_masked: string;
  hero_sms_base_url: string;
  api_auth_required: boolean;
  sms_code_retention_hours: number;
  sms_code_mask_in_history: boolean;
  encryption_active: boolean;
  plugins: Array<{
    id: string;
    name: string;
    description: string;
    enabled: boolean;
    started: boolean;
  }>;
}

export interface LogsResponse {
  path: string;
  level: string;
  counts: Record<string, number>;
  entries: string[];
}

export interface WebhookDeliveryItem {
  delivery_id: string;
  event_id: string;
  event_type: string;
  order_id: string;
  project_id: string;
  target_url: string;
  status: string;
  attempts: number;
  max_attempts: number;
  last_status_code: number;
  last_error: string;
  payload_scrubbed?: boolean | number;
  created_at: string;
  updated_at: string;
}

export interface ReconciliationActivationItem {
  id: number;
  order_id: string;
  attempt_no: number;
  provider: string;
  country_id: string;
  service_code: string;
  activation_id: string;
  phone_raw: string;
  phone_e164: string;
  state: string;
  actual_cost: number | null;
  estimated_cost: number | null;
  currency: string;
  currency_review_required?: boolean | number;
  last_error: string;
  updated_at: string;
}

export interface ReconciliationResponse {
  uncertain_activations: ReconciliationActivationItem[];
  pending_refund_activations: ReconciliationActivationItem[];
  currency_review_activations: ReconciliationActivationItem[];
  orders_requiring_review: Array<{
    order_id: string;
    project_id: string;
    provider: string;
    service: string;
    activation_id: string;
    status: OrderStatus;
    settlement_state: string;
    refund_state: string;
    error_code: string;
    error_message: string;
    updated_at: string;
  }>;
}
