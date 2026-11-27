import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  AlertCircle,
  ArrowRight,
  CheckCircle2,
  ChevronDown,
  Coins,
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
  ProviderDescriptor,
  ServiceOption,
  SmsOrder,
  StatsResponse,
} from "../../api/types";
import { CountryCombobox } from "../../components/CountryCombobox";
import { CustomSelect, type SelectOption } from "../../components/CustomSelect";
import { PriceTierMatrix } from "../../components/PriceTierMatrix";
import { useToast } from "../../components/ToastProvider";
import { ToggleSwitch } from "../../components/ToggleSwitch";
import { formatFinancialSummary, formatMoney, formatPrice } from "../../utils/format";
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
  balanceCurrency?: string;
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
  countryByProvider?: Record<string, string>;
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
  balanceCurrency = "USD",
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
  const [countriesReadyProvider, setCountriesReadyProvider] = useState<string>("smsbower");
  const [providerCapsById, setProviderCapsById] = useState<
    Record<string, ProviderDescriptor["capabilities"]>
  >({});
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

  // 高级策略
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [priceSel, setPriceSel] = useState<PriceSelectionState>(() => ({
    selectedTier: null,
    manualTargetPrice: initialPrefs.targetPrice || "",
    priceLock: Boolean(initialPrefs.priceLock && Number(initialPrefs.targetPrice) > 0),
  }));
  const [priceFloatEnabled, setPriceFloatEnabled] = useState(() =>
    Boolean(initialPrefs.priceFloatEnabled && Number(initialPrefs.targetPrice) > 0),
  );
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
  const acquiringLockRef = useRef(false);
  const busyOrderSetRef = useRef<Set<string>>(new Set());
  const countryByProviderRef = useRef<Record<string, string>>({
    ...(initialPrefs.countryId ? { smsbower: initialPrefs.countryId } : {}),
    ...(initialPrefs.countryByProvider || {}),
  });

  const activeProvider = health?.default_provider || "smsbower";
  const activeProviderLabel = activeProvider.toUpperCase();
  const supportsCustomService =
    providerCapsById[activeProvider]?.supports_custom_service ?? activeProvider !== "5sim";
  const cleanCustomService = customService.trim().replace(/^custom:/i, "").trim();
  const effectiveUseCustom = useCustomService && supportsCustomService;
  const isCustomEmpty = effectiveUseCustom && !cleanCustomService;
  const effectiveService = effectiveUseCustom
    ? cleanCustomService
      ? `custom:${cleanCustomService}`
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

  useEffect(() => {
    setPriceSel((prev) => {
      const next = reconcileComboChange(prev, currentCombo);
      if (prev.selectedTier && !next.selectedTier && !next.manualTargetPrice.trim()) {
        setPriceFloatEnabled(false);
      }
      return next;
    });
  }, [currentCombo]);

  useEffect(() => {
    let active = true;
    api
      .getProviders()
      .then((res) => {
        if (!active) return;
        const map: Record<string, ProviderDescriptor["capabilities"]> = {};
        for (const item of res.catalog || []) {
          if (item?.id && item.capabilities) {
            map[item.id] = item.capabilities;
          }
        }
        setProviderCapsById(map);
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [activeProvider]);

  useEffect(() => {
    const savedCountryId =
      countryByProviderRef.current[activeProvider] ??
      (activeProvider === "smsbower" ? initialPrefs.countryId : undefined) ??
      (selectedCountry ? String(selectedCountry.id) : "");
    const next: SavedPrefs = {
      service: selectedService,
      customService: effectiveUseCustom ? cleanCustomService : "",
      countryId: countryByProviderRef.current.smsbower ?? savedCountryId,
      countryByProvider: { ...countryByProviderRef.current },
      targetPrice: priceSel.manualTargetPrice,
      priceLock: Boolean(priceSel.priceLock && Number(priceSel.manualTargetPrice) > 0),
      priceFloatEnabled: Boolean(priceFloatEnabled && (resolvedPrice.value ?? 0) > 0),
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
    activeProvider,
    selectedService,
    effectiveUseCustom,
    cleanCustomService,
    selectedCountry,
    initialPrefs.countryId,
    priceSel.manualTargetPrice,
    priceSel.priceLock,
    resolvedPrice.value,
    priceFloatEnabled,
    priceFloatPercent,
    continuousPoll,
    maxAttempts,
    requestInterval,
    waitTimeout,
    fallbackCountries,
  ]);

  useEffect(() => {
    let active = true;
    api
      .getServices(activeProvider)
      .then((res) => {
        if (active) setServices(res.services);
      })
      .catch(() => {});

    if (!isConfigured) {
      return () => {
        active = false;
      };
    }
    api
      .getCountries(activeProvider)
      .then((res) => {
        if (!active) return;
        setCountries(res.countries);
        const serverDefault =
          health?.default_country !== undefined && health?.default_country !== null
            ? String(health.default_country)
            : "52";
        const savedForProvider = countryByProviderRef.current[activeProvider];
        const preferredId =
          savedForProvider !== undefined && savedForProvider !== ""
            ? savedForProvider
            : serverDefault;
        const matched =
          res.countries.find((c) => String(c.id) === preferredId) ||
          res.countries.find((c) => (c.iso || "").toLowerCase() === "th");
        if (matched) {
          countryByProviderRef.current[activeProvider] = String(matched.id);
          setSelectedCountry(matched);
        } else if (res.countries.length > 0) {
          setSelectedCountry((prev) => {
            const stillValid = prev && res.countries.some((c) => String(c.id) === String(prev.id));
            const nextCountry = stillValid ? prev : res.countries[0];
            countryByProviderRef.current[activeProvider] = String(nextCountry.id);
            return nextCountry;
          });
        }
        setCountriesReadyProvider(activeProvider);
      })
      .catch(() => {
        if (active) setCountriesReadyProvider(activeProvider);
      });
    return () => {
      active = false;
    };
  }, [activeProvider, health?.default_country, isConfigured]);

  const isCountrySyncedForProvider =
    activeProvider === "smsbower" || countriesReadyProvider === activeProvider;

  const loadPrices = useCallback(
    async (force = false) => {
      priceAbortRef.current?.abort();
      priceAbortRef.current = null;
      const reqSeq = ++priceReqSeqRef.current;

      if (!selectedCountry || !isConfigured || !effectiveService || !isCountrySyncedForProvider) {
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
    if (acquiringLockRef.current) return;
    if (isCustomEmpty) {
      notify("请输入自定义服务码，或点击右上角返回常用服务", "warn");
      return;
    }
    if (!selectedCountry || !currentCombo) {
      notify("请先选择国家", "warn");
      return;
    }
    const { value: parsedTarget, source: priceSource } = resolveEffectiveTargetPrice(priceSel, currentCombo);
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

    acquiringLockRef.current = true;
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
      acquiringLockRef.current = false;
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
    if (busyOrderSetRef.current.has(orderId)) return;
    busyOrderSetRef.current.add(orderId);
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
      busyOrderSetRef.current.delete(orderId);
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
  const otherServiceOptions: SelectOption[] = useMemo(
    () =>
      otherServices.map((svc) => ({
        value: svc.id,
        label: svc.label,
        badge: svc.code,
        badgeTone: "slate",
      })),
    [otherServices],
  );
  const isOtherServiceSelected = !useCustomService && otherServices.some((s) => s.id === selectedService);

  // 高级策略折叠态摘要徽标，避免隐藏生效中的限价或持续抢号状态
  const activeStrategyBadges: string[] = [];
  if (resolvedPrice.value && resolvedPrice.value > 0) {
    activeStrategyBadges.push(`限价 ${formatPrice(resolvedPrice.value)}`);
  }
  if (!priceLock && priceFloatEnabled && Number(priceFloatPercent) > 0) {
    activeStrategyBadges.push(`上浮 +${priceFloatPercent}%`);
  }
  if (continuousPoll) {
    activeStrategyBadges.push("持续抢号");
  }
  if (fallbackCountries.trim()) {
    activeStrategyBadges.push("含备选国家");
  }

  return (
    <div className="space-y-5">
      {/* 首次未配置 API Key 引导横幅 */}
      {showUnconfiguredBanner ? (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3.5 text-sm text-amber-950 shadow-2xs">
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

      {/* 分层高对比 KPI 概览条（基于容器宽度自适应 2 列 / 4 列） */}
      <div className="grid grid-cols-2 gap-3.5 @3xl:grid-cols-4">
        <div className="surface-card relative overflow-hidden rounded-2xl p-4">
          <div className="absolute inset-x-0 top-0 h-0.5 bg-gradient-to-r from-emerald-500 to-teal-400" />
          <div className="flex items-center justify-between text-xs text-slate-500">
            <span className="inline-flex items-center gap-1.5 font-medium text-slate-600">
              <span className="flex h-6 w-6 items-center justify-center rounded-lg bg-emerald-50 text-emerald-600 ring-1 ring-inset ring-emerald-200/70">
                <Wallet className="h-3.5 w-3.5" />
              </span>
              {activeProviderLabel} 余额
            </span>
            <button
              type="button"
              onClick={onRefreshBalance}
              disabled={balanceLoading}
              className="cursor-pointer rounded-lg p-1 text-slate-400 transition hover:bg-slate-100 hover:text-slate-700"
              title="刷新余额"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${balanceLoading ? "animate-spin text-emerald-600" : ""}`} />
            </button>
          </div>
          <div className="font-mono-code mt-2.5 text-2xl font-bold tracking-tight text-slate-900">
            {balance !== null ? formatMoney(balance, balanceCurrency || "USD") : "-"}
          </div>
          <div className="mt-1 truncate text-[11px] text-slate-400">
            {balanceError ? balanceError : `主活接码账户 · ${balanceCurrency || "USD"}`}
          </div>
        </div>

        <div className="surface-card relative overflow-hidden rounded-2xl p-4">
          <div className="absolute inset-x-0 top-0 h-0.5 bg-gradient-to-r from-indigo-500 to-sky-400" />
          <div className="flex items-center gap-1.5 text-xs font-medium text-slate-600">
            <span className="flex h-6 w-6 items-center justify-center rounded-lg bg-indigo-50 text-indigo-600 ring-1 ring-inset ring-indigo-200/70">
              <Activity className="h-3.5 w-3.5" />
            </span>
            <span>当前活跃接码</span>
          </div>
          <div className="font-mono-code mt-2.5 text-2xl font-bold tracking-tight text-slate-900">
            {activeOrders.length}
          </div>
          <div className="mt-1 truncate text-[11px] text-slate-400">
            工作台与外部 API 实时并发单
          </div>
        </div>

        <div className="surface-card relative overflow-hidden rounded-2xl p-4">
          <div className="absolute inset-x-0 top-0 h-0.5 bg-gradient-to-r from-teal-500 to-emerald-400" />
          <div className="flex items-center gap-1.5 text-xs font-medium text-slate-600">
            <span className="flex h-6 w-6 items-center justify-center rounded-lg bg-teal-50 text-teal-600 ring-1 ring-inset ring-teal-200/70">
              <CheckCircle2 className="h-3.5 w-3.5" />
            </span>
            <span>今日成功收码</span>
          </div>
          <div className="font-mono-code mt-2.5 text-2xl font-bold tracking-tight text-emerald-700">
            {stats?.today.received_count ?? 0}
          </div>
          <div className="mt-1 truncate text-[11px] text-slate-400">
            共取号 {stats?.today.total ?? 0} 次 · 收码率{" "}
            {stats ? `${Math.round(stats.today.success_rate * 100)}%` : "0%"}
          </div>
        </div>

        <div className="surface-card relative overflow-hidden rounded-2xl p-4">
          <div className="absolute inset-x-0 top-0 h-0.5 bg-gradient-to-r from-amber-500 to-orange-400" />
          <div className="flex items-center gap-1.5 text-xs font-medium text-slate-600">
            <span className="flex h-6 w-6 items-center justify-center rounded-lg bg-amber-50 text-amber-600 ring-1 ring-inset ring-amber-200/70">
              <Coins className="h-3.5 w-3.5" />
            </span>
            <span>今日净消耗</span>
          </div>
          {(() => {
            const fin = formatFinancialSummary(stats?.today);
            return (
              <>
                <div
                  className="font-mono-code mt-2.5 truncate text-xl font-bold tracking-tight text-slate-900"
                  title={fin.lines.join(" | ") || fin.primary}
                >
                  {fin.primary}
                </div>
                <div className="mt-1 truncate text-[11px] text-slate-400" title={fin.lines.join(" · ")}>
                  确认退款 {stats?.today.refund_confirmed_count ?? stats?.today.refunded_count ?? 0}
                  {(stats?.today.refund_amount_unknown_count ?? 0) > 0
                    ? ` (未知额 ${stats?.today.refund_amount_unknown_count})`
                    : ""}{" "}
                  · 黑名单 {stats?.blacklist_count ?? 0}
                </div>
              </>
            );
          })()}
        </div>
      </div>

      {/* 主操作区：基于容器查询自适应单栏或双栏，侧边栏展开/收起均不拥挤 */}
      <div className="grid items-start gap-5 @4xl:grid-cols-[372px_minmax(0,1fr)] @6xl:grid-cols-[400px_minmax(0,1fr)]">
        {/* 左侧取号配置面板 */}
        <section className="surface-card rounded-2xl">
          <div className="flex items-center justify-between rounded-t-2xl border-b border-slate-200/75 bg-slate-50/80 px-5 py-3.5">
            <div>
              <h2 className="text-sm font-bold text-slate-900">快捷取号台</h2>
              <p className="mt-0.5 text-xs text-slate-500">选定业务与国家一键取号，支持多单并行</p>
            </div>
            <span className="font-mono-code rounded-full bg-emerald-50 px-2.5 py-0.5 text-[11px] font-semibold text-emerald-700 ring-1 ring-inset ring-emerald-200">
              {activeProviderLabel}
            </span>
          </div>

          <div className="space-y-4 p-5">
            {/* 步骤 1：业务服务选择 */}
            <div>
              <div className="mb-2 flex items-center justify-between">
                <label className="inline-flex items-center gap-1.5 text-xs font-semibold text-slate-800">
                  <span className="font-mono-code flex h-4 w-4 items-center justify-center rounded-full bg-slate-900 text-[10px] font-bold text-white">
                    1
                  </span>
                  目标业务
                </label>
                {supportsCustomService ? (
                  <button
                    type="button"
                    onClick={() => setUseCustomService((v) => !v)}
                    className="cursor-pointer text-xs font-medium text-indigo-600 transition hover:text-indigo-700"
                  >
                    {effectiveUseCustom ? "返回常用服务" : "自定义服务码"}
                  </button>
                ) : null}
              </div>

              {effectiveUseCustom ? (
                <input
                  type="text"
                  value={customService}
                  onChange={(e) => setCustomService(e.target.value)}
                  placeholder="输入原生服务码，如 wa / tg / go / wx / ot"
                  className="input-surface font-mono-code w-full rounded-xl px-3.5 py-2.5 text-sm"
                />
              ) : (
                <div className="space-y-2">
                  <div className="flex flex-wrap gap-1.5">
                    {(popularServices.length
                      ? popularServices
                      : [
                          { id: "whatsapp", label: "WhatsApp", code: "wa", popular: true },
                          { id: "telegram", label: "Telegram", code: "tg", popular: true },
                          { id: "google", label: "Google / Gmail", code: "go", popular: true },
                          { id: "apple", label: "Apple / Apple ID", code: "wx", popular: true },
                          { id: "openai", label: "OpenAI / ChatGPT", code: "dr", popular: true },
                          { id: "discord", label: "Discord", code: "ds", popular: true },
                        ]
                    ).map((svc) => {
                      const active = selectedService === svc.id;
                      return (
                        <button
                          key={svc.id}
                          type="button"
                          onClick={() => setSelectedService(svc.id)}
                          className={`cursor-pointer rounded-xl border px-3 py-1.5 text-xs font-medium transition ${
                            active
                              ? "border-emerald-600 bg-emerald-600 text-white shadow-xs"
                              : "border-slate-200/95 bg-white text-slate-700 shadow-2xs hover:border-slate-300 hover:bg-slate-50"
                          }`}
                        >
                          {svc.label}
                        </button>
                      );
                    })}
                  </div>

                  {otherServiceOptions.length > 0 ? (
                    <CustomSelect
                      value={isOtherServiceSelected ? selectedService : ""}
                      onChange={(val) => {
                        if (val) setSelectedService(val);
                      }}
                      options={otherServiceOptions}
                      placeholder={`更多常用服务（Facebook / TikTok / Apple / 通用 ot 等 ${otherServiceOptions.length} 项）`}
                      activeHighlight={isOtherServiceSelected}
                      footerAction={
                        supportsCustomService
                          ? {
                              label: "找不到？直接输入自定义服务码 →",
                              onClick: () => setUseCustomService(true),
                            }
                          : undefined
                      }
                    />
                  ) : null}
                </div>
              )}
            </div>

            {/* 步骤 2：国家选择 */}
            <div>
              <label className="mb-2 inline-flex items-center gap-1.5 text-xs font-semibold text-slate-800">
                <span className="font-mono-code flex h-4 w-4 items-center justify-center rounded-full bg-slate-900 text-[10px] font-bold text-white">
                  2
                </span>
                国家 / 地区
              </label>
              <CountryCombobox
                countries={countries}
                selected={selectedCountry}
                provider={activeProvider}
                onSelect={(c) => {
                  countryByProviderRef.current[activeProvider] = String(c.id);
                  setSelectedCountry(c);
                }}
              />
            </div>

            {/* 步骤 3：实时价格档位矩阵 */}
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

            {/* 可折叠高级价格与抢号策略（折叠时仍显示当前激活策略胶囊） */}
            <div className="rounded-xl border border-slate-200/90 bg-slate-50/40">
              <button
                type="button"
                onClick={() => setShowAdvanced((v) => !v)}
                className="flex w-full cursor-pointer items-center justify-between gap-2 rounded-xl px-3.5 py-2.5 text-xs font-medium text-slate-700 transition hover:bg-slate-100/60"
              >
                <span className="flex min-w-0 flex-wrap items-center gap-1.5">
                  <SlidersHorizontal className="h-3.5 w-3.5 shrink-0 text-slate-500" />
                  <span className="font-semibold text-slate-700">高级策略</span>
                  {!showAdvanced && activeStrategyBadges.length > 0 ? (
                    activeStrategyBadges.map((b) => (
                      <span
                        key={b}
                        className="rounded-md bg-emerald-50 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-700 ring-1 ring-inset ring-emerald-200"
                      >
                        {b}
                      </span>
                    ))
                  ) : !showAdvanced ? (
                    <span className="text-[11px] font-normal text-slate-400">限价 / 持续抢号 / 备选国家</span>
                  ) : null}
                </span>
                <ChevronDown
                  className={`h-4 w-4 shrink-0 text-slate-400 transition-transform ${showAdvanced ? "rotate-180" : ""}`}
                />
              </button>

              {showAdvanced ? (
                <div className="space-y-3 border-t border-slate-200/75 bg-white p-3.5 rounded-b-xl text-xs">
                  <div className="grid grid-cols-2 gap-2.5">
                    <div>
                      <div className="mb-1 flex items-center justify-between gap-1">
                        <label className="font-medium text-slate-600">目标/最高价</label>
                        {resolvedPrice.source === "tier" ? (
                          <span className="rounded bg-emerald-50 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-700">
                            价档绑定
                          </span>
                        ) : resolvedPrice.source === "manual" ? (
                          <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-600">
                            手动上限
                          </span>
                        ) : null}
                      </div>
                      <div className="relative">
                        <span className="font-mono-code pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-xs text-slate-400">
                          $
                        </span>
                        <input
                          type="number"
                          step="0.01"
                          min="0.0001"
                          value={resolvedPrice.displayValue}
                          onChange={(e) => {
                            const val = e.target.value;
                            setPriceSel((prev) => setManualPrice(prev, val));
                            if (!val.trim()) setPriceFloatEnabled(false);
                          }}
                          placeholder="留空按市价"
                          className="input-surface font-mono-code w-full rounded-lg py-1.5 pl-6 pr-2.5 text-xs"
                        />
                      </div>
                    </div>
                    <div>
                      <label className="mb-1 block font-medium text-slate-600">等码超时</label>
                      <div className="relative">
                        <input
                          type="number"
                          min="30"
                          max="1200"
                          value={waitTimeout}
                          onChange={(e) => setWaitTimeout(e.target.value)}
                          className="input-surface font-mono-code w-full rounded-lg py-1.5 pl-2.5 pr-7 text-xs"
                        />
                        <span className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-[11px] text-slate-400">
                          秒
                        </span>
                      </div>
                    </div>
                  </div>

                  <div className="surface-well space-y-2.5 rounded-xl p-3">
                    <ToggleSwitch
                      checked={priceLock}
                      onChange={(checked) => {
                        setPriceSel((prev) => ({ ...prev, priceLock: checked }));
                        if (checked) setPriceFloatEnabled(false);
                      }}
                      label="检查目标价档库存并锁定上限 (maxPrice)"
                    />
                    <ToggleSwitch
                      checked={priceFloatEnabled}
                      disabled={priceLock}
                      onChange={setPriceFloatEnabled}
                      label="目标价无号时允许按比例上浮"
                    />
                    {priceFloatEnabled && !priceLock ? (
                      <div className="flex items-center justify-between gap-2 pt-1">
                        <span className="text-slate-600">允许上浮比例</span>
                        <div className="relative w-28">
                          <input
                            type="number"
                            min="0"
                            max="1000"
                            value={priceFloatPercent}
                            onChange={(e) => setPriceFloatPercent(e.target.value)}
                            className="input-surface font-mono-code w-full rounded-lg py-1 pl-2.5 pr-6 text-xs"
                          />
                          <span className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-[11px] text-slate-400">
                            %
                          </span>
                        </div>
                      </div>
                    ) : null}
                  </div>

                  <div className="grid grid-cols-2 gap-2.5">
                    <div>
                      <label className="mb-1 block font-medium text-slate-600">最多尝试次数</label>
                      <div className="relative">
                        <input
                          type="number"
                          min="1"
                          max="100"
                          disabled={continuousPoll}
                          value={maxAttempts}
                          onChange={(e) => setMaxAttempts(e.target.value)}
                          className="input-surface font-mono-code w-full rounded-lg py-1.5 pl-2.5 pr-7 text-xs disabled:bg-slate-100 disabled:text-slate-400"
                        />
                        <span className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-[11px] text-slate-400">
                          次
                        </span>
                      </div>
                    </div>
                    <div>
                      <label className="mb-1 block font-medium text-slate-600">重试间隔</label>
                      <div className="relative">
                        <input
                          type="number"
                          step="0.5"
                          min="0.1"
                          value={requestInterval}
                          onChange={(e) => setRequestInterval(e.target.value)}
                          className="input-surface font-mono-code w-full rounded-lg py-1.5 pl-2.5 pr-7 text-xs"
                        />
                        <span className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-[11px] text-slate-400">
                          秒
                        </span>
                      </div>
                    </div>
                  </div>

                  <div className="surface-well space-y-2.5 rounded-xl p-3">
                    <ToggleSwitch
                      checked={continuousPoll}
                      onChange={setContinuousPoll}
                      label="持续轮询抢号（后台异步蹲号）"
                    />
                    <ToggleSwitch
                      checked={skipBlacklisted}
                      onChange={setSkipBlacklisted}
                      label="命中黑名单号码时自动退款跳过"
                    />
                  </div>

                  <div>
                    <label className="mb-1 block font-medium text-slate-600">备选国家（逗号分隔，主国家无号自动顺延）</label>
                    <input
                      type="text"
                      value={fallbackCountries}
                      onChange={(e) => setFallbackCountries(e.target.value)}
                      placeholder="如 Indonesia, South Africa, 4"
                      className="input-surface w-full rounded-lg px-2.5 py-1.5 text-xs"
                    />
                  </div>
                </div>
              ) : null}
            </div>

            {/* 上次提交结果不确定时的原单查询与新建订单引导 */}
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
              className="flex w-full cursor-pointer items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-emerald-600 to-teal-600 px-4 py-3 text-sm font-semibold text-white shadow-[0_4px_14px_-3px_rgba(16,185,129,0.45)] transition hover:from-emerald-700 hover:to-teal-700 active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-50"
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
        <section className="min-w-0 space-y-3.5">
          <div className="surface-card flex flex-wrap items-center justify-between gap-2 rounded-2xl px-4 py-3">
            <div className="flex items-center gap-2">
              <h2 className="text-sm font-bold text-slate-900">实时接码看板</h2>
              <span className="font-mono-code rounded-full bg-slate-900 px-2 py-0.5 text-xs font-semibold text-white">
                {filteredActiveOrders.length}
              </span>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <div className="inline-flex rounded-xl border border-slate-200/90 bg-slate-50 p-0.5 text-xs">
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
                        ? "bg-white text-slate-900 shadow-2xs ring-1 ring-slate-200/80"
                        : "text-slate-500 hover:text-slate-900"
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
            <div className="surface-card flex flex-col items-center justify-center rounded-2xl px-6 py-16 text-center">
              <div className="rounded-2xl bg-slate-100 p-3.5 text-slate-400 ring-1 ring-inset ring-slate-200/80">
                <Smartphone className="h-7 w-7" />
              </div>
              <h3 className="mt-4 text-sm font-semibold text-slate-800">当前没有进行中的接码订单</h3>
              <p className="mt-1.5 max-w-md text-xs leading-relaxed text-slate-500">
                在左侧选择目标业务与国家点击「立即取号接码」，或由外部项目调用{" "}
                <code className="font-mono-code rounded bg-slate-100 px-1.5 py-0.5 text-slate-700">
                  POST /api/v1/orders
                </code>{" "}
                后，号码与验证码都会实时显示在这里。
              </p>
            </div>
          ) : (
            <div className="space-y-3.5">
              {filteredActiveOrders.map((order) => (
                <ActiveOrderCard
                  key={order.order_id}
                  order={order}
                  busyAction={busyByOrder[order.order_id] || null}
                  supportsResend={
                    providerCapsById[order.provider]?.supports_resend ??
                    (order.provider === "smsbower" || order.provider === "herosms")
                  }
                  onExpireCheck={(id) => {
                    api
                      .getOrder(id)
                      .then((latest) => {
                        if (mountedRef.current) onOrderMutated(latest);
                      })
                      .catch(() => {});
                  }}
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
