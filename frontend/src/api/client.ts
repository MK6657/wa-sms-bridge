import type {
  AuthStatusResponse,
  BlacklistEntry,
  CountryOption,
  CreateOrderPayload,
  HealthResponse,
  LogsResponse,
  OrderListResponse,
  PriceSnapshot,
  ProjectApiKey,
  ProviderDescriptor,
  ReconciliationActivationItem,
  ReconciliationResponse,
  ServiceOption,
  SettingsResponse,
  SmsOrder,
  StatsResponse,
  WebhookDeliveryItem,
} from "./types";

export class ApiClientError extends Error {
  status: number;
  code: string;
  retryable: boolean;
  details?: Record<string, unknown>;
  orderId?: string;

  constructor(
    message: string,
    status: number,
    code = "API_ERROR",
    retryable = false,
    details?: Record<string, unknown>,
  ) {
    super(message);
    this.status = status;
    this.code = code;
    this.retryable = retryable;
    this.details = details;
    this.orderId = typeof details?.order_id === "string" && details.order_id.trim()
      ? details.order_id.trim()
      : undefined;
  }
}

export function parseApiErrorResponse(
  status: number,
  data: Record<string, unknown>,
): ApiClientError {
  const errObj =
    data.error && typeof data.error === "object"
      ? (data.error as Record<string, unknown>)
      : null;
  const message =
    (errObj?.message as string) ||
    (typeof data.detail === "string" ? data.detail : "") ||
    `请求失败 (HTTP ${status})`;
  const code = (errObj?.code as string) || `HTTP_${status}`;
  const retryable = Boolean(errObj?.retryable);
  const details =
    errObj?.details && typeof errObj.details === "object"
      ? (errObj.details as Record<string, unknown>)
      : undefined;
  return new ApiClientError(message, status, code, retryable, details);
}

let unauthorizedHandler: ((err: ApiClientError) => void) | null = null;

export function setUnauthorizedHandler(handler: ((err: ApiClientError) => void) | null): void {
  unauthorizedHandler = handler;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const method = (init.method || "GET").toUpperCase();
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if ((method === "POST" || method === "PUT" || method === "PATCH" || method === "DELETE") && !headers.has("X-SMS-Hub-CSRF")) {
    headers.set("X-SMS-Hub-CSRF", "1");
  }

  const resp = await fetch(path, {
    ...init,
    headers,
    credentials: "same-origin",
  });
  const text = await resp.text();
  let data: Record<string, unknown> = {};
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { detail: text };
    }
  }

  if (!resp.ok) {
    const apiErr = parseApiErrorResponse(resp.status, data);
    if (
      resp.status === 401 &&
      unauthorizedHandler &&
      !path.startsWith("/api/v1/auth/login") &&
      !path.startsWith("/api/v1/auth/status") &&
      !path.startsWith("/api/v1/auth/session") &&
      !path.startsWith("/api/v1/auth/logout")
    ) {
      unauthorizedHandler(apiErr);
    }
    throw apiErr;
  }
  return data as T;
}

export const api = {
  getAuthStatus: () => request<AuthStatusResponse>("/api/v1/auth/status"),
  bootstrapLocalSession: () => request<{ ok: boolean; is_admin: boolean }>("/api/v1/auth/session", { method: "POST" }),
  adminLogin: (password: string) =>
    request<{ ok: boolean; is_admin: boolean }>("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ password }),
    }),
  adminLogout: () => request<{ ok: boolean }>("/api/v1/auth/logout", { method: "POST" }),

  getHealth: async (): Promise<HealthResponse> => {
    const resp = await fetch("/api/v1/health", { credentials: "same-origin" });
    const text = await resp.text();
    let data: Record<string, unknown> = {};
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = { detail: text };
      }
    }
    if (typeof data.ok === "boolean" && typeof data.db === "boolean") {
      return data as unknown as HealthResponse;
    }
    if (!resp.ok) {
      throw parseApiErrorResponse(resp.status, data);
    }
    return data as unknown as HealthResponse;
  },

  getProviders: () =>
    request<{ default_provider: string; active: string[]; catalog: ProviderDescriptor[] }>(
      "/api/v1/providers",
    ),

  getBalance: (provider = "smsbower") =>
    request<{ provider: string; balance: number; currency: string }>(
      `/api/v1/providers/${encodeURIComponent(provider)}/balance`,
    ),

  getServices: (provider = "smsbower") =>
    request<{ provider: string; supports_custom_service: boolean; services: ServiceOption[] }>(
      `/api/v1/providers/${encodeURIComponent(provider)}/services`,
    ),

  getCountries: (provider = "smsbower", force = false) =>
    request<{ provider: string; count: number; countries: CountryOption[] }>(
      `/api/v1/providers/${encodeURIComponent(provider)}/countries${force ? "?force=true" : ""}`,
    ),

  refreshCountries: (provider = "smsbower") =>
    request<{ provider: string; count: number }>(
      `/api/v1/providers/${encodeURIComponent(provider)}/countries/refresh`,
      { method: "POST" },
    ),

  getPrices: (country: string, service: string, provider = "smsbower", force = false, signal?: AbortSignal) =>
    request<PriceSnapshot>(
      `/api/v1/providers/${encodeURIComponent(provider)}/prices?country=${encodeURIComponent(country)}&service=${encodeURIComponent(service)}${force ? "&force=true" : ""}`,
      { signal },
    ),

  createOrder: (payload: CreateOrderPayload, idempotencyKey?: string) =>
    request<SmsOrder>("/api/v1/orders", {
      method: "POST",
      headers: idempotencyKey ? { "Idempotency-Key": idempotencyKey } : undefined,
      body: JSON.stringify(payload),
    }),

  listOrders: (
    params: {
      status?: string;
      active_only?: boolean;
      project_id?: string;
      service?: string;
      search?: string;
      page?: number;
      page_size?: number;
    } = {},
    signal?: AbortSignal,
  ) => {
    const qs = new URLSearchParams();
    if (params.status) qs.set("status", params.status);
    if (params.active_only) qs.set("active_only", "true");
    if (params.project_id) qs.set("project_id", params.project_id);
    if (params.service) qs.set("service", params.service);
    if (params.search) qs.set("search", params.search);
    if (params.page) qs.set("page", String(params.page));
    if (params.page_size) qs.set("page_size", String(params.page_size));
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<OrderListResponse>(`/api/v1/orders${suffix}`, { signal });
  },

  getOrder: (orderId: string) => request<SmsOrder>(`/api/v1/orders/${encodeURIComponent(orderId)}`),

  resendSms: (orderId: string) =>
    request<SmsOrder>(`/api/v1/orders/${encodeURIComponent(orderId)}/resend`, { method: "POST" }),

  completeOrder: (orderId: string) =>
    request<SmsOrder>(`/api/v1/orders/${encodeURIComponent(orderId)}/complete`, { method: "POST" }),

  cancelOrder: (orderId: string, options: { blacklist?: boolean; reason?: string } = {}) =>
    request<SmsOrder>(`/api/v1/orders/${encodeURIComponent(orderId)}/cancel`, {
      method: "POST",
      body: JSON.stringify(options),
    }),

  scrubOrderCode: (orderId: string) =>
    request<{ success: boolean }>(`/api/v1/orders/${encodeURIComponent(orderId)}/code`, {
      method: "DELETE",
    }),

  getStats: (days = 7) => {
    const offset = new Date().getTimezoneOffset();
    return request<StatsResponse>(
      `/api/v1/stats?timezone_offset_minutes=${encodeURIComponent(offset)}&days=${days}`,
    );
  },

  listBlacklist: (provider = "", limit = 1000) => {
    const qs = new URLSearchParams({ limit: String(limit) });
    if (provider) qs.set("provider", provider);
    return request<{ total: number; entries: BlacklistEntry[] }>(
      `/api/v1/blacklist?${qs.toString()}`,
    );
  },

  addBlacklist: (phone: string, reason: string, provider = "smsbower") =>
    request<{ success: boolean; added?: number }>("/api/v1/blacklist", {
      method: "POST",
      body: JSON.stringify({ phone, reason, provider }),
    }),

  addBlacklistBatch: (phones: string[], reason: string, provider = "smsbower") =>
    request<{ success: boolean; added: number }>("/api/v1/blacklist/batch", {
      method: "POST",
      body: JSON.stringify({ phones, reason, provider }),
    }),

  removeBlacklist: (phone: string, provider = "smsbower", projectId?: string) => {
    const qs = new URLSearchParams({ phone, provider });
    if (projectId !== undefined) {
      qs.set("project_id", projectId);
    }
    return request<{ success: boolean; deleted?: number }>(`/api/v1/blacklist?${qs.toString()}`, {
      method: "DELETE",
    });
  },

  listProjectKeys: () =>
    request<{ api_auth_required: boolean; items: ProjectApiKey[] }>("/api/v1/projects/keys"),

  createProjectKey: (payload: {
    name: string;
    project_id?: string;
    scopes?: string[];
    default_webhook_url?: string;
    webhook_secret?: string;
  }) =>
    request<ProjectApiKey & { api_key: string; notice: string }>("/api/v1/projects/keys", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  updateProjectKey: (
    projectId: string,
    payload: {
      name?: string;
      enabled?: boolean;
      scopes?: string[];
      default_webhook_url?: string;
      webhook_secret?: string;
    },
  ) =>
    request<ProjectApiKey>(`/api/v1/projects/keys/${encodeURIComponent(projectId)}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),

  rotateProjectKey: (projectId: string) =>
    request<ProjectApiKey & { api_key: string; notice: string }>(
      `/api/v1/projects/keys/${encodeURIComponent(projectId)}/rotate`,
      { method: "POST" },
    ),

  deleteProjectKey: (projectId: string) =>
    request<{ success: boolean }>(`/api/v1/projects/keys/${encodeURIComponent(projectId)}`, {
      method: "DELETE",
    }),

  listWebhookDeliveries: (params: { project_id?: string; status?: string; limit?: number } = {}) => {
    const qs = new URLSearchParams();
    if (params.project_id) qs.set("project_id", params.project_id);
    if (params.status) qs.set("status", params.status);
    if (params.limit) qs.set("limit", String(params.limit));
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<{ total: number; items: WebhookDeliveryItem[] }>(
      `/api/v1/projects/webhooks/deliveries${suffix}`,
    );
  },

  getReconciliationPending: () =>
    request<ReconciliationResponse>("/api/v1/orders/reconciliation/pending"),

  resolveReconciliation: (
    activationRowId: number,
    payload: {
      state: "refund_confirmed" | "unrefundable" | "completed" | "manual_resolved";
      note?: string;
      actual_cost?: number;
      refund_amount?: number;
      currency?: string;
    },
  ) =>
    request<ReconciliationActivationItem>(
      `/api/v1/orders/reconciliation/${encodeURIComponent(String(activationRowId))}/resolve`,
      { method: "POST", body: JSON.stringify(payload) },
    ),

  testWebhook: (payload: { url: string; secret?: string; project_id?: string }) =>
    request<{ ok: boolean; status_code: number; error?: string; attempts: number }>(
      "/api/v1/projects/webhook-test",
      { method: "POST", body: JSON.stringify(payload) },
    ),

  getSettings: () => request<SettingsResponse>("/api/v1/settings"),

  updateSettings: (payload: Record<string, unknown>) =>
    request<{ success: boolean }>("/api/v1/settings", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),

  scrubAllHistoricalCodes: () =>
    request<{ success: boolean; scrubbed_orders: number }>("/api/v1/settings/scrub-codes", {
      method: "POST",
    }),

  getRecentLogs: (lines = 200, level = "") =>
    request<LogsResponse>(
      `/api/v1/logs/recent?lines=${lines}${level ? `&level=${encodeURIComponent(level)}` : ""}`,
    ),
};
