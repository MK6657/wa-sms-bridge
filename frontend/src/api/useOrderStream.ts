import { useEffect, useRef, useState } from "react";
import { SseReconnectTracker } from "./sseCursor";
import type { SmsOrder } from "./types";

export interface StreamEventMessage {
  id: string;
  event: string;
  order_id: string;
  project_id: string;
  timestamp: string;
  data: SmsOrder;
}

const STREAM_EVENTS = [
  "order.created",
  "order.acquired",
  "order.code_received",
  "order.status_changed",
  "order.completed",
  "order.cancelled",
  "order.expired",
  "order.failed",
] as const;

export function useOrderStream(
  onEvent: (evt: StreamEventMessage) => void,
  options: {
    enabled: boolean;
    onReconnect?: () => void;
    onResyncRequired?: (reason: string) => void;
    onAuthCheck?: () => void;
  },
) {
  const [connected, setConnected] = useState(false);
  const handlerRef = useRef(onEvent);
  const reconnectRef = useRef(options.onReconnect);
  const resyncRef = useRef(options.onResyncRequired);
  const authCheckRef = useRef(options.onAuthCheck);
  handlerRef.current = onEvent;
  reconnectRef.current = options.onReconnect;
  resyncRef.current = options.onResyncRequired;
  authCheckRef.current = options.onAuthCheck;

  useEffect(() => {
    if (!options.enabled) {
      setConnected(false);
      return;
    }

    let closed = false;
    let source: EventSource | null = null;
    let retryTimer: number | null = null;
    const tracker = new SseReconnectTracker(1500, 15000);

    const connect = () => {
      if (closed) return;
      // 复核 P1-05: 自动重连时显式附带最后收到的事件游标 (?last_event_id=...)，
      // 与服务端 HttpOnly Cookie 认证及 Last-Event-ID 补发闭环衔接
      const url = tracker.nextUrl("/api/v1/events/stream");
      source = new EventSource(url);

      source.addEventListener("connected", (raw) => {
        setConnected(true);
        tracker.markConnected();
        let resyncRequired = false;
        try {
          const info = JSON.parse((raw as MessageEvent).data || "{}") as { resync_required?: boolean };
          resyncRequired = Boolean(info.resync_required);
        } catch {
          // ignore
        }
        if (resyncRequired) {
          // 服务端紧接着会推送独立 resync_required 事件，此处仅重置游标避免重复拉取两次快照
          tracker.resetCursor();
        } else {
          // 首次建连与断线重连均补拉一次快照，消除初始 HTTP 加载与 EventSource 订阅之间的事件时间窗空隙
          reconnectRef.current?.();
        }
      });

      source.addEventListener("resync_required", (raw) => {
        tracker.resetCursor();
        let reason = "cursor_expired";
        try {
          const parsed = JSON.parse((raw as MessageEvent).data || "{}") as { reason?: string };
          if (parsed.reason) reason = parsed.reason;
        } catch {
          // ignore
        }
        resyncRef.current?.(reason);
      });

      for (const evtName of STREAM_EVENTS) {
        source.addEventListener(evtName, (raw) => {
          try {
            const msgEvt = raw as MessageEvent;
            const parsed = JSON.parse(msgEvt.data) as StreamEventMessage;
            tracker.recordEvent(msgEvt.lastEventId, parsed.id);
            handlerRef.current(parsed);
          } catch {
            // ignore malformed frames
          }
        });
      }

      source.onerror = () => {
        setConnected(false);
        source?.close();
        source = null;
        authCheckRef.current?.();
        if (!closed) {
          const delay = tracker.nextBackoffDelayMs();
          retryTimer = window.setTimeout(() => {
            connect();
          }, delay);
        }
      };
    };

    connect();
    return () => {
      closed = true;
      if (retryTimer !== null) window.clearTimeout(retryTimer);
      source?.close();
    };
  }, [options.enabled]);

  return { connected };
}
