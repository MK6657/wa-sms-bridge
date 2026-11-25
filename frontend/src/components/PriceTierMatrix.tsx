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
  const visibleEntries = expanded ? entries : entries.slice(0, 12);

  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50/70 p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-xs font-medium text-slate-600">
          <span>{providerLabel} 实时价档</span>
          {snapshot ? (
            <span className="rounded-full bg-white px-2 py-0.5 font-mono-code text-[11px] text-slate-500 ring-1 ring-slate-200">
              {entries.length} 档 · 库存 {snapshot.stock}
            </span>
          ) : null}
        </div>
        <button
          type="button"
          onClick={onRefresh}
          disabled={loading}
          className="inline-flex cursor-pointer items-center gap-1 rounded-md px-1.5 py-0.5 text-xs text-slate-500 hover:bg-white hover:text-slate-800 disabled:opacity-50"
          title="强制刷新实时价格"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
          <span>刷新</span>
        </button>
      </div>

      {error ? (
        <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
          {error}
        </div>
      ) : loading && !snapshot ? (
        <div className="py-4 text-center text-xs text-slate-400">正在查询 {providerLabel} 价格档位...</div>
      ) : entries.length === 0 ? (
        <div className="py-3 text-center text-xs text-slate-400">
          当前国家与服务暂无可用库存档位
        </div>
      ) : (
        <>
          <div className="grid grid-cols-3 gap-1.5 sm:grid-cols-4">
            {visibleEntries.map((tier) => {
              const isSelected =
                selectedPrice !== undefined &&
                selectedPrice !== "" &&
                Math.abs(Number(selectedPrice) - tier.cost) < 0.0002;
              const tone =
                tier.count <= 0
                  ? "border-slate-200 bg-slate-100/70 text-slate-400"
                  : tier.count <= 5
                    ? "border-rose-200 bg-white text-slate-800 hover:border-rose-400"
                    : tier.count <= 30
                      ? "border-amber-200 bg-white text-slate-800 hover:border-amber-400"
                      : "border-emerald-200 bg-white text-slate-800 hover:border-emerald-500";
              return (
                <button
                  key={tier.cost}
                  type="button"
                  disabled={loading}
                  onClick={() => onSelectPrice(tier.cost)}
                  title={isSelected ? "再次点击取消选中该价档" : "点击设为目标价格上限"}
                  className={`flex cursor-pointer flex-col items-start rounded-lg border px-2.5 py-1.5 text-left transition disabled:cursor-wait disabled:opacity-50 ${
                    isSelected
                      ? "border-emerald-600 bg-emerald-50/90 ring-2 ring-emerald-500/20"
                      : tone
                  }`}
                >
                  <span className="font-mono-code text-xs font-semibold text-slate-900">
                    {formatPrice(tier.cost)}
                  </span>
                  <span className="mt-0.5 text-[11px] text-slate-500">
                    库存 <strong className="font-mono-code text-slate-700">{tier.count}</strong>
                  </span>
                </button>
              );
            })}
          </div>
          {entries.length > 12 ? (
            <div className="mt-2 text-center">
              <button
                type="button"
                onClick={() => setExpanded((v) => !v)}
                className="cursor-pointer text-[11px] font-medium text-indigo-600 hover:text-indigo-700"
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
