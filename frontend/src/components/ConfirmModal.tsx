import { useEffect } from "react";
import { AlertTriangle } from "lucide-react";

interface ConfirmModalProps {
  open: boolean;
  title: string;
  description: string;
  confirmLabel?: string;
  danger?: boolean;
  loading?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmModal({
  open,
  title,
  description,
  confirmLabel = "确认",
  danger = false,
  loading = false,
  onConfirm,
  onCancel,
}: ConfirmModalProps) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !loading) {
        onCancel();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, loading, onCancel]);

  if (!open) return null;
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={title}
      onClick={() => {
        if (!loading) onCancel();
      }}
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4 backdrop-blur-xs"
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-md rounded-2xl border border-slate-200 bg-white p-5 shadow-2xl"
      >
        <div className="flex items-start gap-3">
          <div
            className={`mt-0.5 rounded-xl p-2 ${danger ? "bg-rose-50 text-rose-600" : "bg-amber-50 text-amber-600"}`}
          >
            <AlertTriangle className="h-5 w-5" />
          </div>
          <div className="min-w-0 flex-1">
            <h3 className="text-base font-semibold text-slate-900">{title}</h3>
            <p className="mt-1.5 text-sm leading-relaxed text-slate-600">{description}</p>
          </div>
        </div>
        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            disabled={loading}
            className="cursor-pointer rounded-xl border border-slate-200 bg-white px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
          >
            取消
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={loading}
            className={`cursor-pointer rounded-xl px-4 py-2 text-sm font-medium text-white transition disabled:opacity-50 ${
              danger ? "bg-rose-600 hover:bg-rose-700" : "bg-slate-900 hover:bg-slate-800"
            }`}
          >
            {loading ? "处理中..." : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
