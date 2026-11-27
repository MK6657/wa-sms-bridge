import type { AuthStatusResponse, SmsOrder } from "./types";

export interface TrackedOrderVersion {
  version: number;
  scrubVersion: number;
  scrubbed: boolean;
}

const ACTIVE_ORDER_STATUS_SET = new Set<string>([
  "acquiring",
  "waiting_code",
  "code_received",
  "waiting_next_code",
  "completing",
  "cancelling",
  "compensation_pending",
]);

/**
 * 判断并更新单笔订单的单调状态水位 (version + code_scrub_version + code_scrubbed)。
 * 严格阻止旧版本号快照或同版本号但未包含最新脱敏标记的旧快照把已擦除验证码重新写回前端状态。
 */
export function recordOrderState(
  order: SmsOrder,
  map: Map<string, TrackedOrderVersion>,
): boolean {
  const incomingVer = typeof order.version === "number" ? order.version : 0;
  const incomingScrubVer =
    typeof order.code_scrub_version === "number" ? order.code_scrub_version : 0;
  const incomingScrubbed = Boolean(order.code_scrubbed);

  const known = map.get(order.order_id);
  if (known) {
    if (incomingVer > 0 && known.version > 0 && incomingVer < known.version) {
      return false;
    }
    if (incomingScrubVer < known.scrubVersion) {
      return false;
    }
    if (
      known.scrubbed &&
      !incomingScrubbed &&
      incomingScrubVer <= known.scrubVersion &&
      Boolean(order.latest_code)
    ) {
      return false;
    }
  }

  if (map.size >= 1000 && !map.has(order.order_id)) {
    const firstKey = map.keys().next().value;
    if (firstKey !== undefined) {
      map.delete(firstKey);
    }
  }

  const nextVer = Math.max(known?.version ?? 0, incomingVer);
  const nextScrubVer = Math.max(known?.scrubVersion ?? 0, incomingScrubVer);
  const nextScrubbed =
    incomingScrubVer > (known?.scrubVersion ?? 0)
      ? incomingScrubbed
      : incomingVer > (known?.version ?? 0)
        ? incomingScrubbed
        : Boolean(known?.scrubbed || incomingScrubbed);

  map.set(order.order_id, {
    version: nextVer,
    scrubVersion: nextScrubVer,
    scrubbed: nextScrubbed,
  });
  return true;
}

/**
 * 合并后台活跃订单列表快照与期间并发产生的本地新建/SSE 更新订单，杜绝在途快照覆盖刚创建的活跃单。
 */
export function mergeActiveOrderSnapshot(
  prevOrders: SmsOrder[],
  snapshotItems: SmsOrder[],
  snapshotStartMutationSeq: number,
  mutatedSeqById: ReadonlyMap<string, number>,
  versionMap: Map<string, TrackedOrderVersion>,
): SmsOrder[] {
  const prevById = new Map(prevOrders.map((o) => [o.order_id, o]));
  const seenIds = new Set<string>();
  const mergedFromSnapshot: SmsOrder[] = [];

  for (const item of snapshotItems) {
    seenIds.add(item.order_id);
    if (!recordOrderState(item, versionMap)) {
      const newerLocal = prevById.get(item.order_id);
      if (newerLocal && ACTIVE_ORDER_STATUS_SET.has(newerLocal.status)) {
        mergedFromSnapshot.push(newerLocal);
      }
      continue;
    }
    const localSeq = mutatedSeqById.get(item.order_id) ?? 0;
    const localItem = prevById.get(item.order_id);
    if (
      localSeq > snapshotStartMutationSeq &&
      localItem &&
      (localItem.version ?? 0) > (item.version ?? 0)
    ) {
      if (ACTIVE_ORDER_STATUS_SET.has(localItem.status)) {
        mergedFromSnapshot.push(localItem);
      }
      continue;
    }
    if (ACTIVE_ORDER_STATUS_SET.has(item.status)) {
      mergedFromSnapshot.push(item);
    }
  }

  // 保留快照请求发出之后才新建或通过 SSE 推入的活跃订单
  const preservedRecent: SmsOrder[] = [];
  for (const prev of prevOrders) {
    if (seenIds.has(prev.order_id)) continue;
    const mutatedSeq = mutatedSeqById.get(prev.order_id) ?? 0;
    if (mutatedSeq > snapshotStartMutationSeq && ACTIVE_ORDER_STATUS_SET.has(prev.status)) {
      preservedRecent.push(prev);
    }
  }

  return [...preservedRecent, ...mergedFromSnapshot];
}

/**
 * 判断静默会话检查结果是否应当将当前已登录管理员置为未登录：
 * 仅在服务端明确返回未授权 (200 且非管理员 / 401 / 403) 时登出；网络闪断或 5xx 保持当前会话与页面状态！
 */
export function shouldLogoutOnSilentAuthCheck(
  outcome:
    | { ok: true; status: AuthStatusResponse }
    | { ok: false; httpStatus?: number },
): boolean {
  if (outcome.ok) {
    const s = outcome.status;
    if (s.is_admin && (s.authenticated || s.can_bootstrap_local_session)) {
      return false;
    }
    return true;
  }
  return outcome.httpStatus === 401 || outcome.httpStatus === 403;
}
