import { useState } from "react";
import { RefreshCw } from "lucide-react";
import type { PriceSnapshot } from "../api/types";
import { formatPrice } from "../utils/format";

interface PriceTierMatrixProps {
  snapshot: PriceSnapshot | null;
  loading: boolean;
  error: string;
  selectedPrice?: string;
  providerLabel?: string;
  onSelectPrice: (cost: number) => void;
  onRefresh: () => void;
}

export function PriceTierMatrix({
  snapshot,
  loading,
  error,
  selectedPrice,
  providerLabel = "SMSBower",
  onSelectPrice,
  onRefresh,
}: PriceTierMatrixProps) {
  const [expanded, setExpanded] = useState(false);
  const entries = snapshot?.entries || [];
  const visibleEntries = expanded ? entries : entries.slice(0, 9);

  return (
    <div className="surface-well rounded-xl p-3">
      <div className="mb-2.5 flex items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-1.5 text-xs font-medium text-slate-700">
          <span>{providerLabel} 实时价档</span>
          {snapshot ? (
            <span className="font-mono-code rounded-full bg-white px-2 py-0.5 text-[11px] text-slate-600 shadow-2xs ring-1 ring-slate-200/80">
              {entries.length} 档 · 库存 {snapshot.stock}
              {snapshot.low !== null ? ` · 最低 ${formatPrice(snapshot.low)}` : ""}
            </span>
          ) : null}
        </div>
        <button
          type="button"
          onClick={onRefresh}
          disabled={loading}
          className="inline-flex shrink-0 cursor-pointer items-center gap-1 rounded-lg border border-slate-200/80 bg-white px-2 py-1 text-[11px] font-medium text-slate-600 shadow-2xs transition hover:border-slate-300 hover:text-slate-900 disabled:opacity-50"
          title="强制刷新实时价格"
        >
          <RefreshCw className={`h-3 w-3 ${loading ? "animate-spin text-emerald-600" : ""}`} />
          <span>刷新</span>
        </button>
      </div>

      {error ? (
        <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
          {error}
        </div>
      ) : loading && !snapshot ? (
        <div className="py-5 text-center text-xs text-slate-400">正在查询 {providerLabel} 实时价格与库存...</div>
      ) : entries.length === 0 ? (
        <div className="py-4 text-center text-xs text-slate-400">
          当前国家与服务暂无可用库存档位
        </div>
      ) : (
        <>
          <div className="grid grid-cols-3 gap-2">
            {visibleEntries.map((tier) => {
              const outOfStock = tier.count <= 0;
              const isSelected =
                selectedPrice !== undefined &&
                selectedPrice !== "" &&
                Math.abs(Number(selectedPrice) - tier.cost) < 0.0002;
              const dotTone = outOfStock
                ? "bg-slate-300"
                : tier.count <= 5
                  ? "bg-rose-500"
                  : tier.count <= 30
                    ? "bg-amber-500"
                    : "bg-emerald-500";
              const cardTone = outOfStock
                ? "border-slate-200/70 bg-slate-100/60 text-slate-400 opacity-65"
                : "border-slate-200/90 bg-white text-slate-800 shadow-2xs hover:border-emerald-400 hover:shadow-xs";
              return (
                <button
                  key={tier.cost}
                  type="button"
                  disabled={loading}
                  onClick={() => onSelectPrice(tier.cost)}
                  title={
                    isSelected
                      ? "再次点击取消该目标限价"
                      : outOfStock
                        ? "当前档位库存为 0（仍可设为目标抢号价）"
                        : "点击设为目标价格上限"
                  }
                  className={`flex cursor-pointer flex-col items-start rounded-xl border px-2.5 py-2 text-left transition disabled:cursor-wait disabled:opacity-50 ${
                    isSelected
                      ? "border-emerald-600 bg-emerald-50/95 shadow-xs ring-2 ring-emerald-500/20"
                      : cardTone
                  }`}
                >
                  <div className="flex w-full items-center justify-between gap-1">
                    <span
                      className={`font-mono-code text-xs font-bold ${
                        isSelected ? "text-emerald-900" : outOfStock ? "text-slate-400" : "text-slate-900"
                      }`}
                    >
                      {formatPrice(tier.cost)}
                    </span>
                    <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${dotTone}`} />
                  </div>
                  <span className="mt-1 text-[11px] text-slate-500">
                    库存{" "}
                    <strong className={`font-mono-code ${outOfStock ? "text-slate-400" : "text-slate-700"}`}>
                      {tier.count}
                    </strong>
                  </span>
                </button>
              );
            })}
          </div>
          {entries.length > 9 ? (
            <div className="mt-2.5 text-center">
              <button
                type="button"
                onClick={() => setExpanded((v) => !v)}
                className="cursor-pointer rounded-md px-2 py-0.5 text-[11px] font-medium text-indigo-600 hover:bg-white/80 hover:text-indigo-700"
              >
                {expanded ? "收起多余价档" : `展开全部 ${entries.length} 个价档`}
              </button>
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}
