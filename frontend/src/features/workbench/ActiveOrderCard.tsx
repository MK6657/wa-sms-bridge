import { useEffect, useState } from "react";
import {
  Ban,
  CheckCircle2,
  Clock,
  Loader2,
  MessageSquareText,
  RefreshCcw,
  RotateCcw,
  Sparkles,
  XCircle,
} from "lucide-react";
import type { SmsOrder } from "../../api/types";
import { CopyButton } from "../../components/CopyButton";
import { StatusBadge } from "../../components/StatusBadge";
import { formatDateTime, formatPrice } from "../../utils/format";

interface ActiveOrderCardProps {
  order: SmsOrder;
  busyAction: string | null;
  supportsResend?: boolean;
  onResend: (orderId: string) => void;
  onComplete: (orderId: string) => void;
  onCancel: (orderId: string, blacklist: boolean) => void;
}

export function ActiveOrderCard({
  order,
  busyAction,
  supportsResend = true,
  onResend,
  onComplete,
  onCancel,
}: ActiveOrderCardProps) {
  const [nowMs, setNowMs] = useState(() => Date.now());

  const isWaiting = order.status === "waiting_code" || order.status === "waiting_next_code";
  const expiresMs = order.expires_at ? Date.parse(order.expires_at) : NaN;
  const totalMs = Math.max(1, (order.wait_timeout || 180) * 1000);
  const remainingSec = Number.isFinite(expiresMs)
    ? Math.max(0, Math.ceil((expiresMs - nowMs) / 1000))
    : 0;
  const progressPct = Number.isFinite(expiresMs)
    ? Math.max(0, Math.min(100, ((expiresMs - nowMs) / totalMs) * 100))
    : 0;

  useEffect(() => {
    setNowMs(Date.now());
    if (!isWaiting) return;
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [isWaiting, order.expires_at]);

  const countdownText = `${Math.floor(remainingSec / 60)}:${String(remainingSec % 60).padStart(2, "0")}`;
  const barColor =
    remainingSec <= 15 ? "bg-rose-500" : remainingSec <= 45 ? "bg-amber-500" : "bg-emerald-500";
  const hasCode = !order.code_scrubbed && Boolean(order.latest_code);
  const isTransitioning = order.status === "completing" || order.status === "cancelling";
  const isBusy = Boolean(busyAction) || isTransitioning;

  return (
    <article
      className={`overflow-hidden rounded-2xl border bg-white shadow-xs transition ${
        hasCode
          ? "border-emerald-300 ring-2 ring-emerald-500/15"
          : "border-slate-200 hover:border-slate-300"
      }`}
    >
      {/* 顶部元信息 */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 bg-slate-50/70 px-4 py-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="rounded-md bg-slate-900 px-2 py-0.5 text-xs font-semibold uppercase tracking-wide text-white">
            {order.service}
          </span>
          <span className="text-sm font-medium text-slate-800">
            {order.country_label || `#${order.country_id}`}
          </span>
          <span className="rounded-md border border-slate-200 bg-white px-2 py-0.5 text-[11px] font-medium text-slate-500">
            {order.project_id === "web-console" ? "工作台" : `API · ${order.project_id}`}
          </span>
          {order.cost !== null ? (
            <span className="font-mono-code text-xs text-slate-500">
              {formatPrice(order.cost, order.currency)}
              {order.cost_is_estimated ? " 估" : ""}
            </span>
          ) : null}
        </div>
        <div className="flex items-center gap-2">
          <StatusBadge status={order.status} refundState={order.refund_state} />
          <span className="font-mono-code text-[11px] text-slate-400">
            #{order.order_id.slice(-6)}
          </span>
        </div>
      </div>

      <div className="space-y-4 p-4">
        {/* 手机号展示与一键复制 */}
        {order.status === "acquiring" ? (
          <div className="flex items-center gap-3 rounded-xl border border-indigo-100 bg-indigo-50/70 px-4 py-3 text-sm text-indigo-800">
            <Loader2 className="h-4 w-4 animate-spin text-indigo-600" />
            <div>
              <div className="font-medium">正在按价格策略向 SMSBower 取号...</div>
              <div className="text-xs text-indigo-600/80">
                已尝试 {order.attempts_used || 1} 次
                {order.max_price ? ` · 上限 ${formatPrice(order.max_price, order.currency)}` : ""}
              </div>
            </div>
          </div>
        ) : (
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-200 bg-slate-50/60 px-3.5 py-2.5">
            <div className="min-w-0">
              <div className="text-[11px] font-medium uppercase tracking-wider text-slate-400">
                接码手机号
              </div>
              <div className="font-mono-code mt-0.5 text-lg font-bold tracking-wide text-slate-900 sm:text-xl">
                {order.phone_e164 || order.phone_raw || "-"}
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-1.5">
              <CopyButton
                value={order.phone_e164 || order.phone_raw}
                label="复制号码"
                toastText="已复制完整手机号"
                size="md"
                variant="primary"
              />
              {order.country_calling_code && order.phone_national && order.phone_national !== order.phone_e164 ? (
                <CopyButton
                  value={order.phone_national}
                  label="无区号"
                  toastText="已复制本地号码（不含区号）"
                  size="md"
                />
              ) : null}
            </div>
          </div>
        )}

        {/* 倒计时进度条 */}
        {isWaiting && Number.isFinite(expiresMs) ? (
          <div>
            <div className="mb-1.5 flex items-center justify-between text-xs">
              <span className="inline-flex items-center gap-1.5 font-medium text-slate-600">
                <Clock className="h-3.5 w-3.5 text-amber-500" />
                {order.status === "waiting_next_code"
                  ? "等待重发的下一条短信..."
                  : "等待短信验证码（超时自动退款）"}
              </span>
              <span className="font-mono-code font-semibold text-slate-800">{countdownText}</span>
            </div>
            <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
              <div
                className={`h-full rounded-full transition-all duration-1000 ${barColor}`}
                style={{ width: `${progressPct}%` }}
              />
            </div>
          </div>
        ) : null}

        {/* 验证码核心高亮区域 */}
        {hasCode ? (
          <div className="rounded-2xl border border-emerald-200 bg-gradient-to-br from-emerald-50 to-teal-50/60 p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <div className="inline-flex items-center gap-1.5 text-xs font-semibold text-emerald-700">
                  <Sparkles className="h-3.5 w-3.5" />
                  <span>
                    已收到短信验证码
                    {order.code_count > 1 ? `（第 ${order.code_count} 条）` : ""}
                  </span>
                </div>
                <div className="font-mono-code mt-1 text-3xl font-extrabold tracking-[0.2em] text-emerald-950 sm:text-4xl">
                  {order.latest_code}
                </div>
              </div>
              <CopyButton
                value={order.latest_code}
                label="复制验证码"
                toastText={`已复制验证码 ${order.latest_code}`}
                size="lg"
                variant="emerald"
              />
            </div>

            {order.messages && order.messages.length > 1 ? (
              <div className="mt-3 border-t border-emerald-200/70 pt-2.5">
                <div className="mb-1.5 text-[11px] font-medium text-emerald-800/80">
                  全部已收短信记录
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {order.messages.map((msg) => (
                    <span
                      key={msg.id}
                      className="inline-flex items-center gap-1.5 rounded-lg border border-emerald-200 bg-white/90 px-2 py-1 text-xs"
                    >
                      <span className="text-slate-400">#{msg.seq}</span>
                      <strong className="font-mono-code text-emerald-900">{msg.code}</strong>
                      <span className="text-[11px] text-slate-400">
                        {formatDateTime(msg.received_at).slice(-8)}
                      </span>
                    </span>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
        ) : order.status === "compensation_pending" ? (
          <div className="flex items-center gap-2.5 rounded-xl border border-amber-200 bg-amber-50/70 px-3.5 py-3 text-xs text-amber-800">
            <RotateCcw className="h-4 w-4 shrink-0 text-amber-600" />
            <span>
              上游暂处于取消冷却期或重试窗口，后台正自动轮询完成取消退款，无需停留在当前页。
            </span>
          </div>
        ) : order.status === "cancelling" || order.status === "completing" ? (
          <div className="flex items-center gap-2.5 rounded-xl border border-slate-200 bg-slate-50/70 px-3.5 py-3 text-xs text-slate-600">
            <Loader2 className="h-4 w-4 shrink-0 animate-spin text-slate-500" />
            <span>
              {order.status === "completing" ? "正在向上游平台确认完成订单..." : "正在向上游平台申请取消并退款..."}
            </span>
          </div>
        ) : order.code_scrubbed ? (
          <div className="flex items-center gap-2.5 rounded-xl border border-dashed border-slate-200 bg-slate-50/60 px-3.5 py-3 text-xs text-slate-500">
            <MessageSquareText className="h-4 w-4 shrink-0 text-slate-400" />
            <span>
              该订单历史验证码已按安全策略脱敏清理；如需新验证码可点击下方「再收一条短信」。
            </span>
          </div>
        ) : order.status !== "acquiring" ? (
          <div className="flex items-center gap-2.5 rounded-xl border border-dashed border-slate-200 bg-slate-50/50 px-3.5 py-3 text-xs text-slate-500">
            <MessageSquareText className="h-4 w-4 shrink-0 text-slate-400" />
            <span>
              请将上方手机号填入目标应用发送短信，收到后将通过 SSE 实时弹出验证码。
            </span>
          </div>
        ) : null}

        {order.error_message ? (
          <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
            {order.error_message}
          </div>
        ) : null}

        {/* 操作按钮栏 */}
        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-100 pt-3">
          <div className="flex flex-wrap items-center gap-2">
            {order.status === "code_received" ? (
              <>
                <button
                  type="button"
                  disabled={isBusy}
                  onClick={() => onComplete(order.order_id)}
                  className="inline-flex cursor-pointer items-center gap-1.5 rounded-xl bg-emerald-600 px-3.5 py-2 text-xs font-semibold text-white shadow-2xs transition hover:bg-emerald-700 disabled:opacity-50"
                >
                  {busyAction === "complete" ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <CheckCircle2 className="h-3.5 w-3.5" />
                  )}
                  确认完成
                </button>
                {supportsResend ? (
                  <button
                    type="button"
                    disabled={isBusy}
                    onClick={() => onResend(order.order_id)}
                    className="inline-flex cursor-pointer items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-700 transition hover:border-slate-300 hover:bg-slate-50 disabled:opacity-50"
                  >
                    {busyAction === "resend" ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <RefreshCcw className="h-3.5 w-3.5" />
                    )}
                    再收一条短信
                  </button>
                ) : null}
              </>
            ) : null}
          </div>

          {!isTransitioning ? (
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                disabled={isBusy}
                onClick={() => onCancel(order.order_id, false)}
                className="inline-flex cursor-pointer items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-600 transition hover:border-rose-200 hover:bg-rose-50 hover:text-rose-700 disabled:opacity-50"
              >
                {busyAction === "cancel" ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : order.status === "acquiring" ? (
                  <XCircle className="h-3.5 w-3.5" />
                ) : (
                  <RotateCcw className="h-3.5 w-3.5" />
                )}
                {order.status === "acquiring" ? "停止抢号" : order.status === "compensation_pending" ? "立即重试退款" : "取消退款"}
              </button>

              {order.phone_e164 || order.phone_raw ? (
                <button
                  type="button"
                  disabled={isBusy}
                  onClick={() => onCancel(order.order_id, true)}
                  className="inline-flex cursor-pointer items-center gap-1.5 rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-xs font-medium text-rose-700 transition hover:bg-rose-100 disabled:opacity-50"
                  title="取消激活申请退款，同时拉黑该号码防止重复抽到"
                >
                  {busyAction === "blacklist" ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <Ban className="h-3.5 w-3.5" />
                  )}
                  拉黑并退款
                </button>
              ) : null}
            </div>
          ) : null}
        </div>
      </div>
    </article>
  );
}
