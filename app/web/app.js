const $ = id => document.getElementById(id);

const providerMeta = {
  smsbower: { label: "SMSBower", color: "ok" },
  "5sim": { label: "5sim", color: "warn" },
  herosms: { label: "HeroSMS", color: "ok" },
};

const defaultBaseUrls = {
  smsbower: "https://smsbower.page/stubs/handler_api.php",
  herosms: "https://hero-sms.com/stubs/handler_api.php",
};

const PRICE_REFRESH_MS = 30_000;
const UI_PREFERENCES_KEY = "wa-sms-bridge.ui-preferences.v1";

const uiPreferenceFields = {
  regProvider: "value",
  targetPrice: "value",
  priceLockEnabled: "checked",
  priceFloatEnabled: "checked",
  floatPercent: "value",
  maxAttempts: "value",
  continuousPoll: "checked",
  targetSuccessCount: "value",
  waitTimeout: "value",
  requestInterval: "value",
  requestIntervalEnabled: "checked",
  fallbackCountries: "value",
  dashboardPriceCountries: "value",
  dashboardPriceProvider: "value",
  dashboardPriceService: "value",
  providerPriceSelect: "value",
  providerPriceService: "value",
  providerPriceCountry: "value",
  priceLookupSearch: "value",
  priceServiceSelect: "value",
  recommendTargetPrice: "value",
  recommendFloatPercent: "value",
  taskStatusFilter: "value",
  blacklistProvider: "value",
  blacklistAddProvider: "value",
  blacklistReason: "value",
};

const zhRegionNames = typeof Intl !== "undefined" && Intl.DisplayNames
  ? new Intl.DisplayNames(["zh-CN"], { type: "region" })
  : null;

const commonCountries = {
  cn: { id: "3", name: "China", cn: "中国", cc: "86", providerIds: { "5sim": "china", herosms: "3" }, aliases: ["zhongguo", "china", "中国"] },
  th: { id: "52", name: "Thailand", cn: "泰国", cc: "66", providerIds: { "5sim": "thailand", herosms: "52" }, aliases: ["taiguo", "thai", "thailand", "泰"] },
  id: { id: "6", name: "Indonesia", cn: "印度尼西亚", cc: "62", providerIds: { "5sim": "indonesia", herosms: "6" }, aliases: ["yindunixiya", "indonesia", "印尼"] },
  za: { id: "31", name: "South Africa", cn: "南非", cc: "27", providerIds: { "5sim": "southafrica", herosms: "31" }, aliases: ["nanfei", "south africa", "southafrica"] },
  ru: { id: "0", name: "Russian Federation", cn: "俄罗斯", cc: "7", providerIds: { "5sim": "russia", herosms: "0" }, aliases: ["eluosi", "russia", "russian"] },
  in: { id: "22", name: "India", cn: "印度", cc: "91", providerIds: { "5sim": "india", herosms: "22" }, aliases: ["yindu", "india"] },
  ph: { id: "4", name: "Philippines", cn: "菲律宾", cc: "63", providerIds: { "5sim": "philippines", herosms: "4" }, aliases: ["feilvbin", "philippines"] },
  vn: { id: "10", name: "Vietnam", cn: "越南", cc: "84", providerIds: { "5sim": "vietnam" }, aliases: ["yuenan", "vietnam"] },
  us: { id: "187", name: "United States", cn: "美国", cc: "1", providerIds: { "5sim": "usa", herosms: "12" }, aliases: ["meiguo", "usa", "united states", "america"] },
  gb: { id: "16", name: "United Kingdom", cn: "英国", cc: "44", providerIds: { "5sim": "england" }, aliases: ["yingguo", "uk", "united kingdom", "england"] },
  my: { id: "7", name: "Malaysia", cn: "马来西亚", cc: "60", providerIds: { "5sim": "malaysia" }, aliases: ["malaixiya", "malaysia"] },
};

const state = {
  balances: {},
  countries: {},
  tasks: [],
  accounts: [],
  blacklist: [],
  stats: { providers: {}, success_count: 0, cost_total: 0, estimated_count: 0 },
  config: {},
  selectedCountry: null,
  selectedTaskId: "",
  currentTaskId: "",
  registerSubmitting: false,
  pollingTasks: new Set(),
  stoppingTasks: new Set(),
  taskSyncTimer: null,
  lastTaskLogById: new Map(),
  logRows: new Map(),
  lastRecommendations: null,
  pricePollTimer: null,
  dashboardPriceBusy: false,
  registerPriceBusy: false,
  registerPriceRequestSeq: 0,
  dashboardPriceRequestSeq: 0,
  providerPriceRequestSeq: 0,
  recommendationRequestSeq: 0,
};

const pageMeta = {
  dashboard: ["驾驶舱", "运行概览、当前注册、上游状态和最近任务"],
  tasks: ["任务", "按状态、失败分类和关键 ID 排查注册流程"],
  adapter: ["上游适配", "查看 bridge 与 whatsapp_WA 的适配层、能力和接口映射"],
  providers: ["短信平台", "余额、国家库存、价格档位和轮询策略"],
  blacklist: ["黑名单", "按平台隔离号码，避免重复消耗短信订单"],
  config: ["配置", "WA 上游、bridge 存储和平台密钥配置"],
};

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#39;",
  }[ch]));
}

function money(value) {
  if (value === null || value === undefined || value === "") return "-";
  const n = Number(value);
  return Number.isFinite(n) ? `$${n.toFixed(2)}` : "-";
}

function price(value) {
  if (value === null || value === undefined || value === "") return "-";
  const n = Number(value);
  return Number.isFinite(n) ? `$${n.toFixed(4).replace(/0+$/, "").replace(/\.$/, "")}` : "-";
}

function providerLabel(provider) {
  return providerMeta[provider]?.label || provider || "-";
}

function statusClass(status) {
  if (["completed", "ok", "success", "ready"].includes(status)) return "ok";
  if (["running", "pending"].includes(status)) return "run";
  if (["failed", "error"].includes(status)) return "fail";
  if (["cancelled", "warn", "attention", "compensation_pending"].includes(status)) return "warn";
  return "idle";
}

function normalizeSearch(value) {
  return String(value || "").toLowerCase().replace(/[·\s._-]+/g, "").trim();
}

function fuzzySequenceMatch(source, query) {
  if (!query) return true;
  let cursor = 0;
  for (const ch of query) {
    cursor = source.indexOf(ch, cursor);
    if (cursor === -1) return false;
    cursor += 1;
  }
  return true;
}

function readLocalStorage(key, fallback = "") {
  try {
    const value = window.localStorage.getItem(key);
    return value === null ? fallback : value;
  } catch {
    return fallback;
  }
}

function writeLocalStorage(key, value) {
  try {
    window.localStorage.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

function readUiPreferences() {
  const raw = readLocalStorage(UI_PREFERENCES_KEY, "");
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

function restoreUiPreferences() {
  const preferences = readUiPreferences();
  Object.entries(uiPreferenceFields).forEach(([id, mode]) => {
    const element = $(id);
    if (!element || !Object.prototype.hasOwnProperty.call(preferences, id)) return;
    const stored = preferences[id];
    if (mode === "checked") {
      element.checked = stored === true;
      return;
    }
    const value = String(stored ?? "");
    if (element.tagName === "SELECT") {
      const valid = [...element.options].some(option => option.value === value);
      if (!valid) return;
    }
    element.value = value;
  });
}

function saveUiPreference(id) {
  const mode = uiPreferenceFields[id];
  const element = $(id);
  if (!mode || !element) return;
  const preferences = readUiPreferences();
  preferences[id] = mode === "checked" ? !!element.checked : element.value;
  writeLocalStorage(UI_PREFERENCES_KEY, JSON.stringify(preferences));
}

function saveUiPreferences(ids) {
  ids.forEach(saveUiPreference);
}

function bindUiPreferencePersistence() {
  Object.entries(uiPreferenceFields).forEach(([id, mode]) => {
    const element = $(id);
    if (!element) return;
    const eventName = mode === "checked" || element.tagName === "SELECT" ? "change" : "input";
    element.addEventListener(eventName, () => saveUiPreference(id));
  });
}

async function requestJSON(url, options = {}) {
  const response = await fetch(url, options);
  const text = await response.text();
  let data = {};
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { detail: text };
    }
  }
  if (!response.ok) {
    const rawDetail = data.detail || data.error || `${response.status} ${response.statusText}`;
    const detail = Array.isArray(rawDetail)
      ? rawDetail.map(item => {
        const location = Array.isArray(item?.loc) ? item.loc.filter(part => part !== "body").join(".") : "";
        return [location, item?.msg || String(item)].filter(Boolean).join(": ");
      }).join("；")
      : typeof rawDetail === "object"
        ? JSON.stringify(rawDetail)
        : String(rawDetail);
    throw new Error(detail);
  }
  return data;
}

async function fetchJSON(url, options = {}) {
  try {
    return await requestJSON(url, options);
  } catch (error) {
    log(`${url} 失败: ${error.message}`, "err");
    return null;
  }
}

function log(message, type = "info") {
  const box = $("logBox");
  if (!box) return;
  const timestamp = new Date().toLocaleTimeString();
  String(message ?? "").split(/\r?\n/).forEach((line, index) => {
    const key = index === 0 ? normalizedLogKey(line, type) : "";
    const existing = key ? state.logRows.get(key) : null;
    if (existing?.isConnected) {
      const count = Number(existing.dataset.count || "1") + 1;
      existing.dataset.count = String(count);
      existing.textContent = `[${timestamp}] ${existing.dataset.message}  ×${count}`;
      box.scrollTop = box.scrollHeight;
      return;
    }
    const row = document.createElement("div");
    row.className = type;
    row.textContent = index === 0 ? `[${timestamp}] ${line}` : `           ${line}`;
    if (key) {
      row.dataset.logKey = key;
      row.dataset.message = line;
      row.dataset.count = "1";
      state.logRows.set(key, row);
    }
    box.appendChild(row);
  });
  while (box.children.length > 300) {
    const removed = box.firstElementChild;
    if (removed?.dataset.logKey && state.logRows.get(removed.dataset.logKey) === removed) {
      state.logRows.delete(removed.dataset.logKey);
    }
    box.removeChild(removed);
  }
  box.scrollTop = box.scrollHeight;
}

function normalizedLogKey(message, type = "info") {
  return [
    type,
    String(message || "")
      .replace(/\[[0-9a-z_-]{8,}\]/gi, "[task]")
      .replace(/#?[0-9a-z]{10,}/gi, "#id")
      .replace(/\+?\b\d{7,15}\b/g, "phone")
      .replace(/\btask_\d+\b/gi, "task")
      .replace(/\bactivation[_-]?\w*\b\s*[:=]?\s*[0-9a-z_-]+/gi, "activation:id")
      .replace(/\bwa_account[_-]?\w*\b\s*[:=]?\s*[0-9a-z_-]+/gi, "wa_account:id")
      .replace(/\s+/g, " ")
      .trim(),
  ].join("|");
}

function setStatus(el, text, cls = "idle") {
  if (!el) return;
  el.className = `status ${cls}`;
  el.innerHTML = `<span class="dot"></span>${esc(text)}`;
}

function setResult(text, cls = "idle") {
  const el = $("lastResult");
  if (!el) return;
  el.className = `result ${cls}`;
  el.textContent = text;
}

function switchPage(name) {
  document.querySelectorAll(".nav button[data-page]").forEach(button => {
    button.classList.toggle("active", button.dataset.page === name);
  });
  document.querySelectorAll(".page").forEach(page => {
    page.classList.toggle("active", page.id === `page-${name}`);
  });
  const [title, sub] = pageMeta[name] || pageMeta.dashboard;
  $("pageTitle").textContent = title;
  $("pageSub").textContent = `${sub} · 实时数据`;
  window.scrollTo({ top: 0, behavior: "smooth" });

  if (name === "tasks") renderTasks();
  if (name === "providers") {
    renderProviderCards();
  }
  if (name === "blacklist") loadBlacklist();
  if (name === "config") loadConfig();
}

function bindEvents() {
  document.querySelectorAll(".nav button[data-page]").forEach(button => {
    button.addEventListener("click", () => switchPage(button.dataset.page));
  });
  document.querySelectorAll("[data-action='refresh-all']").forEach(button => {
    button.addEventListener("click", refreshAll);
  });
  document.querySelectorAll("[data-action='refresh-balances']").forEach(button => {
    button.addEventListener("click", loadBalances);
  });
  document.querySelectorAll("[data-action='refresh-tasks']").forEach(button => {
    button.addEventListener("click", loadTasks);
  });
  document.querySelector("[data-jump-register]")?.addEventListener("click", () => {
    switchPage("dashboard");
    $("registerPanel")?.scrollIntoView({ behavior: "smooth", block: "center" });
  });
  document.querySelectorAll("[data-jump-config]").forEach(button => {
    button.addEventListener("click", () => {
      switchPage("config");
      $("providerConfigCards")?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });

  $("regProvider")?.addEventListener("change", async () => {
    const provider = $("regProvider").value;
    if ($("dashboardPriceProvider")) {
      $("dashboardPriceProvider").value = provider;
      saveUiPreference("dashboardPriceProvider");
    }
    await loadCountries(provider, true);
    syncSelectedCountryForProvider(provider);
    refreshRegisterCountryPrices();
    refreshDashboardPrices();
  });
  $("regService")?.addEventListener("change", () => {
    updatePriceSummary();
    refreshRegisterCountryPrices();
  });
  ["targetPrice", "floatPercent", "maxAttempts", "targetSuccessCount", "requestInterval"].forEach(id => $(id)?.addEventListener("input", updatePriceSummary));
  ["priceLockEnabled", "priceFloatEnabled", "continuousPoll", "requestIntervalEnabled"].forEach(id => $(id)?.addEventListener("change", syncStrategyControls));
  $("countrySearch")?.addEventListener("focus", () => renderCountryMenu($("countrySearch").value));
  $("countrySearch")?.addEventListener("input", event => handleCountrySearchInput(event.target.value));
  $("countrySearch")?.addEventListener("change", () => {
    const provider = $("regProvider")?.value || "smsbower";
    const country = findCountryByInput($("countrySearch")?.value || "", provider);
    if (country) selectCountry(country);
  });
  $("countrySearch")?.addEventListener("keydown", event => {
    if (event.key === "Escape") $("countryMenu")?.classList.remove("open");
    if (event.key === "Enter") {
      const first = $("countryMenu")?.querySelector("button[data-country]");
      if (first) first.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    }
  });
  document.addEventListener("mousedown", event => {
    if (!event.target.closest("#countryCombo")) $("countryMenu")?.classList.remove("open");
    if (!event.target.closest("#providerPriceCountryCombo")) $("providerPriceCountryMenu")?.classList.remove("open");
    if (!event.target.closest("#priceLookupCountryCombo")) $("priceLookupCountryMenu")?.classList.remove("open");
  });

  $("startBtn")?.addEventListener("click", startRegister);
  $("stopBtn")?.addEventListener("click", stopRunningTask);
  $("testBtn")?.addEventListener("click", testNumber);
  $("syncCountriesBtn")?.addEventListener("click", syncCountries);
  $("syncAllCountriesBtn")?.addEventListener("click", syncCountries);

  $("taskSearch")?.addEventListener("input", renderTasks);
  $("taskStatusFilter")?.addEventListener("change", renderTasks);

  $("providerPriceSelect")?.addEventListener("change", async () => {
    await loadCountries($("providerPriceSelect").value, true);
    renderInlineCountryMenu("providerPriceCountry");
    loadProviderPrices();
  });
  $("providerPriceService")?.addEventListener("change", loadProviderPrices);
  $("loadProviderPricesBtn")?.addEventListener("click", loadProviderPrices);
  $("queryRecommendationsBtn")?.addEventListener("click", loadRecommendations);
  $("priceServiceSelect")?.addEventListener("change", () => {
    renderFallbackBands($("priceServiceSelect").value);
    loadRecommendations();
  });
  bindInlineCountrySearch("providerPriceCountry");
  bindInlineCountrySearch("priceLookupSearch");
  $("createStrategyBtn")?.addEventListener("click", applyRecommendationToRegister);
  $("refreshDashboardPricesBtn")?.addEventListener("click", () => refreshDashboardPrices());
  $("dashboardPriceProvider")?.addEventListener("change", () => refreshDashboardPrices());
  $("dashboardPriceService")?.addEventListener("change", () => refreshDashboardPrices());
  $("dashboardPriceCountries")?.addEventListener("change", () => refreshDashboardPrices());
  $("registerPriceChips")?.addEventListener("click", event => {
    const button = event.target.closest("[data-apply-price]");
    if (!button) return;
    $("targetPrice").value = button.dataset.applyPrice;
    saveUiPreference("targetPrice");
    syncStrategyControls();
  });

  $("blacklistSearch")?.addEventListener("input", renderBlacklist);
  $("blacklistProvider")?.addEventListener("change", loadBlacklist);
  $("addBlacklistBtn")?.addEventListener("click", addBlacklist);
  $("saveRuntimeBtn")?.addEventListener("click", saveRuntimeConfig);
}

async function init() {
  restoreUiPreferences();
  bindEvents();
  bindUiPreferencePersistence();
  renderTimeline();
  renderProviderCards();
  renderFallbackBands("whatsapp");
  syncStrategyControls();
  await loadConfig();
  await Promise.allSettled([
    loadHealth(),
    loadBalances(),
    loadTasks(),
    loadStats(),
    loadAccounts(),
    loadBlacklist(),
    loadCountries($("regProvider")?.value || "smsbower", true),
  ]);
  refreshRegisterCountryPrices();
  refreshDashboardPrices();
  startPricePolling();
  startTaskSync();
  log("控制台就绪", "ok");
}

async function refreshAll() {
  await Promise.allSettled([loadHealth(), loadBalances(), loadTasks(), loadStats(), loadAccounts(), loadBlacklist(), refreshRegisterCountryPrices(), refreshDashboardPrices()]);
  log("页面数据已刷新", "ok");
}

async function loadHealth() {
  const health = await fetchJSON("/api/health?wa_timeout=2.5");
  if (!health) return;
  const waOk = !!health.wa_app?.ok;
  $("waHealth").className = `pill ${waOk ? "" : "amber"}`;
  $("waHealth").innerHTML = `<span class="dot"></span>${waOk ? "WA 上游正常" : "WA 上游异常"}`;
  setStatus($("adapterStatus"), waOk ? "ready" : "error", waOk ? "ok" : "fail");
  $("adapterHealth").textContent = waOk ? "OK" : "异常";
  $("adapterError").textContent = health.wa_app?.error || "none";
  $("sideBridge").textContent = health.ok ? "OK" : "WARN";
  $("sideAdapter").textContent = "whatsapp_wa";
  $("adapterRows").innerHTML = [
    detailLine("providers", (health.bridge?.providers || []).join(" / ") || "-"),
    detailLine("wa_app", waOk ? "connected" : health.wa_app?.error || "disconnected"),
    detailLine("health", JSON.stringify(health.wa_app?.health || {})),
  ].join("");
}

async function loadBalances() {
  const providers = Object.keys(providerMeta);
  const results = await Promise.all(providers.map(async provider => {
    try {
      const data = await requestJSON(`/api/provider/balance?provider=${encodeURIComponent(provider)}`);
      return [provider, { ok: true, balance: Number(data.balance || 0) }];
    } catch (error) {
      return [provider, { ok: false, error: error.message }];
    }
  }));
  state.balances = Object.fromEntries(results);
  const total = Object.values(state.balances).reduce((sum, item) => sum + (item.ok ? item.balance : 0), 0);
  $("kpiBalance").textContent = money(total);
  $("kpiBalanceDetail").textContent = results.map(([provider, item]) => `${providerLabel(provider)} ${item.ok ? money(item.balance) : "失败"}`).join(" · ");
  renderProviderStrip();
  renderProviderCards();
}

function renderProviderStrip() {
  const html = Object.keys(providerMeta).map(provider => {
    const item = state.balances[provider] || {};
    const cls = item.ok ? "ok" : "fail";
    return `<div class="provider">
      <div class="provider-head"><strong>${providerLabel(provider)}</strong><span class="status ${cls}"><span class="dot"></span>${item.ok ? "在线" : "异常"}</span><div class="balance">${item.ok ? money(item.balance) : "-"}</div></div>
      <div class="meta">${item.ok ? "余额已刷新" : "API - 状态异常，请去《配置》查看"}</div>
    </div>`;
  }).join("");
  $("providerStrip").innerHTML = html;
}

function renderProviderCards() {
  const cards = Object.keys(providerMeta).map(provider => {
    const item = state.balances[provider] || {};
    const countryCount = state.countries[provider]?.length || "-";
    return `<section class="panel">
      <div class="panel-header"><div class="panel-title"><span>▣</span>${providerLabel(provider)}</div><span class="status ${item.ok ? "ok" : "warn"}"><span class="dot"></span>${item.ok ? "在线" : "待配置"}</span></div>
      <div class="panel-body">
        <div class="metric-list">
          <div class="metric"><span>余额</span><strong>${item.ok ? money(item.balance) : "-"}</strong></div>
          <div class="metric"><span>库存国家</span><strong>${countryCount}</strong></div>
          <div class="metric"><span>今日成功</span><strong>${todaySuccessFor(provider)}</strong></div>
          <div class="metric"><span>均价</span><strong>${avgCostFor(provider)}</strong></div>
        </div>
        <div class="toolbar">
          <button class="btn" data-test-provider="${provider}">测试取号</button>
          <button class="btn" data-price-provider="${provider}">价格列表</button>
        </div>
      </div>
    </section>`;
  }).join("");
  $("providerCards").innerHTML = cards;
  document.querySelectorAll("[data-price-provider]").forEach(button => {
    button.addEventListener("click", () => {
      $("providerPriceSelect").value = button.dataset.priceProvider;
      loadProviderPrices();
      $("providerPricePanel")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
  });
  document.querySelectorAll("[data-test-provider]").forEach(button => {
    button.addEventListener("click", async () => {
      $("regProvider").value = button.dataset.testProvider;
      $("regProvider").dispatchEvent(new Event("change"));
      await loadCountries(button.dataset.testProvider, true);
      await testNumber();
    });
  });
}

function isDashboardActive() {
  return $("page-dashboard")?.classList.contains("active");
}

function startPricePolling() {
  if (state.pricePollTimer) return;
  state.pricePollTimer = window.setInterval(() => {
    if (!isDashboardActive()) return;
    refreshRegisterCountryPrices({ silent: true });
    refreshDashboardPrices({ silent: true });
  }, PRICE_REFRESH_MS);
}

function startTaskSync() {
  if (state.taskSyncTimer) return;
  state.taskSyncTimer = window.setInterval(() => {
    loadTasks();
  }, 10_000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      loadTasks();
      loadStats();
    }
  });
}

function selectedCountryMatchesInput(country, value, provider = "") {
  if (!country) return false;
  const q = normalizeSearch(value);
  if (!q) return false;
  return [
    countryLabel(country, provider),
    countryDisplayValue(country, provider),
    countryLabel(country),
    countryDisplayValue(country),
    countryValueForProvider(country, provider),
  ].some(item => normalizeSearch(item) === q);
}

function handleCountrySearchInput(value) {
  const provider = $("regProvider")?.value || "smsbower";
  if (!selectedCountryMatchesInput(state.selectedCountry, value, provider)) {
    state.selectedCountry = null;
  }
  renderCountryMenu(value);
}

function syncSelectedCountryForProvider(provider = "") {
  const input = $("countrySearch");
  const typed = input?.value || "";
  const matched = findCountryByInput(typed, provider);
  if (matched) {
    selectCountry(matched, false);
  } else if (state.selectedCountry && countryValueForProvider(state.selectedCountry, provider)) {
    selectCountry(state.selectedCountry, false);
  }
}

function selectedRegisterCountryForPricing(provider = "") {
  provider = provider || $("regProvider")?.value || "smsbower";
  const typed = $("countrySearch")?.value || "";
  const matched = findCountryByInput(typed, provider);
  const country = matched || (
    selectedCountryMatchesInput(state.selectedCountry, typed, provider)
      ? state.selectedCountry
      : null
  );
  return country ? countryValueForProvider(country, provider) : typed.trim();
}

function hasPriceValue(value) {
  return value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value));
}

function stockTone(count) {
  const n = Number(count || 0);
  if (n <= 0) return { cls: "idle", label: "无号" };
  if (n <= 5) return { cls: "fail", label: "很少" };
  if (n <= 30) return { cls: "warn", label: "较少" };
  return { cls: "ok", label: "很多" };
}

function priceEntries(data) {
  return (data?.entries || [])
    .filter(item => hasPriceValue(item.cost))
    .map(item => ({ cost: Number(item.cost), count: Number(item.count || 0) }))
    .sort((a, b) => a.cost - b.cost);
}

function renderPriceMatrix(entries, options = {}) {
  if (!entries.length) {
    return `<div class="price-empty">${esc(options.emptyText || "暂无价格档位。")}</div>`;
  }
  return `<div class="price-matrix">${entries.map(item => {
    const tone = stockTone(item.count);
    const cost = Number(item.cost);
    return `<button class="price-cell ${tone.cls}" type="button" data-apply-price="${esc(cost.toFixed(4))}" ${options.readonly ? "disabled" : ""}>
      <strong>${price(cost)}</strong>
      <span>${esc(tone.label)}</span>
      <em>数量 ${esc(item.count || 0)}</em>
    </button>`;
  }).join("")}</div>`;
}

function renderProviderPriceMatrix(data, options = {}) {
  if (!data || data.error) {
    return `<div class="price-empty">${esc(data?.error || "价格查询失败。")}</div>`;
  }
  return renderPriceMatrix(priceEntries(data), options);
}

async function refreshRegisterCountryPrices(options = {}) {
  const chips = $("registerPriceChips");
  const status = $("registerPriceStatus");
  const title = $("registerPriceTitle");
  if (!chips || !status) return;

  const selectedProvider = $("regProvider")?.value || "smsbower";
  const country = selectedRegisterCountryForPricing(selectedProvider);
  const service = $("regService")?.value || "whatsapp";
  const requestSeq = ++state.registerPriceRequestSeq;
  if (title) title.textContent = `${providerLabel(selectedProvider)} · 当前国家价格`;
  if (!country) {
    setStatus(status, "等待国家", "idle");
    chips.innerHTML = `<div class="price-empty">选择国家后显示当前平台的价格矩阵。</div>`;
    return;
  }

  state.registerPriceBusy = true;
  if (!options.silent) {
    setStatus(status, "查询中", "run");
    chips.innerHTML = `<div class="price-empty">正在刷新 ${esc(country)} 的价格矩阵...</div>`;
  }

  const data = await fetchJSON(`/api/provider/prices?provider=${encodeURIComponent(selectedProvider)}&service=${encodeURIComponent(service)}&country=${encodeURIComponent(country)}`);
  if (requestSeq !== state.registerPriceRequestSeq) return;
  state.registerPriceBusy = false;
  if (!data) {
    setStatus(status, "查询失败", "fail");
    chips.innerHTML = `<div class="price-empty">价格查询失败，检查平台密钥、国家或网络。</div>`;
    return;
  }

  const entries = priceEntries(data);
  const stock = entries.reduce((sum, item) => sum + item.count, 0);
  setStatus(status, `${entries.length} 档 · ${stock} 个`, stock ? "ok" : "idle");
  chips.innerHTML = renderPriceMatrix(entries, { emptyText: data.available ? "暂无价格档位。" : "当前国家暂无号码。" });
}

function parseDashboardPriceCountries(provider = "") {
  const raw = $("dashboardPriceCountries")?.value || "";
  const seen = new Set();
  return raw.split(/[,，\n]+/)
    .map(value => value.trim())
    .filter(Boolean)
    .map(value => resolveCountryInput(value, provider) || value)
    .filter(value => {
      const key = normalizeSearch(value);
      if (!key || seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .slice(0, 8);
}

function renderDashboardPriceRows(rows, provider) {
  $("dashboardPriceRows").innerHTML = rows.length ? rows.map(row => {
    const data = row.data || {};
    const entries = priceEntries(data);
    const stock = entries.reduce((sum, item) => sum + item.count, 0);
    const best = entries.find(item => item.count > 0) || entries[0];
    const tone = row.error ? { cls: "fail", label: "异常" } : stockTone(stock);
    return `<div class="monitor-country">
      <div class="monitor-country-head">
        <strong>${esc(displayCountryInput(row.country, provider) || row.country)}</strong>
        <span class="status ${tone.cls}"><span class="dot"></span>${esc(row.error ? "异常" : tone.label)}</span>
      </div>
      <div class="monitor-country-meta">${esc(providerLabel(provider))} · ${best ? `最低 ${price(best.cost)} · 总库存 ${stock}` : row.error || "暂无号码"}</div>
      ${row.error ? `<div class="price-empty">${esc(row.error)}</div>` : renderProviderPriceMatrix(data, { readonly: true, emptyText: "当前国家暂无号码。" })}
    </div>`;
  }).join("") : `<div class="price-empty">暂无价格数据。</div>`;
}

async function refreshDashboardPrices(options = {}) {
  const rowsEl = $("dashboardPriceRows");
  const status = $("dashboardPriceStatus");
  if (!rowsEl || !status) return;

  const provider = $("dashboardPriceProvider")?.value || $("regProvider")?.value || "smsbower";
  const countries = parseDashboardPriceCountries(provider);
  const service = $("dashboardPriceService")?.value || "whatsapp";
  const requestSeq = ++state.dashboardPriceRequestSeq;
  if (!countries.length) {
    setStatus(status, "等待国家", "idle");
    rowsEl.innerHTML = `<div class="price-empty">输入多个国家后开始监控。</div>`;
    return;
  }

  state.dashboardPriceBusy = true;
  if (!options.silent) {
    setStatus(status, "刷新中", "run");
    rowsEl.innerHTML = `<div class="price-empty">正在刷新 ${providerLabel(provider)} 的 ${countries.length} 个国家价格矩阵...</div>`;
  }

  const rows = await Promise.all(countries.map(async country => {
    const data = await fetchJSON(`/api/provider/prices?provider=${encodeURIComponent(provider)}&service=${encodeURIComponent(service)}&country=${encodeURIComponent(country)}`);
    return data ? { country, data } : { country, error: "价格查询失败" };
  }));
  if (requestSeq !== state.dashboardPriceRequestSeq) return;
  state.dashboardPriceBusy = false;
  renderDashboardPriceRows(rows, provider);
  const failed = rows.filter(row => row.error).length;
  const stock = rows.reduce((sum, row) => sum + priceEntries(row.data).reduce((inner, item) => inner + item.count, 0), 0);
  const label = failed === rows.length
    ? "全部失败"
    : failed
      ? `${rows.length - failed}/${rows.length} 成功`
      : `${new Date().toLocaleTimeString()} 更新`;
  setStatus(status, label, failed === rows.length ? "fail" : failed ? "warn" : stock ? "ok" : "idle");
}

function todaySuccessFor(provider) {
  return Number(state.stats.providers?.[provider]?.success_count || 0);
}

function avgCostFor(provider) {
  const value = state.stats.providers?.[provider]?.avg_cost;
  return value === null || value === undefined ? "-" : money(value);
}

async function loadAccounts() {
  const data = await fetchJSON("/api/accounts");
  const accounts = Array.isArray(data) ? data : data?.accounts || [];
  state.accounts = accounts;
  $("kpiAccounts").textContent = accounts.length;
}

async function loadTasks() {
  const tasks = await fetchJSON("/api/tasks");
  if (!Array.isArray(tasks)) return;
  state.tasks = tasks;
  if (state.selectedTaskId && !tasks.some(task => task.task_id === state.selectedTaskId)) {
    state.selectedTaskId = "";
  }
  if (!state.selectedTaskId && state.currentTaskId && tasks.some(task => task.task_id === state.currentTaskId)) {
    state.selectedTaskId = state.currentTaskId;
  }
  if (!state.selectedTaskId) {
    state.selectedTaskId = tasks.find(task => task.status === "running")?.task_id || "";
  }
  updateTaskKpis();
  renderTasks();
  renderProviderCards();
  renderSelectedTask();
  tasks.filter(task => task.status === "running").forEach(task => pollTask(task.task_id));
}

async function loadStats() {
  const offset = new Date().getTimezoneOffset();
  const data = await fetchJSON(`/api/stats?timezone_offset_minutes=${encodeURIComponent(offset)}`);
  if (!data) return;
  state.stats = data;
  updateTaskKpis();
  renderProviderCards();
}

function updateTaskKpis() {
  const running = runningTaskCount();
  $("kpiRunning").textContent = running;
  if ($("runningTaskHint")) {
    setStatus($("runningTaskHint"), `并发 ${running}`, running ? "run" : "idle");
  }
  $("kpiToday").textContent = Number(state.stats.success_count || 0);
  const cost = Number(state.stats.cost_total || 0);
  const estimated = Number(state.stats.estimated_count || 0);
  $("kpiTodayCost").textContent = cost > 0
    ? `今日消耗 ${money(cost)}${estimated ? ` · 含 ${estimated} 笔估算` : ""}`
    : "今日暂无已知成本";
}

function taskPhone(task) {
  return task.phone_number || task.phone || "-";
}

function taskCountry(task) {
  return task.country_label || task.country_id || task.country || "-";
}

function parseTaskTimestamp(value) {
  const text = String(value || "").trim();
  if (!text) return NaN;
  if (/[zZ]|[+-]\d{2}:?\d{2}$/.test(text)) return Date.parse(text);
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(text)) {
    return Date.parse(`${text.replace(" ", "T")}Z`);
  }
  return Date.parse(text);
}

function taskDuration(task) {
  const start = parseTaskTimestamp(task.created_at);
  const end = task.status === "running"
    ? Date.now()
    : (parseTaskTimestamp(task.completed_at || task.updated_at) || Date.now());
  if (!Number.isFinite(start)) return "-";
  const sec = Math.max(0, Math.round((end - start) / 1000));
  return sec < 60 ? `${sec}s` : `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
}

function taskFingerprint(taskOrId) {
  const id = typeof taskOrId === "string" ? taskOrId : taskOrId?.task_id;
  const suffix = String(id || "").replace(/^task_/, "").slice(-8);
  return suffix ? `#${suffix}` : "#--------";
}

function taskPhoneTail(task) {
  const phone = taskPhone(task);
  if (!phone || phone === "-") return "待取号";
  const digits = String(phone).replace(/\D/g, "");
  return digits ? `尾号 ${digits.slice(-4)}` : String(phone);
}

function taskIdentityCell(task) {
  return `<div class="task-fingerprint">
    <strong class="mono">${esc(taskFingerprint(task))}</strong>
    <span>${esc(taskPhoneTail(task))}</span>
  </div>`;
}

function taskCanStop(task) {
  return ["running", "compensation_pending"].includes(task?.status);
}

function stopTaskButton(task) {
  if (!taskCanStop(task)) return "-";
  const stopping = state.stoppingTasks.has(task.task_id);
  return `<button class="btn tiny danger ${stopping ? "loading" : ""}" data-stop-task="${esc(task.task_id)}" ${stopping ? "disabled" : ""}>${stopping ? "停止中…" : "终止"}</button>`;
}

function upsertTask(task) {
  const index = state.tasks.findIndex(item => item.task_id === task.task_id);
  if (index >= 0) state.tasks[index] = { ...state.tasks[index], ...task };
  else state.tasks.unshift(task);
}

function selectedTask() {
  return state.tasks.find(task => task.task_id === state.selectedTaskId) || null;
}

function runningTaskCount() {
  return state.tasks.filter(task => task.status === "running").length;
}

function renderTasks() {
  const q = normalizeSearch($("taskSearch")?.value || "");
  const status = $("taskStatusFilter")?.value || "";
  const rows = state.tasks.filter(task => {
    const text = normalizeSearch([task.task_id, task.provider, taskCountry(task), taskPhone(task), task.error, task.step].join(" "));
    return (!status || task.status === status) && (!q || text.includes(q) || fuzzySequenceMatch(text, q));
  });
  const compactRows = state.tasks.slice(0, 8);
  $("taskRows").innerHTML = compactRows.length ? compactRows.map(taskRow).join("") : `<tr><td colspan="9" class="empty">暂无任务</td></tr>`;
  $("allTaskRows").innerHTML = rows.length ? rows.map(taskFullRow).join("") : `<tr><td colspan="9" class="empty">暂无任务</td></tr>`;
  document.querySelectorAll("[data-task-row]").forEach(row => {
    row.addEventListener("click", () => {
      state.selectedTaskId = row.dataset.taskRow;
      renderTasks();
      renderSelectedTask();
    });
  });
  document.querySelectorAll("[data-stop-task]").forEach(button => {
    button.addEventListener("click", event => {
      event.stopPropagation();
      stopTask(button.dataset.stopTask);
    });
  });
}

const failureCategoryLabels = {
  security_verification_required: "二步验证/安全确认",
  otp_invalid: "验证码无效",
  otp_rate_limited: "验证码限流",
  wa_blocked: "WhatsApp 拦截",
  otp_registration_failed: "OTP 注册失败",
};

function taskFailureCell(task) {
  const category = String(task.failure_category || "");
  const label = failureCategoryLabels[category] || category;
  if (!label) return esc(task.error || "-");
  const code = String(task.failure_code || "");
  return `<div><strong>${esc(label)}</strong>${code ? `<div class="muted mono">${esc(code)}</div>` : ""}</div>`;
}

function taskRow(task) {
  const cls = statusClass(task.status);
  return `<tr data-task-row="${esc(task.task_id)}" class="${task.task_id === state.selectedTaskId ? "selected" : ""}">
    <td>${taskIdentityCell(task)}</td>
    <td>${esc(providerLabel(task.provider))}</td>
    <td>${esc(taskCountry(task))}</td>
    <td class="mono">${esc(taskPhone(task))}</td>
    <td>${esc(task.step || "-")}</td>
    <td><span class="status ${cls}"><span class="dot"></span>${esc(task.status || "-")}</span></td>
    <td>${taskFailureCell(task)}</td>
    <td class="mono">${taskDuration(task)}</td>
    <td>${stopTaskButton(task)}</td>
  </tr>`;
}

function taskFullRow(task) {
  const cls = statusClass(task.status);
  return `<tr data-task-row="${esc(task.task_id)}" class="${task.task_id === state.selectedTaskId ? "selected" : ""}">
    <td>${taskIdentityCell(task)}</td>
    <td>${esc(providerLabel(task.provider))}</td>
    <td>${esc(taskCountry(task))}</td>
    <td class="mono">${esc(taskPhone(task))}</td>
    <td>${esc(task.step || "-")}</td>
    <td><span class="status ${cls}"><span class="dot"></span>${esc(task.status || "-")}</span></td>
    <td>${taskFailureCell(task)}</td>
    <td class="mono">${esc(formatTaskTime(task.completed_at || task.updated_at || task.created_at))}</td>
    <td>${stopTaskButton(task)}</td>
  </tr>`;
}

function detailLine(key, value) {
  return `<div class="detail-row"><div class="key">${esc(key)}</div><div class="val">${esc(value ?? "-")}</div></div>`;
}

function formatTaskTime(value) {
  const timestamp = parseTaskTimestamp(value);
  if (!Number.isFinite(timestamp)) return value || "-";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(timestamp));
}

function renderTaskDetail(task) {
  if (!task) {
    $("detailRows").innerHTML = `<div class="empty">暂无任务</div>`;
    setStatus($("detailStatus"), "idle", "idle");
    renderTimeline();
    return;
  }
  setStatus($("detailStatus"), task.status || "idle", statusClass(task.status));
  $("detailRows").innerHTML = [
    detailLine("fingerprint", `${taskFingerprint(task)} / ${taskPhoneTail(task)}`),
    detailLine("task_id", task.task_id),
    detailLine("provider", providerLabel(task.provider)),
    detailLine("country", taskCountry(task)),
    detailLine("phone", taskPhone(task)),
    detailLine("activation_id", task.activation_id || "-"),
    detailLine("wa_account_id", task.wa_account_id || "-"),
    detailLine("success", `${Number(task.successes_done || 0)}/${Number(task.target_success_count || 1)}`),
    detailLine("step", task.step || "-"),
    detailLine("failure_category", task.failure_category || "-"),
    detailLine("failure_code", task.failure_code || "-"),
    detailLine("error", task.error || "-"),
    detailLine("reconciliation", Number(task.reconciliation_required || 0) ? task.reconciliation_error || "需核对" : "none"),
    detailLine("updated_at", formatTaskTime(task.updated_at)),
  ].join("");
}

function renderSelectedTask() {
  const task = selectedTask();
  renderTaskDetail(task);
  renderTimeline(task);
  if (!task) {
    setResult("就绪", "idle");
    setStatus($("currentTaskStatus"), "等待", "idle");
    return;
  }
  const cls = statusClass(task.status);
  const resultClass = cls === "fail" ? "fail" : cls === "ok" ? "ok" : cls === "warn" ? "running" : "running";
  setResult(`${taskFingerprint(task)} · ${task.status || "-"} · ${task.step || "-"} · ${taskPhone(task)}`, resultClass);
  setStatus($("currentTaskStatus"), task.status || "running", cls);
}

function renderTimeline(task = null) {
  const steps = [
    { label: "取号", meta: "request_number", keys: ["锁价检查", "取号", "已取号", "耗尽"] },
    { label: "WA 探测", meta: "probe_phone", keys: ["探测中"] },
    { label: "发起注册", meta: "register", keys: ["注册中", "已注册"] },
    { label: "等待短信", meta: "poll_sms", keys: ["等码中", "等码超时"] },
    { label: "提交 OTP", meta: "resume_otp", keys: ["提交 OTP", "OTP 提交失败", "二步验证", "安全确认", "验证登录态", "完成"] },
  ];
  const stepText = String(task?.step || "");
  let active = steps.findIndex(step => step.keys.some(key => stepText.includes(key)));
  if (active < 0 && task?.status === "completed") active = steps.length - 1;
  if (active < 0) active = 0;
  const status = String(task?.status || "");
  $("taskTimeline").innerHTML = steps.map((step, index) => {
    let cls = "idle";
    let marker = "--";
    if (task) {
      if (status === "completed" || index < active) {
        cls = "ok";
        marker = "done";
      } else if (index === active) {
        cls = status === "failed" ? "fail" : status === "cancelled" ? "warn" : "run";
        marker = stepText || status || "running";
      }
    }
    return `<div class="step ${cls}">
      <span class="status ${cls}"><span class="dot"></span></span>
      <div><strong>${esc(step.label)}</strong><span>${esc(step.meta)}</span></div>
      <span class="mono">${esc(marker)}</span>
    </div>`;
  }).join("");
  if (task) setStatus($("currentTaskStatus"), task.status || "running", statusClass(task.status));
}

function logTaskTransition(task) {
  if (!task?.task_id) return;
  const snapshot = [task.status || "", task.step || "", taskPhone(task), task.error || ""].join("|");
  if (state.lastTaskLogById.get(task.task_id) === snapshot) return;
  state.lastTaskLogById.set(task.task_id, snapshot);
  const cls = statusClass(task.status);
  const type = cls === "fail" ? "err" : cls === "ok" ? "ok" : cls === "warn" ? "warn" : "info";
  const parts = [
    `任务 ${taskFingerprint(task)}`,
    task.status || "-",
    task.step || "-",
    taskPhone(task),
  ];
  if (task.error) parts.push(task.error);
  log(parts.join(" · "), type);
}

async function loadCountries(provider, force = false) {
  if (!force && state.countries[provider]) return;
  const list = await fetchJSON(`/api/provider/countries?provider=${encodeURIComponent(provider)}`);
  state.countries[provider] = Array.isArray(list) ? list.map(enrichCountry) : [];
  if (provider === ($("regProvider")?.value || "smsbower")) {
    const preferred = readLocalStorage(`wa-sms-bridge.country.${provider}`, "")
      || (provider === "smsbower" ? String(state.config.smsbower_default_country ?? "") : "");
    const preferredCountry = preferred ? findCountryByInput(preferred, provider) : null;
    const currentCountry = state.selectedCountry
      ? findCountryByInput(countryValueForProvider(state.selectedCountry, provider), provider)
      : null;
    const fallback = mergedCountryList(provider)[0];
    const selected = preferredCountry || currentCountry || fallback;
    if (selected) selectCountry(selected, false);
  }
  if (document.activeElement === $("countrySearch")) {
    renderCountryMenu($("countrySearch")?.value || "");
  } else {
    $("countryMenu")?.classList.remove("open");
  }
  renderProviderCards();
}

function enrichCountry(country) {
  const iso = String(country.iso || "").toLowerCase();
  const common = commonCountries[iso] || {};
  return {
    ...country,
    iso,
    name: country.name || common.name || "",
    cn: country.name_zh || country.cn || common.cn || localizedCountryName(iso),
    cc: country.cc || common.cc || "",
    providerIds: common.providerIds || {},
    aliases: [...new Set([...(country.aliases || []), ...(common.aliases || [])])],
  };
}

function localCountryList(provider = "") {
  return Object.entries(commonCountries)
    .filter(([, country]) => !provider || provider === "smsbower" || country.providerIds?.[provider])
    .map(([iso, country]) => ({
    id: country.id,
    name: country.name,
    iso,
    cn: country.cn,
    cc: country.cc || "",
    providerIds: country.providerIds || {},
    aliases: country.aliases || [],
    local: true,
    }));
}

function mergedCountryList(provider = "") {
  const remote = (state.countries[provider] || []).map(enrichCountry);
  const byKey = new Map();
  [...remote, ...localCountryList(provider)].forEach(country => {
    const key = country.iso || String(country.id ?? country.name ?? "").toLowerCase();
    if (!byKey.has(key)) byKey.set(key, country);
  });
  return [...byKey.values()];
}

function localizedCountryName(iso) {
  const code = String(iso || "").toUpperCase();
  if (!/^[A-Z]{2}$/.test(code) || !zhRegionNames) return "";
  try {
    const label = zhRegionNames.of(code);
    return label && label !== code ? label : "";
  } catch {
    return "";
  }
}

function countryCallingCode(country) {
  const digits = String(country?.cc || "").replace(/[^\d]/g, "");
  return digits ? `+${digits}` : "";
}

function countryParts(country, provider = "") {
  return [
    country.cn,
    country.name || country.id,
    String(country.iso || "").toUpperCase(),
    countryCallingCode(country),
    provider ? countryValueForProvider(country, provider) : country.id,
  ].filter(Boolean);
}

function countryLabel(country, provider = "") {
  return countryParts(country, provider).join(" · ");
}

function countrySearchText(country, provider = "") {
  return normalizeSearch([
    countryLabel(country, provider),
    country.cn,
    country.name,
    country.iso,
    country.id,
    country.cc,
    ...(country.aliases || []),
    ...(Object.values(country.providerIds || {})),
  ].join(" "));
}

function countrySearchFields(country, provider = "") {
  return [
    countryLabel(country, provider),
    country.cn,
    country.name,
    country.iso,
    country.id,
    country.cc,
    ...(country.aliases || []),
    ...(Object.values(country.providerIds || {})),
  ]
    .map(normalizeSearch)
    .filter(Boolean);
}

function countryMatchScore(country, query, provider = "") {
  const q = normalizeSearch(query);
  if (!q) return 0;
  const fields = countrySearchFields(country, provider);
  const text = countrySearchText(country, provider);
  if (normalizeSearch(country.id) === q) return 1;
  if (Object.values(country.providerIds || {}).some(value => normalizeSearch(value) === q)) return 1;
  if (normalizeSearch(country.iso) === q) return 1;
  if ([country.cn, country.name, ...(country.aliases || [])].some(value => normalizeSearch(value) === q)) return 2;
  if (normalizeSearch(country.cc) === q) return 3;
  if (fields.some(field => field.startsWith(q))) return 2;
  if (fields.some(field => field.includes(q))) return 3;
  if (q.length >= 3 && fuzzySequenceMatch(text, q)) return 4;
  return Infinity;
}

function countryMatches(country, query, provider = "") {
  return Number.isFinite(countryMatchScore(country, query, provider));
}

function rankedCountryMatches(list, query, provider = "") {
  const q = normalizeSearch(query);
  if (!q) return list;
  return list
    .map((country, index) => ({ country, index, score: countryMatchScore(country, q, provider) }))
    .filter(item => Number.isFinite(item.score))
    .sort((a, b) => a.score - b.score || a.index - b.index)
    .map(item => item.country);
}

function countryValueForProvider(country, provider = "") {
  if (!country) return "";
  if (!provider) {
    return String(country.name ?? country.cn ?? country.id ?? "");
  }
  if (country.providerIds?.[provider]) {
    return String(country.providerIds[provider]);
  }
  if (provider === "5sim") {
    return country.providerIds?.["5sim"] || normalizeSearch(country.name || country.id);
  }
  return String(country.id ?? country.name ?? "");
}

function countryDisplayValue(country, provider = "") {
  if (!country) return "";
  return countryParts(country, provider).join(" · ");
}

function findCountryByInput(value, provider = "") {
  const raw = String(value || "").trim();
  if (!raw) return null;
  const list = mergedCountryList(provider);
  const exact = list.find(country => normalizeSearch(countryDisplayValue(country, provider)) === normalizeSearch(raw));
  return exact || rankedCountryMatches(list, raw, provider)[0] || null;
}

function displayCountryInput(value, provider = "") {
  const matched = findCountryByInput(value, provider);
  return matched ? countryDisplayValue(matched, provider) : String(value || "").trim();
}

function resolveCountryInput(value, provider = "") {
  const raw = String(value || "").trim();
  if (!raw) return "";
  const matched = findCountryByInput(raw, provider);
  return matched ? countryValueForProvider(matched, provider) : raw;
}

function renderCountryMenu(query = "") {
  const menu = $("countryMenu");
  const provider = $("regProvider")?.value || "smsbower";
  const list = mergedCountryList(provider);
  const matches = rankedCountryMatches(list, query, provider).slice(0, 10);
  menu.innerHTML = matches.length ? matches.map(country => `<button class="country-option ${state.selectedCountry?.id === country.id || (state.selectedCountry?.iso && state.selectedCountry.iso === country.iso) ? "active" : ""}" type="button" data-country="${esc(country.id)}">
    <span><span class="country-main">${esc(country.cn || country.name || country.id)}</span><span class="country-sub">${esc([...new Set([country.name, ...(country.aliases || [])])].filter(Boolean).slice(0, 4).join(" / "))}</span></span>
    <span class="country-id">${esc([String(country.iso || "").toUpperCase(), countryCallingCode(country), countryValueForProvider(country, provider)].filter(Boolean).join(" · "))}</span>
  </button>`).join("") : `<div class="country-option"><span><span class="country-main">没有匹配国家</span><span class="country-sub">可输入中文、英文、ISO、平台 ID 或拼音别名</span></span><span class="country-id">--</span></div>`;
  menu.classList.add("open");
  menu.querySelectorAll("button[data-country]").forEach(button => {
    button.addEventListener("mousedown", event => {
      event.preventDefault();
      const country = list.find(item => String(item.id) === String(button.dataset.country));
      if (country) selectCountry(country);
    });
  });
}

function selectCountry(country, close = true) {
  const provider = $("regProvider")?.value || "";
  state.selectedCountry = country;
  if (provider) {
    writeLocalStorage(
      `wa-sms-bridge.country.${provider}`,
      countryValueForProvider(country, provider),
    );
  }
  $("countrySearch").value = countryLabel(country, provider);
  $("providerPriceCountry").value = countryDisplayValue(country, $("providerPriceSelect")?.value || provider);
  $("priceLookupSearch").value = countryDisplayValue(country, provider);
  saveUiPreferences(["providerPriceCountry", "priceLookupSearch"]);
  if (close) $("countryMenu")?.classList.remove("open");
  refreshRegisterCountryPrices();
}

function inlineCountryConfig(inputId) {
  if (inputId === "providerPriceCountry") {
    return {
      input: $("providerPriceCountry"),
      menu: $("providerPriceCountryMenu"),
      provider: () => $("providerPriceSelect")?.value || "smsbower",
      selectValue: country => countryDisplayValue(country, $("providerPriceSelect")?.value || "smsbower"),
    };
  }
  return {
    input: $("priceLookupSearch"),
    menu: $("priceLookupCountryMenu"),
    provider: () => $("providerPriceSelect")?.value || $("regProvider")?.value || "smsbower",
    selectValue: country => countryDisplayValue(country, $("providerPriceSelect")?.value || $("regProvider")?.value || "smsbower"),
  };
}

function bindInlineCountrySearch(inputId) {
  const config = inlineCountryConfig(inputId);
  if (!config.input || !config.menu) return;
  config.input.addEventListener("focus", () => renderInlineCountryMenu(inputId));
  config.input.addEventListener("input", () => renderInlineCountryMenu(inputId));
  config.input.addEventListener("keydown", event => {
    if (event.key === "Escape") config.menu.classList.remove("open");
    if (event.key === "Enter") {
      const first = config.menu.querySelector("button[data-country]");
      if (first) first.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    }
  });
}

async function renderInlineCountryMenu(inputId) {
  const config = inlineCountryConfig(inputId);
  if (!config.input || !config.menu) return;
  const provider = config.provider();
  if (!state.countries[provider]) {
    await loadCountries(provider, true);
  }
  const query = config.input.value || "";
  const matches = rankedCountryMatches(mergedCountryList(provider), query, provider).slice(0, 10);
  config.menu.innerHTML = matches.length ? matches.map(country => `<button class="country-option" type="button" data-country="${esc(country.iso || country.id)}">
    <span><span class="country-main">${esc(country.cn || country.name || country.id)}</span><span class="country-sub">${esc([...new Set([country.name, ...(country.aliases || [])])].filter(Boolean).slice(0, 4).join(" / "))}</span></span>
    <span class="country-id">${esc([String(country.iso || "").toUpperCase(), countryCallingCode(country), countryValueForProvider(country, provider)].filter(Boolean).join(" · "))}</span>
  </button>`).join("") : `<div class="country-option"><span><span class="country-main">没有匹配国家</span><span class="country-sub">可输入中文、英文、ISO、平台 ID 或拼音别名</span></span><span class="country-id">--</span></div>`;
  config.menu.classList.add("open");
  config.menu.querySelectorAll("button[data-country]").forEach(button => {
    button.addEventListener("mousedown", event => {
      event.preventDefault();
      const country = matches.find(item => String(item.iso || item.id) === String(button.dataset.country));
      if (!country) return;
      config.input.value = config.selectValue(country);
      saveUiPreference(inputId);
      config.menu.classList.remove("open");
      if (inputId === "providerPriceCountry") loadProviderPrices();
    });
  });
}

function isChecked(id) {
  return !!$(id)?.checked;
}

function numericInput(id, fallback = 0) {
  const raw = String($(id)?.value ?? "").replace(/[^\d.]/g, "");
  const value = Number(raw);
  return Number.isFinite(value) ? value : fallback;
}

function readStrategyOptions() {
  const target = numericInput("targetPrice", 0);
  const lock = isChecked("priceLockEnabled");
  const floatEnabled = !lock && isChecked("priceFloatEnabled");
  const floatPercent = floatEnabled ? numericInput("floatPercent", 0) : 0;
  const priceMax = target > 0
    ? (floatEnabled ? target * (1 + floatPercent / 100) : target)
    : undefined;
  const continuous = isChecked("continuousPoll");
  const intervalEnabled = isChecked("requestIntervalEnabled");
  return {
    target,
    priceMax: priceMax === undefined ? undefined : Number(priceMax.toFixed(4)),
    lock,
    floatEnabled,
    floatPercent,
    continuous,
    targetSuccessCount: Math.max(1, Math.floor(numericInput("targetSuccessCount", 1))),
    maxAttempts: Math.max(1, Math.floor(numericInput("maxAttempts", 3))),
    intervalEnabled,
    requestInterval: Math.min(3600, Math.max(0.1, numericInput("requestInterval", 1))),
  };
}

function syncStrategyControls() {
  const lock = isChecked("priceLockEnabled");
  const floatToggle = $("priceFloatEnabled");
  const floatInput = $("floatPercent");
  if (lock && floatToggle) floatToggle.checked = false;
  if (floatToggle) floatToggle.disabled = lock;
  if (floatInput) floatInput.disabled = lock || !isChecked("priceFloatEnabled");
  if ($("maxAttempts")) $("maxAttempts").disabled = isChecked("continuousPoll");
  updatePriceSummary();
  saveUiPreferences([
    "targetPrice",
    "priceLockEnabled",
    "priceFloatEnabled",
    "floatPercent",
    "maxAttempts",
    "continuousPoll",
    "targetSuccessCount",
    "requestInterval",
    "requestIntervalEnabled",
  ]);
}

function updatePriceSummary() {
  const opts = readStrategyOptions();
  const priceText = opts.lock
    ? `锁定 <strong>${money(opts.target)}</strong>，只轮询该价格档，价格浮动关闭。`
    : opts.floatEnabled
      ? `目标 <strong>${money(opts.target)}</strong>，价格浮动 <strong>${opts.floatPercent}%</strong>，最高 <strong>${money(opts.priceMax)}</strong>。`
      : `目标上限 <strong>${money(opts.target)}</strong>，价格浮动关闭。`;
  const loopText = opts.continuous
    ? `一直轮询，直到成功 <strong>${opts.targetSuccessCount}</strong> 个或手动停止。`
    : `最多尝试 <strong>${opts.maxAttempts}</strong> 次，成功 <strong>${opts.targetSuccessCount}</strong> 个后停止。`;
  const intervalText = opts.intervalEnabled
    ? `取号间隔 <strong>${opts.requestInterval}s</strong>。`
    : opts.continuous
      ? "未启用自定义间隔，持续轮询默认每 <strong>1s</strong> 再取号。"
      : "未启用自定义间隔，有限次数尝试不额外等待。";
  $("priceSummary").innerHTML = `<span>${priceText} ${loopText} ${intervalText}</span>`;
}

async function syncCountries() {
  const button = $("syncAllCountriesBtn") || $("syncCountriesBtn");
  if (button) button.disabled = true;
  setStatus($("countrySyncStatus"), "同步中", "run");
  $("countrySyncMeta").textContent = "正在遍历所有已配置 Provider API";
  const data = await fetchJSON("/api/countries/refresh", { method: "POST" });
  state.countries = {};
  await loadCountries($("regProvider")?.value || "smsbower", true);
  $("countrySyncMeta").textContent = data ? `已合并 ${data.count || 0} 个国家` : "同步失败";
  setStatus($("countrySyncStatus"), data ? "正常" : "失败", data ? "ok" : "fail");
  if (button) button.disabled = false;
}

async function startRegister() {
  if (state.registerSubmitting) return;
  const provider = $("regProvider").value;
  const country = selectedRegisterCountryForPricing(provider);
  if (!country) {
    setResult("请先选择国家", "fail");
    return;
  }
  const strategy = readStrategyOptions();
  if ((strategy.lock || strategy.floatEnabled) && strategy.target <= 0) {
    setResult("开启锁定价格或价格浮动前，请填写大于 0 的目标价格", "fail");
    $("targetPrice")?.focus();
    return;
  }
  if (!strategy.continuous && strategy.targetSuccessCount > strategy.maxAttempts) {
    setResult("未开启一直轮询时，尝试次数不能小于目标成功次数", "fail");
    $("maxAttempts")?.focus();
    return;
  }
  const body = {
    provider,
    service: $("regService").value,
    country,
    target_price: strategy.target || undefined,
    price_max: strategy.priceMax,
    price_lock_enabled: strategy.lock,
    price_float_enabled: strategy.floatEnabled,
    price_float_percent: strategy.floatPercent,
    continuous_poll: strategy.continuous,
    target_success_count: strategy.targetSuccessCount,
    request_interval_enabled: strategy.intervalEnabled,
    request_interval: strategy.requestInterval,
    wait_timeout: Number($("waitTimeout").value) || 180,
    max_attempts: strategy.maxAttempts,
    country_fallback: ($("fallbackCountries").value || "").split(",").map(item => item.trim()).filter(Boolean),
    proxy: $("proxyUrl").value || "",
  };
  const button = $("startBtn");
  state.registerSubmitting = true;
  if (button) {
    button.disabled = true;
    button.classList.add("loading");
    button.dataset.originalText = button.textContent;
    button.textContent = "创建中...";
  }
  setResult("正在提交到后台创建并发任务...", "running");
  setStatus($("registerState"), "提交中", "run");
  try {
    const response = await requestJSON("/api/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (response.error || response.status === "failed") {
      setResult(response.error || "创建任务失败", "fail");
      setStatus($("registerState"), "失败", "fail");
      return;
    }
    state.currentTaskId = response.task_id;
    state.selectedTaskId = response.task_id;
    setResult(`已创建并发任务 ${taskFingerprint(response.task_id)}，等待后台状态回传`, "running");
    setStatus($("registerState"), "运行中", "run");
    log(`已创建并发任务 ${taskFingerprint(response.task_id)} · ${providerLabel(provider)} · ${country}`, "ok");
    pollTask(response.task_id);
    await loadTasks();
  } catch (error) {
    setResult(`创建任务失败：${error.message}`, "fail");
    setStatus($("registerState"), "失败", "fail");
    log(`创建任务失败：${error.message}`, "err");
  } finally {
    state.registerSubmitting = false;
    if (button) {
      button.disabled = false;
      button.classList.remove("loading");
      button.textContent = button.dataset.originalText || "▶ 开始注册";
      delete button.dataset.originalText;
    }
  }
}

async function pollTask(taskId) {
  if (!taskId || state.pollingTasks.has(taskId)) return;
  state.pollingTasks.add(taskId);
  try {
    while (true) {
      await new Promise(resolve => setTimeout(resolve, 3000));
      const task = await fetchJSON(`/api/task/${encodeURIComponent(taskId)}`);
      if (!task) continue;
      upsertTask(task);
      updateTaskKpis();
      renderTasks();
      if (task.task_id === state.selectedTaskId) renderSelectedTask();
      logTaskTransition(task);
      if (task.status !== "running") {
        if (task.task_id === state.currentTaskId) {
          setStatus($("registerState"), task.status || "结束", statusClass(task.status));
        }
        await Promise.allSettled([loadTasks(), loadStats(), loadBalances(), loadAccounts()]);
        break;
      }
    }
  } finally {
    state.pollingTasks.delete(taskId);
  }
}

async function stopTask(taskId) {
  if (!taskId || state.stoppingTasks.has(taskId)) return;
  state.stoppingTasks.add(taskId);
  renderTasks();
  log(`正在停止 ${taskFingerprint(taskId)}，等待后台完成资源收尾`, "warn");
  try {
    const task = await requestJSON(`/api/task/${encodeURIComponent(taskId)}/stop`, { method: "POST" });
    upsertTask(task);
    const type = task.status === "cancelled" ? "ok" : task.status === "attention" ? "warn" : "info";
    log(`任务 ${taskFingerprint(taskId)} 当前状态：${task.status}`, type);
  } catch (error) {
    log(`停止 ${taskFingerprint(taskId)} 失败：${error.message}`, "err");
  } finally {
    state.stoppingTasks.delete(taskId);
    await Promise.allSettled([loadTasks(), loadStats()]);
  }
}

function stopRunningTask() {
  const selected = selectedTask();
  const current = state.tasks.find(item => item.task_id === state.currentTaskId);
  const task = [selected, current, ...state.tasks].find(item => taskCanStop(item));
  if (task) {
    stopTask(task.task_id);
    return;
  }
  setResult("没有运行中的任务可停止", "fail");
}

async function testNumber() {
  const provider = $("regProvider").value;
  const service = $("regService")?.value || "whatsapp";
  const country = selectedRegisterCountryForPricing(provider) || resolveCountryInput($("providerPriceCountry")?.value || "", provider);
  if (!country) {
    setResult("请先选择国家", "fail");
    return;
  }
  const strategy = readStrategyOptions();
  setResult("正在测试取号...", "running");
  const response = await fetchJSON("/api/provider/test-number", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      provider,
      service,
      country,
      target_price: strategy.target || undefined,
      price_max: strategy.priceMax,
      price_lock_enabled: strategy.lock,
      price_float_enabled: strategy.floatEnabled,
      price_float_percent: strategy.floatPercent,
    }),
  });
  setResult(response?.phone ? `测试成功 ${response.phone}` : response?.error || "取号失败", response?.phone ? "ok" : "fail");
}

async function loadProviderPrices() {
  const provider = $("providerPriceSelect").value;
  const service = $("providerPriceService").value;
  const requestSeq = ++state.providerPriceRequestSeq;
  const country = resolveCountryInput($("providerPriceCountry").value.trim(), provider)
    || (state.selectedCountry ? countryValueForProvider(state.selectedCountry, provider) : "")
    || "Thailand";
  setStatus($("providerPriceProvider"), providerLabel(provider), "run");
  $("providerPriceRows").innerHTML = `<tr><td colspan="8" class="empty">查询中...</td></tr>`;
  const data = await fetchJSON(`/api/provider/prices?provider=${encodeURIComponent(provider)}&service=${encodeURIComponent(service)}&country=${encodeURIComponent(country)}`);
  if (requestSeq !== state.providerPriceRequestSeq) return;
  if (!data) {
    $("providerPriceRows").innerHTML = `<tr><td colspan="8" class="empty">价格查询失败，检查 API Key、国家 ID 或平台服务代码</td></tr>`;
    setStatus($("providerPriceProvider"), providerLabel(provider), "fail");
    return;
  }
  setStatus($("providerPriceProvider"), providerLabel(provider), data.available ? "ok" : "warn");
  $("providerPriceRows").innerHTML = `<tr>
    <td>${esc(service)}</td>
    <td>${esc(data.country_label || country)}<div class="country-sub">${esc(data.country || "")}</div></td>
    <td class="mono">${price(data.low)}</td>
    <td class="mono">${price(data.middle)}</td>
    <td class="mono">${price(data.high)}</td>
    <td>${esc(data.stock ?? 0)}</td>
    <td class="mono">${esc(data.service_code || "-")}</td>
    <td><span class="status ${data.available ? "ok" : "idle"}"><span class="dot"></span>${data.available ? "可用" : "无库存"}</span></td>
  </tr>`;
}

async function loadRecommendations() {
  const service = $("priceServiceSelect").value;
  const country = resolveCountryInput($("priceLookupSearch").value.trim(), "");
  const requestSeq = ++state.recommendationRequestSeq;
  setStatus($("priceResultStatus"), "查询中", "run");
  $("priceResults").innerHTML = `<div class="rule"><div><strong>正在查询价格</strong><span>跨平台比较 ${esc(country || "推荐国家")} / ${esc(service)}</span></div><span class="status run"><span class="dot"></span>...</span></div>`;
  const url = `/api/prices/recommendations?service=${encodeURIComponent(service)}&country=${encodeURIComponent(country)}`;
  const data = await fetchJSON(url);
  if (requestSeq !== state.recommendationRequestSeq) return;
  if (!data) {
    setStatus($("priceResultStatus"), "查询失败", "fail");
    renderFallbackBands(service);
    $("priceResults").innerHTML = `<div class="rule"><div><strong>价格查询失败</strong><span>检查平台 API Key、国家输入或网络连接</span></div><span class="status fail"><span class="dot"></span>失败</span></div>`;
    return;
  }
  state.lastRecommendations = data;
  renderRecommendationResults(data);
  renderCountryBands(data.country_bands || [], service);
}

function renderRecommendationResults(data) {
  const target = Number($("recommendTargetPrice").value || 0);
  const floatPercent = Number(String($("recommendFloatPercent").value || "0").replace(/[^\d.]/g, ""));
  const max = target ? target * (1 + floatPercent / 100) : Infinity;
  const rows = data.best || [];
  setStatus($("priceResultStatus"), `${rows.length} 个结果`, rows.length ? "ok" : "idle");
  $("priceResults").innerHTML = rows.length ? rows.map(row => {
    const low = Number(row.low);
    const stateLabel = low <= target ? "推荐" : low <= max ? "备选" : "跳过";
    const cls = stateLabel === "推荐" ? "ok" : stateLabel === "备选" ? "run" : "idle";
    return `<div class="rule">
      <div><strong>${esc(providerLabel(row.provider))} · ${esc(row.country_label || row.country)}</strong><span>${price(row.low)} · 库存 ${esc(row.stock || 0)} · ${esc(row.service_code || row.service)}</span></div>
      <span class="status ${cls}"><span class="dot"></span>${stateLabel}</span>
    </div>`;
  }).join("") : `<div class="rule"><div><strong>没有可推荐价格</strong><span>可换国家、服务模式，或先同步国家索引</span></div><span class="status idle"><span class="dot"></span>空</span></div>`;
}

function renderCountryBands(rows, service) {
  const mode = serviceLabel(service);
  setStatus($("priceCountryMode"), `${mode} 模式`, rows.length ? "ok" : "idle");
  const bands = rows.slice(0, 3);
  $("priceBandGrid").innerHTML = bands.length ? bands.map((row, index) => {
    const labels = ["最低价入口", "中间价稳妥", "最高价上限"];
    const value = index === 0 ? row.low : index === 1 ? row.middle : row.high;
    return `<div class="price-band"><span>${labels[index]} · ${esc(row.country_label)}</span><strong>${price(value)}</strong><span>${esc(row.provider_count || 0)} 平台 · 库存 ${esc(row.stock || 0)}</span></div>`;
  }).join("") : `<div class="price-band"><span>暂无价带</span><strong>-</strong><span>点击查询价格获取实时数据</span></div>`;
  $("priceCountryRows").innerHTML = rows.length ? rows.map(row => `<tr>
    <td>${esc(row.country_label)}</td>
    <td>${esc((row.providers || []).map(providerLabel).join(" / "))}</td>
    <td class="mono">${price(row.low)}</td>
    <td class="mono">${price(row.middle)}</td>
    <td class="mono">${price(row.high)}</td>
    <td>${esc(row.stock || 0)}</td>
    <td>${Number(row.stock || 0) > 20 ? "适合设置目标价轮询" : "库存偏少，建议作为备选"}</td>
  </tr>`).join("") : `<tr><td colspan="7" class="empty">点击右侧“查询价格”后生成价带</td></tr>`;
}

function renderFallbackBands(service) {
  const mode = serviceLabel(service);
  setStatus($("priceCountryMode"), `${mode} 模式`, "idle");
  $("priceBandGrid").innerHTML = `<div class="price-band"><span>最低价入口</span><strong>-</strong><span>等待实时价格</span></div><div class="price-band"><span>中间价稳妥</span><strong>-</strong><span>等待实时价格</span></div><div class="price-band"><span>最高价上限</span><strong>-</strong><span>等待实时价格</span></div>`;
  $("priceCountryRows").innerHTML = `<tr><td colspan="7" class="empty">点击右侧“查询价格”后，根据 ${esc(mode)} 服务生成推荐国家价带</td></tr>`;
}

function serviceLabel(service) {
  return { whatsapp: "WhatsApp", google: "Google", telegram: "Telegram" }[service] || service;
}

async function applyRecommendationToRegister() {
  const first = state.lastRecommendations?.best?.[0];
  if (!first) {
    log("请先查询价格推荐", "warn");
    return;
  }
  const provider = first.provider || $("regProvider").value;
  $("regProvider").value = provider;
  await loadCountries(provider, true);
  const country = findCountryByInput(first.country_label || first.country, provider);
  if (country) selectCountry(country, false);
  $("targetPrice").value = Number(first.low || $("recommendTargetPrice").value || 0);
  $("floatPercent").value = Number(String($("recommendFloatPercent").value || "0").replace(/[^\d.]/g, ""));
  $("priceFloatEnabled").checked = Number($("floatPercent").value || 0) > 0;
  saveUiPreferences([
    "regProvider",
    "targetPrice",
    "floatPercent",
    "priceFloatEnabled",
  ]);
  syncStrategyControls();
  switchPage("dashboard");
  $("registerPanel")?.scrollIntoView({ behavior: "smooth", block: "center" });
  log(`已应用 ${providerLabel(provider)} / ${displayCountryInput(first.country_label || first.country, provider)} 的价格建议`, "ok");
}

async function loadBlacklist() {
  const provider = $("blacklistProvider")?.value || "";
  const data = await fetchJSON(`/api/blacklist${provider ? `?provider=${encodeURIComponent(provider)}` : ""}`);
  state.blacklist = data?.entries || [];
  $("kpiBlacklist").textContent = state.blacklist.length;
  setStatus($("blacklistCount"), `${state.blacklist.length} 条`, "idle");
  renderBlacklist();
}

function renderBlacklist() {
  const q = normalizeSearch($("blacklistSearch")?.value || "");
  const rows = state.blacklist.filter(item => {
    const text = normalizeSearch([item.phone, item.provider, item.reason, item.created_at].join(" "));
    return !q || text.includes(q);
  });
  $("blacklistRows").innerHTML = rows.length ? rows.map(item => `<tr>
    <td class="mono">${esc(item.phone)}</td>
    <td>${esc(providerLabel(item.provider))}</td>
    <td>${esc(item.reason || "-")}</td>
    <td class="mono">${esc(String(item.created_at || "").slice(0, 19))}</td>
    <td><button class="btn" data-remove-blacklist="${esc(item.phone)}" data-provider="${esc(item.provider || "")}">移除</button></td>
  </tr>`).join("") : `<tr><td colspan="5" class="empty">暂无黑名单</td></tr>`;
  document.querySelectorAll("[data-remove-blacklist]").forEach(button => {
    button.addEventListener("click", () => removeBlacklist(button.dataset.removeBlacklist, button.dataset.provider));
  });
}

async function addBlacklist() {
  const phone = $("blacklistPhone").value.trim();
  if (!phone) return;
  await fetchJSON("/api/blacklist", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ phone, provider: $("blacklistAddProvider").value, reason: $("blacklistReason").value }),
  });
  $("blacklistPhone").value = "";
  await loadBlacklist();
}

async function removeBlacklist(phone, provider) {
  await fetchJSON(`/api/blacklist?phone=${encodeURIComponent(phone)}&provider=${encodeURIComponent(provider || "")}`, { method: "DELETE" });
  await loadBlacklist();
}

async function loadConfig() {
  const cfg = await fetchJSON("/api/config");
  if (!cfg) return;
  state.config = cfg;
  $("cfgWaUrl").value = cfg.wa_app_url || "";
  $("cfgAdapter").value = "whatsapp_wa";
  $("adapterPill").textContent = "whatsapp_wa";
  $("adapterName").textContent = "whatsapp_wa";
  $("waConfigState").className = `status ${cfg.wa_app_url ? "ok" : "idle"}`;
  $("waConfigState").innerHTML = `<span class="dot"></span>${cfg.wa_app_url ? "已配置" : "未配置"}`;
  renderProviderConfig(cfg);
}

function renderProviderConfig(cfg) {
  const cards = [
    { id: "smsbower", label: "SMSBower", key: "smsbower_api_key", mask: cfg.smsbower_api_key_masked, base: "smsbower_base_url", baseValue: cfg.smsbower_base_url || defaultBaseUrls.smsbower },
    { id: "5sim", label: "5sim", key: "five_sim_api_key", mask: cfg.five_sim_api_key_masked },
    { id: "herosms", label: "HeroSMS", key: "hero_sms_api_key", mask: cfg.hero_sms_api_key_masked, base: "hero_sms_base_url", baseValue: cfg.hero_sms_base_url || defaultBaseUrls.herosms },
  ];
  $("providerConfigCards").innerHTML = cards.map(card => `<div class="config-card" data-config-provider="${card.id}">
    <h3>${card.label} <span class="status ${card.mask ? "ok" : "idle"}"><span class="dot"></span>${card.mask ? "已保存" : "未设置"}</span></h3>
    <div class="field"><label>API Key</label><input type="password" data-config-key="${card.key}" placeholder="${esc(card.mask || "输入后保存")}"></div>
    ${card.base ? `<div class="field" style="margin-top:10px"><label>Base URL</label><input data-config-key="${card.base}" value="${esc(card.baseValue || "")}"></div>` : ""}
    <div class="toolbar"><button class="btn primary" data-save-provider="${card.id}">保存</button><button class="btn" data-test-config-provider="${card.id}">测试</button><span class="status idle" id="cfgStatus-${card.id}"><span class="dot"></span>待操作</span></div>
  </div>`).join("");
  document.querySelectorAll("[data-save-provider]").forEach(button => button.addEventListener("click", () => saveProviderConfig(button.dataset.saveProvider)));
  document.querySelectorAll("[data-test-config-provider]").forEach(button => button.addEventListener("click", () => testProviderConfig(button.dataset.testConfigProvider)));
}

async function saveRuntimeConfig() {
  const body = { wa_app_url: $("cfgWaUrl").value.trim() };
  const data = await fetchJSON("/api/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  setStatus($("waConfigState"), data ? "已保存" : "失败", data ? "ok" : "fail");
  await loadHealth();
}

async function saveProviderConfig(provider) {
  const card = document.querySelector(`[data-config-provider="${provider}"]`);
  const body = {};
  card?.querySelectorAll("[data-config-key]").forEach(input => {
    if (input.type === "password" && !input.value.trim()) return;
    body[input.dataset.configKey] = input.value.trim();
  });
  const data = await fetchJSON("/api/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  setStatus($(`cfgStatus-${provider}`), data ? "已保存" : "失败", data ? "ok" : "fail");
  if (data) {
    card?.querySelectorAll("input[type='password']").forEach(input => { input.value = ""; });
    state.countries = {};
    await loadConfig();
    await loadCountries(provider, true);
    syncSelectedCountryForProvider($("regProvider")?.value || provider);
  }
}

async function testProviderConfig(provider) {
  setStatus($(`cfgStatus-${provider}`), "测试中", "run");
  const data = await fetchJSON(`/api/provider/balance?provider=${encodeURIComponent(provider)}`);
  setStatus($(`cfgStatus-${provider}`), data?.balance != null ? money(data.balance) : "失败", data?.balance != null ? "ok" : "fail");
  await loadBalances();
}

document.addEventListener("DOMContentLoaded", init);
