import { useCallback, useEffect, useMemo, useState } from "react";
import { Ban, Plus, RefreshCw, Search, Trash2 } from "lucide-react";
import { api } from "../../api/client";
import type { BlacklistEntry } from "../../api/types";
import { useToast } from "../../components/ToastProvider";
import { formatDateTime } from "../../utils/format";

interface BlacklistPageProps {
  defaultProvider?: string;
}

export function BlacklistPage({ defaultProvider = "smsbower" }: BlacklistPageProps) {
  const { notify } = useToast();
  const [entries, setEntries] = useState<BlacklistEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [search, setSearch] = useState("");
  const [phonesText, setPhonesText] = useState("");
  const [reason, setReason] = useState("手动加入");
  const [submitting, setSubmitting] = useState(false);

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

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    const phones = phonesText
      .split(/[\r\n,;，]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    if (phones.length === 0) {
      notify("请输入至少一个手机号", "warn");
      return;
    }
    setSubmitting(true);
    try {
      if (phones.length === 1) {
        await api.addBlacklist(phones[0], reason.trim() || "手动加入", defaultProvider);
        notify(`已将 ${phones[0]} 加入黑名单`, "success");
      } else {
        let totalAdded = 0;
        for (let i = 0; i < phones.length; i += 200) {
          const chunk = phones.slice(i, i + 200);
          const res = await api.addBlacklistBatch(chunk, reason.trim() || "批量加入", defaultProvider);
          totalAdded += res.added;
        }
        notify(`已批量加入 ${totalAdded} 个号码到黑名单`, "success");
      }
      setPhonesText("");
      load();
    } catch (err) {
      notify(err instanceof Error ? err.message : "添加失败", "error");
    } finally {
      setSubmitting(false);
    }
  };

  const handleRemove = async (entry: BlacklistEntry) => {
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
    <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,1fr)_340px]">
      <section className="rounded-2xl border border-slate-200 bg-white shadow-xs">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 p-4">
          <div className="flex items-center gap-2">
            <Ban className="h-4 w-4 text-rose-500" />
            <h2 className="text-sm font-semibold text-slate-900">号码黑名单</h2>
            <span className="rounded-full bg-slate-100 px-2 py-0.5 font-mono-code text-xs text-slate-600">
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
                className="w-48 rounded-xl border border-slate-200 py-1.5 pl-8 pr-3 text-xs outline-none focus:border-emerald-500"
              />
            </div>
            <button
              type="button"
              onClick={load}
              className="cursor-pointer rounded-xl border border-slate-200 p-1.5 text-slate-500 hover:bg-slate-50"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
            </button>
          </div>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-left text-xs">
            <thead>
              <tr className="border-b border-slate-100 bg-slate-50/70 text-slate-500">
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
                  <tr key={item.id} className="hover:bg-slate-50/60">
                    <td className="font-mono-code px-4 py-3 font-medium text-slate-900">{item.phone}</td>
                    <td className="px-4 py-3 text-slate-600">
                      <span className="font-mono-code mr-1.5 rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-700">
                        {item.provider}
                      </span>
                      <span
                        className={`rounded px-1.5 py-0.5 text-[11px] ${
                          item.project_id
                            ? "bg-amber-50 text-amber-700"
                            : "bg-emerald-50 text-emerald-700"
                        }`}
                      >
                        {item.project_id ? `项目: ${item.project_id}` : "全局"}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-slate-600">{item.reason || "-"}</td>
                    <td className="font-mono-code px-4 py-3 text-slate-400">
                      {formatDateTime(item.created_at)}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <button
                        type="button"
                        onClick={() => handleRemove(item)}
                        className="inline-flex cursor-pointer items-center gap-1 rounded-lg px-2 py-1 text-slate-500 hover:bg-rose-50 hover:text-rose-600"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
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

      <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-xs">
        <div className="mb-3 flex items-center gap-2">
          <Plus className="h-4 w-4 text-emerald-600" />
          <h3 className="text-sm font-semibold text-slate-900">添加黑名单（支持批量）</h3>
        </div>
        <p className="mb-4 text-xs text-slate-500">
          命中黑名单的号码在取号时会自动向 SMSBower 申请退款并跳过。
        </p>

        <form onSubmit={handleAdd} className="space-y-3">
          <div>
            <label className="mb-1 block text-xs font-medium text-slate-700">手机号（每行一个，支持逗号分隔）</label>
            <textarea
              rows={4}
              value={phonesText}
              onChange={(e) => setPhonesText(e.target.value)}
              placeholder={"+66812345678\n+62898765432"}
              className="font-mono-code w-full rounded-xl border border-slate-200 px-3 py-2 text-xs outline-none focus:border-emerald-500"
            />
          </div>
          <div>
            <label className="mb-1 block text-xs font-medium text-slate-700">原因说明</label>
            <input
              type="text"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="如：收不到码 / 已封禁"
              className="w-full rounded-xl border border-slate-200 px-3 py-2 text-xs outline-none focus:border-emerald-500"
            />
          </div>
          <button
            type="submit"
            disabled={submitting}
            className="w-full cursor-pointer rounded-xl bg-slate-900 px-4 py-2.5 text-xs font-semibold text-white transition hover:bg-slate-800 disabled:opacity-50"
          >
            {submitting ? "保存中..." : "加入黑名单"}
          </button>
        </form>
      </section>
    </div>
  );
}
