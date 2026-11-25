import { useEffect, useRef, useState } from "react";
import { Check, Copy } from "lucide-react";
import { copyToClipboard } from "../utils/format";
import { useToast } from "./ToastProvider";

interface CopyButtonProps {
  value: string;
  label?: string;
  toastText?: string;
  size?: "sm" | "md" | "lg";
  variant?: "subtle" | "primary" | "emerald";
  className?: string;
}

export function CopyButton({
  value,
  label,
  toastText = "已复制到剪贴板",
  size = "sm",
  variant = "subtle",
  className = "",
}: CopyButtonProps) {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<number | null>(null);
  const { notify } = useToast();

  useEffect(() => {
    return () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    };
  }, []);

  const handleCopy = async () => {
    const ok = await copyToClipboard(value);
    if (ok) {
      setCopied(true);
      notify(toastText, "success");
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
      timerRef.current = window.setTimeout(() => setCopied(false), 1600);
    } else {
      notify("复制失败，请手动复制", "error");
    }
  };

  const sizeClass =
    size === "lg"
      ? "px-4 py-2 text-sm gap-2 rounded-xl"
      : size === "md"
        ? "px-3 py-1.5 text-xs gap-1.5 rounded-lg"
        : "px-2 py-1 text-xs gap-1 rounded-md";

  const variantClass =
    variant === "emerald"
      ? "bg-emerald-600 text-white hover:bg-emerald-700 shadow-xs"
      : variant === "primary"
        ? "bg-slate-900 text-white hover:bg-slate-800"
        : "border border-slate-200 bg-white text-slate-700 hover:bg-slate-50 hover:border-slate-300";

  return (
    <button
      type="button"
      onClick={handleCopy}
      disabled={!value}
      title={label || toastText}
      aria-label={label || toastText}
      className={`inline-flex shrink-0 cursor-pointer items-center whitespace-nowrap font-medium transition disabled:cursor-not-allowed disabled:opacity-40 ${sizeClass} ${variantClass} ${className}`}
    >
      {copied ? (
        <Check className={size === "lg" ? "h-4 w-4" : "h-3.5 w-3.5"} />
      ) : (
        <Copy className={size === "lg" ? "h-4 w-4" : "h-3.5 w-3.5"} />
      )}
      {label ? <span>{copied ? "已复制" : label}</span> : null}
    </button>
  );
}
