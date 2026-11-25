/**
 * SSE 断线续传游标跟踪器（修复复核 P1-05）。
 *
 * 独立为纯函数/轻量状态模块以便前端自动化测试直接验证：
 * - 记录每个业务事件的事件 ID（优先 MessageEvent.lastEventId，回退 payload.id）
 * - 自动重连时显式通过 ?last_event_id=<cursor> 传给服务端补发断线期间遗漏事件
 * - 收到 resync_required 时通知上层刷新快照
 */

export function buildStreamUrl(lastEventId: string, basePath = "/api/v1/events/stream"): string {
  const clean = (lastEventId || "").trim();
  if (!clean) return basePath;
  const sep = basePath.includes("?") ? "&" : "?";
  return `${basePath}${sep}last_event_id=${encodeURIComponent(clean)}`;
}

export function extractEventCursor(rawLastEventId?: string | null, parsedId?: string | null): string {
  const fromFrame = (rawLastEventId || "").trim();
  if (fromFrame) return fromFrame;
  return (parsedId || "").trim();
}

export class SseReconnectTracker {
  private lastEventId = "";
  private hasConnectedBefore = false;
  private backoffMs: number;
  private readonly initialBackoffMs: number;
  private readonly maxBackoffMs: number;

  constructor(initialBackoffMs = 1500, maxBackoffMs = 15000) {
    this.initialBackoffMs = initialBackoffMs;
    this.maxBackoffMs = maxBackoffMs;
    this.backoffMs = initialBackoffMs;
  }

  get cursor(): string {
    return this.lastEventId;
  }

  nextUrl(basePath = "/api/v1/events/stream"): string {
    return buildStreamUrl(this.lastEventId, basePath);
  }

  recordEvent(rawLastEventId?: string | null, parsedId?: string | null): string {
    const id = extractEventCursor(rawLastEventId, parsedId);
    if (id) {
      this.lastEventId = id;
    }
    return this.lastEventId;
  }

  markConnected(): { isReconnect: boolean } {
    const isReconnect = this.hasConnectedBefore;
    this.hasConnectedBefore = true;
    this.backoffMs = this.initialBackoffMs;
    return { isReconnect };
  }

  nextBackoffDelayMs(): number {
    const current = this.backoffMs;
    this.backoffMs = Math.min(Math.round(this.backoffMs * 1.6), this.maxBackoffMs);
    return current;
  }

  resetCursor(): void {
    this.lastEventId = "";
  }
}
