import assert from "node:assert/strict";
import test from "node:test";
import { parseApiErrorResponse } from "../src/api/client.ts";
import {
  type TrackedOrderVersion,
  mergeActiveOrderSnapshot,
  recordOrderState,
  shouldLogoutOnSilentAuthCheck,
} from "../src/api/orderSync.ts";
import { SseReconnectTracker, buildStreamUrl, extractEventCursor } from "../src/api/sseCursor.ts";
import type { SmsOrder } from "../src/api/types.ts";
import {
  WorkbenchIdempotencyController,
  describeOrderSubmitOutcome,
} from "../src/features/workbench/orderSubmission.ts";
import {
  formatFinancialSummary,
  formatMoney,
  formatPrice,
  normCountryQuery,
  resolveOrderStatusMeta,
} from "../src/utils/format.ts";
import {
  type PriceCombo,
  type PriceSelectionState,
  reconcileComboChange,
  resolveEffectiveTargetPrice,
  selectPriceTier,
  setManualPrice,
  shouldAcceptPriceSnapshot,
} from "../src/features/workbench/priceSelection.ts";

// ── P1-05: 工作台 SSE 自动重连携带最后事件游标与 resync 重置 ──────────

test("P1-05: SseReconnectTracker 首次无游标，收到事件后自动重连携带 ?last_event_id=", () => {
  const tracker = new SseReconnectTracker(1500, 15000);
  assert.equal(tracker.nextUrl(), "/api/v1/events/stream");
  assert.deepEqual(tracker.markConnected(), { isReconnect: false });

  // 收到服务端事件（带帧级 lastEventId 或 payload.id）
  tracker.recordEvent("evt_42_abcd1234", "evt_42_abcd1234");
  assert.equal(tracker.cursor, "evt_42_abcd1234");

  // 断线自动重连时，URL 必须显式附带 last_event_id 游标
  assert.equal(
    tracker.nextUrl(),
    "/api/v1/events/stream?last_event_id=evt_42_abcd1234",
  );
  assert.equal(tracker.nextBackoffDelayMs(), 1500);
  assert.equal(tracker.nextBackoffDelayMs(), 2400);

  // 重连成功后识别为 isReconnect=true 并重置退避
  assert.deepEqual(tracker.markConnected(), { isReconnect: true });
  assert.equal(tracker.nextBackoffDelayMs(), 1500);

  // 收到 resync_required 后重置游标，不再带过期游标反复触发 resync
  tracker.resetCursor();
  assert.equal(tracker.nextUrl(), "/api/v1/events/stream");
});

test("P1-05: extractEventCursor 与 buildStreamUrl 正确编码并回退 payload.id", () => {
  assert.equal(extractEventCursor("", "evt_99_xyz"), "evt_99_xyz");
  assert.equal(extractEventCursor("evt_100_hdr", "evt_99_xyz"), "evt_100_hdr");
  assert.equal(
    buildStreamUrl("evt 1/2", "/api/v1/events/stream?project_id=p1"),
    "/api/v1/events/stream?project_id=p1&last_event_id=evt%201%2F2",
  );
});

// ── P2-01: 价档选择严格绑定组合、切换即清空、手工价分离与迟到旧包拦截 ──

test("P2-01: 点击泰国 WhatsApp 价档后切换印尼或 Telegram，立即下单不携带旧组合价档", () => {
  const thWa: PriceCombo = { provider: "smsbower", countryId: "52", service: "whatsapp" };
  const idWa: PriceCombo = { provider: "smsbower", countryId: "6", service: "whatsapp" };
  const thTg: PriceCombo = { provider: "smsbower", countryId: "52", service: "telegram" };

  let state: PriceSelectionState = {
    selectedTier: null,
    manualTargetPrice: "",
    priceLock: false,
  };

  // 1. 点击泰国 WhatsApp 的 $0.12 价档并开启限价
  state = selectPriceTier(state, thWa, 0.12);
  state = { ...state, priceLock: true };
  assert.deepEqual(resolveEffectiveTargetPrice(state, thWa), {
    value: 0.12,
    displayValue: "0.12",
    source: "tier",
  });

  // 2. 即使在 useEffect 尚未触发前直接拿新组合 idWa 提交，resolveEffectiveTargetPrice 也绝不返回旧价档 $0.12！
  assert.deepEqual(resolveEffectiveTargetPrice(state, idWa), {
    value: undefined,
    displayValue: "",
    source: "none",
  });

  // 3. 切换国家到印尼（reconcileComboChange）清空旧价档并自动关闭无目标价的 priceLock
  const afterSwitchCountry = reconcileComboChange(state, idWa);
  assert.equal(afterSwitchCountry.selectedTier, null);
  assert.equal(afterSwitchCountry.priceLock, false);

  // 4. 切换服务到 Telegram 同样立刻清除旧价档
  const afterSwitchService = reconcileComboChange(state, thTg);
  assert.equal(afterSwitchService.selectedTier, null);
});

test("P2-01: 手工输入全局目标价与价档点击清晰分离，切换组合保留手工价但不保留价档", () => {
  const thWa: PriceCombo = { provider: "smsbower", countryId: "52", service: "whatsapp" };
  const idWa: PriceCombo = { provider: "smsbower", countryId: "6", service: "whatsapp" };

  let state: PriceSelectionState = {
    selectedTier: null,
    manualTargetPrice: "",
    priceLock: true,
  };

  state = setManualPrice(state, "0.25");
  assert.equal(state.selectedTier, null);
  assert.deepEqual(resolveEffectiveTargetPrice(state, thWa), {
    value: 0.25,
    displayValue: "0.25",
    source: "manual",
  });

  // 切换国家时手工指定的全局上限仍保留且清晰标记为 manual
  const afterSwitch = reconcileComboChange(state, idWa);
  assert.deepEqual(resolveEffectiveTargetPrice(afterSwitch, idWa), {
    value: 0.25,
    displayValue: "0.25",
    source: "manual",
  });
});

test("P2-01: 迟到的旧请求价格快照被序号与组合双重校验拒绝，绝不覆盖新组合快照", () => {
  const thWa: PriceCombo = { provider: "smsbower", countryId: "52", service: "whatsapp" };
  const idWa: PriceCombo = { provider: "smsbower", countryId: "6", service: "whatsapp" };

  // 慢旧请求 seq=1 (thWa) 在快新请求 seq=2 (idWa) 之后到达 -> 拒绝
  assert.equal(shouldAcceptPriceSnapshot(1, 2, thWa, idWa), false);
  // 即使序号偶然相同但组合已变 -> 拒绝
  assert.equal(shouldAcceptPriceSnapshot(2, 2, thWa, idWa), false);
  // 序号与组合均匹配当前页面组合 -> 接受
  assert.equal(shouldAcceptPriceSnapshot(2, 2, idWa, idWa), true);
});

// ── 补充 P1-02: 工作台下单幂等键在响应不确定时复用，成功或独立新单时换新 ──

test("补充 P1-02: WorkbenchIdempotencyController 在网络/5xx 不确定失败后复用键，成功后换新键", () => {
  let seq = 0;
  const ctrl = new WorkbenchIdempotencyController(() => `wb_key_${++seq}`);
  const payload = { provider: "smsbower", service: "whatsapp", country: "52" };

  // 1. 首次提交生成 wb_key_1，模拟网络断开（status=undefined）
  const k1 = ctrl.beginAttempt(payload);
  assert.equal(k1, "wb_key_1");
  ctrl.onFailure(undefined);

  // 2. 同参数重试复用同一个 wb_key_1，模拟 502 不确定失败仍保留
  assert.equal(ctrl.beginAttempt(payload), "wb_key_1");
  ctrl.onFailure(502);
  assert.equal(ctrl.beginAttempt(payload), "wb_key_1");

  // 3. 成功后清空，再发起独立新单生成全新 wb_key_2
  ctrl.onSuccess();
  const k2 = ctrl.beginAttempt(payload);
  assert.equal(k2, "wb_key_2");

  // 4. 确定性 409 无号拒绝后清空，再次尝试拿到新键 wb_key_3
  ctrl.onFailure(409);
  assert.equal(ctrl.beginAttempt(payload), "wb_key_3");

  // 5. 若未决期间用户修改了国家参数，自动生成新键 wb_key_4
  assert.equal(ctrl.beginAttempt({ ...payload, country: "6" }), "wb_key_4");
});

// ── v5 复核 P1-04 / P1-05: 按币种展示实付/估算/退款，绝不混加多币种为美元 ──

test("v5 复核 P1-05: formatMoney/formatPrice 与 formatFinancialSummary 严格区分币种，绝不混加为美元", () => {
  assert.equal(formatMoney(12.5, "RUB"), "RUB 12.50");
  assert.equal(formatMoney(21, "USD"), "$21.00");
  assert.equal(formatMoney(3, "UNKNOWN"), "未知币种 3.00");
  assert.equal(formatPrice(0.25, "RUB"), "RUB 0.25");

  // 复核样本：RUB 12.50 实付 + USD 21.00 实付 + USD 0.50 估算，绝不显示 $34.00
  const mixed = formatFinancialSummary({
    currency: null,
    is_multi_currency: true,
    cost_total: null,
    by_currency: [
      {
        currency: "USD",
        actual_cost_total: 21.0,
        refunded_amount_total: 0,
        net_actual_cost_total: 21.0,
        estimated_cost_total: 0.5,
        cost_total: 21.5,
        unknown_cost_count: 0,
        refund_amount_unknown_count: 1,
      },
      {
        currency: "RUB",
        actual_cost_total: 12.5,
        refunded_amount_total: 0,
        net_actual_cost_total: 12.5,
        estimated_cost_total: 0,
        cost_total: 12.5,
        unknown_cost_count: 0,
      },
    ],
  });

  assert.equal(mixed.isMultiCurrency, true);
  assert.equal(mixed.primary, "$21.50 + RUB 12.50");
  assert.ok(!mixed.primary.includes("34"));
  assert.equal(mixed.lines.length, 2);
  assert.ok(mixed.lines[0].includes("实付 $21.00"));
  assert.ok(mixed.lines[0].includes("估算 $0.50"));
  assert.ok(mixed.lines[0].includes("金额未知退款 1 笔"));
  assert.ok(mixed.lines[1].includes("RUB 12.50"));
  assert.equal(mixed.fxNote, null);

  // 提供明确汇率与折算日期时展示折算说明
  const withFx = formatFinancialSummary({
    is_multi_currency: true,
    cost_total: null,
    by_currency: [
      {
        currency: "USD",
        actual_cost_total: 21.0,
        refunded_amount_total: 0,
        net_actual_cost_total: 21.0,
        estimated_cost_total: 0.5,
        cost_total: 21.5,
        unknown_cost_count: 0,
      },
      {
        currency: "RUB",
        actual_cost_total: 12.5,
        refunded_amount_total: 0,
        net_actual_cost_total: 12.5,
        estimated_cost_total: 0,
        cost_total: 12.5,
        unknown_cost_count: 0,
      },
    ],
    fx_conversion: {
      target_currency: "USD",
      as_of_date: "2026-11-25",
      rates: { USD: 1, RUB: 0.01 },
      actual_cost_total: 21.125,
      refunded_amount_total: 0,
      net_actual_cost_total: 21.125,
      estimated_cost_total: 0.5,
      cost_total: 21.625,
    },
  });
  assert.ok(withFx.fxNote?.includes("2026-11-25"));
  assert.ok(withFx.fxNote?.includes("$21.63"));
});

// ── v6 复核 P2-02: 保留错误 orderId、202 失败单不误报成功、确认失败后可同参数换新键下单 ──

test("v6 复核 P2-02: 失败的 202 重放不显示取号成功；终态 500 保留 orderId 并支持查单后或显式新建订单", () => {
  // 1. 异步失败订单 202 重放 -> 必须返回 error 提示，绝不误报“取号成功”
  const failed202 = describeOrderSubmitOutcome({
    order_id: "ord_async_fail",
    status: "failed",
    phone_e164: "",
    phone_raw: "",
    error_code: "NO_NUMBERS",
    error_message: "暂无可用号码",
  });
  assert.equal(failed202.tone, "error");
  assert.equal(failed202.isTerminalFailure, true);
  assert.ok(!failed202.message.includes("取号成功"));

  const compPending = describeOrderSubmitOutcome({
    order_id: "ord_comp_1",
    status: "compensation_pending",
    phone_e164: "",
    phone_raw: "",
    error_code: "ACQUIRE_POST_ERROR",
    error_message: "转入退款补偿",
  });
  assert.equal(compPending.tone, "warn");
  assert.ok(!compPending.message.includes("取号成功"));

  // 2. 终态 500 响应解析必须保留 details.order_id
  const err500 = parseApiErrorResponse(500, {
    error: {
      code: "ACQUIRE_POST_ERROR",
      message: "取号后处理异常",
      retryable: true,
      details: { order_id: "ord_500_x", replayed: true },
    },
  });
  assert.equal(err500.status, 500);
  assert.equal(err500.code, "ACQUIRE_POST_ERROR");
  assert.equal(err500.orderId, "ord_500_x");
  assert.equal(err500.details?.order_id, "ord_500_x");

  // 3. 控制器在 500 与断网时保留原键和 orderId，确认旧单终结或显式新建后同参数签发新键
  let seq = 0;
  const ctrl = new WorkbenchIdempotencyController(() => `wb_v6_${++seq}`);
  const payload = { provider: "smsbower", service: "whatsapp", country: "52" };

  const k1 = ctrl.beginAttempt(payload);
  assert.equal(k1, "wb_v6_1");
  ctrl.onFailure(err500.status, { code: err500.code, message: err500.message, orderId: err500.orderId });
  assert.equal(ctrl.currentAttempt?.lastOrderId, "ord_500_x");
  assert.equal(ctrl.beginAttempt(payload), "wb_v6_1");

  // 查询旧单确认已取消/失败 -> 自动释放旧键，允许同参数立即签发新键 wb_v6_2
  assert.equal(ctrl.confirmOrderChecked("cancelled"), true);
  assert.equal(ctrl.beginAttempt(payload), "wb_v6_2");

  // 断网（status=undefined）保留原键防重复扣费；用户显式点击“确认新建订单”后同参数签发新键 wb_v6_3
  ctrl.onFailure(undefined, { message: "Failed to fetch" });
  assert.equal(ctrl.beginAttempt(payload), "wb_v6_2");
  ctrl.resetForNewOrder();
  assert.equal(ctrl.beginAttempt(payload), "wb_v6_3");
});

// ── v7 复核 P1-06 & P1-03: 旧订单慢回包绝不抹除新尝试幂等键 + 待核对币种展示隔离 ──

test("v7 复核 P1-06: 订单 A 的慢查询回包晚于订单 B 网络错误到达时，绝不清除订单 B 的幂等键", () => {
  let seq = 0;
  const ctrl = new WorkbenchIdempotencyController(() => `wb_v7_${++seq}`);
  const payloadA = { provider: "smsbower", service: "whatsapp", country: "52" };
  const payloadB = { provider: "smsbower", service: "whatsapp", country: "6" };

  // 1. 订单 A 遇到 500 保留 keyA 与 orderA，用户发起查单 A（捕获 keyA & ord_A）
  const keyA = ctrl.beginAttempt(payloadA);
  ctrl.onFailure(500, { code: "ACQUIRE_POST_ERROR", orderId: "ord_A" });
  const capturedA = { key: ctrl.currentAttempt?.key, orderId: ctrl.currentAttempt?.lastOrderId };
  assert.equal(capturedA.key, keyA);
  assert.equal(capturedA.orderId, "ord_A");

  // 2. 在 A 的查询尚未返回前，用户切换参数提交新订单 B，且 B 遇到网络不确定错误，保留 keyB
  const keyB = ctrl.beginAttempt(payloadB);
  assert.notEqual(keyB, keyA);
  ctrl.onFailure(undefined, { message: "Network timeout" });
  assert.equal(ctrl.currentKey, keyB);

  // 3. 迟到的订单 A 终态回包到达，调用 confirmOrderChecked 绑定 (keyA, ord_A) -> 必须被拒绝且不清除 keyB！
  assert.equal(ctrl.confirmOrderChecked("failed", capturedA), false);
  assert.equal(ctrl.currentKey, keyB);

  // 4. 用户以相同参数重试 B -> 必须仍沿用 keyB，绝不分配新键引发重复买号！
  assert.equal(ctrl.beginAttempt(payloadB), keyB);

  // 5. 仅当匹配当前尝试 (keyB) 的终态确认到达时才允许清除
  assert.equal(ctrl.confirmOrderChecked("cancelled", { key: keyB }), true);
  assert.equal(ctrl.currentKey, null);
});

test("v7 复核 P1-03: formatFinancialSummary 明确拆分已核实币种与待核对历史币种", () => {
  const summary = formatFinancialSummary({
    currency: null,
    is_multi_currency: true,
    cost_total: null,
    by_currency: [
      {
        currency: "EUR",
        actual_cost_total: 2.0,
        refunded_amount_total: 0,
        net_actual_cost_total: 2.0,
        estimated_cost_total: 0,
        cost_total: 2.0,
        unknown_cost_count: 0,
      },
    ],
    currency_review_count: 1,
    currency_review_by_currency: [
      {
        currency: "USD",
        actual_cost_total: 1.0,
        refunded_amount_total: 0,
        net_actual_cost_total: 1.0,
        estimated_cost_total: 0,
        cost_total: 1.0,
        unknown_cost_count: 0,
      },
    ],
  });
  assert.ok(summary.primary.includes("EUR 2.00"));
  assert.ok(summary.primary.includes("1 笔待核对"));
  assert.ok(summary.lines.some((line) => line.includes("待核对币种 (1 笔)")));
});

// ── 全项目审查补充：价档二次点击取消、国家搜索符号清洗与退款终态徽标区分 ──

test("全项目审查: 再次点击已选中价档可取消选中，normCountryQuery 兼容 +区号 与 #ID 搜索", () => {
  const thGo: PriceCombo = { provider: "smsbower", countryId: "52", service: "google" };
  let state: PriceSelectionState = {
    selectedTier: null,
    manualTargetPrice: "",
    priceLock: false,
  };
  state = selectPriceTier(state, thGo, 0.18);
  state = { ...state, priceLock: true };
  assert.equal(resolveEffectiveTargetPrice(state, thGo).value, 0.18);

  // 再次点击同一价档 -> 自动取消选中并关闭无手工价的 priceLock
  state = selectPriceTier(state, thGo, 0.18);
  assert.equal(state.selectedTier, null);
  assert.equal(state.priceLock, false);
  assert.equal(resolveEffectiveTargetPrice(state, thGo).value, undefined);

  // +66 / #52 / U.S. 统一归一化以匹配 cc 与 id
  assert.equal(normCountryQuery("+66"), "66");
  assert.equal(normCountryQuery("#52"), "52");
  assert.equal(normCountryQuery("U.S."), "us");
});

test("全项目审查: resolveOrderStatusMeta 准确区分已确认退款、未退、待核对与无激活取消", () => {
  assert.equal(resolveOrderStatusMeta("cancelled", "confirmed").label, "已取消退款");
  assert.equal(resolveOrderStatusMeta("cancelled", "unrefundable").label, "已取消(未退)");
  assert.equal(resolveOrderStatusMeta("cancelled", "unknown").label, "已取消(待核对)");
  assert.equal(resolveOrderStatusMeta("cancelled", "none").label, "已取消");
  assert.equal(resolveOrderStatusMeta("expired", "confirmed").label, "超时已退");
  assert.equal(resolveOrderStatusMeta("expired", "unrefundable").label, "超时(未退)");
  assert.equal(resolveOrderStatusMeta("expired", "unknown").label, "超时(待核对)");
});

test("v8 审计补充: parseApiErrorResponse 准确透传 CSRF_REJECTED 与 AUTH_RATE_LIMITED 错误码与重试属性", () => {
  const csrfErr = parseApiErrorResponse(403, {
    error: {
      code: "CSRF_REJECTED",
      message: "跨站写请求已被 CSRF 安全策略拦截",
      retryable: false,
    },
  });
  assert.equal(csrfErr.status, 403);
  assert.equal(csrfErr.code, "CSRF_REJECTED");
  assert.equal(csrfErr.retryable, false);

  const rateErr = parseApiErrorResponse(429, {
    error: {
      code: "AUTH_RATE_LIMITED",
      message: "登录失败次数过多，请稍后再试",
      retryable: true,
    },
  });
  assert.equal(rateErr.status, 429);
  assert.equal(rateErr.code, "AUTH_RATE_LIMITED");
  assert.equal(rateErr.retryable, true);
});

// ── v9 全盘审查补充：脱敏水位防复活、活跃列表在途快照并发合并、静默会话防误登出 ──

function makeMockOrder(overrides: Partial<SmsOrder> & { order_id: string }): SmsOrder {
  return {
    order_id: overrides.order_id,
    project_id: overrides.project_id ?? "web-console",
    external_ref: "",
    provider: "smsbower",
    service: "whatsapp",
    service_code: "wa",
    country_id: "52",
    country_label: "泰国",
    country_iso2: "TH",
    country_calling_code: "66",
    activation_id: "act_1",
    phone_raw: "66812345678",
    phone_e164: "+66812345678",
    phone_national: "812345678",
    status: overrides.status ?? "waiting_code",
    version: overrides.version ?? 1,
    latest_code: overrides.latest_code ?? "",
    code_scrubbed: overrides.code_scrubbed ?? false,
    code_scrub_version: overrides.code_scrub_version ?? 0,
    code_count: overrides.code_count ?? 0,
    max_price: null,
    cost: 0.15,
    cost_is_estimated: false,
    currency: "USD",
    wait_timeout: 180,
    expires_at: "2026-12-01T00:00:00Z",
    attempts_used: 1,
    error_code: "",
    error_message: "",
    webhook_url: "",
    metadata: {},
    messages: [],
    created_at: "2026-11-27T00:00:00Z",
    updated_at: "2026-11-27T00:00:00Z",
    completed_at: "",
  };
}

test("v9 审查: recordOrderState 严格拦截同版本号但落后 code_scrub_version 的旧明文快照复活", () => {
  const map = new Map<string, TrackedOrderVersion>();

  // 1. 收到验证码 (version=3, scrub_version=0)
  const withCode = makeMockOrder({
    order_id: "ord_scrub_1",
    status: "code_received",
    version: 3,
    latest_code: "884920",
    code_scrubbed: false,
    code_scrub_version: 0,
  });
  assert.equal(recordOrderState(withCode, map), true);

  // 2. 订单被脱敏清理 (version 仍为 3 或更新，code_scrub_version 升至 1)
  const scrubbed = makeMockOrder({
    order_id: "ord_scrub_1",
    status: "code_received",
    version: 3,
    latest_code: "",
    code_scrubbed: true,
    code_scrub_version: 1,
  });
  assert.equal(recordOrderState(scrubbed, map), true);

  // 3. 迟到的旧轮询回包 (version=3, code_scrub_version=0, 携带旧明文验证码) -> 必须被拒绝！
  assert.equal(recordOrderState(withCode, map), false);

  // 4. 若用户点击「再收一条短信」收到新验证码 (version=5, code_scrub_version=2, code_scrubbed=false) -> 允许正常更新
  const freshResend = makeMockOrder({
    order_id: "ord_scrub_1",
    status: "code_received",
    version: 5,
    latest_code: "119203",
    code_scrubbed: false,
    code_scrub_version: 2,
  });
  assert.equal(recordOrderState(freshResend, map), true);
});

test("v9 审查: mergeActiveOrderSnapshot 保留在途 listOrders 请求发出后新建的本地活跃订单", () => {
  const versionMap = new Map<string, TrackedOrderVersion>();
  const mutatedSeqById = new Map<string, number>();

  const existingOrder = makeMockOrder({ order_id: "ord_old", status: "waiting_code", version: 2 });
  recordOrderState(existingOrder, versionMap);
  mutatedSeqById.set("ord_old", 1);

  // T0: listOrders 请求发出时，本地变更水位为 1
  const snapshotStartSeq = 1;

  // T1: 请求在途期间，用户新建了 ord_new (水位升至 2)
  const newlyCreated = makeMockOrder({ order_id: "ord_new", status: "acquiring", version: 1 });
  recordOrderState(newlyCreated, versionMap);
  mutatedSeqById.set("ord_new", 2);
  const currentLocal = [newlyCreated, existingOrder];

  // T2: 在途旧快照返回，仅包含 ord_old（尚未包含 ord_new）
  const snapshotItems = [makeMockOrder({ order_id: "ord_old", status: "code_received", version: 3, latest_code: "654321" })];

  const merged = mergeActiveOrderSnapshot(
    currentLocal,
    snapshotItems,
    snapshotStartSeq,
    mutatedSeqById,
    versionMap,
  );
  assert.deepEqual(
    merged.map((o) => o.order_id),
    ["ord_new", "ord_old"],
  );
  assert.equal(merged[1].latest_code, "654321");
});

test("v9 审查: shouldLogoutOnSilentAuthCheck 仅在明确 401/403 或非管理员时登出，网络闪断与 5xx 保持会话", () => {
  assert.equal(shouldLogoutOnSilentAuthCheck({ ok: false, httpStatus: undefined }), false);
  assert.equal(shouldLogoutOnSilentAuthCheck({ ok: false, httpStatus: 502 }), false);
  assert.equal(shouldLogoutOnSilentAuthCheck({ ok: false, httpStatus: 503 }), false);
  assert.equal(shouldLogoutOnSilentAuthCheck({ ok: false, httpStatus: 401 }), true);
  assert.equal(shouldLogoutOnSilentAuthCheck({ ok: false, httpStatus: 403 }), true);
  assert.equal(
    shouldLogoutOnSilentAuthCheck({
      ok: true,
      status: {
        password_required: true,
        api_auth_required: true,
        direct_loopback: false,
        authenticated: true,
        is_admin: true,
        project_id: "web-console",
        can_bootstrap_local_session: false,
      },
    }),
    false,
  );
  assert.equal(
    shouldLogoutOnSilentAuthCheck({
      ok: true,
      status: {
        password_required: true,
        api_auth_required: true,
        direct_loopback: false,
        authenticated: false,
        is_admin: false,
        project_id: "",
        can_bootstrap_local_session: false,
      },
    }),
    true,
  );
});
