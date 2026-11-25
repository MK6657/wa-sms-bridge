import { useCallback, useEffect, useState } from "react";
import {
  CheckCircle2,
  Database,
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
import { useToast } from "../../components/ToastProvider";
import { formatMoney } from "../../utils/format";

interface SettingsPageProps {
  onSettingsUpdated: () => void;
}

export function SettingsPage({ onSettingsUpdated }: SettingsPageProps) {
  const { notify } = useToast();
  const [settings, setSettings] = useState<SettingsResponse | null>(null);
  const [sbKey, setSbKey] = useState("");
  const [sbBaseUrl, setSbBaseUrl] = useState("https://smsbower.page/stubs/handler_api.php");
  const [sbDefaultCountry, setSbDefaultCountry] = useState("52");
  const [heroKey, setHeroKey] = useState("");
  const [heroBaseUrl, setHeroBaseUrl] = useState("https://hero-sms.com/stubs/handler_api.php");
  const [fiveSimKey, setFiveSimKey] = useState("");
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
      setLoadingLogs(true);
      try {
        const data = await api.getRecentLogs(160, logLevel);
        setLogs(data);
      } catch (err) {
        if (manual) {
          notify(err instanceof Error ? err.message : "读取诊断日志失败", "error");
        }
      } finally {
        setLoadingLogs(false);
      }
    },
    [logLevel, notify],
  );

  useEffect(() => {
    loadSettings();
    loadLogs(false);
  }, [loadSettings, loadLogs]);

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

  const archivedProviders = (settings?.provider_catalog || []).filter((p) => !p.active);
  const presetRetentions = new Set(["0", "1", "24", "168", "-1"]);

  return (
    <div className="space-y-5">
      {settings && !settings.encryption_active ? (
        <div className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-xs text-rose-800">
          <strong>尚未配置服务端加密主密钥 (BRIDGE_SECRET_KEY)：</strong> 保存平台 API Key 或 Webhook 签名密钥前，请在{" "}
          <code className="font-mono-code rounded bg-rose-100 px-1 py-0.5">.env</code> 中设置{" "}
          <code className="font-mono-code rounded bg-rose-100 px-1 py-0.5">BRIDGE_SECRET_KEY</code> 并重启服务。
        </div>
      ) : null}

      <div className="grid gap-5 lg:grid-cols-2">
        {/* SMSBower 主平台配置 */}
        <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-xs">
          <div className="mb-4 flex items-center justify-between">
            <div>
              <h2 className="text-sm font-semibold text-slate-900">SMSBower 主平台配置</h2>
              <p className="mt-0.5 text-xs text-slate-500">
                API Key 使用 Fernet + PBKDF2 加密后存储于 SQLite
              </p>
            </div>
            <span
              className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-medium ${
                settings?.smsbower_api_key_masked
                  ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                  : "border-amber-200 bg-amber-50 text-amber-700"
              }`}
            >
              <CheckCircle2 className="h-3.5 w-3.5" />
              {settings?.smsbower_api_key_masked ? "已配置" : "待配置"}
            </span>
          </div>

          <form onSubmit={handleSaveProvider} className="space-y-3.5">
            <div>
              <label className="mb-1 block text-xs font-medium text-slate-700">SMSBower API Key</label>
              <input
                type="password"
                value={sbKey}
                onChange={(e) => setSbKey(e.target.value)}
                placeholder={
                  settings?.smsbower_api_key_masked
                    ? `已保存 (${settings.smsbower_api_key_masked})，留空则保持不变`
                    : "请输入 SMSBower API Key"
                }
                className="font-mono-code w-full rounded-xl border border-slate-200 px-3 py-2 text-xs outline-none focus:border-emerald-500"
              />
            </div>

            <div>
              <label className="mb-1 block text-xs font-medium text-slate-700">API Base URL</label>
              <input
                type="url"
                value={sbBaseUrl}
                onChange={(e) => setSbBaseUrl(e.target.value)}
                className="font-mono-code w-full rounded-xl border border-slate-200 px-3 py-2 text-xs outline-none focus:border-emerald-500"
              />
            </div>

            <div>
              <label className="mb-1 block text-xs font-medium text-slate-700">默认国家 ID（如泰国 52、印尼 6、南非 31）</label>
              <input
                type="number"
                min="0"
                value={sbDefaultCountry}
                onChange={(e) => setSbDefaultCountry(e.target.value)}
                className="font-mono-code w-full rounded-xl border border-slate-200 px-3 py-2 text-xs outline-none focus:border-emerald-500"
              />
            </div>

            {settings?.active_providers.includes("herosms") ? (
              <div className="space-y-2 border-t border-slate-100 pt-3">
                <label className="block text-xs font-medium text-slate-700">HeroSMS API Key（已启用备用平台）</label>
                <input
                  type="password"
                  value={heroKey}
                  onChange={(e) => setHeroKey(e.target.value)}
                  placeholder={
                    settings.hero_sms_api_key_masked
                      ? `已保存 (${settings.hero_sms_api_key_masked})，留空保持不变`
                      : "输入 HeroSMS API Key"
                  }
                  className="font-mono-code w-full rounded-xl border border-slate-200 px-3 py-2 text-xs outline-none focus:border-emerald-500"
                />
              </div>
            ) : null}

            {settings?.active_providers.includes("5sim") ? (
              <div className="space-y-2 border-t border-slate-100 pt-3">
                <label className="block text-xs font-medium text-slate-700">5sim Bearer API Key（已启用备用平台）</label>
                <input
                  type="password"
                  value={fiveSimKey}
                  onChange={(e) => setFiveSimKey(e.target.value)}
                  placeholder={
                    settings.five_sim_api_key_masked
                      ? `已保存 (${settings.five_sim_api_key_masked})，留空保持不变`
                      : "输入 5sim JWT Token"
                  }
                  className="font-mono-code w-full rounded-xl border border-slate-200 px-3 py-2 text-xs outline-none focus:border-emerald-500"
                />
              </div>
            ) : null}

            <div className="flex flex-wrap items-center gap-2 pt-1">
              <button
                type="submit"
                disabled={savingProvider}
                className="inline-flex cursor-pointer items-center gap-1.5 rounded-xl bg-emerald-600 px-4 py-2 text-xs font-semibold text-white transition hover:bg-emerald-700 disabled:opacity-50"
              >
                <Save className="h-3.5 w-3.5" />
                {savingProvider ? "保存中..." : "保存配置"}
              </button>
              <button
                type="button"
                onClick={handleTestConnection}
                disabled={testingConn}
                className="inline-flex cursor-pointer items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3.5 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
              >
                <RefreshCw className={`h-3.5 w-3.5 ${testingConn ? "animate-spin" : ""}`} />
                测试连通查余额
              </button>
              <button
                type="button"
                onClick={handleSyncCountries}
                disabled={syncingCountries}
                className="inline-flex cursor-pointer items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3.5 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
              >
                同步国家列表
              </button>
            </div>
          </form>
        </section>

        {/* 验证码留存与脱敏策略 */}
        <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-xs">
          <div className="mb-4 flex items-center gap-2">
            <ShieldAlert className="h-4 w-4 text-indigo-600" />
            <div>
              <h2 className="text-sm font-semibold text-slate-900">验证码安全与留存策略</h2>
              <p className="text-xs text-slate-500">
                活跃接码订单实时明文展示；订单结束后按此策略自动加密留存或定时脱敏清理
              </p>
            </div>
          </div>

          <div className="space-y-4">
            <div>
              <label className="mb-1.5 block text-xs font-medium text-slate-700">已结束订单验证码保留时长</label>
              <select
                value={retentionHours}
                onChange={(e) => setRetentionHours(e.target.value)}
                className="w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs text-slate-800 outline-none focus:border-emerald-500"
              >
                <option value="0">订单关闭后立即脱敏抹除（最安全）</option>
                <option value="1">保留 1 小时后自动清理</option>
                <option value="24">保留 24 小时后自动清理（推荐）</option>
                <option value="168">保留 7 天后自动清理</option>
                <option value="-1">永久保留（落盘 Fernet 加密）</option>
                {!presetRetentions.has(retentionHours) ? (
                  <option value={retentionHours}>当前自定义保留 {retentionHours} 小时</option>
                ) : null}
              </select>
            </div>

            <label className="flex cursor-pointer items-start gap-2.5 rounded-xl border border-slate-200 bg-slate-50/70 p-3 text-xs">
              <input
                type="checkbox"
                checked={maskInHistory}
                onChange={(e) => setMaskInHistory(e.target.checked)}
                className="mt-0.5 rounded border-slate-300 text-emerald-600"
              />
              <div>
                <div className="font-medium text-slate-800">历史列表默认掩码显示验证码</div>
                <div className="mt-0.5 text-slate-500">
                  开启后在《订单记录》中默认显示为 <span className="font-mono-code">48••••</span>，未过期前可点击眼睛图标临时查看。
                </div>
              </div>
            </label>

            <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
              <button
                type="button"
                onClick={handleSaveRetention}
                disabled={savingRetention}
                className="inline-flex cursor-pointer items-center gap-1.5 rounded-xl bg-slate-900 px-4 py-2 text-xs font-semibold text-white hover:bg-slate-800 disabled:opacity-50"
              >
                <Save className="h-3.5 w-3.5" />
                {savingRetention ? "保存中..." : "保存策略"}
              </button>

              <button
                type="button"
                onClick={() => setScrubModalOpen(true)}
                className="inline-flex cursor-pointer items-center gap-1.5 rounded-xl border border-rose-200 bg-rose-50 px-3.5 py-2 text-xs font-medium text-rose-700 hover:bg-rose-100"
              >
                <Trash2 className="h-3.5 w-3.5" />
                立即清理全部历史验证码
              </button>
            </div>
          </div>
        </section>
      </div>

      {/* 归档平台与可选插件面板 */}
      <div className="grid gap-5 lg:grid-cols-2">
        <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-2xs">
          <div className="mb-3 flex items-center gap-2">
            <Database className="h-4 w-4 text-slate-500" />
            <h3 className="text-sm font-semibold text-slate-900">已保留待测平台（默认休眠）</h3>
          </div>
          <p className="mb-3 text-xs text-slate-500">
            原有平台代码已完整保留，未启用时不占连接与内存；日后如需测试，在环境变量中设置{" "}
            <code className="font-mono-code rounded bg-slate-100 px-1.5 py-0.5 text-slate-700">
              ACTIVE_PROVIDERS=smsbower,herosms,5sim
            </code>
            。
          </p>
          <div className="space-y-2">
            {archivedProviders.map((p) => (
              <div
                key={p.id}
                className="flex items-center justify-between rounded-xl border border-slate-100 bg-slate-50/70 px-3.5 py-2.5 text-xs"
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

        <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-2xs">
          <div className="mb-3 flex items-center gap-2">
            <Plug className="h-4 w-4 text-slate-500" />
            <h3 className="text-sm font-semibold text-slate-900">可选内置业务插件</h3>
          </div>
          <p className="mb-3 text-xs text-slate-500">
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
                className="flex items-center justify-between rounded-xl border border-slate-100 bg-slate-50/70 px-3.5 py-2.5 text-xs"
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

      {/* 脱敏服务端日志查看器 */}
      <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-xs">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <Terminal className="h-4 w-4 text-slate-600" />
            <h3 className="text-sm font-semibold text-slate-900">服务端诊断日志（已自动脱敏密钥、手机号与验证码）</h3>
          </div>
          <div className="flex items-center gap-2">
            <select
              value={logLevel}
              onChange={(e) => setLogLevel(e.target.value)}
              className="rounded-xl border border-slate-200 bg-white px-2.5 py-1.5 text-xs text-slate-700 outline-none"
            >
              <option value="">全部级别</option>
              <option value="DEBUG">DEBUG</option>
              <option value="INFO">INFO</option>
              <option value="WARNING">WARNING</option>
              <option value="ERROR">ERROR</option>
              <option value="CRITICAL">CRITICAL</option>
            </select>
            <button
              type="button"
              onClick={() => loadLogs(true)}
              disabled={loadingLogs}
              className="inline-flex cursor-pointer items-center gap-1 rounded-xl border border-slate-200 px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${loadingLogs ? "animate-spin" : ""}`} />
              刷新日志
            </button>
          </div>
        </div>

        <pre className="font-mono-code max-h-72 overflow-auto rounded-xl bg-slate-950 p-3.5 text-[11px] leading-relaxed text-slate-200">
          {logs && logs.entries.length > 0
            ? logs.entries.join("\n")
            : "暂无可显示的本地日志文件条目。"}
        </pre>
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
