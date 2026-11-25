"""每次上游取号激活的独立追踪与财务补偿仓储（修复 P1-03、复核 P1-01、v3/v5/v6 复核与补充 P1-01 / P1-03 / P1-04 / P1-05）。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from app.db.connection import DatabaseConnection


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_currency(currency: Optional[str], *, fallback: str = "") -> str:
    cleaned = (currency or "").strip().upper()
    return cleaned or fallback


class ActivationsRepo:
    def __init__(self, conn: DatabaseConnection):
        self._conn = conn

    async def record_intent(
        self,
        *,
        order_id: str,
        attempt_no: int,
        provider: str,
        country_id: str,
        service_code: str,
        quoted_max_price: Optional[float] = None,
        currency: str = "",
        currency_source: str = "",
    ) -> int:
        """取号前先落盘购买意图，防止进程崩溃或响应丢失后无法追踪。"""
        now = _utc_now_iso()
        norm_ccy = _normalize_currency(currency, fallback="")
        ccy_src = (currency_source or ("explicit" if norm_ccy else "")).strip()
        cur = await self._conn.write(
            "INSERT INTO order_activations ("
            "order_id, attempt_no, provider, country_id, service_code, state, quoted_max_price, "
            "currency, currency_source, currency_review_required, cost_status, "
            "refund_amount_status, refund_amount_source, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, 'purchasing', ?, ?, ?, 0, 'unknown', 'none', '', ?, ?)",
            (
                order_id,
                attempt_no,
                provider,
                str(country_id),
                service_code,
                quoted_max_price,
                norm_ccy,
                ccy_src,
                now,
                now,
            ),
        )
        return int(cur.lastrowid or 0)

    async def bind_acquired(
        self,
        row_id: int,
        *,
        activation_id: str,
        phone_raw: str,
        phone_e164: str = "",
        actual_cost: Optional[float] = None,
        estimated_cost: Optional[float] = None,
        cost_status: Optional[str] = None,
        currency: Optional[str] = None,
        currency_source: str = "",
        state: str = "active",
    ) -> None:
        resolved_status = cost_status or (
            "actual" if actual_cost is not None else ("estimated" if estimated_cost is not None else "unknown")
        )
        resolved_ccy = _normalize_currency(currency, fallback="UNKNOWN")
        resolved_ccy_src = (
            currency_source
            or ("provider_default" if resolved_ccy != "UNKNOWN" else "unknown")
        ).strip()
        await self._conn.write(
            "UPDATE order_activations SET activation_id=?, phone_raw=?, phone_e164=?, "
            "actual_cost=?, estimated_cost=?, cost_status=?, currency=?, currency_source=?, "
            "currency_review_required=0, state=?, updated_at=? WHERE id=?",
            (
                activation_id,
                phone_raw,
                phone_e164,
                actual_cost,
                estimated_cost,
                resolved_status,
                resolved_ccy,
                resolved_ccy_src,
                state,
                _utc_now_iso(),
                row_id,
            ),
        )

    async def bind_acquired_atomic(
        self,
        row_id: int,
        *,
        order_id: str,
        activation_id: str,
        phone_raw: str,
        phone_e164: str = "",
        actual_cost: Optional[float] = None,
        estimated_cost: Optional[float] = None,
        cost_status: Optional[str] = None,
        currency: Optional[str] = None,
        currency_source: str = "",
        state: str = "active",
    ) -> bool:
        """同事务原子绑定激活台账与订单的 activation_id 及财务成本口径（仅允许从 purchasing 状态转换）。"""
        now = _utc_now_iso()
        resolved_status = cost_status or (
            "actual" if actual_cost is not None else ("estimated" if estimated_cost is not None else "unknown")
        )
        resolved_ccy = _normalize_currency(currency, fallback="UNKNOWN")
        resolved_ccy_src = (
            currency_source
            or ("provider_default" if resolved_ccy != "UNKNOWN" else "unknown")
        ).strip()
        async with self._conn.transaction() as raw:
            cur = await raw.execute(
                "UPDATE order_activations SET activation_id=?, phone_raw=?, phone_e164=?, "
                "actual_cost=?, estimated_cost=?, cost_status=?, currency=?, currency_source=?, "
                "currency_review_required=0, state=?, updated_at=? WHERE id=? AND state='purchasing'",
                (
                    activation_id,
                    phone_raw,
                    phone_e164,
                    actual_cost,
                    estimated_cost,
                    resolved_status,
                    resolved_ccy,
                    resolved_ccy_src,
                    state,
                    now,
                    row_id,
                ),
            )
            if (cur.rowcount or 0) != 1:
                return False
            await raw.execute(
                "UPDATE sms_orders SET activation_id=?, phone_raw=?, cost=?, actual_cost=?, "
                "cost_status=?, currency=?, updated_at=? WHERE order_id=?",
                (
                    activation_id,
                    phone_raw,
                    actual_cost if actual_cost is not None else estimated_cost,
                    actual_cost,
                    resolved_status,
                    resolved_ccy,
                    now,
                    order_id,
                ),
            )
            return True

    async def update_state(
        self,
        row_id: int,
        *,
        state: str,
        last_error: str = "",
        phone_e164: Optional[str] = None,
        refund_amount: Optional[float] = None,
        refund_amount_source: str = "",
    ) -> bool:
        """更新激活状态；已落盘 refund_confirmed 绝不降级，且缺少证据时绝不自动推定全额退款（修复 v5 复核 P1-04）。"""
        guard = "" if state == "refund_confirmed" else " AND state != 'refund_confirmed'"
        sets = ["state=?", "last_error=?", "updated_at=?"]
        params: list[Any] = [state, last_error, _utc_now_iso()]
        if phone_e164 is not None:
            sets.append("phone_e164=?")
            params.append(phone_e164)
        if state in {"no_numbers", "failed"}:
            # 确认未购买的尝试保留 quoted_max_price 供诊断，绝不计入估算支出
            sets.append("estimated_cost=CASE WHEN COALESCE(activation_id, '') = '' AND actual_cost IS NULL THEN NULL ELSE estimated_cost END")
            sets.append("cost_status=CASE WHEN COALESCE(activation_id, '') = '' AND actual_cost IS NULL THEN 'none' ELSE cost_status END")
        if refund_amount is not None:
            sets.append("refund_amount=?")
            params.append(float(refund_amount))
            sets.append("refund_amount_status='verified'")
            sets.append("refund_amount_source=?")
            params.append((refund_amount_source or "explicit").strip())
        elif state == "refund_confirmed":
            # 仅在已有已核实金额时保留 verified，否则明确标为 unknown，绝不补填 actual_cost
            sets.append(
                "refund_amount_status=CASE WHEN refund_amount IS NOT NULL AND refund_amount_status='verified' "
                "THEN 'verified' ELSE 'unknown' END"
            )
        params.append(row_id)
        cur = await self._conn.write(
            f"UPDATE order_activations SET {', '.join(sets)} WHERE id=?{guard}",
            tuple(params),
        )
        return (cur.rowcount or 0) > 0

    async def update_by_activation_id(
        self,
        order_id: str,
        activation_id: str,
        *,
        state: str,
        last_error: str = "",
        refund_amount: Optional[float] = None,
        refund_amount_source: str = "",
    ) -> bool:
        """按 (order_id, activation_id) 更新状态；保护已确认退款证据不被降级覆盖且不推定未知退款额。"""
        if not activation_id:
            return False
        guard = "" if state == "refund_confirmed" else " AND state != 'refund_confirmed'"
        sets = ["state=?", "last_error=?", "updated_at=?"]
        params: list[Any] = [state, last_error, _utc_now_iso()]
        if refund_amount is not None:
            sets.append("refund_amount=?")
            params.append(float(refund_amount))
            sets.append("refund_amount_status='verified'")
            sets.append("refund_amount_source=?")
            params.append((refund_amount_source or "explicit").strip())
        elif state == "refund_confirmed":
            sets.append(
                "refund_amount_status=CASE WHEN refund_amount IS NOT NULL AND refund_amount_status='verified' "
                "THEN 'verified' ELSE 'unknown' END"
            )
        params.extend([order_id, activation_id])
        cur = await self._conn.write(
            f"UPDATE order_activations SET {', '.join(sets)} WHERE order_id=? AND activation_id=?{guard}",
            tuple(params),
        )
        return (cur.rowcount or 0) > 0

    @staticmethod
    async def update_state_in_tx(
        raw,
        row_id: int,
        *,
        state: str,
        now: Optional[str] = None,
        last_error: str = "",
        phone_e164: Optional[str] = None,
        refund_amount: Optional[float] = None,
        refund_amount_source: str = "",
        protect_settled: bool = False,
    ) -> bool:
        """在调用方已开启的写事务内更新激活状态；保护已确认退款与（可选）已人工/完成结清的记录。"""
        ts = now or _utc_now_iso()
        if protect_settled:
            guard = " AND state NOT IN ('refund_confirmed', 'completed', 'manual_resolved')"
        else:
            guard = "" if state == "refund_confirmed" else " AND state != 'refund_confirmed'"
        sets = ["state=?", "last_error=?", "updated_at=?"]
        params: list[Any] = [state, last_error, ts]
        if phone_e164 is not None:
            sets.append("phone_e164=?")
            params.append(phone_e164)
        if state in {"no_numbers", "failed"}:
            sets.append("estimated_cost=CASE WHEN COALESCE(activation_id, '') = '' AND actual_cost IS NULL THEN NULL ELSE estimated_cost END")
            sets.append("cost_status=CASE WHEN COALESCE(activation_id, '') = '' AND actual_cost IS NULL THEN 'none' ELSE cost_status END")
        if refund_amount is not None:
            sets.append("refund_amount=?")
            params.append(float(refund_amount))
            sets.append("refund_amount_status='verified'")
            sets.append("refund_amount_source=?")
            params.append((refund_amount_source or "explicit").strip())
        elif state == "refund_confirmed":
            sets.append(
                "refund_amount_status=CASE WHEN refund_amount IS NOT NULL AND refund_amount_status='verified' "
                "THEN 'verified' ELSE 'unknown' END"
            )
        params.append(row_id)
        cur = await raw.execute(
            f"UPDATE order_activations SET {', '.join(sets)} WHERE id=?{guard}",
            tuple(params),
        )
        return (cur.rowcount or 0) > 0

    @staticmethod
    async def ensure_activation_state_in_tx(
        raw,
        order_id: str,
        activation_id: str,
        *,
        provider: str,
        state: str,
        now: Optional[str] = None,
        last_error: str = "",
        country_id: str = "",
        service_code: str = "",
        currency: Optional[str] = None,
        refund_amount: Optional[float] = None,
        refund_amount_source: str = "",
        protect_settled: bool = False,
    ) -> None:
        """在调用方已开启的写事务内原子确保激活台账状态，杜绝父子记录跨事务分裂（修复 v7 复核 P1-01 & 并发审查 Finding 2）。"""
        if not activation_id:
            return
        ts = now or _utc_now_iso()
        ref_status = "verified" if refund_amount is not None else ("unknown" if state == "refund_confirmed" else "none")
        ref_src = (refund_amount_source or "explicit").strip() if refund_amount is not None else ""

        dup_cur = await raw.execute(
            "SELECT id FROM order_activations WHERE order_id=? AND activation_id=? ORDER BY id DESC LIMIT 1",
            (order_id, activation_id),
        )
        dup = await dup_cur.fetchone()
        if dup is not None:
            await ActivationsRepo.update_state_in_tx(
                raw,
                int(dup["id"]),
                state=state,
                now=ts,
                last_error=last_error,
                refund_amount=refund_amount,
                refund_amount_source=refund_amount_source,
                protect_settled=protect_settled,
            )
            return

        actual_cost: Optional[float] = None
        estimated_cost: Optional[float] = None
        cost_status = "unknown"
        parent_ccy = ""
        p_cur = await raw.execute(
            "SELECT activation_id, actual_cost, cost, cost_status, currency, phone_raw, phone_e164 "
            "FROM sms_orders WHERE order_id=?",
            (order_id,),
        )
        parent = await p_cur.fetchone()
        phone_raw = ""
        phone_e164 = ""
        if parent is not None and str(parent["activation_id"] or "").strip() == activation_id:
            p_status = str(parent["cost_status"] or "unknown")
            if p_status == "actual" and parent["actual_cost"] is not None:
                actual_cost = float(parent["actual_cost"])
                cost_status = "actual"
            elif p_status == "estimated" and parent["cost"] is not None:
                estimated_cost = float(parent["cost"])
                cost_status = "estimated"
            parent_ccy = _normalize_currency(str(parent["currency"] or ""), fallback="")
            phone_raw = str(parent["phone_raw"] or "")
            phone_e164 = str(parent["phone_e164"] or "")

        norm_explicit = _normalize_currency(currency, fallback="")
        resolved_ccy = norm_explicit or parent_ccy or "UNKNOWN"
        ccy_src = (
            "explicit"
            if norm_explicit and norm_explicit != "UNKNOWN"
            else ("migrated_from_order" if parent_ccy and parent_ccy != "UNKNOWN" else "unknown")
        )
        await raw.execute(
            "INSERT INTO order_activations ("
            "order_id, attempt_no, provider, country_id, service_code, activation_id, phone_raw, phone_e164, "
            "state, actual_cost, estimated_cost, currency, currency_source, currency_review_required, "
            "cost_status, refund_amount, refund_amount_status, refund_amount_source, "
            "last_error, created_at, updated_at"
            ") VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)",
            (
                order_id,
                provider,
                str(country_id),
                service_code,
                activation_id,
                phone_raw,
                phone_e164,
                state,
                actual_cost,
                estimated_cost,
                resolved_ccy,
                ccy_src,
                cost_status,
                float(refund_amount) if refund_amount is not None else None,
                ref_status,
                ref_src,
                last_error,
                ts,
                ts,
            ),
        )

    async def ensure_activation_state(
        self,
        order_id: str,
        activation_id: str,
        *,
        provider: str,
        state: str,
        last_error: str = "",
        country_id: str = "",
        service_code: str = "",
        currency: Optional[str] = None,
        refund_amount: Optional[float] = None,
        refund_amount_source: str = "",
        protect_settled: bool = False,
    ) -> None:
        """确保激活台账留存可审计状态：有则受保护更新，无则同事务补建并同步匹配父订单的可核实实付/估算成本（修复 v6 复核 P1-03）。"""
        if not activation_id:
            return
        async with self._conn.transaction() as raw:
            await self.ensure_activation_state_in_tx(
                raw,
                order_id,
                activation_id,
                provider=provider,
                state=state,
                last_error=last_error,
                country_id=country_id,
                service_code=service_code,
                currency=currency,
                refund_amount=refund_amount,
                refund_amount_source=refund_amount_source,
                protect_settled=protect_settled,
            )

    async def resolve_manually_atomic(
        self,
        row_id: int,
        *,
        state: str,
        note: str = "",
        refund_amount: Optional[float] = None,
        currency: Optional[str] = None,
        outbox_entry_builder=None,
        result_meta: Optional[dict[str, Any]] = None,
        scrub_if_terminal: bool = False,
        scrub_result: Optional[dict[str, int]] = None,
    ) -> tuple[Optional[dict[str, Any]], bool]:
        """在同一事务内受条件保护地人工核销激活台账并同步推进父订单终态与 Outbox（修复 v6 P1-03 & v7 P1-02 / P1-03）。

        返回 (row, applied)：
        - row 为 None：激活不存在；
        - applied 为 False：因原记录已为 refund_confirmed 拒绝降级覆盖（父订单同样未被改动）；
        - applied 为 True：激活与父订单已在同一事务一致提交。
        """
        from app.db.orders_repo import OrdersRepo
        from app.db.webhooks_repo import WebhooksRepo

        now = _utc_now_iso()
        guard = "" if state == "refund_confirmed" else " AND state != 'refund_confirmed'"
        sets = ["state=?", "last_error=?", "updated_at=?"]
        params: list[Any] = [state, note.strip() or f"管理员人工核销为 {state}", now]
        if refund_amount is not None:
            sets.extend(["refund_amount=?", "refund_amount_status='verified'", "refund_amount_source='manual_resolve'"])
            params.append(float(refund_amount))
        elif state == "refund_confirmed":
            sets.append(
                "refund_amount_status=CASE WHEN refund_amount IS NOT NULL AND refund_amount_status='verified' "
                "THEN 'verified' ELSE 'unknown' END"
            )
        # v7 复核 P1-03: 仅在明确提供有效且非 UNKNOWN 的币种时才清除 currency_review_required，普通结算核销绝不误消待核对标记！
        norm_ccy = _normalize_currency(currency, fallback="")
        if norm_ccy and norm_ccy != "UNKNOWN":
            sets.extend(["currency=?", "currency_source='manual_resolve'", "currency_review_required=0"])
            params.append(norm_ccy)

        async with self._conn.transaction() as raw:
            cur_get = await raw.execute("SELECT * FROM order_activations WHERE id=?", (row_id,))
            existing_raw = await cur_get.fetchone()
            if existing_raw is None:
                return None, False
            existing = dict(existing_raw)

            params.append(row_id)
            upd = await raw.execute(
                f"UPDATE order_activations SET {', '.join(sets)} WHERE id=?{guard}",
                tuple(params),
            )
            if (upd.rowcount or 0) != 1:
                return existing, False

            order_id = str(existing["order_id"])
            ord_cur = await raw.execute(
                "SELECT * FROM sms_orders WHERE order_id=?",
                (order_id,),
            )
            ord_raw = await ord_cur.fetchone()
            if ord_raw is not None:
                ord_row = dict(ord_raw)
                cur_status = str(ord_row.get("status") or "")
                cur_settlement = str(ord_row.get("settlement_state") or "")
                act_aid = str(existing.get("activation_id") or "").strip()
                ord_aid = str(ord_row.get("activation_id") or "").strip()
                # 当父订单已绑定具体 activation_id 时，核销早期无 activation_id 的 uncertain_purchase 绝不能提前终结父订单！
                matches_primary = (not ord_aid) if not act_aid else ((not ord_aid) or (ord_aid == act_aid))

                if matches_primary and norm_ccy and norm_ccy != "UNKNOWN":
                    cur_ord_ccy = _normalize_currency(str(ord_row.get("currency") or ""), fallback="UNKNOWN")
                    if cur_ord_ccy == "UNKNOWN" or (act_aid and ord_aid == act_aid):
                        await raw.execute(
                            "UPDATE sms_orders SET currency=?, updated_at=? WHERE order_id=?",
                            (norm_ccy, now, order_id),
                        )
                        ord_row["currency"] = norm_ccy

                sib_cur = await raw.execute(
                    "SELECT 1 FROM order_activations "
                    "WHERE order_id=? AND id!=? "
                    "AND state IN ('purchasing', 'uncertain_purchase', 'refund_pending', 'code_persist_failed') "
                    "LIMIT 1",
                    (order_id, row_id),
                )
                has_unresolved_siblings = (await sib_cur.fetchone()) is not None

                if matches_primary and (
                    cur_settlement in {"uncertain", "complete_uncertain", "complete_failed", "code_persist_failed"}
                    or cur_status in {"compensation_pending", "cancelling"}
                    or (
                        cur_status in {"code_received", "waiting_code", "waiting_next_code"}
                        and state in {"completed", "refund_confirmed", "unrefundable", "manual_resolved"}
                    )
                ):
                    new_status = cur_status
                    completed_at = str(ord_row.get("completed_at") or "")
                    if cur_status in {"compensation_pending", "cancelling", "code_received", "waiting_code", "waiting_next_code"}:
                        if state == "completed":
                            new_status = "completed"
                        elif state in {"refund_confirmed", "unrefundable"}:
                            new_status = "cancelled"
                        else:
                            new_status = "completed" if int(ord_row.get("code_count") or 0) > 0 else "cancelled"
                        if new_status in {"completed", "cancelled", "expired", "failed"} and not completed_at:
                            completed_at = now

                    raw_prev_refund = str(ord_row.get("refund_state") or "none")
                    fallback_manual_refund = (
                        ("none" if new_status == "completed" else "unrefundable")
                        if raw_prev_refund == "pending"
                        else raw_prev_refund
                    )
                    refund_map = {
                        "refund_confirmed": "confirmed",
                        "unrefundable": "unrefundable",
                        "completed": "none",
                        "manual_resolved": fallback_manual_refund,
                    }
                    new_refund = refund_map.get(state, "none")
                    next_settlement = (
                        cur_settlement
                        if (has_unresolved_siblings and cur_settlement in {"uncertain", "code_persist_failed"})
                        else "manual_resolved"
                    )

                    is_terminal_now = new_status in {"completed", "cancelled", "expired", "failed"}
                    do_scrub = bool(scrub_if_terminal and is_terminal_now)
                    if do_scrub:
                        await raw.execute(
                            "UPDATE sms_orders SET status=?, settlement_state=?, refund_state=?, "
                            "completed_at=?, latest_code='', resend_base_code='', resend_base_digest='', code_scrubbed=1, "
                            "code_scrub_version=COALESCE(code_scrub_version, 0)+1, version=version+1, updated_at=? WHERE order_id=?",
                            (new_status, next_settlement, new_refund, completed_at, now, order_id),
                        )
                        await raw.execute("UPDATE sms_messages SET code='', raw_text='' WHERE order_id=?", (order_id,))
                        await WebhooksRepo.scrub_for_orders_in_tx(raw, [order_id], now)
                        v_cur = await raw.execute("SELECT code_scrub_version FROM sms_orders WHERE order_id=?", (order_id,))
                        v_row = await v_cur.fetchone()
                        if v_row is not None and scrub_result is not None:
                            scrub_result[order_id] = int(v_row["code_scrub_version"] or 1)
                    else:
                        if is_terminal_now:
                            await raw.execute(
                                "UPDATE sms_orders SET status=?, settlement_state=?, refund_state=?, "
                                "completed_at=?, resend_base_digest='', version=version+1, updated_at=? WHERE order_id=?",
                                (new_status, next_settlement, new_refund, completed_at, now, order_id),
                            )
                        else:
                            await raw.execute(
                                "UPDATE sms_orders SET status=?, settlement_state=?, refund_state=?, "
                                "completed_at=?, version=version+1, updated_at=? WHERE order_id=?",
                                (new_status, next_settlement, new_refund, completed_at, now, order_id),
                            )
                    if result_meta is not None:
                        result_meta["order_id"] = order_id
                        result_meta["parent_status_changed"] = new_status != cur_status
                        result_meta["new_status"] = new_status
                    if outbox_entry_builder is not None and new_status != cur_status:
                        snap = {
                            **ord_row,
                            "status": new_status,
                            "settlement_state": next_settlement,
                            "refund_state": new_refund,
                            "completed_at": completed_at,
                            **({"latest_code": "", "code_scrubbed": 1} if do_scrub else {}),
                        }
                        outbox_entry = outbox_entry_builder(snap, new_status)
                        if outbox_entry:
                            await OrdersRepo._insert_outbox_in_tx(raw, outbox_entry, now)
                            if result_meta is not None:
                                result_meta["outbox_enqueued"] = True

        updated = await self.get(row_id)
        return updated, True

    async def get(self, row_id: int) -> Optional[dict[str, Any]]:
        return await self._conn.fetchone("SELECT * FROM order_activations WHERE id=?", (row_id,))

    async def get_by_activation_id(self, order_id: str, activation_id: str) -> Optional[dict[str, Any]]:
        if not activation_id:
            return None
        return await self._conn.fetchone(
            "SELECT * FROM order_activations WHERE order_id=? AND activation_id=? ORDER BY id DESC LIMIT 1",
            (order_id, activation_id),
        )

    async def list_for_order(self, order_id: str) -> list[dict[str, Any]]:
        return await self._conn.fetchall(
            "SELECT * FROM order_activations WHERE order_id=? ORDER BY attempt_no ASC, id ASC",
            (order_id,),
        )

    async def list_pending_refunds(
        self,
        *,
        active_acquiring_order_ids: Optional[Iterable[str]] = None,
    ) -> list[dict[str, Any]]:
        """返回所有需要退款补偿的激活：
        1. state = 'refund_pending'；
        2. state = 'active' 且所属订单已结束/待补偿（排除已人工结清或收码写库失败的已交付短信单）；
        3. state = 'active' 且所属订单卡在 acquiring 但已无活跃在跑的取号任务（无需重启即可自动发现并补偿）。
        """
        in_flight = [str(oid) for oid in (active_acquiring_order_ids or ()) if oid]
        if in_flight:
            ph = ",".join("?" for _ in in_flight)
            acquiring_cond = f"(COALESCE(o.status, '') = 'acquiring' AND oa.order_id NOT IN ({ph}))"
            params: tuple[Any, ...] = tuple(in_flight)
        else:
            acquiring_cond = "COALESCE(o.status, '') = 'acquiring'"
            params = ()

        return await self._conn.fetchall(
            f"""
            SELECT oa.*
            FROM order_activations oa
            LEFT JOIN sms_orders o ON o.order_id = oa.order_id
            WHERE oa.activation_id != ''
              AND (
                  oa.state = 'refund_pending'
                  OR (
                      oa.state = 'active'
                      AND COALESCE(o.settlement_state, '') NOT IN ('code_persist_failed', 'manual_resolved')
                      AND (
                          COALESCE(o.status, 'cancelled') IN ('cancelled', 'expired', 'failed', 'compensation_pending')
                          OR {acquiring_cond}
                      )
                  )
              )
            ORDER BY oa.id ASC
            """,
            params,
        )

    async def list_uncertain(
        self,
        *,
        limit: int = 100,
        active_acquiring_order_ids: Optional[Iterable[str]] = None,
    ) -> list[dict[str, Any]]:
        """返回结果未知或收码落库持续失败、需人工核对上游账单的激活记录（排除仍在活跃购买中的正常尝试）。"""
        safe_limit = max(1, min(int(limit), 500))
        in_flight = [str(oid) for oid in (active_acquiring_order_ids or ()) if oid]
        if in_flight:
            ph = ",".join("?" for _ in in_flight)
            return await self._conn.fetchall(
                f"SELECT * FROM order_activations "
                f"WHERE state IN ('uncertain_purchase', 'code_persist_failed') "
                f"   OR (state = 'purchasing' AND order_id NOT IN ({ph})) "
                f"ORDER BY id DESC LIMIT ?",
                (*in_flight, safe_limit),
            )
        return await self._conn.fetchall(
            "SELECT * FROM order_activations "
            "WHERE state IN ('purchasing', 'uncertain_purchase', 'code_persist_failed') "
            "ORDER BY id DESC LIMIT ?",
            (safe_limit,),
        )

    async def list_currency_review(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """返回历史迁移中币种来源不明确、待人工复核的激活记录（修复 v6 复核 P1-05）。"""
        return await self._conn.fetchall(
            "SELECT * FROM order_activations WHERE currency_review_required = 1 ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        )
