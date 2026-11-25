"""SSE（Server-Sent Events）实时订单与验证码事件流（支持游标回放、过期重同步与密钥撤销即时断连）。"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse

from app.core.auth_service import AuthService, CallerContext
from app.core.event_bus import EventBus
from app.dependencies import get_auth_service, get_caller, get_event_bus

router = APIRouter(prefix="/api/v1/events", tags=["events"])


@router.get("/stream")
async def stream_events(
    request: Request,
    order_id: str = Query(default="", description="仅监听指定订单事件"),
    project_id: str = Query(default="", description="仅监听指定项目事件（仅管理员可指定其他项目）"),
    last_event_id_query: str = Query(default="", alias="last_event_id", description="可选断线续传游标"),
    last_event_id_header: Optional[str] = Header(default=None, alias="Last-Event-ID"),
    caller: CallerContext = Depends(get_caller),
    auth_service: AuthService = Depends(get_auth_service),
    event_bus: EventBus = Depends(get_event_bus),
):
    AuthService.ensure_scope(caller, "events:r")
    effective_project = project_id if caller.is_admin else caller.project_id
    cred_project = "" if caller.is_admin else caller.project_id
    cursor = (last_event_id_header or last_event_id_query or "").strip()

    async def _still_authorized() -> bool:
        return await auth_service.verify_stream_request(request, caller, required_scope="events:r")

    async def generator() -> AsyncIterator[str]:
        async with event_bus.subscribe_with_control(
            order_id=order_id,
            project_id=effective_project,
            credential_project_id=cred_project,
        ) as (queue, revoke_event):
            if not await _still_authorized():
                return

            missed_events, cursor_found = event_bus.replay_since_with_status(
                cursor,
                order_id=order_id,
                project_id=effective_project,
            )
            resync_required = bool(cursor and not cursor_found)
            connected_data = {
                "ok": True,
                "project_id": effective_project or "all",
                "cursor_found": cursor_found,
                "resync_required": resync_required,
                "replayed_count": len(missed_events),
            }
            yield f"event: connected\ndata: {json.dumps(connected_data, ensure_ascii=False)}\n\n"

            if resync_required:
                resync_payload = json.dumps(
                    {"reason": "cursor_expired_or_restarted", "cursor": cursor},
                    ensure_ascii=False,
                )
                yield f"event: resync_required\ndata: {resync_payload}\n\n"

            # 断线重连回放阶段：每条推送前复核凭据有效性（修复补充 P1-03：密钥撤销同步停止历史回放）
            seen_ids: set[str] = set()
            for missed in missed_events:
                if revoke_event.is_set() or not await _still_authorized():
                    return
                seen_ids.add(missed.id)
                payload = json.dumps(missed.to_payload(), ensure_ascii=False)
                yield f"id: {missed.id}\nevent: {missed.event}\ndata: {payload}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                if revoke_event.is_set():
                    revoke_event.clear()
                    if not await _still_authorized():
                        break

                get_task = asyncio.create_task(queue.get())
                revoke_task = asyncio.create_task(revoke_event.wait())
                try:
                    done, pending = await asyncio.wait(
                        {get_task, revoke_task},
                        timeout=20.0,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for p in pending:
                        p.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)

                    if revoke_task in done:
                        revoke_event.clear()
                        if not await _still_authorized():
                            break
                        if get_task not in done:
                            continue

                    if get_task in done:
                        evt = get_task.result()
                        if not await _still_authorized():
                            break
                        if evt.id in seen_ids:
                            continue
                        payload = json.dumps(evt.to_payload(), ensure_ascii=False)
                        yield f"id: {evt.id}\nevent: {evt.event}\ndata: {payload}\n\n"
                    else:
                        # Keepalive 周期复核数据库凭据（支持跨进程撤销检测）
                        if not await _still_authorized():
                            break
                        yield ": keepalive\n\n"
                except asyncio.CancelledError:
                    for t in (get_task, revoke_task):
                        if not t.done():
                            t.cancel()
                    raise

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
