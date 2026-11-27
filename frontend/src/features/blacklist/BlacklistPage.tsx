import { useCallback, useEffect, useMemo, useState } from "react";
import { Ban, Loader2, Plus, RefreshCw, Search, Trash2, X } from "lucide-react";
import { api } from "../../api/client";
import type { BlacklistEntry } from "../../api/types";
import { useToast } from "../../components/ToastProvider";
import { formatDateTime } from "../../utils/format";

interface BlacklistPageProps {
  defaultProvider?: string;
}

function normalizeBlacklistPhoneInput(raw: string): string {
  const withoutExt = raw.replace(/\s*(?:ext\.?|x|#)\s*\d+$/i, "").trim();
  if (!withoutExt) return "";
  const digits = withoutExt.replace(/\D/g, "");
  if (digits.length < 5 || digits.length > 20) return "";
  if (withoutExt.startsWith("+")) return `+${digits}`;
  if (withoutExt.startsWith("00") && digits.length >= 7) return `+${digits.slice(2)}`;
  return digits;
}

export function BlacklistPage({ defaultProvider = "smsbower" }: BlacklistPageProps) {
  const { notify } = useToast();
  const [entries, setEntries] = useState<BlacklistEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [search, setSearch] = useState("");
  const [phonesText, setPhonesText] = useState("");
  const [reason, setReason] = useState("手动加入");
  const [submitting, setSubmitting] = useState(false);
  const [removingId, setRemovingId] = useState<number | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.listBlacklist("");
      setEntries(res.entries);
    } catch (err) {
      notify(err instanceof Error ? err.message : "加载黑名单失败", "error");
    } finally {
      setLoading(false);
    }
  }, [notify]);

  useEffect(() => {
    load();
  }, [load]);

  const parsedPhones = useMemo(
    () =>
      Array.from(
        new Set(
          phonesText
            .split(/[\r\n,;，]+/)
            .map(normalizeBlacklistPhoneInput)
            .filter(Boolean),
        ),
      ),
    [phonesText],
  );

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    if (parsedPhones.length === 0) {
      notify("请输入至少一个有效手机号（至少 5 位数字）", "warn");
      return;
    }
    setSubmitting(true);
    let totalAdded = 0;
    try {
      if (parsedPhones.length === 1) {
        const res = await api.addBlacklist(parsedPhones[0], reason.trim() || "手动加入", defaultProvider);
        if (res.added === 0) {
          notify(`号码 ${parsedPhones[0]} 已存在于黑名单中`, "info");
        } else {
          notify(`已将 ${parsedPhones[0]} 加入黑名单`, "success");
        }
      } else {
        for (let i = 0; i < parsedPhones.length; i += 200) {
          const chunk = parsedPhones.slice(i, i + 200);
          const res = await api.addBlacklistBatch(chunk, reason.trim() || "批量加入", defaultProvider);
          totalAdded += res.added;
        }
        notify(`已批量加入 ${totalAdded} 个新号码到黑名单`, "success");
      }
      setPhonesText("");
      load();
    } catch (err) {
      if (totalAdded > 0) {
        load();
      }
      notify(
        err instanceof Error
          ? totalAdded > 0
            ? `已加入 ${totalAdded} 个号码，后续批次失败：${err.message}`
            : err.message
          : "添加失败",
        "error",
      );
    } finally {
      setSubmitting(false);
    }
  };

  const handleRemove = async (entry: BlacklistEntry) => {
    setRemovingId(entry.id);
    try {
      const res = await api.removeBlacklist(entry.phone, entry.provider, entry.project_id ?? "");
      if (res.deleted === 0) {
        notify(`未找到匹配的黑名单记录 (${entry.phone})`, "warn");
      } else {
        notify(`已移除 ${entry.phone}`, "info");
      }
      load();
    } catch (err) {
      notify(err instanceof Error ? err.message : "移除失败", "error");
    } finally {
      setRemovingId(null);
    }
  };

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return entries;
    return entries.filter((e) =>
      `${e.phone} ${e.provider} ${e.project_id || "global"} ${e.reason} ${e.created_at}`.toLowerCase().includes(q),
    );
  }, [entries, search]);

  return (
    <div className="grid items-start gap-5 @4xl:grid-cols-[minmax(0,1fr)_330px]">
      <section className="surface-card min-w-0 rounded-2xl">
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-t-2xl border-b border-slate-200/75 bg-slate-50/75 p-4">
          <div className="flex items-center gap-2">
            <Ban className="h-4 w-4 text-rose-500" />
            <h2 className="text-sm font-bold text-slate-900">号码黑名单</h2>
            <span className="font-mono-code rounded-full bg-slate-200/80 px-2 py-0.5 text-xs font-semibold text-slate-700">
              {entries.length}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <div className="relative">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-400" />
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="筛选号码或原因"
                className="input-surface w-48 rounded-xl py-1.5 pl-8 pr-7 text-xs"
              />
              {search ? (
                <button
                  type="button"
                  onClick={() => setSearch("")}
                  className="absolute right-2 top-1/2 -translate-y-1/2 cursor-pointer rounded p-0.5 text-slate-400 hover:text-slate-700"
                >
                  <X className="h-3 w-3" />
                </button>
              ) : null}
            </div>
            <button
              type="button"
              onClick={load}
              className="cursor-pointer rounded-xl border border-slate-200 bg-white p-1.5 text-slate-500 shadow-2xs hover:bg-slate-50"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin text-emerald-600" : ""}`} />
            </button>
          </div>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full min-w-[560px] border-collapse text-left text-xs">
            <thead>
              <tr className="border-b border-slate-100 bg-slate-50/50 text-slate-500">
                <th className="px-4 py-3 font-medium">手机号</th>
                <th className="px-4 py-3 font-medium">平台 / 作用域</th>
                <th className="px-4 py-3 font-medium">原因</th>
                <th className="px-4 py-3 font-medium">加入时间</th>
                <th className="px-4 py-3 font-medium text-right">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {filtered.length === 0 ? (
                <tr>
                  <td colSpan={5} className="px-4 py-12 text-center text-slate-400">
                    {loading ? "加载中..." : "当前没有黑名单号码"}
                  </td>
                </tr>
              ) : (
                filtered.map((item) => (
                  <tr key={item.id} className="transition hover:bg-slate-50/70">
                    <td className="font-mono-code whitespace-nowrap px-4 py-3 font-semibold text-slate-900">
                      {item.phone}
                    </td>
                    <td className="whitespace-nowrap px-4 py-3 text-slate-600">
                      <span className="font-mono-code mr-1.5 rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-700">
                        {item.provider}
                      </span>
                      <span
                        className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${
                          item.project_id
                            ? "bg-amber-50 text-amber-700"
                            : "bg-emerald-50 text-emerald-700"
                        }`}
                      >
                        {item.project_id ? `项目: ${item.project_id}` : "全局"}
                      </span>
                    </td>
                    <td className="max-w-[200px] truncate px-4 py-3 text-slate-600" title={item.reason}>
                      {item.reason || "-"}
                    </td>
                    <td className="font-mono-code whitespace-nowrap px-4 py-3 text-slate-400">
                      {formatDateTime(item.created_at)}
                    </td>
                    <td className="whitespace-nowrap px-4 py-3 text-right">
                      <button
                        type="button"
                        disabled={removingId === item.id}
                        onClick={() => handleRemove(item)}
                        className="inline-flex cursor-pointer items-center gap-1 rounded-lg px-2 py-1 text-slate-500 transition hover:bg-rose-50 hover:text-rose-600 disabled:opacity-40"
                      >
                        {removingId === item.id ? (
                          <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        ) : (
                          <Trash2 className="h-3.5 w-3.5" />
                        )}
                        移除
                      </button>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="surface-card rounded-2xl">
        <div className="flex items-center justify-between rounded-t-2xl border-b border-slate-200/75 bg-slate-50/75 px-5 py-3.5">
          <div className="flex items-center gap-2">
            <Plus className="h-4 w-4 text-emerald-600" />
            <h3 className="text-sm font-bold text-slate-900">添加黑名单（支持批量）</h3>
          </div>
          {parsedPhones.length > 0 ? (
            <span className="font-mono-code rounded-full bg-emerald-50 px-2 py-0.5 text-[10px] font-semibold text-emerald-700 ring-1 ring-inset ring-emerald-200">
              已识别 {parsedPhones.length} 个
            </span>
          ) : null}
        </div>

        <form onSubmit={handleAdd} className="space-y-3.5 p-5">
          <p className="text-xs leading-relaxed text-slate-500">
            命中黑名单的号码在取号时会自动向平台申请取消退款并跳过（自动兼容带或不带{" "}
            <code className="font-mono-code rounded bg-slate-100 px-1 py-0.5 text-slate-700">+</code> 前缀）。
          </p>
          <div>
            <label className="mb-1.5 block text-xs font-medium text-slate-700">手机号（每行一个，支持逗号分隔）</label>
            <textarea
              rows={4}
              value={phonesText}
              onChange={(e) => setPhonesText(e.target.value)}
              placeholder={"+66812345678\n+62898765432"}
              className="input-surface font-mono-code w-full rounded-xl px-3 py-2 text-xs"
            />
          </div>
          <div>
            <label className="mb-1.5 block text-xs font-medium text-slate-700">原因说明</label>
            <input
              type="text"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="如：收不到码 / 已封禁"
              className="input-surface w-full rounded-xl px-3 py-2 text-xs"
            />
          </div>
          <button
            type="submit"
            disabled={submitting}
            className="w-full cursor-pointer rounded-xl bg-slate-900 px-4 py-2.5 text-xs font-semibold text-white shadow-xs transition hover:bg-slate-800 disabled:opacity-50"
          >
            {submitting ? "保存中..." : "加入黑名单"}
          </button>
        </form>
      </section>
    </div>
  );
}
