"""进程内轻量事件总线（支持有界历史环、SSE 游标状态反馈与版本门控全视图实时脱敏失效）。

修复 P1-08、复核 P0-02 / P1-05、v3 复核 P0-02 与 v6 复核 P0-01：
- 历史环、慢订阅者队列、已提取回放列表以及延迟发布的收码事件共享同一脱敏版本门控 (`code_scrub_version`)；
- 调用 scrub_order_codes(order_ids, scrub_versions=...) 时原子抬高订单的脱敏版本门槛与事件序号截点，
  并立即原地擦除所有仍存活 BusEvent 中的明文验证码；
- 任何携带旧 `code_scrub_version < current_scrub_version` 的延迟发布事件，即使在脱敏之后才到达
  `publish()`，也会在入环/入队前即被彻底抹除明文验证码（含 `latest_code`、`messages` 与 `message`），
  而脱敏后新收到的短信（携带最新 `code_scrub_version >= current_scrub_version`）仍可正常推送。
"""

from __future__ import annotations

import asyncio
import copy
import weakref
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Iterable, Optional
from uuid import uuid4


def _scrub_event_payload_in_place(data: dict[str, Any]) -> None:
    """原地擦除事件数据字典中的所有明文验证码副本，保留订单非敏感字段。"""
    if not isinstance(data, dict):
        return
    if "latest_code" in data:
        data["latest_code"] = ""
    if "resend_base_code" in data:
        data["resend_base_code"] = ""
    if "resend_base_digest" in data:
        data["resend_base_digest"] = ""
    if "code_scrubbed" in data or "latest_code" in data or "messages" in data or "message" in data:
        data["code_scrubbed"] = True
    messages = data.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if isinstance(msg, dict):
                if "code" in msg:
                    msg["code"] = ""
                if "raw_text" in msg:
                    msg["raw_text"] = ""
    extra_msg = data.get("message")
    if isinstance(extra_msg, dict):
        if "code" in extra_msg:
            extra_msg["code"] = ""
        if "raw_text" in extra_msg:
            extra_msg["raw_text"] = ""


def _scrub_event_payload(data: dict[str, Any]) -> dict[str, Any]:
    cleaned = copy.deepcopy(data)
    _scrub_event_payload_in_place(cleaned)
    return cleaned


@dataclass(frozen=True)
class BusEvent:
    id: str
    seq: int
    event: str
    order_id: str
    project_id: str
    timestamp: str
    data: dict[str, Any]
    code_scrub_version: Optional[int] = None
    _is_scrubbed: Optional[Callable[[str, int, Optional[int]], bool]] = field(
        default=None, repr=False, compare=False
    )

    def to_payload(self) -> dict[str, Any]:
        payload_data = (
            _scrub_event_payload(self.data)
            if (
                self._is_scrubbed is not None
                and self._is_scrubbed(self.order_id, self.seq, self.code_scrub_version)
            )
            else self.data
        )
        return {
            "id": self.id,
            "event": self.event,
            "order_id": self.order_id,
            "project_id": self.project_id,
            "timestamp": self.timestamp,
            "data": payload_data,
        }


@dataclass
class _Subscriber:
    queue: asyncio.Queue[BusEvent]
    revoke_event: asyncio.Event = field(default_factory=asyncio.Event)
    order_id: str = ""
    project_id: str = ""
    credential_project_id: str = ""
    overflowed: bool = False


class EventBus:
    """无外部依赖的 asyncio 广播总线 + 500 条有界历史环供 SSE 断线补发。"""

    def __init__(self, *, max_queue_size: int = 128, history_size: int = 500):
        self._max_queue_size = max_queue_size
        self._history: deque[BusEvent] = deque(maxlen=max(10, history_size))
        self._sub_map: dict[int, _Subscriber] = {}
        self._live_refs: dict[str, list[weakref.ref[BusEvent]]] = {}
        self._scrubbed_up_to_seq: dict[str, int] = {}
        self._scrub_versions: dict[str, int] = {}
        self._next_sub_id = 1
        self._next_seq = 1
        self._closed = False

    @property
    def is_closed(self) -> bool:
        return self._closed

    def close_all(self) -> None:
        """优雅停机时唤醒所有 SSE 订阅协程立即退出，不卡死 Uvicorn 关停。"""
        self._closed = True
        for sub in list(self._sub_map.values()):
            sub.revoke_event.set()

    def consume_overflow(self, queue: asyncio.Queue[BusEvent]) -> bool:
        """检查并重置指定订阅队列的溢出丢包标记。"""
        for sub in self._sub_map.values():
            if sub.queue is queue:
                if sub.overflowed:
                    sub.overflowed = False
                    return True
                return False
        return False

    def _check_scrubbed(self, order_id: str, seq: int, code_scrub_version: Optional[int] = None) -> bool:
        if not order_id:
            return False
        if seq <= self._scrubbed_up_to_seq.get(order_id, 0):
            return True
        if code_scrub_version is not None:
            min_ver = self._scrub_versions.get(order_id, 0)
            if int(code_scrub_version) < min_ver:
                return True
        return False

    def publish(
        self,
        event: str,
        data: dict[str, Any],
        *,
        order_id: str = "",
        project_id: str = "",
        code_scrub_version: Optional[int] = None,
    ) -> BusEvent:
        seq = self._next_seq
        self._next_seq += 1
        snapshot = copy.deepcopy(data) if isinstance(data, dict) else {}
        resolved_oid = order_id or str(snapshot.get("order_id") or "")

        eff_ver = code_scrub_version
        if eff_ver is None and isinstance(snapshot, dict) and snapshot.get("code_scrub_version") is not None:
            try:
                eff_ver = int(snapshot["code_scrub_version"])
            except (TypeError, ValueError):
                eff_ver = None

        # v6 复核 P0-01: 只要事件携带旧脱敏版本，或订单快照已标 code_scrubbed，发布前立即彻底擦除全部明文副本！
        if resolved_oid and (
            snapshot.get("code_scrubbed") is True
            or (eff_ver is not None and eff_ver < self._scrub_versions.get(resolved_oid, 0))
        ):
            _scrub_event_payload_in_place(snapshot)

        item = BusEvent(
            id=f"evt_{seq}_{uuid4().hex[:8]}",
            seq=seq,
            event=event,
            order_id=resolved_oid,
            project_id=project_id or str(snapshot.get("project_id") or ""),
            timestamp=datetime.now(timezone.utc).isoformat(),
            data=snapshot,
            code_scrub_version=eff_ver,
            _is_scrubbed=self._check_scrubbed,
        )
        if resolved_oid:
            bucket = self._live_refs.setdefault(resolved_oid, [])
            bucket.append(weakref.ref(item))
            if len(bucket) > 32:
                self._live_refs[resolved_oid] = [r for r in bucket if r() is not None]

        self._history.append(item)
        for sub in list(self._sub_map.values()):
            if sub.order_id and sub.order_id != item.order_id:
                continue
            if sub.project_id and sub.project_id != item.project_id:
                continue
            if sub.queue.full():
                try:
                    sub.queue.get_nowait()
                    sub.overflowed = True
                except asyncio.QueueEmpty:
                    pass
            try:
                sub.queue.put_nowait(item)
            except asyncio.QueueFull:
                sub.overflowed = True
        return item

    def scrub_order_codes(
        self,
        order_ids: Iterable[str],
        *,
        scrub_versions: Optional[dict[str, int]] = None,
    ) -> int:
        """脱敏清理订单验证码时，同步抬高版本门控并擦除历史环、慢订阅者队列及进行中回放中的明文验证码。"""
        target_ids = {str(oid) for oid in order_ids if oid}
        if scrub_versions:
            target_ids.update(str(oid) for oid in scrub_versions if oid)
        if not target_ids:
            return 0
        current_max_seq = self._next_seq - 1
        scrubbed = 0
        for oid in target_ids:
            prev_seq = self._scrubbed_up_to_seq.get(oid, 0)
            if current_max_seq > prev_seq:
                self._scrubbed_up_to_seq[oid] = current_max_seq
            prev_ver = self._scrub_versions.get(oid, 0)
            if scrub_versions and oid in scrub_versions:
                self._scrub_versions[oid] = max(prev_ver, int(scrub_versions[oid]))
            else:
                self._scrub_versions[oid] = prev_ver + 1
            if len(self._scrubbed_up_to_seq) > 4096:
                self._scrubbed_up_to_seq.pop(next(iter(self._scrubbed_up_to_seq)), None)
            if len(self._scrub_versions) > 4096:
                self._scrub_versions.pop(next(iter(self._scrub_versions)), None)

            refs = self._live_refs.pop(oid, [])
            for ref in refs:
                evt = ref()
                if evt is not None:
                    _scrub_event_payload_in_place(evt.data)
                    scrubbed += 1
        # 兜底遍历历史环与订阅队列
        for item in self._history:
            if item.order_id in target_ids:
                _scrub_event_payload_in_place(item.data)
        for sub in list(self._sub_map.values()):
            queued_items = list(getattr(sub.queue, "_queue", ()))
            for item in queued_items:
                if item.order_id in target_ids:
                    _scrub_event_payload_in_place(item.data)
        return scrubbed

    def replay_since_with_status(
        self,
        last_event_id: str,
        *,
        order_id: str = "",
        project_id: str = "",
    ) -> tuple[list[BusEvent], bool]:
        """根据 SSE Last-Event-ID 补发断线期间遗漏的事件，并显式返回游标是否仍在有效窗口内。"""
        if not last_event_id:
            return [], True
        found = False
        replayed: list[BusEvent] = []
        for item in self._history:
            if not found:
                if item.id == last_event_id:
                    found = True
                continue
            if order_id and item.order_id != order_id:
                continue
            if project_id and item.project_id != project_id:
                continue
            replayed.append(item)
        return replayed, found

    def replay_since(
        self,
        last_event_id: str,
        *,
        order_id: str = "",
        project_id: str = "",
    ) -> list[BusEvent]:
        """兼容接口：返回自 last_event_id 之后的遗漏事件列表。"""
        replayed, _ = self.replay_since_with_status(
            last_event_id,
            order_id=order_id,
            project_id=project_id,
        )
        return replayed

    def revoke_project_subscribers(self, project_id: str) -> int:
        """项目密钥撤销、停用、轮转或权限变更时立即唤醒并终止该项目的全部活跃 SSE 订阅（修复补充 P1-03）。"""
        pid = (project_id or "").strip()
        if not pid:
            return 0
        count = 0
        for sub in list(self._sub_map.values()):
            if sub.credential_project_id == pid:
                sub.revoke_event.set()
                count += 1
        return count

    @asynccontextmanager
    async def subscribe_with_control(
        self,
        *,
        order_id: str = "",
        project_id: str = "",
        credential_project_id: str = "",
    ) -> AsyncIterator[tuple[asyncio.Queue[BusEvent], asyncio.Event]]:
        sub_id = self._next_sub_id
        self._next_sub_id += 1
        queue: asyncio.Queue[BusEvent] = asyncio.Queue(maxsize=self._max_queue_size)
        revoke_event = asyncio.Event()
        self._sub_map[sub_id] = _Subscriber(
            queue=queue,
            revoke_event=revoke_event,
            order_id=order_id,
            project_id=project_id,
            credential_project_id=credential_project_id,
        )
        try:
            yield queue, revoke_event
        finally:
            self._sub_map.pop(sub_id, None)

    @asynccontextmanager
    async def subscribe(
        self,
        *,
        order_id: str = "",
        project_id: str = "",
    ) -> AsyncIterator[asyncio.Queue[BusEvent]]:
        async with self.subscribe_with_control(order_id=order_id, project_id=project_id) as (queue, _revoke):
            yield queue

    @property
    def subscriber_count(self) -> int:
        return len(self._sub_map)
