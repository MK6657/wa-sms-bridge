import { useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, Search } from "lucide-react";
import type { CountryOption } from "../api/types";
import { normCountryQuery } from "../utils/format";

export { normCountryQuery };

const POPULAR_ISO_ORDER = ["th", "id", "za", "ph", "vn", "in", "my", "us", "gb", "ru"];

const FALLBACK_COUNTRIES: CountryOption[] = [
  { id: 52, name: "Thailand", name_zh: "泰国", cc: "66", iso: "th", aliases: ["taiguo", "thailand"] },
  { id: 6, name: "Indonesia", name_zh: "印度尼西亚", cc: "62", iso: "id", aliases: ["yinni", "indonesia"] },
  { id: 31, name: "South Africa", name_zh: "南非", cc: "27", iso: "za", aliases: ["nanfei", "southafrica"] },
  { id: 4, name: "Philippines", name_zh: "菲律宾", cc: "63", iso: "ph", aliases: ["feilvbin"] },
  { id: 10, name: "Viet nam", name_zh: "越南", cc: "84", iso: "vn", aliases: ["yuenan", "vietnam"] },
  { id: 22, name: "India", name_zh: "印度", cc: "91", iso: "in", aliases: ["yindu"] },
  { id: 7, name: "Malaysia", name_zh: "马来西亚", cc: "60", iso: "my", aliases: ["malaixiya"] },
  { id: 187, name: "United States", name_zh: "美国", cc: "1", iso: "us", aliases: ["meiguo", "usa"] },
  { id: 16, name: "United Kingdom", name_zh: "英国", cc: "44", iso: "gb", aliases: ["yingguo", "uk"] },
  { id: 0, name: "Russian Federation", name_zh: "俄罗斯", cc: "7", iso: "ru", aliases: ["eluosi", "russia"] },
];

interface CountryComboboxProps {
  countries: CountryOption[];
  selected: CountryOption | null;
  onSelect: (country: CountryOption) => void;
  provider?: string;
  placeholder?: string;
}

export function formatCountryDisplay(country: CountryOption | null): string {
  if (!country) return "";
  const primary = country.name_zh || country.name;
  const secondary = country.name_zh && country.name !== country.name_zh ? ` · ${country.name}` : "";
  const cc = country.cc ? ` (+${country.cc})` : "";
  return `${primary}${secondary}${cc}`;
}

export function CountryCombobox({
  countries,
  selected,
  onSelect,
  provider = "smsbower",
  placeholder = "搜索国家（中文 / 英文 / ISO / +区号 / #ID）",
}: CountryComboboxProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIdx, setActiveIdx] = useState(0);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([]);

  const list = useMemo(() => {
    const merged = new Map<string, CountryOption>();
    for (const c of countries) {
      merged.set(String(c.id), c);
    }
    if (provider.toLowerCase() === "smsbower") {
      for (const c of FALLBACK_COUNTRIES) {
        if (!merged.has(String(c.id))) merged.set(String(c.id), c);
      }
    }
    return [...merged.values()].sort((a, b) => {
      const aRank = POPULAR_ISO_ORDER.indexOf((a.iso || "").toLowerCase());
      const bRank = POPULAR_ISO_ORDER.indexOf((b.iso || "").toLowerCase());
      if (aRank !== -1 || bRank !== -1) {
        if (aRank === -1) return 1;
        if (bRank === -1) return -1;
        return aRank - bRank;
      }
      return (a.name_zh || a.name).localeCompare(b.name_zh || b.name, "zh-CN");
    });
  }, [countries, provider]);

  const matches = useMemo(() => {
    const q = normCountryQuery(query);
    if (!q) return list.slice(0, 250);
    const scored: Array<{ c: CountryOption; score: number }> = [];
    for (const c of list) {
      const fields = [
        String(c.id),
        c.iso || "",
        c.cc || "",
        c.name_zh || "",
        c.name || "",
        ...(c.aliases || []),
      ].map(normCountryQuery);

      let score = Infinity;
      if (fields.some((f) => f === q)) score = 1;
      else if (fields.some((f) => f.startsWith(q))) score = 2;
      else if (fields.some((f) => f.includes(q))) score = 3;
      if (Number.isFinite(score)) scored.push({ c, score });
    }
    return scored
      .sort((a, b) => a.score - b.score)
      .slice(0, 60)
      .map((s) => s.c);
  }, [list, query]);

  useEffect(() => {
    setActiveIdx(0);
  }, [query, open]);

  useEffect(() => {
    if (open && itemRefs.current[activeIdx]) {
      itemRefs.current[activeIdx]?.scrollIntoView({ block: "nearest" });
    }
  }, [open, activeIdx]);

  useEffect(() => {
    const handleOutside = (e: MouseEvent) => {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) {
        setOpen(false);
        setQuery("");
      }
    };
    document.addEventListener("mousedown", handleOutside);
    return () => document.removeEventListener("mousedown", handleOutside);
  }, []);

  const pick = (c: CountryOption) => {
    onSelect(c);
    setOpen(false);
    setQuery("");
    inputRef.current?.blur();
  };

  const selectedIso = (selected?.iso || "").toUpperCase();

  return (
    <div
      ref={wrapperRef}
      className="relative"
      onBlur={(e) => {
        if (open && !wrapperRef.current?.contains(e.relatedTarget as Node)) {
          setOpen(false);
          setQuery("");
        }
      }}
    >
      <div className="relative">
        {!open && selectedIso ? (
          <span className="font-mono-code pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 rounded-md bg-slate-100 px-1.5 py-0.5 text-[10px] font-bold text-slate-600 ring-1 ring-inset ring-slate-200/80">
            {selectedIso}
          </span>
        ) : (
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
        )}
        <input
          ref={inputRef}
          type="text"
          value={open ? query : formatCountryDisplay(selected)}
          onFocus={() => {
            setOpen(true);
            setQuery("");
          }}
          onClick={() => {
            if (!open) {
              setOpen(true);
              setQuery("");
            }
          }}
          onChange={(e) => {
            setQuery(e.target.value);
            if (!open) setOpen(true);
          }}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              setOpen(false);
              setQuery("");
              inputRef.current?.blur();
            } else if (e.key === "Tab") {
              setOpen(false);
              setQuery("");
            } else if (e.key === "ArrowDown") {
              e.preventDefault();
              setActiveIdx((i) => Math.min(i + 1, Math.max(0, matches.length - 1)));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setActiveIdx((i) => Math.max(i - 1, 0));
            } else if (e.key === "Enter" && !e.nativeEvent.isComposing && open && matches[activeIdx]) {
              e.preventDefault();
              pick(matches[activeIdx]);
            }
          }}
          placeholder={open && selected ? `当前：${formatCountryDisplay(selected)}（输入关键字检索）` : placeholder}
          className={`input-surface w-full rounded-xl py-2.5 pr-9 text-sm font-medium text-slate-900 placeholder:font-normal placeholder:text-slate-400 ${
            !open && selectedIso ? "pl-11" : "pl-9"
          }`}
        />
        <ChevronDown
          className={`pointer-events-none absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400 transition-transform ${open ? "rotate-180 text-emerald-600" : ""}`}
        />
      </div>

      {open && (
        <div className="absolute left-0 right-0 top-[calc(100%+6px)] z-40 max-h-72 overflow-y-auto rounded-xl border border-slate-200/95 bg-white p-1.5 shadow-[0_12px_32px_-8px_rgba(15,23,42,0.18),0_2px_6px_rgba(15,23,42,0.06)]">
          {matches.length === 0 ? (
            <div className="px-3 py-6 text-center text-xs text-slate-400">
              未找到匹配国家，支持输入中文、英文、ISO、+区号 或 #ID
            </div>
          ) : (
            matches.map((c, idx) => {
              const isSelected = Boolean(selected && String(selected.id) === String(c.id));
              const isActive = idx === activeIdx;
              const iso = (c.iso || "").toUpperCase();
              return (
                <button
                  key={`${c.id}-${c.iso}`}
                  ref={(el) => {
                    itemRefs.current[idx] = el;
                  }}
                  type="button"
                  onMouseDown={(e) => {
                    e.preventDefault();
                    pick(c);
                  }}
                  onMouseEnter={() => setActiveIdx(idx)}
                  className={`flex w-full cursor-pointer items-center justify-between gap-2 rounded-lg px-2.5 py-2 text-left text-xs transition ${
                    isActive
                      ? "bg-emerald-50/90 text-emerald-950"
                      : isSelected
                        ? "bg-slate-50 font-semibold text-slate-900"
                        : "text-slate-700 hover:bg-slate-50"
                  }`}
                >
                  <div className="flex min-w-0 items-center gap-2">
                    {iso ? (
                      <span className="font-mono-code shrink-0 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-bold text-slate-600">
                        {iso}
                      </span>
                    ) : null}
                    <div className="min-w-0 truncate">
                      <span className="font-medium text-slate-900">{c.name_zh || c.name}</span>
                      {c.name_zh && c.name && c.name_zh !== c.name ? (
                        <span className="ml-1.5 text-[11px] font-normal text-slate-400">{c.name}</span>
                      ) : null}
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-1.5 font-mono-code text-[11px] text-slate-500">
                    {c.cc ? <span>+{c.cc}</span> : null}
                    <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600">
                      #{c.id}
                    </span>
                    {isSelected ? <Check className="h-3.5 w-3.5 text-emerald-600" /> : null}
                  </div>
                </button>
              );
            })
          )}
        </div>
      )}
    </div>
  );
}
