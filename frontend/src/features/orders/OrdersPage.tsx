import { useCallback, useEffect, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, Download, Eye, EyeOff, RefreshCw, Search, ShieldOff } from "lucide-react";
import { api } from "../../api/client";
import type { ReconciliationActivationItem, ReconciliationResponse, SmsOrder, StatsResponse } from "../../api/types";
import { CopyButton } from "../../components/CopyButton";
import { StatusBadge } from "../../components/StatusBadge";
import { useToast } from "../../components/ToastProvider";
import { formatDateTime, formatFinancialSummary, formatPrice, maskCode } from "../../utils/format";

interface OrdersPageProps {
  stats: StatsResponse | null;
  maskInHistory: boolean;
  onOrdersChanged?: () => void;
}

export function OrdersPage({ stats, maskInHistory, onOrdersChanged }: OrdersPageProps) {
  const { notify } = useToast();
  const [orders, setOrders] = useState<SmsOrder[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState("");
  const [projectId, setProjectId] = useState("");
  const [serviceFilter, setServiceFilter] = useState("");
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [reconciliation, setReconciliation] = useState<ReconciliationResponse | null>(null);
  const [resolvingId, setResolvingId] = useState<number | null>(null);
  const [revealedIds, setRevealedIds] = useState<Set<string>>(new Set());
  const loadSeqRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);

  const pageSize = 25;

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
          search,
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
  }, [status, projectId, serviceFilter, search, page, notify]);

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

  const handleScrubOne = async (orderId: string) => {
    try {
      await api.scrubOrderCode(orderId);
      notify("已立即脱敏清理该条验证码", "success");
      load();
      onOrdersChanged?.();
    } catch (err) {
      notify(err instanceof Error ? err.message : "脱敏失败", "error");
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
          search,
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

  const pendingActItems: ReconciliationActivationItem[] = [
    ...(reconciliation?.uncertain_activations || []),
    ...(reconciliation?.pending_refund_activations || []),
    ...(reconciliation?.currency_review_activations || []),
  ].filter((item, idx, arr) => arr.findIndex((x) => x.id === item.id) === idx);

  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  return (
    <div className="space-y-5">
      {/* 待人工核销对账队列（仅在存在不确定购买/待退款/历史币种待核对记录时显示） */}
      {pendingActItems.length > 0 ? (
        <section className="rounded-2xl border border-amber-200 bg-amber-50/70 p-4 shadow-2xs">
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
                className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-amber-200/80 bg-white px-3 py-2 text-xs"
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

      {/* 近 7 日按项目 / 按服务汇总 */}
      <div className="grid gap-4 lg:grid-cols-2">
        <section className="rounded-2xl border border-slate-200 bg-white p-4 shadow-2xs">
          <div className="mb-3 flex items-center justify-between">
            <h3 className="text-sm font-semibold text-slate-900">近 7 日接入项目分布</h3>
            <span className="text-xs text-slate-400">按调用来源归属</span>
          </div>
          {stats && stats.by_project.length > 0 ? (
            <div className="space-y-2">
              {stats.by_project.map((item) => (
                <div
                  key={item.project_id}
                  className="flex items-center justify-between rounded-xl bg-slate-50 px-3 py-2 text-xs"
                >
                  <button
                    type="button"
                    onClick={() => {
                      setProjectId(item.project_id === projectId ? "" : item.project_id);
                      setPage(1);
                    }}
                    className="cursor-pointer font-medium text-slate-800 hover:text-emerald-700"
                  >
                    {item.project_id === "web-console" ? "Web 工作台 (web-console)" : item.project_id}
                  </button>
                  <div className="font-mono-code flex items-center gap-3 text-slate-600">
                    <span>收码 {item.received_count}/{item.total}</span>
                    <strong className="text-slate-900" title={formatFinancialSummary(item).lines.join(" | ")}>
                      {formatFinancialSummary(item).primary}
                    </strong>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="py-6 text-center text-xs text-slate-400">近 7 日暂无订单数据</div>
          )}
        </section>

        <section className="rounded-2xl border border-slate-200 bg-white p-4 shadow-2xs">
          <div className="mb-3 flex items-center justify-between">
            <h3 className="text-sm font-semibold text-slate-900">近 7 日业务服务分布</h3>
            <span className="text-xs text-slate-400">SMSBower 服务维度</span>
          </div>
          {stats && stats.by_service.length > 0 ? (
            <div className="space-y-2">
              {stats.by_service.map((item) => (
                <div
                  key={`${item.service}-${item.service_code}`}
                  className="flex items-center justify-between rounded-xl bg-slate-50 px-3 py-2 text-xs"
                >
                  <button
                    type="button"
                    onClick={() => {
                      setServiceFilter(item.service === serviceFilter ? "" : item.service);
                      setPage(1);
                    }}
                    className="cursor-pointer font-medium text-slate-800 hover:text-emerald-700"
                  >
                    {item.service}{" "}
                    <code className="font-mono-code text-slate-400">({item.service_code})</code>
                  </button>
                  <div className="font-mono-code flex items-center gap-3 text-slate-600">
                    <span>收码 {item.received_count}/{item.total}</span>
                    <strong className="text-slate-900" title={formatFinancialSummary(item).lines.join(" | ")}>
                      {formatFinancialSummary(item).primary}
                    </strong>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="py-6 text-center text-xs text-slate-400">近 7 日暂无业务统计</div>
          )}
        </section>
      </div>

      {/* 订单检索表格 */}
      <section className="rounded-2xl border border-slate-200 bg-white shadow-xs">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 p-4">
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
              <input
                type="text"
                value={search}
                onChange={(e) => {
                  setSearch(e.target.value);
                  setPage(1);
                }}
                placeholder="搜索手机号 / 订单号 / 国家 / 外部ID"
                className="w-64 rounded-xl border border-slate-200 py-2 pl-9 pr-3 text-xs outline-none focus:border-emerald-500"
              />
            </div>

            <select
              value={status}
              onChange={(e) => {
                setStatus(e.target.value);
                setPage(1);
              }}
              className="rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs text-slate-700 outline-none focus:border-emerald-500"
            >
              <option value="">全部状态</option>
              <option value="acquiring">取号中</option>
              <option value="waiting_code">等码中</option>
              <option value="code_received">已收到验证码</option>
              <option value="waiting_next_code">重发等码中</option>
              <option value="completing">确认完成中</option>
              <option value="cancelling">取消退款中</option>
              <option value="compensation_pending">退款重试中</option>
              <option value="completed">已完成</option>
              <option value="cancelled">已取消</option>
              <option value="expired">已超时</option>
              <option value="failed">失败</option>
            </select>

            {projectId ? (
              <button
                type="button"
                onClick={() => {
                  setProjectId("");
                  setPage(1);
                }}
                className="cursor-pointer rounded-xl bg-indigo-50 px-2.5 py-1.5 text-xs font-medium text-indigo-700 hover:bg-indigo-100"
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
                className="cursor-pointer rounded-xl bg-emerald-50 px-2.5 py-1.5 text-xs font-medium text-emerald-700 hover:bg-emerald-100"
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
              className="inline-flex cursor-pointer items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-40"
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
              className="inline-flex cursor-pointer items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
              刷新
            </button>
          </div>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full min-w-[820px] border-collapse text-left text-xs">
            <thead>
              <tr className="border-b border-slate-100 bg-slate-50/70 text-slate-500">
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
                    <tr key={order.order_id} className="hover:bg-slate-50/60">
                      <td className="px-4 py-3">
                        <div className="font-mono-code font-medium text-slate-800">
                          {formatDateTime(order.created_at)}
                        </div>
                        <div className="font-mono-code mt-0.5 text-[11px] text-slate-400">
                          {order.order_id}
                        </div>
                      </td>
                      <td className="px-4 py-3">
                        <span className="rounded-md bg-slate-100 px-2 py-0.5 font-medium text-slate-700">
                          {order.project_id === "web-console" ? "工作台" : order.project_id}
                        </span>
                        {order.external_ref ? (
                          <div className="font-mono-code mt-0.5 text-[11px] text-slate-400">
                            ref: {order.external_ref}
                          </div>
                        ) : null}
                      </td>
                      <td className="px-4 py-3">
                        <div className="font-medium text-slate-800">
                          {order.service}{" "}
                          <span className="font-mono-code text-slate-400">({order.service_code})</span>
                        </div>
                        <div className="text-slate-500">{order.country_label || `#${order.country_id}`}</div>
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-1.5">
                          <span className="font-mono-code font-medium text-slate-800">
                            {order.phone_e164 || order.phone_raw || "-"}
                          </span>
                          {order.phone_e164 || order.phone_raw ? (
                            <CopyButton value={order.phone_e164 || order.phone_raw} toastText="已复制手机号" />
                          ) : null}
                        </div>
                      </td>
                      <td className="px-4 py-3">
                        {order.code_scrubbed ? (
                          <span className="text-slate-400">已脱敏清理</span>
                        ) : codeVisible ? (
                          <div className="flex items-center gap-1.5">
                            <span className="font-mono-code rounded-md bg-emerald-50 px-2 py-0.5 font-bold text-emerald-800">
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
                              onClick={() => handleScrubOne(order.order_id)}
                              className="cursor-pointer rounded p-1 text-slate-400 hover:bg-rose-50 hover:text-rose-600"
                              title="立即脱敏抹除该验证码"
                            >
                              <ShieldOff className="h-3.5 w-3.5" />
                            </button>
                          </div>
                        ) : (
                          <span className="text-slate-400">-</span>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        <StatusBadge status={order.status} refundState={order.refund_state} />
                        {order.error_message ? (
                          <div className="mt-1 max-w-[200px] truncate text-[11px] text-slate-400" title={order.error_message}>
                            {order.error_message}
                          </div>
                        ) : null}
                      </td>
                      <td className="font-mono-code px-4 py-3 text-slate-700">
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

        <div className="flex items-center justify-between border-t border-slate-100 px-4 py-3 text-xs text-slate-500">
          <span>
            共 <strong className="font-mono-code text-slate-800">{total}</strong> 条记录
          </span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={page <= 1}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              className="cursor-pointer rounded-lg border border-slate-200 px-3 py-1 hover:bg-slate-50 disabled:opacity-40"
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
              className="cursor-pointer rounded-lg border border-slate-200 px-3 py-1 hover:bg-slate-50 disabled:opacity-40"
            >
              下一页
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}
