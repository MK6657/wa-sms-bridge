import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Download,
  Eye,
  EyeOff,
  RefreshCw,
  Search,
  ShieldOff,
  X,
} from "lucide-react";
import { api } from "../../api/client";
import type { ReconciliationActivationItem, ReconciliationResponse, SmsOrder, StatsResponse } from "../../api/types";
import { ConfirmModal } from "../../components/ConfirmModal";
import { CopyButton } from "../../components/CopyButton";
import { CustomSelect, type SelectOption } from "../../components/CustomSelect";
import { StatusBadge } from "../../components/StatusBadge";
import { useToast } from "../../components/ToastProvider";
import { formatDateTime, formatFinancialSummary, formatPrice, maskCode } from "../../utils/format";

interface OrdersPageProps {
  stats: StatsResponse | null;
  maskInHistory: boolean;
  onOrdersChanged?: () => void;
}

const ORDER_STATUS_OPTIONS: SelectOption[] = [
  { value: "", label: "全部订单状态" },
  { value: "acquiring", label: "取号中", dotColor: "bg-indigo-500", badge: "acquiring", group: "进行中" },
  { value: "waiting_code", label: "等码中", dotColor: "bg-amber-500", badge: "waiting", group: "进行中" },
  { value: "code_received", label: "已收到验证码", dotColor: "bg-emerald-500", badge: "received", group: "进行中" },
  { value: "waiting_next_code", label: "重发等码中", dotColor: "bg-sky-500", badge: "resend", group: "进行中" },
  { value: "completing", label: "确认完成中", dotColor: "bg-emerald-500", group: "进行中" },
  { value: "cancelling", label: "取消退款中", dotColor: "bg-amber-500", group: "进行中" },
  { value: "compensation_pending", label: "退款重试中", dotColor: "bg-amber-500", group: "进行中" },
  { value: "completed", label: "已完成", dotColor: "bg-emerald-600", badge: "completed", group: "已终结" },
  { value: "cancelled", label: "已取消", dotColor: "bg-slate-400", badge: "cancelled", group: "已终结" },
  { value: "expired", label: "已超时", dotColor: "bg-orange-400", badge: "expired", group: "已终结" },
  { value: "failed", label: "失败", dotColor: "bg-rose-500", badge: "failed", group: "已终结" },
];

export function OrdersPage({ stats, maskInHistory, onOrdersChanged }: OrdersPageProps) {
  const { notify } = useToast();
  const [orders, setOrders] = useState<SmsOrder[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState("");
  const [projectId, setProjectId] = useState("");
  const [serviceFilter, setServiceFilter] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [reconciliation, setReconciliation] = useState<ReconciliationResponse | null>(null);
  const [resolvingId, setResolvingId] = useState<number | null>(null);
  const [scrubTargetId, setScrubTargetId] = useState<string | null>(null);
  const [scrubbingOne, setScrubbingOne] = useState(false);
  const [revealedIds, setRevealedIds] = useState<Set<string>>(new Set());
  const loadSeqRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);

  const pageSize = 25;

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedSearch(searchInput.trim());
    }, 240);
    return () => window.clearTimeout(timer);
  }, [searchInput]);

  const loadReconciliation = useCallback(async () => {
    try {
      const rec = await api.getReconciliationPending();
      setReconciliation(rec);
    } catch {
      // ignore if non-admin or unavailable
    }
  }, []);

  const load = useCallback(async () => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const reqSeq = ++loadSeqRef.current;
    setLoading(true);
    try {
      const res = await api.listOrders(
        {
          status,
          project_id: projectId,
          service: serviceFilter,
          search: debouncedSearch,
          page,
          page_size: pageSize,
        },
        controller.signal,
      );
      if (reqSeq !== loadSeqRef.current || controller.signal.aborted) return;
      setOrders(res.items);
      setTotal(res.total);
    } catch (err) {
      if (controller.signal.aborted || reqSeq !== loadSeqRef.current) return;
      notify(err instanceof Error ? err.message : "加载订单失败", "error");
    } finally {
      if (reqSeq === loadSeqRef.current) {
        setLoading(false);
      }
    }
  }, [status, projectId, serviceFilter, debouncedSearch, page, notify]);

  useEffect(() => {
    load();
    loadReconciliation();
    return () => {
      abortRef.current?.abort();
    };
  }, [load, loadReconciliation]);

  const toggleReveal = (orderId: string) => {
    setRevealedIds((prev) => {
      const next = new Set(prev);
      if (next.has(orderId)) next.delete(orderId);
      else next.add(orderId);
      return next;
    });
  };

  const handleScrubConfirmed = async () => {
    if (!scrubTargetId) return;
    setScrubbingOne(true);
    try {
      await api.scrubOrderCode(scrubTargetId);
      notify("已立即脱敏清理该条验证码", "success");
      setScrubTargetId(null);
      load();
      onOrdersChanged?.();
    } catch (err) {
      notify(err instanceof Error ? err.message : "脱敏失败", "error");
    } finally {
      setScrubbingOne(false);
    }
  };

  const handleResolveActivation = async (
    item: ReconciliationActivationItem,
    nextState: "refund_confirmed" | "unrefundable" | "completed",
  ) => {
    setResolvingId(item.id);
    try {
      await api.resolveReconciliation(item.id, {
        state: nextState,
        note: "控制台人工核销",
        currency: item.currency && item.currency !== "UNKNOWN" ? item.currency : undefined,
      });
      notify(`已将激活记录 #${item.id} 人工核销为 ${nextState}`, "success");
      await Promise.allSettled([load(), loadReconciliation()]);
      onOrdersChanged?.();
    } catch (err) {
      notify(err instanceof Error ? err.message : "人工核销失败", "error");
    } finally {
      setResolvingId(null);
    }
  };

  const exportCsv = async () => {
    setExporting(true);
    try {
      const allItems: SmsOrder[] = [];
      for (let p = 1; p <= 5; p += 1) {
        const res = await api.listOrders({
          status,
          project_id: projectId,
          service: serviceFilter,
          search: debouncedSearch,
          page: p,
          page_size: 200,
        });
        allItems.push(...res.items);
        if (allItems.length >= res.total || res.items.length < 200) break;
      }
      const source = allItems.length > 0 ? allItems : orders;
      const header = [
        "order_id",
        "project_id",
        "external_ref",
        "provider",
        "service",
        "country",
        "phone_e164",
        "status",
        "refund_state",
        "code_count",
        "cost",
        "cost_status",
        "currency",
        "created_at",
      ];
      const escapeCsv = (val: string) => {
        const safe = /^[=+\-@]/.test(val) && !/^\+\d+$/.test(val) ? `'${val}` : val;
        return `"${safe.replace(/"/g, '""')}"`;
      };
      const rows = source.map((o) => {
        const phone = o.phone_e164 || o.phone_raw || "";
        return [
          o.order_id,
          o.project_id,
          o.external_ref || "",
          o.provider,
          o.service,
          o.country_label || o.country_id,
          phone ? `\t${phone}` : "",
          o.status,
          o.refund_state || "none",
          String(o.code_count),
          o.cost !== null ? String(o.cost) : "",
          o.cost_status || (o.cost_is_estimated ? "estimated" : "actual"),
          o.currency || "USD",
          o.created_at,
        ]
          .map((v) => escapeCsv(String(v ?? "")))
          .join(",");
      });
      const blob = new Blob(["\ufeff" + [header.join(","), ...rows].join("\n")], {
        type: "text/csv;charset=utf-8",
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `sms-orders-${new Date().toISOString().slice(0, 10)}.csv`;
      a.click();
      URL.revokeObjectURL(url);
      notify(`已导出 ${source.length} 条订单记录`, "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "导出 CSV 失败", "error");
    } finally {
      setExporting(false);
    }
  };

  const pendingActItems: ReconciliationActivationItem[] = useMemo(
    () =>
      [
        ...(reconciliation?.uncertain_activations || []),
        ...(reconciliation?.pending_refund_activations || []),
        ...(reconciliation?.currency_review_activations || []),
      ].filter((item, idx, arr) => arr.findIndex((x) => x.id === item.id) === idx),
    [reconciliation],
  );

  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const maxProjectTotal = Math.max(1, ...(stats?.by_project.map((p) => p.total) || [1]));
  const maxServiceTotal = Math.max(1, ...(stats?.by_service.map((s) => s.total) || [1]));

  return (
    <div className="space-y-5">
      {/* 待人工核销对账队列 */}
      {pendingActItems.length > 0 ? (
        <section className="rounded-2xl border border-amber-200 bg-amber-50/75 p-4 shadow-2xs">
          <div className="mb-2.5 flex items-center justify-between gap-2">
            <div className="flex items-center gap-2 text-xs font-semibold text-amber-950">
              <AlertTriangle className="h-4 w-4 text-amber-600" />
              <span>待对账 / 异常激活记录 ({pendingActItems.length})</span>
            </div>
            <button
              type="button"
              onClick={loadReconciliation}
              className="cursor-pointer rounded-lg border border-amber-200 bg-white px-2.5 py-1 text-[11px] font-medium text-amber-800 hover:bg-amber-100"
            >
              刷新对账队列
            </button>
          </div>
          <div className="space-y-2">
            {pendingActItems.slice(0, 8).map((act) => (
              <div
                key={act.id}
                className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-amber-200/80 bg-white px-3 py-2 text-xs shadow-2xs"
              >
                <div className="min-w-0">
                  <div className="font-mono-code font-medium text-slate-900">
                    #{act.id} · 订单 {act.order_id} · {act.provider} ({act.state})
                  </div>
                  <div className="truncate text-[11px] text-slate-500">
                    激活ID: {act.activation_id || "未获取"} · {act.last_error || "待核对上游账单"}
                  </div>
                </div>
                <div className="flex flex-wrap items-center gap-1.5">
                  <button
                    type="button"
                    disabled={resolvingId === act.id}
                    onClick={() => handleResolveActivation(act, "refund_confirmed")}
                    className="cursor-pointer rounded-lg border border-emerald-200 bg-emerald-50 px-2.5 py-1 text-[11px] font-medium text-emerald-700 hover:bg-emerald-100 disabled:opacity-50"
                  >
                    确认已退
                  </button>
                  <button
                    type="button"
                    disabled={resolvingId === act.id}
                    onClick={() => handleResolveActivation(act, "unrefundable")}
                    className="cursor-pointer rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-1 text-[11px] font-medium text-slate-700 hover:bg-slate-100 disabled:opacity-50"
                  >
                    确认未扣/不可退
                  </button>
                  <button
                    type="button"
                    disabled={resolvingId === act.id}
                    onClick={() => handleResolveActivation(act, "completed")}
                    className="inline-flex cursor-pointer items-center gap-1 rounded-lg border border-indigo-200 bg-indigo-50 px-2.5 py-1 text-[11px] font-medium text-indigo-700 hover:bg-indigo-100 disabled:opacity-50"
                  >
                    <CheckCircle2 className="h-3 w-3" />
                    确认已完成
                  </button>
                </div>
              </div>
            ))}
          </div>
        </section>
      ) : null}

      {/* 近 7 日按项目 / 按服务汇总（点击行即高亮联动过滤下方列表） */}
      <div className="grid gap-4 @3xl:grid-cols-2">
        <section className="surface-card rounded-2xl p-4">
          <div className="mb-3 flex items-center justify-between">
            <h3 className="text-sm font-bold text-slate-900">近 7 日接入项目分布</h3>
            <span className="text-xs text-slate-400">点击项目可筛选表格</span>
          </div>
          {stats && stats.by_project.length > 0 ? (
            <div className="space-y-2">
              {stats.by_project.map((item) => {
                const isSelected = projectId === item.project_id;
                const pct = Math.max(6, Math.round((item.total / maxProjectTotal) * 100));
                return (
                  <button
                    key={item.project_id}
                    type="button"
                    onClick={() => {
                      setProjectId(isSelected ? "" : item.project_id);
                      setPage(1);
                    }}
                    className={`relative flex w-full cursor-pointer items-center justify-between overflow-hidden rounded-xl border px-3 py-2 text-left text-xs transition ${
                      isSelected
                        ? "border-indigo-400 bg-indigo-50/75 ring-1 ring-indigo-400/30"
                        : "border-slate-200/75 bg-slate-50/70 hover:border-slate-300 hover:bg-white"
                    }`}
                  >
                    <span
                      className="pointer-events-none absolute inset-y-0 left-0 bg-indigo-500/[0.045]"
                      style={{ width: `${pct}%` }}
                    />
                    <span className="relative font-semibold text-slate-800">
                      {item.project_id === "web-console" ? "Web 工作台 (web-console)" : item.project_id}
                    </span>
                    <div className="font-mono-code relative flex items-center gap-3 text-slate-600">
                      <span>收码 {item.received_count}/{item.total}</span>
                      <strong className="text-slate-900" title={formatFinancialSummary(item).lines.join(" | ")}>
                        {formatFinancialSummary(item).primary}
                      </strong>
                    </div>
                  </button>
                );
              })}
            </div>
          ) : (
            <div className="py-6 text-center text-xs text-slate-400">近 7 日暂无订单数据</div>
          )}
        </section>

        <section className="surface-card rounded-2xl p-4">
          <div className="mb-3 flex items-center justify-between">
            <h3 className="text-sm font-bold text-slate-900">近 7 日业务服务分布</h3>
            <span className="text-xs text-slate-400">点击业务可筛选表格</span>
          </div>
          {stats && stats.by_service.length > 0 ? (
            <div className="space-y-2">
              {stats.by_service.map((item) => {
                const isSelected = serviceFilter === item.service;
                const pct = Math.max(6, Math.round((item.total / maxServiceTotal) * 100));
                return (
                  <button
                    key={`${item.service}-${item.service_code}`}
                    type="button"
                    onClick={() => {
                      setServiceFilter(isSelected ? "" : item.service);
                      setPage(1);
                    }}
                    className={`relative flex w-full cursor-pointer items-center justify-between overflow-hidden rounded-xl border px-3 py-2 text-left text-xs transition ${
                      isSelected
                        ? "border-emerald-400 bg-emerald-50/75 ring-1 ring-emerald-400/30"
                        : "border-slate-200/75 bg-slate-50/70 hover:border-slate-300 hover:bg-white"
                    }`}
                  >
                    <span
                      className="pointer-events-none absolute inset-y-0 left-0 bg-emerald-500/[0.05]"
                      style={{ width: `${pct}%` }}
                    />
                    <span className="relative font-semibold text-slate-800">
                      {item.service}{" "}
                      <code className="font-mono-code text-slate-400">({item.service_code})</code>
                    </span>
                    <div className="font-mono-code relative flex items-center gap-3 text-slate-600">
                      <span>收码 {item.received_count}/{item.total}</span>
                      <strong className="text-slate-900" title={formatFinancialSummary(item).lines.join(" | ")}>
                        {formatFinancialSummary(item).primary}
                      </strong>
                    </div>
                  </button>
                );
              })}
            </div>
          ) : (
            <div className="py-6 text-center text-xs text-slate-400">近 7 日暂无业务统计</div>
          )}
        </section>
      </div>

      {/* 订单检索表格（不设 overflow-hidden 以免裁切自定义状态下拉菜单） */}
      <section className="surface-card rounded-2xl">
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-t-2xl border-b border-slate-200/75 bg-slate-50/70 p-4">
          <div className="flex flex-wrap items-center gap-2.5">
            <div className="relative">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
              <input
                type="text"
                value={searchInput}
                onChange={(e) => {
                  setSearchInput(e.target.value);
                  setPage(1);
                }}
                placeholder="搜索手机号 / 订单号 / 国家 / 外部ID"
                className="input-surface w-64 rounded-xl py-2 pl-9 pr-8 text-xs"
              />
              {searchInput ? (
                <button
                  type="button"
                  onClick={() => {
                    setSearchInput("");
                    setDebouncedSearch("");
                    setPage(1);
                  }}
                  className="absolute right-2.5 top-1/2 -translate-y-1/2 cursor-pointer rounded p-0.5 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
                  title="清空搜索"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              ) : null}
            </div>

            <CustomSelect
              value={status}
              onChange={(val) => {
                setStatus(val);
                setPage(1);
              }}
              options={ORDER_STATUS_OPTIONS}
              activeHighlight={Boolean(status)}
              minMenuWidth={230}
              className="w-44"
            />

            {projectId ? (
              <button
                type="button"
                onClick={() => {
                  setProjectId("");
                  setPage(1);
                }}
                className="cursor-pointer rounded-xl border border-indigo-200 bg-indigo-50 px-2.5 py-1.5 text-xs font-medium text-indigo-700 hover:bg-indigo-100"
              >
                项目: {projectId} ×
              </button>
            ) : null}

            {serviceFilter ? (
              <button
                type="button"
                onClick={() => {
                  setServiceFilter("");
                  setPage(1);
                }}
                className="cursor-pointer rounded-xl border border-emerald-200 bg-emerald-50 px-2.5 py-1.5 text-xs font-medium text-emerald-700 hover:bg-emerald-100"
              >
                业务: {serviceFilter} ×
              </button>
            ) : null}
          </div>

          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={exportCsv}
              disabled={total === 0 || exporting}
              className="inline-flex cursor-pointer items-center gap-1.5 rounded-xl border border-slate-200/90 bg-white px-3 py-2 text-xs font-medium text-slate-700 shadow-2xs transition hover:border-slate-300 hover:bg-slate-50 disabled:opacity-40"
            >
              <Download className="h-3.5 w-3.5" />
              {exporting ? "导出中..." : "导出 CSV"}
            </button>
            <button
              type="button"
              onClick={() => {
                load();
                loadReconciliation();
              }}
              disabled={loading}
              className="inline-flex cursor-pointer items-center gap-1.5 rounded-xl border border-slate-200/90 bg-white px-3 py-2 text-xs font-medium text-slate-700 shadow-2xs transition hover:border-slate-300 hover:bg-slate-50"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin text-emerald-600" : ""}`} />
              刷新
            </button>
          </div>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full min-w-[780px] border-collapse text-left text-xs">
            <thead>
              <tr className="border-b border-slate-100 bg-slate-50/50 text-slate-500">
                <th className="px-4 py-3 font-medium">时间 / 订单</th>
                <th className="px-4 py-3 font-medium">来源项目</th>
                <th className="px-4 py-3 font-medium">业务 / 国家</th>
                <th className="px-4 py-3 font-medium">手机号</th>
                <th className="px-4 py-3 font-medium">验证码</th>
                <th className="px-4 py-3 font-medium">状态</th>
                <th className="px-4 py-3 font-medium">费用</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {orders.length === 0 ? (
                <tr>
                  <td colSpan={7} className="px-4 py-12 text-center text-slate-400">
                    {loading ? "加载中..." : "暂无匹配的接码记录"}
                  </td>
                </tr>
              ) : (
                orders.map((order) => {
                  const isRevealed = revealedIds.has(order.order_id) || !maskInHistory;
                  const codeVisible = !order.code_scrubbed && Boolean(order.latest_code);
                  return (
                    <tr key={order.order_id} className="transition hover:bg-slate-50/70">
                      <td className="whitespace-nowrap px-4 py-3">
                        <div className="font-mono-code font-medium text-slate-800">
                          {formatDateTime(order.created_at)}
                        </div>
                        <div
                          className="font-mono-code mt-0.5 max-w-[170px] truncate text-[11px] text-slate-400"
                          title={order.order_id}
                        >
                          {order.order_id}
                        </div>
                      </td>
                      <td className="whitespace-nowrap px-4 py-3">
                        <span className="rounded-md bg-slate-100 px-2 py-0.5 font-medium text-slate-700">
                          {order.project_id === "web-console" ? "工作台" : order.project_id}
                        </span>
                        {order.external_ref ? (
                          <div className="font-mono-code mt-0.5 text-[11px] text-slate-400">
                            ref: {order.external_ref}
                          </div>
                        ) : null}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3">
                        <div className="font-medium text-slate-800">
                          {order.service}{" "}
                          <span className="font-mono-code text-slate-400">({order.service_code})</span>
                        </div>
                        <div className="text-slate-500">{order.country_label || `#${order.country_id}`}</div>
                      </td>
                      <td className="whitespace-nowrap px-4 py-3">
                        <div className="flex items-center gap-1.5">
                          <span className="font-mono-code font-medium text-slate-800">
                            {order.phone_e164 || order.phone_raw || "-"}
                          </span>
                          {order.phone_e164 || order.phone_raw ? (
                            <CopyButton value={order.phone_e164 || order.phone_raw} toastText="已复制手机号" />
                          ) : null}
                        </div>
                      </td>
                      <td className="whitespace-nowrap px-4 py-3">
                        {order.code_scrubbed ? (
                          <span className="text-slate-400">已脱敏清理</span>
                        ) : codeVisible ? (
                          <div className="flex items-center gap-1.5">
                            <span className="font-mono-code rounded-md bg-emerald-50 px-2 py-0.5 font-bold text-emerald-800 ring-1 ring-inset ring-emerald-200/70">
                              {isRevealed ? order.latest_code : maskCode(order.latest_code)}
                            </span>
                            {maskInHistory ? (
                              <button
                                type="button"
                                onClick={() => toggleReveal(order.order_id)}
                                className="cursor-pointer rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
                                title={isRevealed ? "隐藏验证码" : "查看明文"}
                              >
                                {isRevealed ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                              </button>
                            ) : null}
                            <CopyButton value={order.latest_code} toastText="已复制验证码" />
                            <button
                              type="button"
                              onClick={() => setScrubTargetId(order.order_id)}
                              className="cursor-pointer rounded p-1 text-slate-400 hover:bg-rose-50 hover:text-rose-600"
                              title="脱敏抹除该条验证码"
                            >
                              <ShieldOff className="h-3.5 w-3.5" />
                            </button>
                          </div>
                        ) : (
                          <span className="text-slate-400">-</span>
                        )}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3">
                        <StatusBadge status={order.status} refundState={order.refund_state} />
                        {order.error_message ? (
                          <div className="mt-1 max-w-[190px] truncate text-[11px] text-slate-400" title={order.error_message}>
                            {order.error_message}
                          </div>
                        ) : null}
                      </td>
                      <td className="font-mono-code whitespace-nowrap px-4 py-3 text-slate-700">
                        {order.cost !== null
                          ? `${formatPrice(order.cost, order.currency)}${order.cost_is_estimated ? " (估)" : ""}`
                          : "-"}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>

        <div className="flex items-center justify-between rounded-b-2xl border-t border-slate-100 px-4 py-3 text-xs text-slate-500">
          <span>
            共 <strong className="font-mono-code text-slate-800">{total}</strong> 条记录
          </span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={page <= 1}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              className="cursor-pointer rounded-lg border border-slate-200 bg-white px-3 py-1 hover:bg-slate-50 disabled:opacity-40"
            >
              上一页
            </button>
            <span className="font-mono-code">
              {page} / {totalPages}
            </span>
            <button
              type="button"
              disabled={page >= totalPages}
              onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              className="cursor-pointer rounded-lg border border-slate-200 bg-white px-3 py-1 hover:bg-slate-50 disabled:opacity-40"
            >
              下一页
            </button>
          </div>
        </div>
      </section>

      <ConfirmModal
        open={Boolean(scrubTargetId)}
        title="确认脱敏抹除该条验证码？"
        description={`将永久清空订单 ${scrubTargetId || ""} 中保存的短信验证码明文（操作不可撤销，订单金额与账本记录不受影响）。`}
        confirmLabel="确认抹除"
        danger
        loading={scrubbingOne}
        onConfirm={handleScrubConfirmed}
        onCancel={() => setScrubTargetId(null)}
      />
    </div>
  );
}
