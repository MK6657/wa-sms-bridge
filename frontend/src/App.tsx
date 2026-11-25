import { useCallback, useEffect, useRef, useState } from "react";
import {
  Ban,
  BookOpen,
  ClipboardList,
  Code2,
  Lock,
  LogOut,
  Menu,
  Radio,
  Settings,
  Smartphone,
  X,
} from "lucide-react";
import { api, setUnauthorizedHandler } from "./api/client";
import type { AuthStatusResponse, HealthResponse, SmsOrder, StatsResponse } from "./api/types";
import { useOrderStream, type StreamEventMessage } from "./api/useOrderStream";
import { ToastProvider, useToast } from "./components/ToastProvider";
import { BlacklistPage } from "./features/blacklist/BlacklistPage";
import { IntegrationsPage } from "./features/integrations/IntegrationsPage";
import { OrdersPage } from "./features/orders/OrdersPage";
import { SettingsPage } from "./features/settings/SettingsPage";
import { WorkbenchPage } from "./features/workbench/WorkbenchPage";
import { ACTIVE_STATUSES, formatMoney, playCodeChime } from "./utils/format";

type PageKey = "workbench" | "orders" | "integrations" | "blacklist" | "settings";

const NAV_ITEMS: Array<{ key: PageKey; label: string; subtitle: string; icon: typeof Smartphone }> = [
  { key: "workbench", label: "接码工作台", subtitle: "实时取号、接收与复制验证码", icon: Smartphone },
  { key: "orders", label: "订单与统计", subtitle: "历史接码台账、分项目与业务统计", icon: ClipboardList },
  { key: "integrations", label: "API 接入中心", subtitle: "签发外部项目密钥、Webhook 与接入示例", icon: Code2 },
  { key: "blacklist", label: "号码黑名单", subtitle: "自动跳过并退款废号", icon: Ban },
  { key: "settings", label: "系统配置", subtitle: "SMSBower 平台密钥、留存策略与诊断日志", icon: Settings },
];

function AppShell() {
  const { notify } = useToast();
  const [page, setPage] = useState<PageKey>("workbench");
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);

  // 管理员会话认证状态（修复 P0-01 / P1-08）
  const [authReady, setAuthReady] = useState(false);
  const [authStatus, setAuthStatus] = useState<AuthStatusResponse | null>(null);
  const [isAdminSession, setIsAdminSession] = useState(false);
  const [loginPassword, setLoginPassword] = useState("");
  const [loggingIn, setLoggingIn] = useState(false);
  const [loginError, setLoginError] = useState("");

  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [balance, setBalance] = useState<number | null>(null);
  const [balanceError, setBalanceError] = useState("");
  const [balanceLoading, setBalanceLoading] = useState(false);
  const [stats, setStats] = useState<StatsResponse | null>(null);
  const [activeOrders, setActiveOrders] = useState<SmsOrder[]>([]);
  const [maskInHistory, setMaskInHistory] = useState(true);
  const [soundEnabled, setSoundEnabled] = useState(() => {
    try {
      return localStorage.getItem("sms-hub.sound") !== "off";
    } catch {
      return true;
    }
  });

  // 初始化会话：若已设口令则要求登录；若本机直连未设口令则自动向服务端申请 HttpOnly Cookie（覆盖严格模式与 SSE）
  const initSession = useCallback(async () => {
    try {
      const status = await api.getAuthStatus();
      setAuthStatus(status);
      if (status.is_admin && status.authenticated) {
        setIsAdminSession(true);
      } else if (status.can_bootstrap_local_session) {
        try {
          await api.bootstrapLocalSession();
          setIsAdminSession(true);
        } catch {
          setIsAdminSession(status.is_admin);
        }
      } else {
        setIsAdminSession(false);
      }
    } catch {
      setIsAdminSession(false);
    } finally {
      setAuthReady(true);
    }
  }, []);

  useEffect(() => {
    initSession();
  }, [initSession]);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      setIsAdminSession(false);
      initSession();
    });
    return () => {
      setUnauthorizedHandler(null);
    };
  }, [initSession]);

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoginError("");
    setLoggingIn(true);
    try {
      await api.adminLogin(loginPassword);
      setLoginPassword("");
      setIsAdminSession(true);
      notify("管理员登录成功", "success");
    } catch (err) {
      setLoginError(err instanceof Error ? err.message : "登录失败");
    } finally {
      setLoggingIn(false);
    }
  };

  const handleLogout = async () => {
    try {
      await api.adminLogout();
    } catch {
      // ignore
    }
    setIsAdminSession(false);
    await initSession();
  };

  const loadHealth = useCallback(async () => {
    try {
      const h = await api.getHealth();
      setHealth(h);
      return h;
    } catch {
      return null;
    }
  }, []);

  const loadBalance = useCallback(async (providerName?: string) => {
    const prov = providerName || health?.default_provider || "smsbower";
    setBalanceLoading(true);
    setBalanceError("");
    try {
      const res = await api.getBalance(prov);
      setBalance(res.balance);
    } catch (err) {
      setBalance(null);
      setBalanceError(err instanceof Error ? err.message : "余额获取失败");
    } finally {
      setBalanceLoading(false);
    }
  }, [health?.default_provider]);

  const activeOrdersSeqRef = useRef(0);
  const loadActiveOrders = useCallback(async () => {
    const seq = ++activeOrdersSeqRef.current;
    try {
      const res = await api.listOrders({ active_only: true, page: 1, page_size: 50 });
      if (seq === activeOrdersSeqRef.current) {
        setActiveOrders(res.items);
      }
    } catch {
      // ignore initial error
    }
  }, []);

  const loadStats = useCallback(async () => {
    try {
      const s = await api.getStats(7);
      setStats(s);
    } catch {
      // ignore
    }
  }, []);

  const loadHistoryMaskPref = useCallback(async () => {
    try {
      const cfg = await api.getSettings();
      setMaskInHistory(cfg.sms_code_mask_in_history);
    } catch {
      // ignore
    }
  }, []);

  const refreshCoreData = useCallback(async () => {
    if (!isAdminSession) return;
    const h = await loadHealth();
    const prov = h?.default_provider || "smsbower";
    await Promise.allSettled([loadActiveOrders(), loadStats(), loadHistoryMaskPref()]);
    if (h?.provider_configured?.[prov] ?? h?.provider_configured?.smsbower) {
      await loadBalance(prov);
    } else {
      setBalance(null);
      setBalanceError("待配置 API Key");
    }
  }, [isAdminSession, loadHealth, loadActiveOrders, loadStats, loadHistoryMaskPref, loadBalance]);

  useEffect(() => {
    if (isAdminSession) {
      refreshCoreData();
    }
  }, [isAdminSession, refreshCoreData]);

  const upsertOrder = useCallback((order: SmsOrder) => {
    setActiveOrders((prev) => {
      const exists = prev.some((o) => o.order_id === order.order_id);
      if (!ACTIVE_STATUSES.has(order.status)) {
        return prev.filter((o) => o.order_id !== order.order_id);
      }
      if (exists) {
        return prev.map((o) => (o.order_id === order.order_id ? order : o));
      }
      return [order, ...prev];
    });
  }, []);

  const handleStreamEvent = useCallback(
    (evt: StreamEventMessage) => {
      if (!evt?.data?.order_id) return;
      upsertOrder(evt.data);
      if (evt.event === "order.code_received") {
        if (!evt.data.code_scrubbed && evt.data.latest_code) {
          if (soundEnabled) playCodeChime();
          notify(`收到验证码 ${evt.data.latest_code}（${evt.data.phone_e164 || evt.data.phone_raw}）`, "success");
        }
        loadStats();
      } else if (evt.event === "order.expired") {
        notify(`订单 #${evt.data.order_id.slice(-6)} 等码超时已处理`, "warn");
        loadStats();
        loadBalance();
      } else if (
        evt.event === "order.acquired" ||
        evt.event === "order.completed" ||
        evt.event === "order.cancelled" ||
        evt.event === "order.failed"
      ) {
        loadStats();
        loadBalance();
      }
    },
    [upsertOrder, soundEnabled, notify, loadStats, loadBalance],
  );

  // P1-08 & 复核 P1-05: 会话就绪后建立 SSE，自动携带 last_event_id 续传并在游标过期或重连时刷新快照
  const handleStreamResync = useCallback(() => {
    loadActiveOrders();
    loadStats();
    loadBalance();
  }, [loadActiveOrders, loadStats, loadBalance]);

  const { connected: sseConnected } = useOrderStream(handleStreamEvent, {
    enabled: isAdminSession,
    onReconnect: handleStreamResync,
    onResyncRequired: handleStreamResync,
    onAuthCheck: () => {
      initSession();
    },
  });

  const toggleSound = () => {
    setSoundEnabled((prev) => {
      const next = !prev;
      try {
        localStorage.setItem("sms-hub.sound", next ? "on" : "off");
      } catch {
        // ignore
      }
      return next;
    });
  };

  if (!authReady) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-50 text-sm text-slate-500">
        正在验证控制台会话...
      </div>
    );
  }

  if (!isAdminSession) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-50 p-4">
        <div className="w-full max-w-sm rounded-2xl border border-slate-200 bg-white p-6 shadow-lg">
          <div className="mb-5 flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-emerald-600 text-white">
              <Lock className="h-5 w-5" />
            </div>
            <div>
              <h1 className="text-base font-bold text-slate-900">SMS Hub 管理员登录</h1>
              <p className="text-xs text-slate-500">
                {authStatus?.password_required
                  ? "请输入 CONSOLE_ADMIN_PASSWORD 登录工作台"
                  : "检测到非本机直连访问，请先配置 CONSOLE_ADMIN_PASSWORD"}
              </p>
            </div>
          </div>

          {authStatus?.password_required ? (
            <form onSubmit={handleLogin} className="space-y-3.5">
              <div>
                <label className="mb-1 block text-xs font-medium text-slate-700">管理员口令</label>
                <input
                  type="password"
                  required
                  autoFocus
                  value={loginPassword}
                  onChange={(e) => setLoginPassword(e.target.value)}
                  placeholder="输入管理员口令"
                  className="w-full rounded-xl border border-slate-200 px-3.5 py-2.5 text-sm outline-none focus:border-emerald-500 focus:ring-2 focus:ring-emerald-500/15"
                />
              </div>
              {loginError ? (
                <div className="rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
                  {loginError}
                </div>
              ) : null}
              <button
                type="submit"
                disabled={loggingIn}
                className="w-full cursor-pointer rounded-xl bg-emerald-600 py-2.5 text-sm font-semibold text-white transition hover:bg-emerald-700 disabled:opacity-50"
              >
                {loggingIn ? "验证中..." : "登录控制台"}
              </button>
            </form>
          ) : (
            <div className="rounded-xl border border-amber-200 bg-amber-50 p-3.5 text-xs leading-relaxed text-amber-900">
              当前请求来自非回环地址或经过反向代理，出于安全策略已禁止免密管理访问。请在服务端环境变量中设置{" "}
              <code className="font-mono-code rounded bg-amber-100 px-1 py-0.5">CONSOLE_ADMIN_PASSWORD</code>{" "}
              后重启再登录。
            </div>
          )}
        </div>
      </div>
    );
  }

  const currentNav = NAV_ITEMS.find((n) => n.key === page) || NAV_ITEMS[0];
  const defaultProviderLabel = (health?.default_provider || "smsbower").toUpperCase();

  const renderSidebarContent = () => (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-3 border-b border-slate-800/80 px-4 py-4">
        <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-emerald-500 font-mono-code text-sm font-black text-slate-950 shadow-sm">
          SMS
        </div>
        <div className="min-w-0">
          <div className="truncate text-sm font-bold text-white">SMS Hub</div>
          <div className="truncate text-[11px] text-slate-400">独立接码中心 · v1.0</div>
        </div>
      </div>

      <nav className="flex-1 space-y-1 p-3">
        {NAV_ITEMS.map((item) => {
          const Icon = item.icon;
          const active = page === item.key;
          return (
            <button
              key={item.key}
              type="button"
              onClick={() => {
                setPage(item.key);
                setMobileMenuOpen(false);
              }}
              className={`flex w-full cursor-pointer items-center justify-between rounded-xl px-3 py-2.5 text-left text-sm font-medium transition ${
                active
                  ? "bg-emerald-500/15 text-emerald-300 ring-1 ring-emerald-500/30"
                  : "text-slate-300 hover:bg-slate-800/70 hover:text-white"
              }`}
            >
              <span className="flex items-center gap-2.5">
                <Icon className="h-4 w-4" />
                <span>{item.label}</span>
              </span>
              {item.key === "workbench" && activeOrders.length > 0 ? (
                <span className="rounded-full bg-emerald-500 px-1.5 py-0.5 font-mono-code text-[10px] font-bold text-slate-950">
                  {activeOrders.length}
                </span>
              ) : null}
            </button>
          );
        })}
      </nav>

      <div className="space-y-2.5 border-t border-slate-800/80 p-3.5 text-xs">
        <div className="rounded-xl bg-slate-800/60 p-3">
          <div className="flex items-center justify-between text-slate-400">
            <span>默认平台</span>
            <span className="font-semibold text-emerald-400">{defaultProviderLabel}</span>
          </div>
          <div className="mt-1.5 flex items-center justify-between text-slate-400">
            <span>余额</span>
            <span className="font-mono-code font-semibold text-white">
              {balance !== null ? formatMoney(balance) : "-"}
            </span>
          </div>
          <div className="mt-1.5 flex items-center justify-between text-slate-400">
            <span>实时通道</span>
            <span className={`inline-flex items-center gap-1 font-medium ${sseConnected ? "text-emerald-400" : "text-amber-400"}`}>
              <span className={`h-1.5 w-1.5 rounded-full ${sseConnected ? "bg-emerald-400" : "bg-amber-400 animate-pulse"}`} />
              {sseConnected ? "SSE 在线" : "重连中"}
            </span>
          </div>
        </div>

        <a
          href="/docs"
          target="_blank"
          rel="noreferrer"
          className="flex items-center justify-center gap-1.5 rounded-xl border border-slate-700/80 px-3 py-2 text-xs font-medium text-slate-300 transition hover:bg-slate-800 hover:text-white"
        >
          <BookOpen className="h-3.5 w-3.5" />
          <span>OpenAPI 接口文档</span>
        </a>

        {authStatus?.password_required ? (
          <button
            type="button"
            onClick={handleLogout}
            className="flex w-full cursor-pointer items-center justify-center gap-1.5 rounded-xl border border-slate-700/80 px-3 py-2 text-xs font-medium text-slate-400 transition hover:bg-slate-800 hover:text-white"
          >
            <LogOut className="h-3.5 w-3.5" />
            <span>退出管理员登录</span>
          </button>
        ) : null}
      </div>
    </div>
  );

  return (
    <div className="flex min-h-screen bg-slate-50 text-slate-900">
      <aside className="sticky top-0 hidden h-screen w-60 shrink-0 bg-slate-900 lg:block">
        {renderSidebarContent()}
      </aside>

      {mobileMenuOpen ? (
        <div className="fixed inset-0 z-50 flex lg:hidden">
          <div className="w-64 bg-slate-900 shadow-2xl">{renderSidebarContent()}</div>
          <button
            type="button"
            className="flex-1 bg-slate-950/50 backdrop-blur-xs"
            onClick={() => setMobileMenuOpen(false)}
            aria-label="关闭菜单"
          />
        </div>
      ) : null}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex items-center justify-between gap-3 border-b border-slate-200 bg-white/90 px-4 py-3.5 backdrop-blur-md lg:px-7">
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => setMobileMenuOpen((v) => !v)}
              className="rounded-lg border border-slate-200 p-1.5 text-slate-600 hover:bg-slate-50 lg:hidden"
              aria-label="菜单"
            >
              {mobileMenuOpen ? <X className="h-5 w-5" /> : <Menu className="h-5 w-5" />}
            </button>
            <div>
              <h1 className="text-lg font-bold tracking-tight text-slate-900">{currentNav.label}</h1>
              <p className="text-xs text-slate-500">{currentNav.subtitle}</p>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <span
              className={`hidden items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium sm:inline-flex ${
                sseConnected
                  ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                  : "border-amber-200 bg-amber-50 text-amber-700"
              }`}
            >
              <Radio className="h-3.5 w-3.5" />
              {sseConnected ? "实时推码已连接" : "SSE 重连中"}
            </span>
          </div>
        </header>

        <main className="mx-auto w-full max-w-7xl flex-1 p-4 lg:p-7">
          <div className={page === "workbench" ? "block" : "hidden"}>
            <WorkbenchPage
              health={health}
              balance={balance}
              balanceError={balanceError}
              balanceLoading={balanceLoading}
              onRefreshBalance={() => loadBalance()}
              stats={stats}
              activeOrders={activeOrders}
              onOrderMutated={upsertOrder}
              onRefreshAll={refreshCoreData}
              soundEnabled={soundEnabled}
              onToggleSound={toggleSound}
              onGoSettings={() => setPage("settings")}
            />
          </div>

          {page === "orders" ? (
            <OrdersPage stats={stats} maskInHistory={maskInHistory} onOrdersChanged={refreshCoreData} />
          ) : null}
          <div className={page === "integrations" ? "block" : "hidden"}>
            <IntegrationsPage />
          </div>
          {page === "blacklist" ? <BlacklistPage defaultProvider={health?.default_provider || "smsbower"} /> : null}
          {page === "settings" ? <SettingsPage onSettingsUpdated={refreshCoreData} /> : null}
        </main>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <ToastProvider>
      <AppShell />
    </ToastProvider>
  );
}
