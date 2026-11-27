"""轻量后台维护协程：合并补偿重试与过期验证码自动脱敏清理。"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.config import env
from app.core.order_service import OrderService
from app.db import Database

logger = logging.getLogger(__name__)


class RetentionService:
    """低资源占用的周期维护循环（默认每 60s 扫补偿、每 5 分钟清理过期验证码）。"""

    def __init__(
        self,
        db: Database,
        order_service: OrderService,
        *,
        reconcile_interval: float = 60.0,
        scrub_every_n_ticks: int = 5,
    ):
        self._db = db
        self._order_service = order_service
        self._interval = max(1.0, float(reconcile_interval))
        self._scrub_every = max(1, int(scrub_every_n_ticks))
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())

    async def shutdown(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def get_retention_hours(self) -> int:
        raw = await self._db.config.get("sms_code_retention_hours", "")
        if raw != "":
            try:
                return int(raw)
            except ValueError:
                pass
        return int(env.sms_code_retention_hours)

    async def run_scrub_once(self, *, retention_hours: Optional[int] = None) -> int:
        hours = await self.get_retention_hours() if retention_hours is None else int(retention_hours)
        scrubbed_versions, non_terminal_ids = await self._db.orders.scrub_expired_codes_detailed(
            hours,
            on_scrub_committed=lambda vers: self._order_service.on_orders_scrubbed(
                list(vers.keys()),
                scrub_versions=vers,
            ),
        )
        if scrubbed_versions:
            for oid in non_terminal_ids:
                ver = scrubbed_versions.get(oid)
                if ver is None:
                    continue
                try:
                    refreshed = await self._db.orders.get(oid, include_messages=True, include_activations=False)
                    if refreshed is not None and refreshed.get("status") not in {"completed", "cancelled", "expired", "failed"}:
                        self._order_service._emit("order.status_changed", refreshed, code_scrub_version=ver)
                except Exception:
                    pass
        return len(scrubbed_versions)

    async def _loop(self) -> None:
        tick = 0
        try:
            while True:
                tick += 1
                try:
                    reconciled = await self._order_service.reconcile_pending()
                    if reconciled:
                        logger.info("后台订单补偿已结清 %d 笔", reconciled)
                except Exception:
                    logger.warning("后台订单补偿扫描异常", exc_info=True)

                if (tick - 1) % self._scrub_every == 0:
                    try:
                        scrubbed = await self.run_scrub_once()
                        if scrubbed:
                            logger.info("验证码留存策略已自动脱敏 %d 条历史订单", scrubbed)
                    except Exception:
                        logger.warning("验证码自动脱敏任务异常", exc_info=True)

                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            return
