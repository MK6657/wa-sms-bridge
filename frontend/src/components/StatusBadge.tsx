import type { OrderStatus } from "../api/types";
import { resolveOrderStatusMeta } from "../utils/format";

export function StatusBadge({
  status,
  refundState,
}: {
  status: OrderStatus;
  refundState?: string;
}) {
  const meta = resolveOrderStatusMeta(status, refundState);
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 py-0.5 text-xs font-medium ${meta.badge}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${meta.dot}`} />
      {meta.label}
    </span>
  );
}
