import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { AlertTriangle } from "lucide-react";

interface ConfirmModalProps {
  open: boolean;
  title: string;
  description: string;
  confirmLabel?: string;
  danger?: boolean;
  loading?: boolean;
  onConfirm: () => void | Promise<unknown>;
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
  const cancelBtnRef = useRef<HTMLButtonElement>(null);
  const confirmBtnRef = useRef<HTMLButtonElement>(null);
  const prevFocusRef = useRef<HTMLElement | null>(null);
  const confirmLockRef = useRef(false);

  useEffect(() => {
    if (!loading) {
      confirmLockRef.current = false;
    }
  }, [loading, open]);

  useEffect(() => {
    if (open) {
      confirmLockRef.current = false;
      prevFocusRef.current =
        typeof document !== "undefined" && document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null;
      cancelBtnRef.current?.focus();
      return;
    }
    if (prevFocusRef.current) {
      const target = prevFocusRef.current;
      prevFocusRef.current = null;
      target.focus?.();
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !loading) {
        onCancel();
        return;
      }
      if (e.key === "Tab") {
        const focusables = [cancelBtnRef.current, confirmBtnRef.current].filter(
          (el): el is HTMLButtonElement => Boolean(el && !el.disabled),
        );
        if (focusables.length === 0) {
          e.preventDefault();
          return;
        }
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        const active = typeof document !== "undefined" ? document.activeElement : null;
        if (e.shiftKey) {
          if (active === first || !focusables.includes(active as HTMLButtonElement)) {
            e.preventDefault();
            last.focus();
          }
        } else if (active === last || !focusables.includes(active as HTMLButtonElement)) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, loading, onCancel]);

  if (!open) return null;
  const modalNode = (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={title}
      onClick={(e) => {
        if (e.target === e.currentTarget && !loading) onCancel();
      }}
      className="fixed inset-0 z-[60] flex items-center justify-center bg-slate-900/45 p-4 backdrop-blur-xs"
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
            ref={cancelBtnRef}
            type="button"
            onClick={onCancel}
            disabled={loading}
            className="cursor-pointer rounded-xl border border-slate-200 bg-white px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
          >
            取消
          </button>
          <button
            ref={confirmBtnRef}
            type="button"
            onClick={() => {
              if (loading || confirmLockRef.current) return;
              confirmLockRef.current = true;
              Promise.resolve(onConfirm()).finally(() => {
                confirmLockRef.current = false;
              });
            }}
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

  return typeof document !== "undefined" ? createPortal(modalNode, document.body) : modalNode;
}
