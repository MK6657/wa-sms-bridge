import { useCallback, useEffect, useRef, useState } from "react";
import {
  Ban,
  BookOpen,
  ChevronLeft,
  ChevronRight,
  ClipboardList,
  Code2,
  Eye,
  EyeOff,
  Lock,
  LogOut,
  PanelLeftClose,
  PanelLeftOpen,
  Radio,
  RefreshCw,
  Settings,
  Smartphone,
  Wallet,
} from "lucide-react";
import { ApiClientError, api, setUnauthorizedHandler } from "./api/client";
import {
  mergeActiveOrderSnapshot,
  recordOrderState,
  shouldLogoutOnSilentAuthCheck,
  type TrackedOrderVersion,
} from "./api/orderSync";
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

const SIDEBAR_STORAGE_KEY = "sms-hub.sidebar.collapsed.v1";

const NAV_ITEMS: Array<{ key: PageKey; label: string; subtitle: string; icon: typeof Smartphone }> = [
  { key: "workbench", label: "接码工作台", subtitle: "实时取号、接收与一键复制验证码", icon: Smartphone },
  { key: "orders", label: "订单与统计", subtitle: "历史接码台账、分项目与多维统计", icon: ClipboardList },
  { key: "integrations", label: "API 接入中心", subtitle: "外部项目密钥、Webhook 投递与代码示例", icon: Code2 },
  { key: "blacklist", label: "号码黑名单", subtitle: "自动跳过并退款历史废号", icon: Ban },
  { key: "settings", label: "系统配置", subtitle: "接码平台密钥、留存脱敏策略与诊断日志", icon: Settings },
];

function AppShell() {
  const { notify } = useToast();
  const [page, setPage] = useState<PageKey>("workbench");

  // 常驻可折叠侧边栏状态（支持随时展开/收起并记忆偏好）
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(() => {
    try {
      const saved = localStorage.getItem(SIDEBAR_STORAGE_KEY);
      if (saved === "1" || saved === "0") return saved === "1";
    } catch {
      // ignore
    }
    return typeof window !== "undefined" ? window.innerWidth < 960 : false;
  });

  const toggleSidebar = () => {
    setSidebarCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(SIDEBAR_STORAGE_KEY, next ? "1" : "0");
      } catch {
        // ignore
      }
      return next;
    });
  };

  // 管理员会话认证状态
  const [authReady, setAuthReady] = useState(false);
  const [authStatus, setAuthStatus] = useState<AuthStatusResponse | null>(null);
  const [authCheckError, setAuthCheckError] = useState("");
  const [isAdminSession, setIsAdminSession] = useState(false);
  const [loginPassword, setLoginPassword] = useState("");
  const [showLoginPwd, setShowLoginPwd] = useState(false);
  const [loggingIn, setLoggingIn] = useState(false);
  const [loginError, setLoginError] = useState("");

  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [balance, setBalance] = useState<number | null>(null);
  const [balanceCurrency, setBalanceCurrency] = useState("USD");
  const [balanceError, setBalanceError] = useState("");
  const [balanceLoading, setBalanceLoading] = useState(false);
  const [stats, setStats] = useState<StatsResponse | null>(null);
  const [activeOrders, setActiveOrders] = useState<SmsOrder[]>([]);
  const [maskInHistory, setMaskInHistory] = useState(true);
  const initInFlightRef = useRef<Promise<void> | null>(null);
  const latestOrderVersionRef = useRef<Map<string, TrackedOrderVersion>>(new Map());
  const localMutationCounterRef = useRef(0);
  const orderMutationSeqRef = useRef<Map<string, number>>(new Map());
  const lastNotifiedCodeCountRef = useRef<Map<string, number>>(new Map());
  const [soundEnabled, setSoundEnabled] = useState(() => {
    try {
      return localStorage.getItem("sms-hub.sound") !== "off";
    } catch {
      return true;
    }
  });

  const initSession = useCallback(async () => {
    if (initInFlightRef.current) {
      return initInFlightRef.current;
    }
    const task = (async () => {
      try {
        const status = await api.getAuthStatus();
        setAuthStatus(status);
        setAuthCheckError("");
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
      } catch (err) {
        setAuthCheckError(err instanceof Error ? err.message : "无法连接后端服务");
        setIsAdminSession(false);
      } finally {
        setAuthReady(true);
        initInFlightRef.current = null;
      }
    })();
    initInFlightRef.current = task;
    return task;
  }, []);

  const verifySessionSilently = useCallback(async () => {
    try {
      const status = await api.getAuthStatus();
      setAuthStatus(status);
      setAuthCheckError("");
      if (status.can_bootstrap_local_session && !status.authenticated) {
        try {
          await api.bootstrapLocalSession();
          setIsAdminSession(true);
          return;
        } catch {
          // ignore transient bootstrap failure
        }
      }
      if (shouldLogoutOnSilentAuthCheck({ ok: true, status })) {
        setIsAdminSession(false);
      }
    } catch (err) {
      const httpStatus = err instanceof ApiClientError ? err.status : undefined;
      if (shouldLogoutOnSilentAuthCheck({ ok: false, httpStatus })) {
        setIsAdminSession(false);
      }
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

  const loadBalance = useCallback(
    async (providerName?: string) => {
      const prov = providerName || health?.default_provider || "smsbower";
      setBalanceLoading(true);
      setBalanceError("");
      try {
        const res = await api.getBalance(prov);
        setBalance(res.balance);
        setBalanceCurrency(res.currency || "USD");
      } catch (err) {
        setBalance(null);
        setBalanceError(err instanceof Error ? err.message : "余额获取失败");
      } finally {
        setBalanceLoading(false);
      }
    },
    [health?.default_provider],
  );

  const recordOrderVersion = useCallback((order: SmsOrder): boolean => {
    return recordOrderState(order, latestOrderVersionRef.current);
  }, []);

  const activeOrdersSeqRef = useRef(0);
  const loadActiveOrders = useCallback(async () => {
    const seq = ++activeOrdersSeqRef.current;
    const snapshotStartMutSeq = localMutationCounterRef.current;
    try {
      const res = await api.listOrders({ active_only: true, page: 1, page_size: 200 });
      if (seq === activeOrdersSeqRef.current) {
        setActiveOrders((prev) =>
          mergeActiveOrderSnapshot(
            prev,
            res.items,
            snapshotStartMutSeq,
            orderMutationSeqRef.current,
            latestOrderVersionRef.current,
          ),
        );
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
    if (!h?.db && h !== null) {
      setBalance(null);
      setBalanceError("数据库暂不可用");
    } else if (h?.provider_configured?.[prov] ?? h?.provider_configured?.smsbower) {
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

  const upsertOrder = useCallback(
    (order: SmsOrder) => {
      if (!recordOrderVersion(order)) {
        return;
      }
      const nextMutSeq = ++localMutationCounterRef.current;
      if (orderMutationSeqRef.current.size >= 1000 && !orderMutationSeqRef.current.has(order.order_id)) {
        const oldestKey = orderMutationSeqRef.current.keys().next().value;
        if (oldestKey !== undefined) orderMutationSeqRef.current.delete(oldestKey);
      }
      orderMutationSeqRef.current.set(order.order_id, nextMutSeq);
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
    },
    [recordOrderVersion],
  );

  const handleStreamEvent = useCallback(
    (evt: StreamEventMessage) => {
      if (!evt?.data?.order_id) return;
      upsertOrder(evt.data);
      if (evt.event === "order.code_received") {
        const codeSeq = evt.data.code_count || 1;
        const prevNotified = lastNotifiedCodeCountRef.current.get(evt.data.order_id) || 0;
        if (!evt.data.code_scrubbed && evt.data.latest_code && codeSeq > prevNotified) {
          if (lastNotifiedCodeCountRef.current.size >= 500) {
            const first = lastNotifiedCodeCountRef.current.keys().next().value;
            if (first !== undefined) lastNotifiedCodeCountRef.current.delete(first);
          }
          lastNotifiedCodeCountRef.current.set(evt.data.order_id, codeSeq);
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
      verifySessionSilently();
    },
  });

  // 当 SSE 暂时断连且存在进行中订单时，自动启用 5 秒低频兜底轮询，保证验证码不因网络代理中断而漏显
  useEffect(() => {
    if (!isAdminSession || sseConnected || activeOrders.length === 0) return;
    const timer = window.setInterval(() => {
      loadActiveOrders();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [isAdminSession, sseConnected, activeOrders.length, loadActiveOrders]);

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
      <div className="flex min-h-screen items-center justify-center text-sm text-slate-500">
        正在验证控制台会话...
      </div>
    );
  }

  if (!isAdminSession) {
    return (
      <div className="flex min-h-screen items-center justify-center p-4">
        <div className="surface-card w-full max-w-sm rounded-2xl p-6">
          <div className="mb-5 flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br from-emerald-500 to-teal-600 text-white shadow-sm">
              <Lock className="h-5 w-5" />
            </div>
            <div>
              <h1 className="text-base font-bold text-slate-900">SMS Hub 管理员登录</h1>
              <p className="text-xs text-slate-500">
                {authCheckError
                  ? "后端服务暂不可达，请检查服务运行状态后重试"
                  : authStatus?.password_required
                    ? "请输入 CONSOLE_ADMIN_PASSWORD 登录工作台"
                    : "检测到非本机直连访问，请先配置 CONSOLE_ADMIN_PASSWORD"}
              </p>
            </div>
          </div>

          {authCheckError && !authStatus ? (
            <div className="space-y-3">
              <div className="rounded-xl border border-rose-200 bg-rose-50 p-3.5 text-xs leading-relaxed text-rose-800">
                无法校验控制台会话状态：{authCheckError}
              </div>
              <button
                type="button"
                onClick={() => initSession()}
                className="w-full cursor-pointer rounded-xl bg-slate-900 py-2.5 text-sm font-semibold text-white shadow-xs transition hover:bg-slate-800"
              >
                重试连接
              </button>
            </div>
          ) : authStatus?.password_required ? (
            <form onSubmit={handleLogin} className="space-y-3.5">
              <div>
                <label className="mb-1.5 block text-xs font-medium text-slate-700">管理员口令</label>
                <div className="relative">
                  <input
                    type={showLoginPwd ? "text" : "password"}
                    required
                    autoFocus
                    value={loginPassword}
                    onChange={(e) => setLoginPassword(e.target.value)}
                    placeholder="输入管理员口令"
                    className="input-surface w-full rounded-xl py-2.5 pl-3.5 pr-9 text-sm"
                  />
                  <button
                    type="button"
                    onClick={() => setShowLoginPwd((v) => !v)}
                    className="absolute right-2.5 top-1/2 -translate-y-1/2 cursor-pointer rounded p-1 text-slate-400 hover:text-slate-700"
                    title={showLoginPwd ? "隐藏口令" : "显示口令"}
                  >
                    {showLoginPwd ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                  </button>
                </div>
              </div>
              {loginError ? (
                <div className="rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
                  {loginError}
                </div>
              ) : null}
              <button
                type="submit"
                disabled={loggingIn}
                className="w-full cursor-pointer rounded-xl bg-emerald-600 py-2.5 text-sm font-semibold text-white shadow-xs transition hover:bg-emerald-700 disabled:opacity-50"
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
  const CurrentNavIcon = currentNav.icon;
  const defaultProviderLabel = (health?.default_provider || "smsbower").toUpperCase();
  const appVersion = health?.version ? `v${health.version}` : "v1.2.0";

  return (
    <div className="flex min-h-screen text-slate-900">
      {/* 常驻左侧可伸缩导航栏（全视口保留，附悬浮边缘小箭头随时展开/收起） */}
      <aside
        className={`relative sticky top-0 z-30 flex h-screen shrink-0 flex-col border-r border-slate-800/90 bg-gradient-to-b from-slate-900 via-slate-900 to-slate-950 text-slate-200 shadow-[4px_0_24px_-12px_rgba(15,23,42,0.45)] transition-[width] duration-200 ease-out ${
          sidebarCollapsed ? "w-[70px]" : "w-[236px]"
        }`}
      >
        {/* 侧边栏边缘伸缩控制小箭头 */}
        <button
          type="button"
          onClick={toggleSidebar}
          title={sidebarCollapsed ? "展开侧边导航栏" : "收起侧边导航栏"}
          aria-label={sidebarCollapsed ? "展开侧边导航栏" : "收起侧边导航栏"}
          className="absolute -right-3 top-6 z-40 flex h-6 w-6 cursor-pointer items-center justify-center rounded-full border border-slate-700/90 bg-slate-900 text-slate-300 shadow-md transition hover:scale-110 hover:border-emerald-500 hover:bg-slate-800 hover:text-emerald-300"
        >
          {sidebarCollapsed ? (
            <ChevronRight className="h-3.5 w-3.5" />
          ) : (
            <ChevronLeft className="h-3.5 w-3.5" />
          )}
        </button>

        {/* 品牌区 */}
        <div
          className={`flex h-16 shrink-0 items-center border-b border-slate-800/80 ${
            sidebarCollapsed ? "justify-center px-2" : "gap-3 px-4"
          }`}
        >
          <div
            onClick={() => setPage("workbench")}
            title="SMS Hub 独立接码中心"
            className="flex h-9 w-9 shrink-0 cursor-pointer items-center justify-center rounded-xl bg-gradient-to-br from-emerald-400 to-emerald-600 font-mono-code text-xs font-black tracking-tight text-slate-950 shadow-[0_2px_10px_rgba(16,185,129,0.35)]"
          >
            SMS
          </div>
          {!sidebarCollapsed ? (
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-1.5">
                <span className="truncate text-sm font-bold tracking-tight text-white">SMS Hub</span>
                <span className="font-mono-code rounded bg-slate-800 px-1.5 py-0.5 text-[10px] font-medium text-emerald-400 ring-1 ring-inset ring-slate-700">
                  {appVersion}
                </span>
              </div>
              <div className="truncate text-[11px] text-slate-400">独立接码与 API 中心</div>
            </div>
          ) : null}
        </div>

        {/* 导航菜单区 */}
        <nav className="flex-1 space-y-1.5 overflow-y-auto overflow-x-hidden px-2.5 py-3.5">
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon;
            const active = page === item.key;
            const showBadge = item.key === "workbench" && activeOrders.length > 0;
            return (
              <button
                key={item.key}
                type="button"
                title={sidebarCollapsed ? `${item.label} — ${item.subtitle}` : undefined}
                onClick={() => setPage(item.key)}
                className={`group relative flex w-full cursor-pointer items-center rounded-xl text-left text-sm font-medium transition ${
                  sidebarCollapsed ? "justify-center px-0 py-2.5" : "justify-between px-3 py-2.5"
                } ${
                  active
                    ? "bg-emerald-500/15 text-emerald-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.06)] ring-1 ring-inset ring-emerald-500/35"
                    : "text-slate-300/90 hover:bg-slate-800/75 hover:text-white"
                }`}
              >
                {active ? (
                  <span className="absolute left-0 top-1/2 h-5 w-1 -translate-y-1/2 rounded-r-full bg-emerald-400" />
                ) : null}
                <span className={`flex items-center ${sidebarCollapsed ? "justify-center" : "gap-2.5"}`}>
                  <Icon className={`h-4 w-4 shrink-0 ${active ? "text-emerald-400" : "text-slate-400 group-hover:text-slate-200"}`} />
                  {!sidebarCollapsed ? <span className="truncate">{item.label}</span> : null}
                </span>

                {showBadge ? (
                  sidebarCollapsed ? (
                    <span className="font-mono-code absolute right-1.5 top-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-emerald-500 px-1 text-[9px] font-bold text-slate-950 shadow-xs">
                      {activeOrders.length}
                    </span>
                  ) : (
                    <span className="font-mono-code rounded-full bg-emerald-500 px-1.5 py-0.5 text-[10px] font-bold text-slate-950">
                      {activeOrders.length}
                    </span>
                  )
                ) : null}
              </button>
            );
          })}
        </nav>

        {/* 底部平台状态与快捷入口 */}
        <div className="shrink-0 space-y-2 border-t border-slate-800/80 p-2.5 text-xs">
          {sidebarCollapsed ? (
            <div className="flex flex-col items-center gap-2 py-1">
              <div
                title={`${defaultProviderLabel} · 余额 ${balance !== null ? formatMoney(balance, balanceCurrency) : "-"} · ${sseConnected ? "SSE 在线" : "SSE 重连中"}`}
                className="flex h-9 w-9 items-center justify-center rounded-xl bg-slate-800/70 ring-1 ring-inset ring-slate-700/70"
              >
                <span
                  className={`h-2.5 w-2.5 rounded-full ${
                    sseConnected
                      ? "bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.7)]"
                      : "bg-amber-400 animate-pulse"
                  }`}
                />
              </div>
              <a
                href="/docs"
                target="_blank"
                rel="noreferrer"
                title="OpenAPI 接口文档 (/docs)"
                className="flex h-9 w-9 items-center justify-center rounded-xl border border-slate-800 text-slate-400 transition hover:bg-slate-800 hover:text-white"
              >
                <BookOpen className="h-4 w-4" />
              </a>
              {authStatus?.password_required ? (
                <button
                  type="button"
                  onClick={handleLogout}
                  title="退出管理员登录"
                  className="flex h-9 w-9 cursor-pointer items-center justify-center rounded-xl border border-slate-800 text-slate-400 transition hover:bg-rose-500/15 hover:text-rose-300"
                >
                  <LogOut className="h-4 w-4" />
                </button>
              ) : null}
            </div>
          ) : (
            <>
              <div className="rounded-xl bg-slate-800/65 p-3 ring-1 ring-inset ring-slate-700/60">
                <div className="flex items-center justify-between text-slate-400">
                  <span>主活平台</span>
                  <span className="font-mono-code font-semibold text-emerald-400">{defaultProviderLabel}</span>
                </div>
                <div className="mt-1.5 flex items-center justify-between text-slate-400">
                  <span>可用余额</span>
                  <span className="font-mono-code font-semibold text-white">
                    {balance !== null ? formatMoney(balance, balanceCurrency) : "-"}
                  </span>
                </div>
                <div className="mt-1.5 flex items-center justify-between text-slate-400">
                  <span>推码通道</span>
                  <span className={`inline-flex items-center gap-1.5 font-medium ${sseConnected ? "text-emerald-400" : "text-amber-400"}`}>
                    <span className={`h-1.5 w-1.5 rounded-full ${sseConnected ? "bg-emerald-400" : "bg-amber-400 animate-pulse"}`} />
                    {sseConnected ? "SSE 在线" : "重连中"}
                  </span>
                </div>
              </div>

              <a
                href="/docs"
                target="_blank"
                rel="noreferrer"
                className="flex items-center justify-center gap-1.5 rounded-xl border border-slate-700/80 bg-slate-900/60 px-3 py-2 text-xs font-medium text-slate-300 transition hover:border-slate-600 hover:bg-slate-800 hover:text-white"
              >
                <BookOpen className="h-3.5 w-3.5" />
                <span>OpenAPI 接口文档</span>
              </a>

              {authStatus?.password_required ? (
                <button
                  type="button"
                  onClick={handleLogout}
                  className="flex w-full cursor-pointer items-center justify-center gap-1.5 rounded-xl border border-slate-800 px-3 py-2 text-xs font-medium text-slate-400 transition hover:border-rose-500/30 hover:bg-rose-500/10 hover:text-rose-300"
                >
                  <LogOut className="h-3.5 w-3.5" />
                  <span>退出管理员登录</span>
                </button>
              ) : null}
            </>
          )}
        </div>
      </aside>

      {/* 右侧主视图区域 */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex h-16 items-center justify-between gap-3 border-b border-slate-200/85 bg-white/85 px-4 shadow-[0_1px_2px_rgba(15,23,42,0.03)] backdrop-blur-md sm:px-6 lg:px-7">
          <div className="flex min-w-0 items-center gap-3">
            <button
              type="button"
              onClick={toggleSidebar}
              title={sidebarCollapsed ? "展开侧边栏" : "收起侧边栏"}
              className="flex h-8 w-8 shrink-0 cursor-pointer items-center justify-center rounded-lg border border-slate-200/90 bg-white text-slate-500 shadow-2xs transition hover:border-slate-300 hover:bg-slate-50 hover:text-slate-800"
            >
              {sidebarCollapsed ? (
                <PanelLeftOpen className="h-4 w-4" />
              ) : (
                <PanelLeftClose className="h-4 w-4" />
              )}
            </button>
            <div className="hidden h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-emerald-50 text-emerald-600 ring-1 ring-inset ring-emerald-200/70 sm:flex">
              <CurrentNavIcon className="h-4 w-4" />
            </div>
            <div className="min-w-0">
              <h1 className="truncate text-base font-bold tracking-tight text-slate-900 sm:text-lg">
                {currentNav.label}
              </h1>
              <p className="hidden truncate text-xs text-slate-500 sm:block">{currentNav.subtitle}</p>
            </div>
          </div>

          <div className="flex shrink-0 items-center gap-2">
            {/* 顶栏快捷余额胶囊（侧边栏收起时也能随时查看并刷新余额） */}
            <div className="inline-flex items-center gap-1.5 rounded-full border border-slate-200/90 bg-slate-50/90 px-2.5 py-1 text-xs shadow-2xs">
              <Wallet className="h-3.5 w-3.5 text-emerald-600" />
              <span className="hidden text-slate-500 md:inline">{defaultProviderLabel}</span>
              <span className="font-mono-code font-semibold text-slate-900">
                {balance !== null ? formatMoney(balance, balanceCurrency) : "-"}
              </span>
              <button
                type="button"
                onClick={() => loadBalance()}
                disabled={balanceLoading}
                title="刷新平台余额"
                className="cursor-pointer rounded-full p-0.5 text-slate-400 transition hover:bg-white hover:text-slate-700 disabled:opacity-50"
              >
                <RefreshCw className={`h-3 w-3 ${balanceLoading ? "animate-spin text-emerald-600" : ""}`} />
              </button>
            </div>

            <span
              className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium shadow-2xs ${
                sseConnected
                  ? "border-emerald-200/90 bg-emerald-50/90 text-emerald-700"
                  : "border-amber-200/90 bg-amber-50/90 text-amber-700"
              }`}
            >
              <Radio className="h-3.5 w-3.5" />
              <span className="hidden sm:inline">{sseConnected ? "实时推码在线" : "SSE 重连中"}</span>
            </span>
          </div>
        </header>

        <main className="@container mx-auto w-full max-w-[1360px] flex-1 p-4 sm:p-5 lg:p-7">
          <div className={page === "workbench" ? "block" : "hidden"}>
            <WorkbenchPage
              health={health}
              balance={balance}
              balanceCurrency={balanceCurrency}
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
            <IntegrationsPage active={page === "integrations"} />
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
