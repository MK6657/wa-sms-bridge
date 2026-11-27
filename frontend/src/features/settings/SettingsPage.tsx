import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CheckCircle2,
  Database,
  Eye,
  EyeOff,
  Plug,
  RefreshCw,
  Save,
  ShieldAlert,
  Terminal,
  Trash2,
} from "lucide-react";
import { api } from "../../api/client";
import type { LogsResponse, SettingsResponse } from "../../api/types";
import { ConfirmModal } from "../../components/ConfirmModal";
import { CopyButton } from "../../components/CopyButton";
import { CustomSelect, type SelectOption } from "../../components/CustomSelect";
import { useToast } from "../../components/ToastProvider";
import { ToggleSwitch } from "../../components/ToggleSwitch";
import { formatMoney } from "../../utils/format";

interface SettingsPageProps {
  onSettingsUpdated: () => void;
}

const DEFAULT_COUNTRY_PRESETS = [
  { id: "52", label: "泰国 #52" },
  { id: "6", label: "印尼 #6" },
  { id: "31", label: "南非 #31" },
  { id: "4", label: "菲律宾 #4" },
  { id: "187", label: "美国 #187" },
  { id: "16", label: "英国 #16" },
];

export function SettingsPage({ onSettingsUpdated }: SettingsPageProps) {
  const { notify } = useToast();
  const [settings, setSettings] = useState<SettingsResponse | null>(null);
  const [sbKey, setSbKey] = useState("");
  const [showSbKey, setShowSbKey] = useState(false);
  const [sbBaseUrl, setSbBaseUrl] = useState("https://smsbower.page/stubs/handler_api.php");
  const [sbDefaultCountry, setSbDefaultCountry] = useState("52");
  const [heroKey, setHeroKey] = useState("");
  const [showHeroKey, setShowHeroKey] = useState(false);
  const [heroBaseUrl, setHeroBaseUrl] = useState("https://hero-sms.com/stubs/handler_api.php");
  const [fiveSimKey, setFiveSimKey] = useState("");
  const [showFiveSimKey, setShowFiveSimKey] = useState(false);
  const [retentionHours, setRetentionHours] = useState("24");
  const [maskInHistory, setMaskInHistory] = useState(true);

  const [savingProvider, setSavingProvider] = useState(false);
  const [testingConn, setTestingConn] = useState(false);
  const [syncingCountries, setSyncingCountries] = useState(false);
  const [savingRetention, setSavingRetention] = useState(false);
  const [scrubModalOpen, setScrubModalOpen] = useState(false);
  const [scrubbing, setScrubbing] = useState(false);

  const [logs, setLogs] = useState<LogsResponse | null>(null);
  const [logLevel, setLogLevel] = useState("");
  const [loadingLogs, setLoadingLogs] = useState(false);
  const logReqSeqRef = useRef(0);

  const loadSettings = useCallback(async () => {
    try {
      const data = await api.getSettings();
      setSettings(data);
      setSbBaseUrl(data.smsbower_base_url);
      setSbDefaultCountry(String(data.smsbower_default_country));
      if (data.hero_sms_base_url) setHeroBaseUrl(data.hero_sms_base_url);
      setRetentionHours(String(data.sms_code_retention_hours));
      setMaskInHistory(data.sms_code_mask_in_history);
    } catch (err) {
      notify(err instanceof Error ? err.message : "加载系统配置失败", "error");
    }
  }, [notify]);

  const loadLogs = useCallback(
    async (manual = false) => {
      const seq = ++logReqSeqRef.current;
      setLoadingLogs(true);
      try {
        const data = await api.getRecentLogs(160, logLevel);
        if (seq === logReqSeqRef.current) {
          setLogs(data);
        }
      } catch (err) {
        if (manual && seq === logReqSeqRef.current) {
          notify(err instanceof Error ? err.message : "读取诊断日志失败", "error");
        }
      } finally {
        if (seq === logReqSeqRef.current) {
          setLoadingLogs(false);
        }
      }
    },
    [logLevel, notify],
  );

  useEffect(() => {
    loadSettings();
  }, [loadSettings]);

  useEffect(() => {
    loadLogs(false);
  }, [loadLogs]);

  const handleSaveProvider = async (e: React.FormEvent) => {
    e.preventDefault();
    setSavingProvider(true);
    try {
      const parsedDefaultCountry = sbDefaultCountry.trim() === "" ? 52 : Number(sbDefaultCountry.trim());
      const body: Record<string, unknown> = {
        smsbower_base_url: sbBaseUrl.trim(),
        smsbower_default_country:
          Number.isInteger(parsedDefaultCountry) && parsedDefaultCountry >= 0 ? parsedDefaultCountry : 52,
      };
      if (sbKey.trim()) body.smsbower_api_key = sbKey.trim();
      if (heroKey.trim()) body.hero_sms_api_key = heroKey.trim();
      if (settings?.active_providers.includes("herosms") && heroBaseUrl.trim()) {
        body.hero_sms_base_url = heroBaseUrl.trim();
      }
      if (fiveSimKey.trim()) body.five_sim_api_key = fiveSimKey.trim();
      await api.updateSettings(body);
      setSbKey("");
      setHeroKey("");
      setFiveSimKey("");
      notify("平台配置已加密保存", "success");
      await loadSettings();
      onSettingsUpdated();
    } catch (err) {
      notify(err instanceof Error ? err.message : "保存失败", "error");
    } finally {
      setSavingProvider(false);
    }
  };

  const handleTestConnection = async () => {
    if (sbKey.trim() || heroKey.trim() || fiveSimKey.trim()) {
      notify("检测到输入框中有尚未保存的新 API Key，请先点击「保存配置」再测试连通", "warn");
      return;
    }
    const prov = settings?.default_provider || "smsbower";
    setTestingConn(true);
    try {
      const res = await api.getBalance(prov);
      notify(`${prov} 连通正常！当前余额 ${formatMoney(res.balance, res.currency || "USD")}`, "success");
      onSettingsUpdated();
    } catch (err) {
      notify(err instanceof Error ? err.message : "连接测试失败", "error");
    } finally {
      setTestingConn(false);
    }
  };

  const handleSyncCountries = async () => {
    const prov = settings?.default_provider || "smsbower";
    setSyncingCountries(true);
    try {
      const res = await api.refreshCountries(prov);
      notify(`${prov} 国家索引已同步，共 ${res.count} 个国家`, "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "同步国家失败", "error");
    } finally {
      setSyncingCountries(false);
    }
  };

  const handleSaveRetention = async () => {
    setSavingRetention(true);
    try {
      await api.updateSettings({
        sms_code_retention_hours: Number(retentionHours),
        sms_code_mask_in_history: maskInHistory,
      });
      notify("验证码留存策略已保存", "success");
      await loadSettings();
      onSettingsUpdated();
    } catch (err) {
      notify(err instanceof Error ? err.message : "保存失败", "error");
    } finally {
      setSavingRetention(false);
    }
  };

  const handleScrubAll = async () => {
    setScrubbing(true);
    try {
      const res = await api.scrubAllHistoricalCodes();
      notify(`已清理 ${res.scrubbed_orders} 条历史订单的验证码明文`, "success");
      setScrubModalOpen(false);
      onSettingsUpdated();
    } catch (err) {
      notify(err instanceof Error ? err.message : "清理失败", "error");
    } finally {
      setScrubbing(false);
    }
  };

  const retentionOptions: SelectOption[] = useMemo(() => {
    const base: SelectOption[] = [
      {
        value: "0",
        label: "订单关闭后立即脱敏抹除",
        description: "完成、取消或过期瞬间同步清除数据库、Outbox 与 SSE 中的验证码",
        badge: "最安全",
        badgeTone: "teal",
      },
      {
        value: "1",
        label: "保留 1 小时后自动清理",
        description: "适合短期集中调试，超期自动擦除明文",
        badge: "1h",
        badgeTone: "slate",
      },
      {
        value: "24",
        label: "保留 24 小时后自动清理",
        description: "兼顾当日回溯排查与自动隐私清理",
        badge: "推荐",
        badgeTone: "emerald",
      },
      {
        value: "168",
        label: "保留 7 天后自动清理",
        description: "按周滚动清理历史订单验证码",
        badge: "7d",
        badgeTone: "slate",
      },
      {
        value: "-1",
        label: "永久保留（Fernet 加密落盘）",
        description: "仅在手动点击脱敏清理时抹除",
        badge: "加密存储",
        badgeTone: "indigo",
      },
    ];
    if (!base.some((o) => o.value === retentionHours)) {
      base.push({
        value: retentionHours,
        label: `自定义保留 ${retentionHours} 小时`,
        badge: `${retentionHours}h`,
        badgeTone: "amber",
      });
    }
    return base;
  }, [retentionHours]);

  const logLevelOptions: SelectOption[] = useMemo(() => {
    const counts = logs?.counts || {};
    return [
      { value: "", label: "全部日志级别" },
      {
        value: "DEBUG",
        label: "DEBUG",
        dotColor: "bg-slate-400",
        badge: counts.DEBUG ? String(counts.DEBUG) : undefined,
        badgeTone: "slate",
      },
      {
        value: "INFO",
        label: "INFO",
        dotColor: "bg-sky-500",
        badge: counts.INFO ? String(counts.INFO) : undefined,
        badgeTone: "slate",
      },
      {
        value: "WARNING",
        label: "WARNING",
        dotColor: "bg-amber-500",
        badge: counts.WARNING ? String(counts.WARNING) : undefined,
        badgeTone: "amber",
      },
      {
        value: "ERROR",
        label: "ERROR",
        dotColor: "bg-rose-500",
        badge: counts.ERROR ? String(counts.ERROR) : undefined,
        badgeTone: "rose",
      },
      {
        value: "CRITICAL",
        label: "CRITICAL",
        dotColor: "bg-purple-500",
        badge: counts.CRITICAL ? String(counts.CRITICAL) : undefined,
        badgeTone: "rose",
      },
    ];
  }, [logs?.counts]);

  const retentionDirty =
    settings !== null &&
    (String(settings.sms_code_retention_hours) !== retentionHours || settings.sms_code_mask_in_history !== maskInHistory);

  const archivedProviders = (settings?.provider_catalog || []).filter((p) => !p.active);

  return (
    <div className="space-y-5">
      {settings && !settings.encryption_active ? (
        <div className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-xs text-rose-800 shadow-2xs">
          <strong>尚未配置服务端加密主密钥 (BRIDGE_SECRET_KEY)：</strong> 保存平台 API Key 或 Webhook 签名密钥前，请在{" "}
          <code className="font-mono-code rounded bg-rose-100 px-1 py-0.5">.env</code> 中设置{" "}
          <code className="font-mono-code rounded bg-rose-100 px-1 py-0.5">BRIDGE_SECRET_KEY</code> 并重启服务。
        </div>
      ) : null}

      <div className="grid gap-5 @4xl:grid-cols-2">
        {/* SMSBower 主平台配置 */}
        <section className="surface-card rounded-2xl">
          <div className="flex items-center justify-between rounded-t-2xl border-b border-slate-200/75 bg-slate-50/75 px-5 py-3.5">
            <div>
              <h2 className="text-sm font-bold text-slate-900">SMSBower 主平台配置</h2>
              <p className="mt-0.5 text-xs text-slate-500">
                API Key 使用 Fernet + PBKDF2 加密后落盘于本地 SQLite
              </p>
            </div>
            <span
              className={`inline-flex shrink-0 items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-medium ${
                settings?.smsbower_api_key_masked
                  ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                  : "border-amber-200 bg-amber-50 text-amber-700"
              }`}
            >
              <CheckCircle2 className="h-3.5 w-3.5" />
              {settings?.smsbower_api_key_masked ? "已配置" : "待配置"}
            </span>
          </div>

          <form onSubmit={handleSaveProvider} className="space-y-4 p-5">
            <div>
              <label className="mb-1.5 block text-xs font-medium text-slate-700">SMSBower API Key</label>
              <div className="relative">
                <input
                  type={showSbKey ? "text" : "password"}
                  value={sbKey}
                  onChange={(e) => setSbKey(e.target.value)}
                  placeholder={
                    settings?.smsbower_api_key_masked
                      ? `已保存 (${settings.smsbower_api_key_masked})，留空则保持不变`
                      : "请输入 SMSBower API Key"
                  }
                  className="input-surface font-mono-code w-full rounded-xl py-2 pl-3 pr-9 text-xs"
                />
                <button
                  type="button"
                  onClick={() => setShowSbKey((v) => !v)}
                  className="absolute right-2.5 top-1/2 -translate-y-1/2 cursor-pointer rounded p-0.5 text-slate-400 hover:text-slate-700"
                  title={showSbKey ? "隐藏输入" : "查看输入"}
                >
                  {showSbKey ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                </button>
              </div>
            </div>

            <div>
              <label className="mb-1.5 block text-xs font-medium text-slate-700">API Base URL</label>
              <input
                type="url"
                value={sbBaseUrl}
                onChange={(e) => setSbBaseUrl(e.target.value)}
                className="input-surface font-mono-code w-full rounded-xl px-3 py-2 text-xs"
              />
            </div>

            <div>
              <label className="mb-1.5 block text-xs font-medium text-slate-700">默认国家 ID</label>
              <div className="flex flex-wrap items-center gap-2">
                <input
                  type="number"
                  min="0"
                  value={sbDefaultCountry}
                  onChange={(e) => setSbDefaultCountry(e.target.value)}
                  className="input-surface font-mono-code w-24 rounded-xl px-3 py-2 text-xs font-semibold"
                />
                <div className="flex flex-wrap gap-1">
                  {DEFAULT_COUNTRY_PRESETS.map((p) => {
                    const active = sbDefaultCountry === p.id;
                    return (
                      <button
                        key={p.id}
                        type="button"
                        onClick={() => setSbDefaultCountry(p.id)}
                        className={`cursor-pointer rounded-lg border px-2 py-1 text-[11px] font-medium transition ${
                          active
                            ? "border-emerald-500 bg-emerald-50 text-emerald-800"
                            : "border-slate-200 bg-slate-50 text-slate-600 hover:bg-white"
                        }`}
                      >
                        {p.label}
                      </button>
                    );
                  })}
                </div>
              </div>
            </div>

            {settings?.active_providers.includes("herosms") ? (
              <div className="space-y-2.5 border-t border-slate-100 pt-3">
                <div>
                  <label className="mb-1 block text-xs font-medium text-slate-700">HeroSMS API Key（已启用备用平台）</label>
                  <div className="relative">
                    <input
                      type={showHeroKey ? "text" : "password"}
                      value={heroKey}
                      onChange={(e) => setHeroKey(e.target.value)}
                      placeholder={
                        settings.hero_sms_api_key_masked
                          ? `已保存 (${settings.hero_sms_api_key_masked})，留空保持不变`
                          : "输入 HeroSMS API Key"
                      }
                      className="input-surface font-mono-code w-full rounded-xl py-2 pl-3 pr-9 text-xs"
                    />
                    <button
                      type="button"
                      onClick={() => setShowHeroKey((v) => !v)}
                      className="absolute right-2.5 top-1/2 -translate-y-1/2 cursor-pointer rounded p-0.5 text-slate-400 hover:text-slate-700"
                      title={showHeroKey ? "隐藏输入" : "查看输入"}
                    >
                      {showHeroKey ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                    </button>
                  </div>
                </div>
                <div>
                  <label className="mb-1 block text-xs font-medium text-slate-700">HeroSMS Base URL</label>
                  <input
                    type="url"
                    value={heroBaseUrl}
                    onChange={(e) => setHeroBaseUrl(e.target.value)}
                    className="input-surface font-mono-code w-full rounded-xl px-3 py-2 text-xs"
                  />
                </div>
              </div>
            ) : null}

            {settings?.active_providers.includes("5sim") ? (
              <div className="space-y-2 border-t border-slate-100 pt-3">
                <label className="block text-xs font-medium text-slate-700">5sim Bearer API Key（已启用备用平台）</label>
                <div className="relative">
                  <input
                    type={showFiveSimKey ? "text" : "password"}
                    value={fiveSimKey}
                    onChange={(e) => setFiveSimKey(e.target.value)}
                    placeholder={
                      settings.five_sim_api_key_masked
                        ? `已保存 (${settings.five_sim_api_key_masked})，留空保持不变`
                        : "输入 5sim JWT Token"
                    }
                    className="input-surface font-mono-code w-full rounded-xl py-2 pl-3 pr-9 text-xs"
                  />
                  <button
                    type="button"
                    onClick={() => setShowFiveSimKey((v) => !v)}
                    className="absolute right-2.5 top-1/2 -translate-y-1/2 cursor-pointer rounded p-0.5 text-slate-400 hover:text-slate-700"
                    title={showFiveSimKey ? "隐藏输入" : "查看输入"}
                  >
                    {showFiveSimKey ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                  </button>
                </div>
              </div>
            ) : null}

            <div className="flex flex-wrap items-center gap-2 border-t border-slate-100 pt-3">
              <button
                type="submit"
                disabled={savingProvider}
                className="inline-flex cursor-pointer items-center gap-1.5 rounded-xl bg-emerald-600 px-4 py-2 text-xs font-semibold text-white shadow-xs transition hover:bg-emerald-700 disabled:opacity-50"
              >
                <Save className="h-3.5 w-3.5" />
                {savingProvider ? "保存中..." : "保存配置"}
              </button>
              <button
                type="button"
                onClick={handleTestConnection}
                disabled={testingConn}
                className="inline-flex cursor-pointer items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3.5 py-2 text-xs font-medium text-slate-700 shadow-2xs hover:bg-slate-50 disabled:opacity-50"
              >
                <RefreshCw className={`h-3.5 w-3.5 ${testingConn ? "animate-spin text-emerald-600" : ""}`} />
                测试连通查余额
              </button>
              <button
                type="button"
                onClick={handleSyncCountries}
                disabled={syncingCountries}
                className="inline-flex cursor-pointer items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3.5 py-2 text-xs font-medium text-slate-700 shadow-2xs hover:bg-slate-50 disabled:opacity-50"
              >
                同步国家列表
              </button>
            </div>
          </form>
        </section>

        {/* 验证码留存与脱敏策略 */}
        <section className="surface-card flex flex-col justify-between rounded-2xl">
          <div>
            <div className="flex items-center justify-between rounded-t-2xl border-b border-slate-200/75 bg-slate-50/75 px-5 py-3.5">
              <div className="flex items-center gap-2">
                <ShieldAlert className="h-4 w-4 text-indigo-600" />
                <div>
                  <h2 className="text-sm font-bold text-slate-900">验证码安全与留存策略</h2>
                  <p className="mt-0.5 text-xs text-slate-500">
                    活跃订单实时明文展示；订单结束后按策略加密留存或自动脱敏
                  </p>
                </div>
              </div>
              {retentionDirty ? (
                <span className="shrink-0 rounded-full bg-amber-50 px-2 py-0.5 text-[11px] font-medium text-amber-700 ring-1 ring-inset ring-amber-200">
                  未保存更改
                </span>
              ) : null}
            </div>

            <div className="space-y-4 p-5">
              <div>
                <label className="mb-1.5 block text-xs font-medium text-slate-700">已结束订单验证码保留时长</label>
                <CustomSelect
                  value={retentionHours}
                  onChange={setRetentionHours}
                  options={retentionOptions}
                />
              </div>

              <div className="surface-well rounded-xl p-3.5">
                <ToggleSwitch
                  size="md"
                  checked={maskInHistory}
                  onChange={setMaskInHistory}
                  label="历史列表默认掩码显示验证码"
                  description="开启后在《订单与统计》中默认显示为 48••••，未过期前可点击眼睛图标临时查看明文。"
                />
              </div>

              <div>
                <button
                  type="button"
                  onClick={handleSaveRetention}
                  disabled={savingRetention}
                  className="inline-flex cursor-pointer items-center gap-1.5 rounded-xl bg-slate-900 px-4 py-2 text-xs font-semibold text-white shadow-xs transition hover:bg-slate-800 disabled:opacity-50"
                >
                  <Save className="h-3.5 w-3.5" />
                  {savingRetention ? "保存中..." : "保存留存策略"}
                </button>
              </div>
            </div>
          </div>

          {/* 独立危险操作栏 */}
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-b-2xl border-t border-rose-100 bg-rose-50/45 px-5 py-3.5 text-xs">
            <div className="text-slate-600">
              <strong className="font-semibold text-rose-900">全量隐私抹除：</strong> 立即清空所有已结束订单中的历史验证码
            </div>
            <button
              type="button"
              onClick={() => setScrubModalOpen(true)}
              className="inline-flex shrink-0 cursor-pointer items-center gap-1.5 rounded-xl border border-rose-200 bg-white px-3 py-1.5 text-xs font-semibold text-rose-700 shadow-2xs transition hover:bg-rose-50"
            >
              <Trash2 className="h-3.5 w-3.5" />
              立即清理历史验证码
            </button>
          </div>
        </section>
      </div>

      {/* 归档平台与可选插件面板 */}
      <div className="grid gap-5 @4xl:grid-cols-2">
        <section className="surface-card rounded-2xl p-5">
          <div className="mb-2.5 flex items-center gap-2">
            <Database className="h-4 w-4 text-slate-500" />
            <h3 className="text-sm font-bold text-slate-900">已保留待测平台（默认休眠）</h3>
          </div>
          <p className="mb-3 text-xs leading-relaxed text-slate-500">
            原有平台代码已完整保留，未启用时不占连接与内存；如需开启，在环境变量中设置{" "}
            <code className="font-mono-code rounded bg-slate-100 px-1.5 py-0.5 text-slate-700">
              ACTIVE_PROVIDERS=smsbower,herosms,5sim
            </code>
            。
          </p>
          <div className="space-y-2">
            {archivedProviders.map((p) => (
              <div
                key={p.id}
                className="surface-well flex items-center justify-between rounded-xl px-3.5 py-2.5 text-xs"
              >
                <div>
                  <strong className="text-slate-800">{p.label}</strong>
                  <div className="mt-0.5 text-[11px] text-slate-500">{p.note}</div>
                </div>
                <span className="shrink-0 rounded-full bg-slate-200/80 px-2.5 py-0.5 text-[11px] font-medium text-slate-600">
                  未启用
                </span>
              </div>
            ))}
          </div>
        </section>

        <section className="surface-card rounded-2xl p-5">
          <div className="mb-2.5 flex items-center gap-2">
            <Plug className="h-4 w-4 text-slate-500" />
            <h3 className="text-sm font-bold text-slate-900">可选内置业务插件</h3>
          </div>
          <p className="mb-3 text-xs leading-relaxed text-slate-500">
            原有 wa-app 自动注册编排已解耦为独立可选插件，默认关闭以确保接码核心轻量独立；如需开启请设置{" "}
            <code className="font-mono-code rounded bg-slate-100 px-1.5 py-0.5 text-slate-700">
              ENABLE_WA_PLUGIN=true
            </code>
            。
          </p>
          <div className="space-y-2">
            {(settings?.plugins || []).map((pl) => (
              <div
                key={pl.id}
                className="surface-well flex items-center justify-between rounded-xl px-3.5 py-2.5 text-xs"
              >
                <div>
                  <strong className="text-slate-800">{pl.name}</strong>
                  <div className="mt-0.5 text-[11px] text-slate-500">{pl.description}</div>
                </div>
                <span
                  className={`shrink-0 rounded-full px-2.5 py-0.5 text-[11px] font-medium ${
                    pl.started
                      ? "bg-emerald-100 text-emerald-700"
                      : "bg-slate-200/80 text-slate-600"
                  }`}
                >
                  {pl.started ? "运行中" : "已关闭"}
                </span>
              </div>
            ))}
          </div>
        </section>
      </div>

      {/* 脱敏服务端日志查看器（下拉菜单支持自动向上弹出防底部裁切） */}
      <section className="surface-card rounded-2xl">
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-t-2xl border-b border-slate-200/75 bg-slate-50/75 px-5 py-3.5">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <Terminal className="h-4 w-4 text-slate-600" />
              <h3 className="text-sm font-bold text-slate-900">服务端诊断日志（自动脱敏密钥、手机号与验证码）</h3>
            </div>
            {logs?.path ? (
              <div className="font-mono-code mt-0.5 truncate text-[11px] text-slate-400">
                日志文件：{logs.path}
              </div>
            ) : null}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <CustomSelect
              value={logLevel}
              onChange={setLogLevel}
              options={logLevelOptions}
              size="sm"
              align="right"
              activeHighlight={Boolean(logLevel)}
              minMenuWidth={190}
              className="w-40"
            />
            {logs && logs.entries.length > 0 ? (
              <CopyButton value={logs.entries.join("\n")} label="复制日志" size="md" />
            ) : null}
            <button
              type="button"
              onClick={() => loadLogs(true)}
              disabled={loadingLogs}
              className="inline-flex cursor-pointer items-center gap-1 rounded-xl border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 shadow-2xs hover:bg-slate-50"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${loadingLogs ? "animate-spin text-emerald-600" : ""}`} />
              刷新日志
            </button>
          </div>
        </div>

        <div className="p-4">
          <div className="font-mono-code max-h-72 overflow-auto rounded-xl bg-slate-950 p-3.5 text-[11px] leading-relaxed text-slate-200 shadow-inner">
            {logs && logs.entries.length > 0 ? (
              logs.entries.map((line, idx) => {
                const isErr = line.includes("[ERROR]") || line.includes("[CRITICAL]");
                const isWarn = line.includes("[WARNING]");
                return (
                  <div
                    key={idx}
                    className={`whitespace-pre-wrap break-all py-0.5 ${
                      isErr ? "text-rose-300" : isWarn ? "text-amber-300" : "text-slate-200"
                    }`}
                  >
                    {line}
                  </div>
                );
              })
            ) : (
              <div className="text-slate-400">暂无可显示的本地日志文件条目。</div>
            )}
          </div>
        </div>
      </section>

      <ConfirmModal
        open={scrubModalOpen}
        title="确认立即清理历史验证码？"
        description="将清空所有已结束订单中留存的短信验证码明文（进行中的活跃订单不受影响，订单号/费用等账本记录保留）。"
        confirmLabel="立即清理"
        danger
        loading={scrubbing}
        onConfirm={handleScrubAll}
        onCancel={() => setScrubModalOpen(false)}
      />
    </div>
  );
}
