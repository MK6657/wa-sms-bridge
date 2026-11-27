import { useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown } from "lucide-react";

export type BadgeTone = "emerald" | "indigo" | "amber" | "rose" | "slate" | "teal";

export interface SelectOption {
  value: string;
  label: string;
  description?: string;
  badge?: string;
  badgeTone?: BadgeTone;
  dotColor?: string;
  group?: string;
}

interface CustomSelectProps {
  value: string;
  onChange: (value: string) => void;
  options: SelectOption[];
  placeholder?: string;
  size?: "sm" | "md";
  activeHighlight?: boolean;
  align?: "left" | "right";
  minMenuWidth?: number;
  className?: string;
  footerAction?: {
    label: string;
    onClick: () => void;
  };
}

const BADGE_TONES: Record<BadgeTone, string> = {
  emerald: "bg-emerald-50 text-emerald-700 ring-emerald-200/80",
  teal: "bg-teal-50 text-teal-700 ring-teal-200/80",
  indigo: "bg-indigo-50 text-indigo-700 ring-indigo-200/80",
  amber: "bg-amber-50 text-amber-700 ring-amber-200/80",
  rose: "bg-rose-50 text-rose-700 ring-rose-200/80",
  slate: "bg-slate-100 text-slate-600 ring-slate-200/80",
};

export function CustomSelect({
  value,
  onChange,
  options,
  placeholder = "请选择...",
  size = "md",
  activeHighlight = false,
  align = "left",
  minMenuWidth = 220,
  className = "",
  footerAction,
}: CustomSelectProps) {
  const [open, setOpen] = useState(false);
  const [openUpward, setOpenUpward] = useState(false);
  const [activeIdx, setActiveIdx] = useState(0);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);

  const selectedOption = useMemo(
    () => options.find((opt) => opt.value === value) || null,
    [options, value],
  );

  const toggleOpen = () => {
    if (!open && triggerRef.current) {
      const rect = triggerRef.current.getBoundingClientRect();
      const spaceBelow = window.innerHeight - rect.bottom;
      setOpenUpward(spaceBelow < 260 && rect.top > 260);
      const curIdx = options.findIndex((o) => o.value === value);
      setActiveIdx(curIdx >= 0 ? curIdx : 0);
    }
    setOpen((prev) => !prev);
  };

  useEffect(() => {
    if (!open) return;
    const handleOutside = (e: MouseEvent) => {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const handleGlobalEscape = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    document.addEventListener("mousedown", handleOutside);
    document.addEventListener("keydown", handleGlobalEscape);
    return () => {
      document.removeEventListener("mousedown", handleOutside);
      document.removeEventListener("keydown", handleGlobalEscape);
    };
  }, [open]);

  useEffect(() => {
    if (open && optionRefs.current[activeIdx]) {
      optionRefs.current[activeIdx]?.scrollIntoView({ block: "nearest" });
    }
  }, [open, activeIdx]);

  const selectItem = (nextVal: string) => {
    onChange(nextVal);
    setOpen(false);
  };

  const sizeClass =
    size === "sm"
      ? "px-2.5 py-1.5 text-xs rounded-xl gap-1.5"
      : "px-3 py-2 text-xs rounded-xl gap-2";

  const highlighted = activeHighlight && Boolean(selectedOption && selectedOption.value !== "");

  return (
    <div
      ref={wrapperRef}
      className={`relative ${className}`}
      onBlur={(e) => {
        if (open && !wrapperRef.current?.contains(e.relatedTarget as Node)) {
          setOpen(false);
        }
      }}
    >
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={toggleOpen}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            setOpen(false);
          } else if (e.key === "ArrowDown") {
            e.preventDefault();
            if (!open) {
              toggleOpen();
            } else {
              setActiveIdx((i) => Math.min(i + 1, Math.max(0, options.length - 1)));
            }
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            if (!open) {
              toggleOpen();
            } else {
              setActiveIdx((i) => Math.max(i - 1, 0));
            }
          } else if ((e.key === "Enter" || e.key === " ") && open && options[activeIdx]) {
            e.preventDefault();
            selectItem(options[activeIdx].value);
          } else if (e.key === " " && !open) {
            e.preventDefault();
            toggleOpen();
          }
        }}
        className={`flex w-full cursor-pointer items-center justify-between border text-left font-medium transition outline-none ${sizeClass} ${
          open
            ? "border-emerald-500 bg-white text-slate-900 ring-3 ring-emerald-500/12"
            : highlighted
              ? "border-emerald-400/90 bg-emerald-50/70 text-emerald-950 shadow-2xs hover:border-emerald-500"
              : "border-slate-200/95 bg-white text-slate-700 shadow-2xs hover:border-slate-300 hover:bg-slate-50/40"
        }`}
      >
        <span className="flex min-w-0 flex-1 items-center gap-2">
          {selectedOption?.dotColor ? (
            <span className={`h-2 w-2 shrink-0 rounded-full ${selectedOption.dotColor}`} />
          ) : null}
          <span className={`truncate ${selectedOption ? "text-slate-800" : "text-slate-500"}`}>
            {selectedOption ? selectedOption.label : placeholder}
          </span>
          {selectedOption?.badge ? (
            <span
              className={`font-mono-code shrink-0 rounded-md px-1.5 py-0.5 text-[10px] font-semibold ring-1 ring-inset ${
                BADGE_TONES[selectedOption.badgeTone || "slate"]
              }`}
            >
              {selectedOption.badge}
            </span>
          ) : null}
        </span>
        <ChevronDown
          className={`h-3.5 w-3.5 shrink-0 text-slate-400 transition-transform duration-150 ${
            open ? "rotate-180 text-emerald-600" : ""
          }`}
        />
      </button>

      {open ? (
        <div
          role="listbox"
          style={{ minWidth: minMenuWidth }}
          className={`absolute z-40 max-h-72 w-full overflow-y-auto rounded-xl border border-slate-200/95 bg-white p-1.5 shadow-[0_12px_32px_-8px_rgba(15,23,42,0.18),0_2px_6px_rgba(15,23,42,0.06)] ${
            openUpward ? "bottom-[calc(100%+6px)]" : "top-[calc(100%+6px)]"
          } ${align === "right" ? "right-0" : "left-0"}`}
        >
          {options.map((opt, idx) => {
            const isSelected = opt.value === value;
            const isActive = idx === activeIdx;
            const prevGroup = idx > 0 ? options[idx - 1].group : undefined;
            const showGroupHeader = Boolean(opt.group && opt.group !== prevGroup);

            return (
              <div key={opt.value || `__empty_${idx}`}>
                {showGroupHeader ? (
                  <div
                    className={`px-2.5 pb-1 pt-2 text-[10px] font-semibold tracking-wider text-slate-400 uppercase ${
                      idx > 0 ? "mt-1 border-t border-slate-100" : ""
                    }`}
                  >
                    {opt.group}
                  </div>
                ) : null}
                <button
                  ref={(el) => {
                    optionRefs.current[idx] = el;
                  }}
                  type="button"
                  role="option"
                  aria-selected={isSelected}
                  onMouseEnter={() => setActiveIdx(idx)}
                  onMouseDown={(e) => {
                    e.preventDefault();
                  }}
                  onClick={() => selectItem(opt.value)}
                  className={`flex w-full cursor-pointer items-start justify-between gap-2 rounded-lg px-2.5 py-2 text-left text-xs transition ${
                    isActive
                      ? "bg-emerald-50/90 text-emerald-950"
                      : isSelected
                        ? "bg-slate-50 text-slate-900 font-semibold"
                        : "text-slate-700 hover:bg-slate-50"
                  }`}
                >
                  <div className="flex min-w-0 flex-1 items-start gap-2">
                    {opt.dotColor ? (
                      <span className={`mt-1 h-2 w-2 shrink-0 rounded-full ${opt.dotColor}`} />
                    ) : null}
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1.5">
                        <span className={`truncate ${isSelected ? "font-semibold text-slate-900" : "font-medium"}`}>
                          {opt.label}
                        </span>
                      </div>
                      {opt.description ? (
                        <div className="mt-0.5 text-[11px] leading-snug font-normal text-slate-400">
                          {opt.description}
                        </div>
                      ) : null}
                    </div>
                  </div>

                  <div className="flex shrink-0 items-center gap-1.5">
                    {opt.badge ? (
                      <span
                        className={`font-mono-code rounded-md px-1.5 py-0.5 text-[10px] font-semibold ring-1 ring-inset ${
                          BADGE_TONES[opt.badgeTone || "slate"]
                        }`}
                      >
                        {opt.badge}
                      </span>
                    ) : null}
                    <span className="flex h-4 w-4 items-center justify-center">
                      {isSelected ? <Check className="h-3.5 w-3.5 text-emerald-600" /> : null}
                    </span>
                  </div>
                </button>
              </div>
            );
          })}

          {footerAction ? (
            <div className="mt-1 border-t border-slate-100 pt-1">
              <button
                type="button"
                onMouseDown={(e) => {
                  e.preventDefault();
                }}
                onClick={() => {
                  setOpen(false);
                  footerAction.onClick();
                }}
                className="flex w-full cursor-pointer items-center justify-between rounded-lg px-2.5 py-2 text-xs font-medium text-indigo-600 transition hover:bg-indigo-50/70"
              >
                <span>{footerAction.label}</span>
              </button>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
