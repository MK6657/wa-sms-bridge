import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertCircle,
  ArrowRight,
  ChevronDown,
  Loader2,
  Play,
  RefreshCw,
  SlidersHorizontal,
  Smartphone,
  Volume2,
  VolumeX,
  Wallet,
} from "lucide-react";
import { ApiClientError, api } from "../../api/client";
import type {
  CountryOption,
  HealthResponse,
  PriceSnapshot,
  ServiceOption,
  SmsOrder,
  StatsResponse,
} from "../../api/types";
import { CountryCombobox } from "../../components/CountryCombobox";
import { PriceTierMatrix } from "../../components/PriceTierMatrix";
import { useToast } from "../../components/ToastProvider";
import { formatFinancialSummary, formatMoney } from "../../utils/format";
import { ActiveOrderCard } from "./ActiveOrderCard";
import {
  describeOrderSubmitOutcome,
  type PendingSubmitAttempt,
  WorkbenchIdempotencyController,
} from "./orderSubmission";
import {
  type PriceCombo,
  type PriceSelectionState,
  reconcileComboChange,
  resolveEffectiveTargetPrice,
  selectPriceTier,
  setManualPrice,
  shouldAcceptPriceSnapshot,
} from "./priceSelection";

interface WorkbenchPageProps {
  health: HealthResponse | null;
  balance: number | null;
  balanceError: string;
  balanceLoading: boolean;
  onRefreshBalance: () => void;
  stats: StatsResponse | null;
  activeOrders: SmsOrder[];
  onOrderMutated: (order: SmsOrder) => void;
  onRefreshAll: () => void;
  soundEnabled: boolean;
  onToggleSound: () => void;
  onGoSettings: () => void;
}

const PREF_STORAGE_KEY = "sms-hub.workbench.prefs.v1";

interface SavedPrefs {
  service?: string;
  customService?: string;
  countryId?: string;
  targetPrice?: string;
  priceLock?: boolean;
  priceFloatEnabled?: boolean;
  priceFloatPercent?: string;
  continuousPoll?: boolean;
  maxAttempts?: string;
  requestInterval?: string;
  waitTimeout?: string;
  fallbackCountries?: string;
}

function loadPrefs(): SavedPrefs {
  try {
    return JSON.parse(localStorage.getItem(PREF_STORAGE_KEY) || "{}") as SavedPrefs;
  } catch {
    return {};
  }
}

export function WorkbenchPage({
  health,
  balance,
  balanceError,
  balanceLoading,
  onRefreshBalance,
  stats,
  activeOrders,
  onOrderMutated,
  onRefreshAll,
  soundEnabled,
  onToggleSound,
  onGoSettings,
}: WorkbenchPageProps) {
  const { notify } = useToast();
  const initialPrefs = useMemo(() => loadPrefs(), []);

  const [services, setServices] = useState<ServiceOption[]>([]);
  const [countries, setCountries] = useState<CountryOption[]>([]);
  const [selectedService, setSelectedService] = useState(initialPrefs.service || "whatsapp");
  const [customService, setCustomService] = useState(initialPrefs.customService || "");
  const [useCustomService, setUseCustomService] = useState(Boolean(initialPrefs.customService));
  const [selectedCountry, setSelectedCountry] = useState<CountryOption | null>({
    id: 52,
    name: "Thailand",
    name_zh: "泰国",
    cc: "66",
    iso: "th",
  });

  // 价格快照
  const [priceSnapshot, setPriceSnapshot] = useState<PriceSnapshot | null>(null);
  const [priceLoading, setPriceLoading] = useState(false);
  const [priceError, setPriceError] = useState("");

  // 高级策略（默认折叠，高频极简；复核 P2-01：严格区分组合绑定价档与手工全局目标价）
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [priceSel, setPriceSel] = useState<PriceSelectionState>(() => ({
    selectedTier: null,
    manualTargetPrice: initialPrefs.targetPrice || "",
    priceLock: Boolean(initialPrefs.priceLock && Number(initialPrefs.targetPrice) > 0),
  }));
  const [priceFloatEnabled, setPriceFloatEnabled] = useState(Boolean(initialPrefs.priceFloatEnabled));
  const [priceFloatPercent, setPriceFloatPercent] = useState(initialPrefs.priceFloatPercent || "10");
  const [continuousPoll, setContinuousPoll] = useState(Boolean(initialPrefs.continuousPoll));
  const [maxAttempts, setMaxAttempts] = useState(initialPrefs.maxAttempts || "2");
  const [requestInterval, setRequestInterval] = useState(initialPrefs.requestInterval || "1.0");
  const [waitTimeout, setWaitTimeout] = useState(initialPrefs.waitTimeout || "180");
  const [fallbackCountries, setFallbackCountries] = useState(initialPrefs.fallbackCountries || "");
  const [skipBlacklisted, setSkipBlacklisted] = useState(true);

  const [acquiring, setAcquiring] = useState(false);
  const [checkingPendingOrder, setCheckingPendingOrder] = useState(false);
  const [pendingSubmitInfo, setPendingSubmitInfo] = useState<PendingSubmitAttempt | null>(null);
  const [busyByOrder, setBusyByOrder] = useState<Record<string, string>>({});
  const [sourceFilter, setSourceFilter] = useState<"all" | "console" | "external">("all");
  const countriesLoadedRef = useRef(false);

  const activeProvider = health?.default_provider || "smsbower";
  const activeProviderLabel = activeProvider.toUpperCase();
  const isCustomEmpty = useCustomService && !customService.trim();
  const effectiveService = useCustomService
    ? customService.trim()
      ? `custom:${customService.trim()}`
      : ""
    : selectedService;

  const currentCombo: PriceCombo | null = useMemo(
    () =>
      selectedCountry && effectiveService
        ? {
            provider: activeProvider,
            countryId: String(selectedCountry.id),
            service: effectiveService,
          }
        : null,
    [activeProvider, selectedCountry, effectiveService],
  );
  const latestComboRef = useRef<PriceCombo | null>(currentCombo);
  latestComboRef.current = currentCombo;

  const resolvedPrice = useMemo(
    () => resolveEffectiveTargetPrice(priceSel, currentCombo),
    [priceSel, currentCombo],
  );
  const priceLock = priceSel.priceLock;

  const isConfigured = Boolean(health?.provider_configured?.[activeProvider] ?? health?.provider_configured?.smsbower);
  const showUnconfiguredBanner = health !== null && !isConfigured;
  const priceReqSeqRef = useRef(0);
  const priceAbortRef = useRef<AbortController | null>(null);
  const idempotencyRef = useRef<WorkbenchIdempotencyController>(new WorkbenchIdempotencyController());
  const mountedRef = useRef(true);
  const pendingCheckSeqRef = useRef(0);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // 切换国家 / 服务 / 平台时立即清除旧组合绑定的价档选择（复核 P2-01）
  useEffect(() => {
    setPriceSel((prev) => reconcileComboChange(prev, currentCombo));
  }, [currentCombo]);

  // 保存偏好（仅持久化手工输入的目标价，且未加载完国家列表前绝不把初始兜底 52 覆盖掉用户保存的国家 ID）
  useEffect(() => {
    const savedCountryId = countriesLoadedRef.current
      ? selectedCountry
        ? String(selectedCountry.id)
        : ""
      : initialPrefs.countryId ?? (selectedCountry ? String(selectedCountry.id) : "");
    const next: SavedPrefs = {
      service: selectedService,
      customService: useCustomService ? customService : "",
      countryId: savedCountryId,
      targetPrice: priceSel.manualTargetPrice,
      priceLock: Boolean(priceSel.priceLock && Number(priceSel.manualTargetPrice) > 0),
      priceFloatEnabled,
      priceFloatPercent,
      continuousPoll,
      maxAttempts,
      requestInterval,
      waitTimeout,
      fallbackCountries,
    };
    try {
      localStorage.setItem(PREF_STORAGE_KEY, JSON.stringify(next));
    } catch {
      // ignore storage quota errors
    }
  }, [
    selectedService,
    useCustomService,
    customService,
    selectedCountry,
    initialPrefs.countryId,
    priceSel.manualTargetPrice,
    priceSel.priceLock,
    priceFloatEnabled,
    priceFloatPercent,
    continuousPoll,
    maxAttempts,
    requestInterval,
    waitTimeout,
    fallbackCountries,
  ]);

  // 加载服务目录与国家列表（遵守服务端默认平台与默认国家，正确处理合法国家 ID 0）
  useEffect(() => {
    let active = true;
    api
      .getServices(activeProvider)
      .then((res) => {
        if (active) setServices(res.services);
      })
      .catch(() => {});

    if (!isConfigured) return;
    api
      .getCountries(activeProvider)
      .then((res) => {
        if (!active) return;
        countriesLoadedRef.current = true;
        setCountries(res.countries);
        const serverDefault =
          health?.default_country !== undefined && health?.default_country !== null
            ? String(health.default_country)
            : "52";
        const preferredId =
          initialPrefs.countryId !== undefined && initialPrefs.countryId !== ""
            ? initialPrefs.countryId
            : serverDefault;
        const matched = res.countries.find((c) => String(c.id) === preferredId);
        if (matched) {
          setSelectedCountry(matched);
        } else if (res.countries.length > 0) {
          const stillValid = selectedCountry && res.countries.some((c) => String(c.id) === String(selectedCountry.id));
          if (!stillValid) setSelectedCountry(res.countries[0]);
        }
      })
      .catch(() => {
        // 未配置 API Key 时静默保留默认内置热门国家列表
      });
    return () => {
      active = false;
    };
  }, [activeProvider, health?.default_country, initialPrefs.countryId, isConfigured]);

  // 查询实时价格档位（修复 P2-02：切换条件立即清旧快照并取消旧请求，按序号抛弃过期响应）
  const loadPrices = useCallback(
    async (force = false) => {
      priceAbortRef.current?.abort();
      priceAbortRef.current = null;
      const reqSeq = ++priceReqSeqRef.current;

      if (!selectedCountry || !isConfigured || !effectiveService) {
        setPriceSnapshot(null);
        setPriceError("");
        setPriceLoading(false);
        return;
      }

      const requestCombo: PriceCombo = {
        provider: activeProvider,
        countryId: String(selectedCountry.id),
        service: effectiveService,
      };
      const controller = new AbortController();
      priceAbortRef.current = controller;
      setPriceSnapshot(null);
      setPriceLoading(true);
      setPriceError("");

      try {
        const snap = await api.getPrices(
          requestCombo.countryId,
          requestCombo.service,
          requestCombo.provider,
          force,
          controller.signal,
        );
        if (!shouldAcceptPriceSnapshot(reqSeq, priceReqSeqRef.current, requestCombo, latestComboRef.current)) {
          return;
        }
        setPriceSnapshot(snap);
      } catch (err) {
        if (controller.signal.aborted || reqSeq !== priceReqSeqRef.current) return;
        setPriceSnapshot(null);
        setPriceError(err instanceof Error ? err.message : "价格查询失败");
      } finally {
        if (reqSeq === priceReqSeqRef.current) {
          setPriceLoading(false);
        }
      }
    },
    [selectedCountry, effectiveService, activeProvider, isConfigured],
  );

  useEffect(() => {
    if (useCustomService) {
      const timer = window.setTimeout(() => {
        loadPrices(false);
      }, 240);
      return () => {
        window.clearTimeout(timer);
        priceAbortRef.current?.abort();
      };
    }
    loadPrices(false);
    return () => {
      priceAbortRef.current?.abort();
    };
  }, [loadPrices, useCustomService]);

  const handleAcquire = async () => {
    if (isCustomEmpty) {
      notify("请输入自定义服务码，或点击右上角返回常用服务", "warn");
      return;
    }
    if (!selectedCountry || !currentCombo) {
      notify("请先选择国家", "warn");
      return;
    }
    const { value: parsedTarget, source: priceSource } = resolveEffectiveTargetPrice(priceSel, currentCombo);
    // 禁止手工输入 0 或负数时静默去掉价格上限导致按市价不限价下单
    if (priceSource === "manual" && priceSel.manualTargetPrice.trim() !== "" && (!parsedTarget || parsedTarget <= 0)) {
      notify("目标/最高价必须大于 0（留空表示不限价）", "error");
      setShowAdvanced(true);
      return;
    }
    if ((priceLock || priceFloatEnabled) && (!parsedTarget || parsedTarget <= 0)) {
      notify("开启限价或价格浮动时，目标价格必须大于 0", "warn");
      setShowAdvanced(true);
      return;
    }

    setAcquiring(true);
    try {
      const fallbacks = fallbackCountries
        .split(/[,，\n]+/)
        .map((s) => s.trim())
        .filter(Boolean);
      const parsedAttempts = Math.min(100, Math.max(1, Math.round(Number(maxAttempts) || 2)));
      const parsedInterval = Math.min(3600, Math.max(0.1, Number(requestInterval) || 1));
      const parsedFloatPct = Math.min(1000, Math.max(0, Number(priceFloatPercent) || 0));
      const parsedTimeout = Math.min(1200, Math.max(30, Math.round(Number(waitTimeout) || 180)));

      const payload = {
        provider: activeProvider,
        service: effectiveService,
        country: String(selectedCountry.id),
        country_fallback: fallbacks,
        strategy: {
          target_price: parsedTarget && parsedTarget > 0 ? parsedTarget : undefined,
          price_max: !priceFloatEnabled && parsedTarget && parsedTarget > 0 ? parsedTarget : undefined,
          price_lock: priceLock,
          price_float_enabled: !priceLock && priceFloatEnabled,
          price_float_percent: !priceLock && priceFloatEnabled ? parsedFloatPct : 0,
          max_attempts: parsedAttempts,
          continuous_poll: continuousPoll,
          request_interval_enabled: continuousPoll || parsedAttempts > 1,
          request_interval: parsedInterval,
        },
        wait_timeout: parsedTimeout,
        skip_blacklisted: skipBlacklisted,
        async_acquire: continuousPoll,
      };
      const idempotencyKey = idempotencyRef.current.beginAttempt(payload);
      const order = await api.createOrder(payload, idempotencyKey);
      idempotencyRef.current.onSuccess();
      if (!mountedRef.current) return;
      setPendingSubmitInfo(null);

      onOrderMutated(order);
      onRefreshBalance();
      const feedback = describeOrderSubmitOutcome(order);
      notify(feedback.message, feedback.tone);
    } catch (err) {
      const apiErr = err instanceof ApiClientError ? err : null;
      idempotencyRef.current.onFailure(apiErr?.status, {
        code: apiErr?.code,
        message: apiErr?.message || (err instanceof Error ? err.message : "取号失败"),
        orderId: apiErr?.orderId,
      });
      if (!mountedRef.current) return;
      setPendingSubmitInfo(idempotencyRef.current.currentAttempt);
      if (apiErr?.code !== "USER_CANCELLED") {
        const oidHint = apiErr?.orderId ? `（关联订单 ${apiErr.orderId}）` : "";
        notify(`${err instanceof Error ? err.message : "取号失败"}${oidHint}`, "error");
      }
    } finally {
      if (mountedRef.current) setAcquiring(false);
    }
  };

  const handleCheckPendingOrder = async () => {
    const targetId = pendingSubmitInfo?.lastOrderId;
    const attemptKey = pendingSubmitInfo?.key;
    if (!targetId || !attemptKey) return;
    const reqSeq = ++pendingCheckSeqRef.current;
    setCheckingPendingOrder(true);
    try {
      const latest = await api.getOrder(targetId);
      if (!mountedRef.current) return;
      onOrderMutated(latest);
      // v7 复核 P1-06: 仅当返回仍匹配发起查询时的未决尝试 (key, orderId) 时才更新/清除控制器状态，过时回包直接忽略！
      if (idempotencyRef.current.currentKey !== attemptKey) {
        return;
      }
      const feedback = describeOrderSubmitOutcome(latest);
      notify(feedback.message, feedback.tone);
      if (idempotencyRef.current.confirmOrderChecked(latest.status, { key: attemptKey, orderId: targetId })) {
        setPendingSubmitInfo(null);
      }
    } catch (err) {
      if (!mountedRef.current || idempotencyRef.current.currentKey !== attemptKey) return;
      notify(err instanceof Error ? err.message : "查询原订单失败", "error");
    } finally {
      if (mountedRef.current && reqSeq === pendingCheckSeqRef.current) {
        setCheckingPendingOrder(false);
      }
    }
  };

  const handleStartFreshOrder = () => {
    idempotencyRef.current.resetForNewOrder();
    setPendingSubmitInfo(null);
    notify("已重置幂等键，下次点击将以全新独立订单提交", "info");
  };

  const runOrderAction = async (
    orderId: string,
    actionKey: string,
    fn: () => Promise<SmsOrder>,
    describeSuccess: string | ((updated: SmsOrder) => { text: string; tone: "success" | "warn" | "info" }),
  ) => {
    setBusyByOrder((prev) => ({ ...prev, [orderId]: actionKey }));
    try {
      const updated = await fn();
      onOrderMutated(updated);
      if (typeof describeSuccess === "function") {
        const msg = describeSuccess(updated);
        notify(msg.text, msg.tone);
      } else {
        notify(describeSuccess, "success");
      }
      onRefreshAll();
    } catch (err) {
      notify(err instanceof Error ? err.message : "操作失败", "error");
    } finally {
      setBusyByOrder((prev) => {
        const next = { ...prev };
        delete next[orderId];
        return next;
      });
    }
  };

  const filteredActiveOrders = useMemo(() => {
    if (sourceFilter === "console") return activeOrders.filter((o) => o.project_id === "web-console");
    if (sourceFilter === "external") return activeOrders.filter((o) => o.project_id !== "web-console");
    return activeOrders;
  }, [activeOrders, sourceFilter]);

  const popularServices = services.filter((s) => s.popular);
  const otherServices = services.filter((s) => !s.popular);

  return (
    <div className="space-y-5">
      {/* 首次未配置 API Key 引导横幅 */}
      {showUnconfiguredBanner ? (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3.5 text-sm text-amber-950">
          <div className="flex items-center gap-2.5">
            <AlertCircle className="h-5 w-5 shrink-0 text-amber-600" />
            <div>
              <strong className="font-semibold">尚未配置 {activeProviderLabel} API Key</strong>
              <span className="ml-2 text-amber-800">
                配置完成后即可查询实时价格档位并开始取号接码。
              </span>
            </div>
          </div>
          <button
            type="button"
            onClick={onGoSettings}
            className="inline-flex cursor-pointer items-center gap-1.5 rounded-xl bg-amber-600 px-3.5 py-2 text-xs font-semibold text-white transition hover:bg-amber-700"
          >
            <span>立即去配置</span>
            <ArrowRight className="h-3.5 w-3.5" />
          </button>
        </div>
      ) : null}

      {/* 精简 KPI 概览条 */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <div className="rounded-2xl border border-slate-200 bg-white p-4 shadow-2xs">
          <div className="flex items-center justify-between text-xs text-slate-500">
            <span className="inline-flex items-center gap-1.5 font-medium">
              <Wallet className="h-3.5 w-3.5 text-emerald-600" />
              {activeProviderLabel} 余额
            </span>
            <button
              type="button"
              onClick={onRefreshBalance}
              disabled={balanceLoading}
              className="cursor-pointer rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
              title="刷新余额"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${balanceLoading ? "animate-spin" : ""}`} />
            </button>
          </div>
          <div className="font-mono-code mt-2 text-2xl font-bold text-slate-900">
            {balance !== null ? formatMoney(balance) : "-"}
          </div>
          <div className="mt-1 truncate text-xs text-slate-400">
            {balanceError ? balanceError : "主活接码平台 · USD"}
          </div>
        </div>

        <div className="rounded-2xl border border-slate-200 bg-white p-4 shadow-2xs">
          <div className="text-xs font-medium text-slate-500">当前活跃接码</div>
          <div className="font-mono-code mt-2 text-2xl font-bold text-slate-900">
            {activeOrders.length}
          </div>
          <div className="mt-1 text-xs text-slate-400">
            工作台与外部 API 实时订单
          </div>
        </div>

        <div className="rounded-2xl border border-slate-200 bg-white p-4 shadow-2xs">
          <div className="text-xs font-medium text-slate-500">今日收到验证码</div>
          <div className="font-mono-code mt-2 text-2xl font-bold text-emerald-700">
            {stats?.today.received_count ?? 0}
          </div>
          <div className="mt-1 text-xs text-slate-400">
            共取号 {stats?.today.total ?? 0} 次 · 成功率{" "}
            {stats ? `${Math.round(stats.today.success_rate * 100)}%` : "0%"}
          </div>
        </div>

        <div className="rounded-2xl border border-slate-200 bg-white p-4 shadow-2xs">
          <div className="text-xs font-medium text-slate-500">今日接码消耗</div>
          {(() => {
            const fin = formatFinancialSummary(stats?.today);
            return (
              <>
                <div className="font-mono-code mt-2 text-xl font-bold text-slate-900 break-words">
                  {fin.primary}
                </div>
                {fin.lines.length > 0 ? (
                  <div className="mt-1 space-y-0.5 text-[11px] text-slate-500">
                    {fin.lines.map((line) => (
                      <div key={line} className="truncate" title={line}>
                        {line}
                      </div>
                    ))}
                  </div>
                ) : null}
                {fin.fxNote ? (
                  <div className="mt-0.5 text-[11px] font-medium text-indigo-600">{fin.fxNote}</div>
                ) : fin.isMultiCurrency ? (
                  <div className="mt-0.5 text-[11px] text-amber-600">多币种未折算（无汇率）</div>
                ) : null}
                <div className="mt-1 text-xs text-slate-400">
                  确认退款 {stats?.today.refund_confirmed_count ?? stats?.today.refunded_count ?? 0}
                  {(stats?.today.refund_amount_unknown_count ?? 0) > 0
                    ? ` (金额未知 ${stats?.today.refund_amount_unknown_count})`
                    : ""}{" "}
                  · 黑名单 {stats?.blacklist_count ?? 0}
                </div>
              </>
            );
          })()}
        </div>
      </div>

      {/* 主操作区：左侧快捷取号面板 + 右侧实时接码卡片墙 */}
      <div className="grid items-start gap-5 lg:grid-cols-[400px_minmax(0,1fr)]">
        {/* 左侧取号面板 */}
        <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-xs">
          <div className="mb-4 flex items-center justify-between">
            <div>
              <h2 className="text-base font-semibold text-slate-900">快捷取号</h2>
              <p className="text-xs text-slate-500">选定业务与国家后一键取号，支持并发多开</p>
            </div>
            <span className="rounded-full bg-emerald-50 px-2.5 py-0.5 text-xs font-medium text-emerald-700 ring-1 ring-emerald-200">
              {activeProviderLabel}
            </span>
          </div>

          <div className="space-y-4">
            {/* 业务服务选择 */}
            <div>
              <div className="mb-1.5 flex items-center justify-between">
                <label className="text-xs font-medium text-slate-700">目标业务</label>
                <button
                  type="button"
                  onClick={() => setUseCustomService((v) => !v)}
                  className="cursor-pointer text-xs font-medium text-indigo-600 hover:text-indigo-700"
                >
                  {useCustomService ? "返回常用服务" : "输入自定义服务码"}
                </button>
              </div>

              {useCustomService ? (
                <input
                  type="text"
                  value={customService}
                  onChange={(e) => setCustomService(e.target.value)}
                  placeholder="输入 SMSBower 原生服务码，如 wa / tg / go / ot"
                  className="w-full rounded-xl border border-slate-200 bg-white px-3.5 py-2.5 font-mono-code text-sm outline-none focus:border-emerald-500 focus:ring-2 focus:ring-emerald-500/15"
                />
              ) : (
                <div className="space-y-2">
                  <div className="flex flex-wrap gap-1.5">
                    {(popularServices.length
                      ? popularServices
                      : [
                          { id: "whatsapp", label: "WhatsApp", code: "wa", popular: true },
                          { id: "telegram", label: "Telegram", code: "tg", popular: true },
                          { id: "google", label: "Google", code: "go", popular: true },
                          { id: "openai", label: "OpenAI", code: "dr", popular: true },
                          { id: "discord", label: "Discord", code: "ds", popular: true },
                        ]
                    ).map((svc) => (
                      <button
                        key={svc.id}
                        type="button"
                        onClick={() => setSelectedService(svc.id)}
                        className={`cursor-pointer rounded-xl border px-3 py-1.5 text-xs font-medium transition ${
                          selectedService === svc.id
                            ? "border-emerald-600 bg-emerald-600 text-white shadow-2xs"
                            : "border-slate-200 bg-white text-slate-700 hover:border-slate-300 hover:bg-slate-50"
                        }`}
                      >
                        {svc.label}
                      </button>
                    ))}
                  </div>
                  {otherServices.length > 0 ? (
                    <select
                      value={otherServices.some((s) => s.id === selectedService) ? selectedService : ""}
                      onChange={(e) => {
                        if (e.target.value) setSelectedService(e.target.value);
                      }}
                      className="w-full rounded-xl border border-slate-200 bg-slate-50/70 px-3 py-2 text-xs text-slate-700 outline-none focus:border-emerald-500"
                    >
                      <option value="">更多常用服务（Facebook / TikTok / Apple / 通用 ot...）</option>
                      {otherServices.map((svc) => (
                        <option key={svc.id} value={svc.id}>
                          {svc.label} ({svc.code})
                        </option>
                      ))}
                    </select>
                  ) : null}
                </div>
              )}
            </div>

            {/* 国家选择 */}
            <div>
              <label className="mb-1.5 block text-xs font-medium text-slate-700">国家 / 地区</label>
              <CountryCombobox
                countries={countries}
                selected={selectedCountry}
                provider={activeProvider}
                onSelect={(c) => setSelectedCountry(c)}
              />
            </div>

            {/* 实时价格档位矩阵 */}
            <PriceTierMatrix
              snapshot={priceSnapshot}
              loading={priceLoading}
              error={priceError}
              providerLabel={activeProviderLabel}
              selectedPrice={resolvedPrice.source === "tier" ? resolvedPrice.displayValue : undefined}
              onSelectPrice={(cost) => {
                if (!currentCombo) return;
                setPriceSel((prev) => selectPriceTier(prev, currentCombo, cost));
                setShowAdvanced(true);
              }}
              onRefresh={() => loadPrices(true)}
            />

            {/* 可折叠高级价格与抢号策略 */}
            <div className="rounded-xl border border-slate-200 bg-white">
              <button
                type="button"
                onClick={() => setShowAdvanced((v) => !v)}
                className="flex w-full cursor-pointer items-center justify-between px-3.5 py-2.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
              >
                <span className="inline-flex items-center gap-1.5">
                  <SlidersHorizontal className="h-3.5 w-3.5 text-slate-400" />
                  高级策略（限价 / 目标档上限 / 持续抢号 / 备选国家）
                </span>
                <ChevronDown
                  className={`h-4 w-4 text-slate-400 transition-transform ${showAdvanced ? "rotate-180" : ""}`}
                />
              </button>

              {showAdvanced ? (
                <div className="space-y-3 border-t border-slate-100 p-3.5 text-xs">
                  <div className="grid grid-cols-2 gap-2.5">
                    <div>
                      <div className="mb-1 flex items-center justify-between gap-1">
                        <label className="text-slate-500">目标/最高价 ($)</label>
                        {resolvedPrice.source === "tier" ? (
                          <span className="rounded bg-emerald-50 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700">
                            当前组合价档
                          </span>
                        ) : resolvedPrice.source === "manual" ? (
                          <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-600">
                            手动指定
                          </span>
                        ) : null}
                      </div>
                      <input
                        type="number"
                        step="0.01"
                        min="0.0001"
                        value={resolvedPrice.displayValue}
                        onChange={(e) => setPriceSel((prev) => setManualPrice(prev, e.target.value))}
                        placeholder="留空按市价"
                        className="font-mono-code w-full rounded-lg border border-slate-200 px-2.5 py-1.5 outline-none focus:border-emerald-500"
                      />
                    </div>
                    <div>
                      <label className="mb-1 block text-slate-500">等码超时 (秒)</label>
                      <input
                        type="number"
                        min="30"
                        max="1200"
                        value={waitTimeout}
                        onChange={(e) => setWaitTimeout(e.target.value)}
                        className="font-mono-code w-full rounded-lg border border-slate-200 px-2.5 py-1.5 outline-none focus:border-emerald-500"
                      />
                    </div>
                  </div>

                  <div className="grid grid-cols-2 gap-2">
                    <label
                      className="flex cursor-pointer items-center gap-2 rounded-lg border border-slate-200 px-2.5 py-2 hover:bg-slate-50"
                      title="检查目标价档库存并按 maxPrice 上限下单（SMSBower 协议为最高价上限语义）"
                    >
                      <input
                        type="checkbox"
                        checked={priceLock}
                        onChange={(e) => {
                          const checked = e.target.checked;
                          setPriceSel((prev) => ({ ...prev, priceLock: checked }));
                          if (checked) setPriceFloatEnabled(false);
                        }}
                        className="rounded border-slate-300 text-emerald-600"
                      />
                      <span className="font-medium text-slate-700">目标档上限 (maxPrice)</span>
                    </label>

                    <label className="flex cursor-pointer items-center gap-2 rounded-lg border border-slate-200 px-2.5 py-2 hover:bg-slate-50">
                      <input
                        type="checkbox"
                        checked={priceFloatEnabled}
                        disabled={priceLock}
                        onChange={(e) => setPriceFloatEnabled(e.target.checked)}
                        className="rounded border-slate-300 text-emerald-600"
                      />
                      <span className="font-medium text-slate-700">允许上浮 %</span>
                    </label>
                  </div>

                  {priceFloatEnabled && !priceLock ? (
                    <div>
                      <label className="mb-1 block text-slate-500">价格上浮比例 (%)</label>
                      <input
                        type="number"
                        min="0"
                        max="1000"
                        value={priceFloatPercent}
                        onChange={(e) => setPriceFloatPercent(e.target.value)}
                        className="font-mono-code w-full rounded-lg border border-slate-200 px-2.5 py-1.5 outline-none focus:border-emerald-500"
                      />
                    </div>
                  ) : null}

                  <div className="grid grid-cols-2 gap-2.5">
                    <div>
                      <label className="mb-1 block text-slate-500">最多尝试次数</label>
                      <input
                        type="number"
                        min="1"
                        max="100"
                        disabled={continuousPoll}
                        value={maxAttempts}
                        onChange={(e) => setMaxAttempts(e.target.value)}
                        className="font-mono-code w-full rounded-lg border border-slate-200 px-2.5 py-1.5 outline-none disabled:bg-slate-100"
                      />
                    </div>
                    <div>
                      <label className="mb-1 block text-slate-500">重试间隔 (秒)</label>
                      <input
                        type="number"
                        step="0.5"
                        min="0.1"
                        value={requestInterval}
                        onChange={(e) => setRequestInterval(e.target.value)}
                        className="font-mono-code w-full rounded-lg border border-slate-200 px-2.5 py-1.5 outline-none"
                      />
                    </div>
                  </div>

                  <div className="flex flex-wrap gap-3">
                    <label className="inline-flex cursor-pointer items-center gap-1.5 text-slate-700">
                      <input
                        type="checkbox"
                        checked={continuousPoll}
                        onChange={(e) => setContinuousPoll(e.target.checked)}
                      />
                      持续轮询抢号（异步）
                    </label>
                    <label className="inline-flex cursor-pointer items-center gap-1.5 text-slate-700">
                      <input
                        type="checkbox"
                        checked={skipBlacklisted}
                        onChange={(e) => setSkipBlacklisted(e.target.checked)}
                      />
                      自动跳过黑名单号
                    </label>
                  </div>

                  <div>
                    <label className="mb-1 block text-slate-500">备选国家（逗号分隔）</label>
                    <input
                      type="text"
                      value={fallbackCountries}
                      onChange={(e) => setFallbackCountries(e.target.value)}
                      placeholder="如 Indonesia, South Africa, 4"
                      className="w-full rounded-lg border border-slate-200 px-2.5 py-1.5 outline-none focus:border-emerald-500"
                    />
                  </div>
                </div>
              ) : null}
            </div>

            {/* 上次提交结果不确定或失败时的原单查询与新建订单引导（v6 复核 P2-02） */}
            {pendingSubmitInfo?.outcomeUncertain ? (
              <div className="rounded-xl border border-amber-200 bg-amber-50/90 p-3 text-xs text-amber-950">
                <div className="font-medium">
                  {pendingSubmitInfo.lastOrderId
                    ? `上次请求已生成订单 ${pendingSubmitInfo.lastOrderId}（HTTP ${pendingSubmitInfo.lastStatus ?? "-"}）`
                    : "上次请求网络中断或结果未确认"}
                </div>
                <div className="mt-1 text-[11px] text-amber-800">
                  直接点击下方取号按钮将复用原幂等键防重复扣费；若已确认旧单终结，请点击“确认新建订单”再同参数重下。
                </div>
                <div className="mt-2 flex flex-wrap gap-2">
                  {pendingSubmitInfo.lastOrderId ? (
                    <button
                      type="button"
                      disabled={checkingPendingOrder}
                      onClick={handleCheckPendingOrder}
                      className="cursor-pointer rounded-lg border border-amber-300 bg-white px-2.5 py-1 text-[11px] font-medium text-amber-900 hover:bg-amber-100 disabled:opacity-50"
                    >
                      {checkingPendingOrder ? "查询中..." : "查询原订单状态"}
                    </button>
                  ) : null}
                  <button
                    type="button"
                    onClick={handleStartFreshOrder}
                    className="cursor-pointer rounded-lg bg-amber-700 px-2.5 py-1 text-[11px] font-semibold text-white hover:bg-amber-800"
                  >
                    确认新建订单（换新键）
                  </button>
                </div>
              </div>
            ) : null}

            {/* 立即取号按钮 */}
            <button
              type="button"
              onClick={handleAcquire}
              disabled={acquiring || !isConfigured || isCustomEmpty}
              className="flex w-full cursor-pointer items-center justify-center gap-2 rounded-xl bg-emerald-600 px-4 py-3 text-sm font-semibold text-white shadow-sm transition hover:bg-emerald-700 active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-50"
            >
              {acquiring ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  <span>正在向 {activeProviderLabel} 取号...</span>
                </>
              ) : (
                <>
                  <Play className="h-4 w-4 fill-current" />
                  <span>{continuousPoll ? "启动持续抢号" : "立即取号接码"}</span>
                </>
              )}
            </button>
          </div>
        </section>

        {/* 右侧活跃接码卡片墙 */}
        <section className="space-y-3.5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <h2 className="text-base font-semibold text-slate-900">实时接码看板</h2>
              <span className="rounded-full bg-slate-200/70 px-2 py-0.5 font-mono-code text-xs font-semibold text-slate-700">
                {filteredActiveOrders.length}
              </span>
            </div>

            <div className="flex items-center gap-2">
              <div className="inline-flex rounded-xl border border-slate-200 bg-white p-0.5 text-xs">
                {(
                  [
                    ["all", "全部"],
                    ["console", "仅工作台"],
                    ["external", "外部 API"],
                  ] as const
                ).map(([key, label]) => (
                  <button
                    key={key}
                    type="button"
                    onClick={() => setSourceFilter(key)}
                    className={`cursor-pointer rounded-lg px-2.5 py-1 font-medium transition ${
                      sourceFilter === key
                        ? "bg-slate-900 text-white"
                        : "text-slate-600 hover:text-slate-900"
                    }`}
                  >
                    {label}
                  </button>
                ))}
              </div>

              <button
                type="button"
                onClick={onToggleSound}
                className={`inline-flex cursor-pointer items-center gap-1 rounded-xl border px-2.5 py-1.5 text-xs font-medium transition ${
                  soundEnabled
                    ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                    : "border-slate-200 bg-white text-slate-500 hover:bg-slate-50"
                }`}
                title="收到验证码时播放提示音"
              >
                {soundEnabled ? <Volume2 className="h-3.5 w-3.5" /> : <VolumeX className="h-3.5 w-3.5" />}
                <span>提示音</span>
              </button>
            </div>
          </div>

          {filteredActiveOrders.length === 0 ? (
            <div className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-slate-300 bg-white/70 px-6 py-16 text-center">
              <div className="rounded-2xl bg-slate-100 p-3.5 text-slate-400">
                <Smartphone className="h-7 w-7" />
              </div>
              <h3 className="mt-4 text-sm font-semibold text-slate-800">当前没有进行中的接码订单</h3>
              <p className="mt-1.5 max-w-md text-xs leading-relaxed text-slate-500">
                在左侧选择目标业务与国家点击「立即取号接码」，或由外部项目调用{" "}
                <code className="font-mono-code rounded bg-slate-100 px-1.5 py-0.5 text-slate-700">
                  POST /api/v1/orders
                </code>{" "}
                后，号码与验证码都会实时出现在这里。
              </p>
            </div>
          ) : (
            <div className="space-y-3.5">
              {filteredActiveOrders.map((order) => (
                <ActiveOrderCard
                  key={order.order_id}
                  order={order}
                  busyAction={busyByOrder[order.order_id] || null}
                  supportsResend={order.provider !== "5sim"}
                  onResend={(id) =>
                    runOrderAction(id, "resend", () => api.resendSms(id), `已请求 ${order.provider.toUpperCase()} 重发短信`)
                  }
                  onComplete={(id) =>
                    runOrderAction(id, "complete", () => api.completeOrder(id), "订单已确认完成")
                  }
                  onCancel={(id, bl) =>
                    runOrderAction(
                      id,
                      bl ? "blacklist" : "cancel",
                      () =>
                        api.cancelOrder(id, {
                          blacklist: bl,
                          reason: bl ? "工作台拉黑并取消" : "工作台手动取消",
                        }),
                      (updated) => {
                        const blPrefix = bl ? "已拉黑号码，" : "";
                        if (updated.status === "compensation_pending" || updated.refund_state === "pending") {
                          return {
                            text: `${blPrefix}上游冷却中，已转入后台自动重试退款`,
                            tone: "warn",
                          };
                        }
                        if (updated.refund_state === "confirmed") {
                          return {
                            text: `${blPrefix}订单已取消并确认退款`,
                            tone: "success",
                          };
                        }
                        if (updated.refund_state === "unrefundable") {
                          return {
                            text: `${blPrefix}订单已取消（上游未确认可退）`,
                            tone: "warn",
                          };
                        }
                        return {
                          text: `${blPrefix}已停止取号`,
                          tone: "info",
                        };
                      },
                    )
                  }
                />
              ))}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
