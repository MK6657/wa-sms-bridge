interface ToggleSwitchProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  label?: string;
  description?: string;
  size?: "sm" | "md";
  className?: string;
}

export function ToggleSwitch({
  checked,
  onChange,
  disabled = false,
  label,
  description,
  size = "sm",
  className = "",
}: ToggleSwitchProps) {
  const trackSize = size === "md" ? "h-5 w-9" : "h-4 w-7";
  const thumbSize = size === "md" ? "h-4 w-4" : "h-3 w-3";
  const translateClass = size === "md" ? "translate-x-4" : "translate-x-3";

  const control = (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => {
        if (!disabled) onChange(!checked);
      }}
      className={`relative inline-flex shrink-0 cursor-pointer items-center rounded-full p-0.5 transition-colors duration-150 focus:outline-none focus:ring-3 focus:ring-emerald-500/20 disabled:cursor-not-allowed disabled:opacity-45 ${trackSize} ${
        checked ? "bg-emerald-600" : "bg-slate-300/90 hover:bg-slate-400/80"
      }`}
    >
      <span
        className={`pointer-events-none inline-block rounded-full bg-white shadow-xs transition-transform duration-150 ${thumbSize} ${
          checked ? translateClass : "translate-x-0"
        }`}
      />
    </button>
  );

  if (!label && !description) {
    return control;
  }

  return (
    <div
      onClick={() => {
        if (!disabled) onChange(!checked);
      }}
      className={`flex items-center justify-between gap-3 select-none ${
        disabled ? "cursor-not-allowed opacity-55" : "cursor-pointer"
      } ${className}`}
    >
      <div className="min-w-0 flex-1">
        {label ? <div className="text-xs font-medium text-slate-800">{label}</div> : null}
        {description ? <div className="mt-0.5 text-[11px] leading-relaxed text-slate-500">{description}</div> : null}
      </div>
      <div onClick={(e) => e.stopPropagation()}>{control}</div>
    </div>
  );
}
