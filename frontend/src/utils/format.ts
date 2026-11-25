import type { CurrencyBreakdown, FinancialAggregateFields, OrderStatus } from "../api/types";

function normalizeCurrencyCode(currency?: string | null): string {
  const code = (currency || "").trim().toUpperCase();
  return code || "UNKNOWN";
}

function formatWithCurrency(formattedNum: string, currency?: string | null): string {
  const code = normalizeCurrencyCode(currency);
  if (code === "USD") return `$${formattedNum}`;
  if (code === "UNKNOWN") return `未知币种 ${formattedNum}`;
  return `${code} ${formattedNum}`;
}

export function formatMoney(
  value: number | null | undefined,
  currency: string | null = "USD",
  digits = 2,
): string {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return formatWithCurrency(Number(value).toFixed(digits), currency);
}

export function formatPrice(
  value: number | null | undefined,
  currency: string | null = "USD",
): string {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  const text = Number(value).toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
  return formatWithCurrency(text, currency);
}

export interface FormattedFinancialSummary {
  /** 主展示文本：单币种展示该币种金额；多币种按币种拆分拼接，绝不直接跨币种相加 */
  primary: string;
  /** 每个币种的实付/估算/退款明细 */
  lines: string[];
  /** 显式提供汇率与折算日期时的折算合计说明 */
  fxNote: string | null;
  isMultiCurrency: boolean;
}

function formatBucketHeadline(bucket: CurrencyBreakdown): string {
  const netOrTotal = bucket.cost_total ?? (bucket.net_actual_cost_total + bucket.estimated_cost_total);
  return formatMoney(netOrTotal, bucket.currency);
}

function formatBucketDetail(bucket: CurrencyBreakdown): string {
  const parts: string[] = [];
  if (bucket.actual_cost_total > 0) {
    parts.push(`实付 ${formatMoney(bucket.actual_cost_total, bucket.currency)}`);
  }
  if (bucket.refunded_amount_total > 0) {
    parts.push(`已退 ${formatMoney(bucket.refunded_amount_total, bucket.currency)}`);
  }
  if (bucket.estimated_cost_total > 0) {
    parts.push(`估算 ${formatMoney(bucket.estimated_cost_total, bucket.currency)}`);
  }
  if ((bucket.refund_amount_unknown_count ?? 0) > 0) {
    parts.push(`金额未知退款 ${bucket.refund_amount_unknown_count} 笔`);
  }
  if (bucket.unknown_cost_count > 0) {
    parts.push(`未知成本 ${bucket.unknown_cost_count} 笔`);
  }
  if (parts.length === 0) {
    parts.push(`净消耗 ${formatMoney(0, bucket.currency)}`);
  }
  const label = normalizeCurrencyCode(bucket.currency) === "UNKNOWN" ? "未知币种" : normalizeCurrencyCode(bucket.currency);
  return `${label}: ${parts.join(" · ")}`;
}

export function formatFinancialSummary(
  summary: FinancialAggregateFields | null | undefined,
): FormattedFinancialSummary {
  if (!summary) {
    return { primary: "0.00", lines: [], fxNote: null, isMultiCurrency: false };
  }

  const buckets = Array.isArray(summary.by_currency) ? summary.by_currency : [];
  const reviewBuckets = Array.isArray(summary.currency_review_by_currency)
    ? summary.currency_review_by_currency
    : [];
  const reviewCount = Number(summary.currency_review_count || 0);
  const reviewLines =
    reviewCount > 0
      ? [
          `待核对币种 (${reviewCount} 笔): ${
            reviewBuckets.length > 0 ? reviewBuckets.map(formatBucketHeadline).join(" + ") : "待复核"
          }`,
        ]
      : [];
  const fx = summary.fx_conversion;
  const fxNote =
    fx && fx.target_currency && fx.as_of_date
      ? `折算合计 (${fx.target_currency}, 汇率日期 ${fx.as_of_date}): ${formatMoney(fx.cost_total, fx.target_currency)}`
      : null;

  if (buckets.length > 1 || summary.is_multi_currency || reviewCount > 0) {
    const knownPrimary = buckets.length > 0 ? buckets.map(formatBucketHeadline).join(" + ") : "";
    const primary =
      reviewCount > 0
        ? knownPrimary
          ? `${knownPrimary}（+${reviewCount} 笔待核对）`
          : `待核对币种 (${reviewCount} 笔)`
        : knownPrimary || "多币种明细";
    return {
      primary,
      lines: [...buckets.map(formatBucketDetail), ...reviewLines],
      fxNote,
      isMultiCurrency: buckets.length > 1 || Boolean(summary.is_multi_currency) || reviewCount > 0,
    };
  }

  if (buckets.length === 1) {
    const only = buckets[0];
    return {
      primary: formatBucketHeadline(only),
      lines: [formatBucketDetail(only)],
      fxNote,
      isMultiCurrency: false,
    };
  }

  if (summary.cost_total !== null && summary.cost_total !== undefined) {
    const ccy = summary.currency ? summary.currency : null;
    return {
      primary: ccy ? formatMoney(summary.cost_total, ccy) : Number(summary.cost_total).toFixed(2),
      lines: [],
      fxNote,
      isMultiCurrency: false,
    };
  }

  return { primary: "0.00", lines: [], fxNote, isMultiCurrency: false };
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  const ts = Date.parse(iso);
  if (!Number.isFinite(ts)) return iso;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(ts));
}

export function normCountryQuery(v: string): string {
  return String(v || "").toLowerCase().replace(/[\s·._+#-]+/g, "").trim();
}

export function maskCode(code: string): string {
  if (!code) return "";
  if (code.length <= 2) return "****";
  return `${code.slice(0, 2)}${"•".repeat(Math.max(2, code.length - 2))}`;
}

export async function copyToClipboard(text: string): Promise<boolean> {
  if (!text) return false;
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(area);
    return ok;
  }
}

export function playCodeChime(): void {
  try {
    const AudioCtx = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    if (!AudioCtx) return;
    const ctx = new AudioCtx();
    const now = ctx.currentTime;
    const freqs = [660, 880];
    freqs.forEach((freq, idx) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.setValueAtTime(freq, now + idx * 0.11);
      gain.gain.setValueAtTime(0.001, now + idx * 0.11);
      gain.gain.exponentialRampToValueAtTime(0.08, now + idx * 0.11 + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + idx * 0.11 + 0.24);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(now + idx * 0.11);
      osc.stop(now + idx * 0.11 + 0.25);
    });
    window.setTimeout(() => ctx.close().catch(() => {}), 700);
  } catch {
    // ignore browser audio restriction
  }
}

export const ORDER_STATUS_META: Record<
  OrderStatus,
  { label: string; badge: string; dot: string }
> = {
  acquiring: {
    label: "取号中",
    badge: "bg-indigo-50 text-indigo-700 border-indigo-200",
    dot: "bg-indigo-500 animate-pulse",
  },
  waiting_code: {
    label: "等码中",
    badge: "bg-amber-50 text-amber-700 border-amber-200",
    dot: "bg-amber-500 animate-pulse",
  },
  code_received: {
    label: "已收到验证码",
    badge: "bg-emerald-50 text-emerald-700 border-emerald-200",
    dot: "bg-emerald-500",
  },
  waiting_next_code: {
    label: "重发等码中",
    badge: "bg-sky-50 text-sky-700 border-sky-200",
    dot: "bg-sky-500 animate-pulse",
  },
  completing: {
    label: "确认完成中",
    badge: "bg-emerald-50 text-emerald-700 border-emerald-200",
    dot: "bg-emerald-500 animate-pulse",
  },
  cancelling: {
    label: "取消退款中",
    badge: "bg-amber-50 text-amber-700 border-amber-200",
    dot: "bg-amber-500 animate-pulse",
  },
  completed: {
    label: "已完成",
    badge: "bg-emerald-50 text-emerald-700 border-emerald-200",
    dot: "bg-emerald-500",
  },
  cancelled: {
    label: "已取消退款",
    badge: "bg-slate-100 text-slate-600 border-slate-200",
    dot: "bg-slate-400",
  },
  expired: {
    label: "超时已退",
    badge: "bg-orange-50 text-orange-700 border-orange-200",
    dot: "bg-orange-400",
  },
  failed: {
    label: "失败",
    badge: "bg-rose-50 text-rose-700 border-rose-200",
    dot: "bg-rose-500",
  },
  compensation_pending: {
    label: "退款重试中",
    badge: "bg-amber-50 text-amber-700 border-amber-200",
    dot: "bg-amber-500 animate-pulse",
  },
};

export function resolveOrderStatusMeta(
  status: OrderStatus,
  refundState?: string,
): { label: string; badge: string; dot: string } {
  if (status === "cancelled") {
    if (refundState === "unrefundable") {
      return {
        label: "已取消(未退)",
        badge: "bg-rose-50 text-rose-700 border-rose-200",
        dot: "bg-rose-400",
      };
    }
    if (refundState === "unknown") {
      return {
        label: "已取消(待核对)",
        badge: "bg-amber-50 text-amber-700 border-amber-200",
        dot: "bg-amber-500",
      };
    }
    if (refundState === "none") {
      return {
        label: "已取消",
        badge: "bg-slate-100 text-slate-600 border-slate-200",
        dot: "bg-slate-400",
      };
    }
  }
  if (status === "expired") {
    if (refundState === "unrefundable") {
      return {
        label: "超时(未退)",
        badge: "bg-rose-50 text-rose-700 border-rose-200",
        dot: "bg-rose-400",
      };
    }
    if (refundState === "unknown") {
      return {
        label: "超时(待核对)",
        badge: "bg-amber-50 text-amber-700 border-amber-200",
        dot: "bg-amber-500",
      };
    }
    if (refundState === "none") {
      return {
        label: "已超时",
        badge: "bg-orange-50 text-orange-700 border-orange-200",
        dot: "bg-orange-400",
      };
    }
  }
  return (
    ORDER_STATUS_META[status] || {
      label: status,
      badge: "bg-slate-100 text-slate-600 border-slate-200",
      dot: "bg-slate-400",
    }
  );
}

export const ACTIVE_STATUSES = new Set<OrderStatus>([
  "acquiring",
  "waiting_code",
  "code_received",
  "waiting_next_code",
  "completing",
  "cancelling",
  "compensation_pending",
]);
