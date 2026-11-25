/**
 * 工作台下单幂等键控制器与结果语义判定（修复补充 P1-02 & v6 复核 P2-02）。
 *
 * 规则：
 * 1. 每次用户点击取号生成一个稳定 Idempotency-Key，绑定该次请求的规范签名；
 * 2. 网络断开或 5xx 错误时保留原幂等键与服务端返回的 orderId/错误上下文，引导先查原订单或安全重试防重复扣费；
 * 3. 一旦收到确定性 4xx 业务拒绝、查询确认旧订单已终结，或操作者显式点击“确认新建订单”/修改参数，
 *    立即清空并签发全新的 Idempotency-Key，允许同参数发起独立新单；
 * 4. HTTP 2xx（含 202 异步重放）必须严格按订单业务 status 区分成功、抢号中、失败、补偿中或已取消，
 *    绝不把 202 + status='failed' 误报为“取号成功”。
 */

import type { OrderStatus, SmsOrder } from "../../api/types";

export interface PendingSubmitAttempt {
  signature: string;
  key: string;
  lastOrderId?: string;
  lastStatus?: number;
  lastErrorCode?: string;
  lastErrorMessage?: string;
  outcomeUncertain: boolean;
}

export interface SubmitFailureContext {
  code?: string;
  message?: string;
  orderId?: string;
}

export interface OrderSubmitFeedback {
  tone: "success" | "info" | "warn" | "error";
  message: string;
  isTerminalFailure: boolean;
}

const TERMINAL_ORDER_STATUSES = new Set<OrderStatus>(["completed", "cancelled", "expired", "failed"]);

export function isTerminalOrderStatus(status: string): boolean {
  return TERMINAL_ORDER_STATUSES.has(status as OrderStatus);
}

export function describeOrderSubmitOutcome(
  order: Pick<SmsOrder, "order_id" | "status" | "phone_e164" | "phone_raw" | "error_code" | "error_message">,
): OrderSubmitFeedback {
  switch (order.status) {
    case "acquiring":
      return {
        tone: "info",
        message: "已开启后台抢号任务，拿到号码将实时展示",
        isTerminalFailure: false,
      };
    case "waiting_code":
    case "code_received":
    case "waiting_next_code":
    case "completing":
    case "completed":
      return {
        tone: "success",
        message: `取号成功：${order.phone_e164 || order.phone_raw || order.order_id}`,
        isTerminalFailure: false,
      };
    case "compensation_pending":
    case "cancelling":
      return {
        tone: "warn",
        message: `订单 ${order.order_id} 正在退款补偿中：${order.error_message || order.error_code || "请稍候查看状态"}`,
        isTerminalFailure: false,
      };
    case "failed":
      return {
        tone: "error",
        message: `取号失败 (${order.order_id})：${order.error_message || order.error_code || "上游未分配号码"}`,
        isTerminalFailure: true,
      };
    case "cancelled":
    case "expired":
      return {
        tone: "warn",
        message: `订单 ${order.order_id} 已结束 (${order.status})：${order.error_message || "可重新发起新单"}`,
        isTerminalFailure: true,
      };
    default:
      return {
        tone: "info",
        message: `订单 ${order.order_id} 当前状态：${order.status}`,
        isTerminalFailure: false,
      };
  }
}

function defaultKeyGenerator(): string {
  const rand =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID().replace(/-/g, "").slice(0, 12)
      : Math.random().toString(36).slice(2, 14);
  return `wb_${Date.now().toString(36)}_${rand}`;
}

export class WorkbenchIdempotencyController {
  private pending: PendingSubmitAttempt | null = null;
  private readonly genKey: () => string;

  constructor(keyGenerator: () => string = defaultKeyGenerator) {
    this.genKey = keyGenerator;
  }

  beginAttempt(payload: unknown): string {
    const signature = JSON.stringify(payload);
    if (this.pending && this.pending.signature === signature) {
      return this.pending.key;
    }
    const key = this.genKey();
    this.pending = { signature, key, outcomeUncertain: false };
    return key;
  }

  onSuccess(): void {
    this.pending = null;
  }

  onFailure(status?: number, context?: SubmitFailureContext): void {
    // 4xx（如 409 无号、402 余额不足、422 参数错误）属于确定性业务拒绝，清空以便下次独立发起新单；
    // 网络断开 (undefined / 0) 或 5xx 保留未决键及服务端返回的 orderId，便于先查原单或安全重放，
    // 待确认旧单终结或操作者主动点击“确认新建订单”后通过 resetForNewOrder() 换新键。
    if (status !== undefined && status >= 400 && status < 500) {
      this.pending = null;
      return;
    }
    if (this.pending) {
      this.pending = {
        ...this.pending,
        lastStatus: status,
        lastOrderId: context?.orderId || this.pending.lastOrderId,
        lastErrorCode: context?.code || this.pending.lastErrorCode,
        lastErrorMessage: context?.message || this.pending.lastErrorMessage,
        outcomeUncertain: true,
      };
    }
  }

  /** 当查询原订单确认已进入终态（failed / cancelled / expired / completed）时，仅当仍匹配发起查询时的未决尝试才释放旧键（修复 v7 复核 P1-06）。 */
  confirmOrderChecked(
    orderStatus: string,
    expected?: { key?: string; orderId?: string },
  ): boolean {
    if (!this.pending) {
      return false;
    }
    if (expected?.key && this.pending.key !== expected.key) {
      return false;
    }
    if (expected?.orderId && this.pending.lastOrderId !== expected.orderId) {
      return false;
    }
    if (isTerminalOrderStatus(orderStatus)) {
      this.pending = null;
      return true;
    }
    return false;
  }

  /** 操作者明确要求以相同参数发起一笔全新独立订单时手动换新幂等键。 */
  resetForNewOrder(): void {
    this.pending = null;
  }

  get currentAttempt(): PendingSubmitAttempt | null {
    return this.pending ? { ...this.pending } : null;
  }

  get currentKey(): string | null {
    return this.pending ? this.pending.key : null;
  }
}
