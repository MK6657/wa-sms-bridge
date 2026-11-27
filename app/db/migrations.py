"""SQLite 前向迁移管理（v1 -> v2 -> v3，事务安全重建外键并修复历史库升级）。

关键保证（修复复核 P0-01 / P0-02 / P1-03）：
1. 迁移全程在显式 BEGIN IMMEDIATE 事务中执行，避免 executescript 隐式提交导致半迁移状态。
2. v1 -> v2 重建 sms_orders 时同步重建子表 sms_messages（及 order_activations），
   彻底杜绝 SQLite 3.26+ RENAME 改写外键指向已删除临时表 `_sms_orders_v1` 的故障。
3. v2 -> v3 为此前已标记 v2 但外键损坏的数据库提供自动前向修复，同时为 webhook_deliveries
   增加 contains_code / payload_scrubbed 字段并加密或清理历史明文验证码负载。
4. 每次迁移写版本号前强制执行 `PRAGMA foreign_key_check` 与 DDL 残留检查，失败立即回滚可安全重试。
5. v1 历史 cancelled 订单迁移时标记 refund_state='unknown'，不冒充已确认退款。
"""

from __future__ import annotations

import json
import logging
import aiosqlite

from app.config import env

logger = logging.getLogger(__name__)

BASE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now')),
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS blacklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'smsbower',
    project_id TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(phone, provider, project_id)
);

CREATE TABLE IF NOT EXISTS task_state (
    task_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'running',
    provider TEXT NOT NULL DEFAULT 'smsbower',
    country_id TEXT NOT NULL DEFAULT '',
    country_label TEXT NOT NULL DEFAULT '',
    activation_id TEXT NOT NULL DEFAULT '',
    phone_number TEXT NOT NULL DEFAULT '',
    order_id TEXT NOT NULL DEFAULT '',
    wa_account_id TEXT NOT NULL DEFAULT '',
    verification_request_id TEXT NOT NULL DEFAULT '',
    wa_status TEXT NOT NULL DEFAULT '',
    registration_phase TEXT NOT NULL DEFAULT '',
    failure_category TEXT NOT NULL DEFAULT '',
    failure_code TEXT NOT NULL DEFAULT '',
    step TEXT NOT NULL DEFAULT 'init',
    error TEXT NOT NULL DEFAULT '',
    attempts_used INTEGER NOT NULL DEFAULT 0,
    successes_done INTEGER NOT NULL DEFAULT 0,
    target_success_count INTEGER NOT NULL DEFAULT 1,
    cost_total REAL NOT NULL DEFAULT 0,
    account_commit_state TEXT NOT NULL DEFAULT 'none',
    attempt_request_id TEXT NOT NULL DEFAULT '',
    sms_finalize_state TEXT NOT NULL DEFAULT '',
    reconciliation_required INTEGER NOT NULL DEFAULT 0,
    reconciliation_error TEXT NOT NULL DEFAULT '',
    activation_compensated INTEGER NOT NULL DEFAULT 0,
    account_compensated INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS registration_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'smsbower',
    phone TEXT NOT NULL,
    country_id TEXT NOT NULL DEFAULT '',
    country_label TEXT NOT NULL DEFAULT '',
    activation_id TEXT NOT NULL DEFAULT '',
    wa_account_id TEXT NOT NULL DEFAULT '',
    verification_request_id TEXT NOT NULL DEFAULT '',
    sms_code TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'completed',
    cost REAL NOT NULL DEFAULT 0,
    cost_known INTEGER NOT NULL DEFAULT 0,
    cost_is_estimated INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'USD',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_task_state_status ON task_state(status);
CREATE INDEX IF NOT EXISTS idx_blacklist_phone ON blacklist(phone);
CREATE INDEX IF NOT EXISTS idx_history_phone ON registration_history(phone);
CREATE INDEX IF NOT EXISTS idx_history_created ON registration_history(created_at);
"""

SMS_ORDERS_DDL = """
CREATE TABLE IF NOT EXISTS sms_orders (
    order_id TEXT PRIMARY KEY,
    idempotency_key TEXT,
    request_hash TEXT NOT NULL DEFAULT '',
    project_id TEXT NOT NULL DEFAULT 'web-console',
    external_ref TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT 'smsbower',
    service TEXT NOT NULL DEFAULT 'whatsapp',
    service_code TEXT NOT NULL DEFAULT 'wa',
    country_id TEXT NOT NULL DEFAULT '',
    country_label TEXT NOT NULL DEFAULT '',
    country_iso2 TEXT NOT NULL DEFAULT '',
    country_calling_code TEXT NOT NULL DEFAULT '',
    activation_id TEXT NOT NULL DEFAULT '',
    phone_raw TEXT NOT NULL DEFAULT '',
    phone_e164 TEXT NOT NULL DEFAULT '',
    phone_national TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'acquiring',
    version INTEGER NOT NULL DEFAULT 1,
    settlement_state TEXT NOT NULL DEFAULT 'none',
    refund_state TEXT NOT NULL DEFAULT 'none',
    latest_code TEXT NOT NULL DEFAULT '',
    code_received_at TEXT NOT NULL DEFAULT '',
    code_scrubbed INTEGER NOT NULL DEFAULT 0,
    code_count INTEGER NOT NULL DEFAULT 0,
    resend_base_code TEXT NOT NULL DEFAULT '',
    resend_seen_wait INTEGER NOT NULL DEFAULT 0,
    max_price REAL,
    quoted_max_price REAL,
    cost REAL,
    actual_cost REAL,
    cost_status TEXT NOT NULL DEFAULT 'unknown',
    cost_is_estimated INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'USD',
    wait_timeout INTEGER NOT NULL DEFAULT 180,
    expires_at TEXT NOT NULL DEFAULT '',
    attempts_used INTEGER NOT NULL DEFAULT 0,
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    webhook_url TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT NOT NULL DEFAULT '',
    UNIQUE(project_id, idempotency_key)
)
"""

SMS_MESSAGES_DDL = """
CREATE TABLE IF NOT EXISTS sms_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL REFERENCES sms_orders(order_id) ON DELETE CASCADE,
    seq INTEGER NOT NULL DEFAULT 1,
    code TEXT NOT NULL DEFAULT '',
    raw_text TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

ORDER_ACTIVATIONS_DDL = """
CREATE TABLE IF NOT EXISTS order_activations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL REFERENCES sms_orders(order_id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL DEFAULT 1,
    provider TEXT NOT NULL DEFAULT 'smsbower',
    country_id TEXT NOT NULL DEFAULT '',
    service_code TEXT NOT NULL DEFAULT '',
    activation_id TEXT NOT NULL DEFAULT '',
    phone_raw TEXT NOT NULL DEFAULT '',
    phone_e164 TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'purchasing',
    quoted_max_price REAL,
    actual_cost REAL,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

WEBHOOK_DELIVERIES_DDL = """
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    order_id TEXT NOT NULL DEFAULT '',
    project_id TEXT NOT NULL DEFAULT '',
    target_url TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    contains_code INTEGER NOT NULL DEFAULT 0,
    payload_scrubbed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    next_retry_at TEXT NOT NULL DEFAULT (datetime('now')),
    lease_expires_at TEXT NOT NULL DEFAULT '',
    last_status_code INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    delivered_at TEXT NOT NULL DEFAULT ''
)
"""

API_KEYS_DDL = """
CREATE TABLE IF NOT EXISTS api_keys (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    key_prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    key_version INTEGER NOT NULL DEFAULT 1,
    scopes TEXT NOT NULL DEFAULT 'orders:rw,prices:r,blacklist:rw,events:r',
    default_webhook_url TEXT NOT NULL DEFAULT '',
    webhook_secret_enc TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_used_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

PROJECTS_DDL = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

V2_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_sms_orders_status ON sms_orders(status)",
    "CREATE INDEX IF NOT EXISTS idx_sms_orders_project ON sms_orders(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_sms_orders_created ON sms_orders(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_sms_orders_status_created ON sms_orders(status, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sms_orders_project_created ON sms_orders(project_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sms_orders_phone ON sms_orders(phone_e164)",
    "CREATE INDEX IF NOT EXISTS idx_sms_messages_order ON sms_messages(order_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_order_activations_order ON order_activations(order_id, attempt_no)",
    "CREATE INDEX IF NOT EXISTS idx_order_activations_state ON order_activations(state)",
    "CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_due ON webhook_deliveries(status, next_retry_at)",
    "CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_project ON webhook_deliveries(project_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_order ON webhook_deliveries(order_id)",
)


async def _table_columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    return {str(r["name"]) for r in rows}


async def _table_exists(conn: aiosqlite.Connection, table: str) -> bool:
    cur = await conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return (await cur.fetchone()) is not None


async def _table_ddl(conn: aiosqlite.Connection, table: str) -> str:
    cur = await conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    row = await cur.fetchone()
    return str(row["sql"] or "") if row else ""


async def _fk_targets(conn: aiosqlite.Connection, table: str) -> set[str]:
    cur = await conn.execute(f"PRAGMA foreign_key_list({table})")
    rows = await cur.fetchall()
    return {str(r["table"]) for r in rows}


async def _verify_schema_integrity(conn: aiosqlite.Connection) -> None:
    """严格校验所有表外键与 sqlite_master 定义，存在坏外键立即抛出异常以触发回滚。"""
    cur = await conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql LIKE '%_sms_orders_v1%'"
    )
    dangling = [dict(r) for r in await cur.fetchall()]
    if dangling:
        raise RuntimeError(f"检测到残留临时表外键引用: {[d['name'] for d in dangling]}")

    for child in ("sms_messages", "order_activations"):
        if await _table_exists(conn, child):
            targets = await _fk_targets(conn, child)
            if targets != {"sms_orders"}:
                raise RuntimeError(f"子表 {child} 外键目标异常: {sorted(targets)}")

    fk_cur = await conn.execute("PRAGMA foreign_key_check")
    violations = [tuple(r) for r in await fk_cur.fetchall()]
    if violations:
        raise RuntimeError(f"PRAGMA foreign_key_check 发现 {len(violations)} 处外键完整性违规: {violations[:5]}")


async def _rebuild_sms_messages_in_tx(conn: aiosqlite.Connection) -> None:
    """在当前事务内重建 sms_messages，保证其外键精确指向 sms_orders(order_id) 且数据无损。"""
    if not await _table_exists(conn, "sms_messages"):
        await conn.execute(SMS_MESSAGES_DDL)
        return
    await conn.execute("ALTER TABLE sms_messages RENAME TO _sms_messages_rebuild")
    await conn.execute(SMS_MESSAGES_DDL)
    await conn.execute(
        "INSERT INTO sms_messages (id, order_id, seq, code, raw_text, received_at) "
        "SELECT id, order_id, seq, code, raw_text, received_at FROM _sms_messages_rebuild "
        "WHERE order_id IN (SELECT order_id FROM sms_orders)"
    )
    await conn.execute("DROP TABLE _sms_messages_rebuild")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_sms_messages_order ON sms_messages(order_id, seq)")


async def _rebuild_order_activations_in_tx(conn: aiosqlite.Connection) -> None:
    """在当前事务内重建 order_activations，保证外键指向 sms_orders(order_id)。"""
    if not await _table_exists(conn, "order_activations"):
        await conn.execute(ORDER_ACTIVATIONS_DDL)
        return
    await conn.execute("ALTER TABLE order_activations RENAME TO _order_activations_rebuild")
    await conn.execute(ORDER_ACTIVATIONS_DDL)
    await conn.execute(
        "INSERT INTO order_activations ("
        "id, order_id, attempt_no, provider, country_id, service_code, activation_id, "
        "phone_raw, phone_e164, state, quoted_max_price, actual_cost, last_error, created_at, updated_at"
        ") SELECT "
        "id, order_id, attempt_no, provider, country_id, service_code, activation_id, "
        "phone_raw, phone_e164, state, quoted_max_price, actual_cost, last_error, created_at, updated_at "
        "FROM _order_activations_rebuild "
        "WHERE order_id IN (SELECT order_id FROM sms_orders)"
    )
    await conn.execute("DROP TABLE _order_activations_rebuild")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_order_activations_order ON order_activations(order_id, attempt_no)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_order_activations_state ON order_activations(state)")


async def _ensure_v2_tables_and_indexes_in_tx(conn: aiosqlite.Connection) -> None:
    await conn.execute(SMS_ORDERS_DDL)
    await conn.execute(SMS_MESSAGES_DDL)
    await conn.execute(ORDER_ACTIVATIONS_DDL)
    await conn.execute(WEBHOOK_DELIVERIES_DDL)
    await conn.execute(API_KEYS_DDL)
    for stmt in V2_INDEX_STATEMENTS:
        await conn.execute(stmt)


async def _ensure_legacy_columns(conn: aiosqlite.Connection) -> None:
    """确保旧版 v0.1/v0.2 SQLite 数据库补齐缺失列。"""
    for table in ("task_state", "blacklist"):
        try:
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN provider TEXT NOT NULL DEFAULT 'smsbower'")
            await conn.commit()
        except Exception:
            pass

    legacy_additions = {
        "task_state": [
            ("verification_request_id", "TEXT NOT NULL DEFAULT ''"),
            ("wa_status", "TEXT NOT NULL DEFAULT ''"),
            ("registration_phase", "TEXT NOT NULL DEFAULT ''"),
            ("failure_category", "TEXT NOT NULL DEFAULT ''"),
            ("failure_code", "TEXT NOT NULL DEFAULT ''"),
            ("successes_done", "INTEGER NOT NULL DEFAULT 0"),
            ("target_success_count", "INTEGER NOT NULL DEFAULT 1"),
            ("cost_total", "REAL NOT NULL DEFAULT 0"),
            ("account_commit_state", "TEXT NOT NULL DEFAULT 'none'"),
            ("attempt_request_id", "TEXT NOT NULL DEFAULT ''"),
            ("sms_finalize_state", "TEXT NOT NULL DEFAULT ''"),
            ("reconciliation_required", "INTEGER NOT NULL DEFAULT 0"),
            ("reconciliation_error", "TEXT NOT NULL DEFAULT ''"),
            ("activation_compensated", "INTEGER NOT NULL DEFAULT 0"),
            ("account_compensated", "INTEGER NOT NULL DEFAULT 0"),
            ("completed_at", "TEXT NOT NULL DEFAULT ''"),
        ],
        "registration_history": [
            ("provider", "TEXT NOT NULL DEFAULT 'smsbower'"),
            ("verification_request_id", "TEXT NOT NULL DEFAULT ''"),
            ("cost_known", "INTEGER NOT NULL DEFAULT 0"),
            ("cost_is_estimated", "INTEGER NOT NULL DEFAULT 0"),
            ("currency", "TEXT NOT NULL DEFAULT 'USD'"),
        ],
    }
    for table, columns in legacy_additions.items():
        for column, definition in columns:
            try:
                await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                await conn.commit()
            except Exception:
                pass

    try:
        await conn.execute("UPDATE task_state SET country_id = CAST(country_id AS TEXT) WHERE country_id != ''")
        await conn.execute("UPDATE registration_history SET sms_code='' WHERE sms_code != ''")
        await conn.commit()
    except Exception:
        pass


async def _migrate_v1_to_v2(conn: aiosqlite.Connection, applied: set[int]) -> None:
    """原子事务内将 v1 升级到 v2：重建 blacklist、sms_orders 及其所有子表外键并校验。"""
    await conn.commit()
    await conn.execute("PRAGMA foreign_keys=OFF")
    try:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            # 1. 重建 blacklist 增加 project_id 与 (phone, provider, project_id) 联合唯一约束
            bl_cols = await _table_columns(conn, "blacklist")
            if bl_cols and "project_id" not in bl_cols:
                await conn.execute("ALTER TABLE blacklist RENAME TO _blacklist_v1")
                await conn.execute(
                    """
                    CREATE TABLE blacklist (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        phone TEXT NOT NULL,
                        provider TEXT NOT NULL DEFAULT 'smsbower',
                        project_id TEXT NOT NULL DEFAULT '',
                        reason TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT (datetime('now')),
                        UNIQUE(phone, provider, project_id)
                    )
                    """
                )
                await conn.execute(
                    "INSERT OR IGNORE INTO blacklist (id, phone, provider, project_id, reason, created_at) "
                    "SELECT id, phone, provider, '', reason, created_at FROM _blacklist_v1"
                )
                await conn.execute("DROP TABLE _blacklist_v1")
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_blacklist_phone ON blacklist(phone)")

            # 2. 若存在旧版 v1 sms_orders，重建 sms_orders 并同步重建子表避免 SQLite RENAME 破坏外键
            ord_cols = await _table_columns(conn, "sms_orders")
            if ord_cols and "request_hash" not in ord_cols:
                had_messages = await _table_exists(conn, "sms_messages")
                had_activations = await _table_exists(conn, "order_activations")
                await conn.execute("ALTER TABLE sms_orders RENAME TO _sms_orders_v1")
                await conn.execute(SMS_ORDERS_DDL)
                # P1-03 & v3 复核 P1-02: v1 历史 cancelled 订单无激活记 none，有激活但无退款凭据记 unknown
                await conn.execute(
                    """
                    INSERT INTO sms_orders (
                        order_id, idempotency_key, request_hash, project_id, external_ref, provider,
                        service, service_code, country_id, country_label, country_iso2, country_calling_code,
                        activation_id, phone_raw, phone_e164, phone_national, status, version,
                        settlement_state, refund_state, latest_code, code_received_at, code_scrubbed,
                        code_count, resend_base_code, resend_seen_wait, max_price, quoted_max_price,
                        cost, actual_cost, cost_status, cost_is_estimated, currency, wait_timeout,
                        expires_at, attempts_used, error_code, error_message, webhook_url, metadata_json,
                        created_at, updated_at, completed_at
                    )
                    SELECT
                        order_id, NULLIF(TRIM(idempotency_key), ''), '', project_id, external_ref, provider,
                        service, service_code, country_id, country_label, country_iso2, country_calling_code,
                        activation_id, phone_raw, phone_e164, phone_national, status, 1,
                        CASE WHEN status='completed' THEN 'completed' WHEN status='cancelled' THEN 'cancelled' ELSE 'none' END,
                        CASE
                            WHEN status='cancelled' AND COALESCE(activation_id, '') = '' THEN 'none'
                            WHEN status='cancelled' THEN 'unknown'
                            ELSE 'none'
                        END,
                        latest_code,
                        CASE WHEN code_count > 0 THEN updated_at ELSE '' END,
                        code_scrubbed, code_count, '', 0, max_price, max_price,
                        cost,
                        CASE WHEN cost IS NOT NULL AND cost_is_estimated=0 THEN cost ELSE NULL END,
                        CASE WHEN cost IS NULL THEN 'unknown' WHEN cost_is_estimated=1 THEN 'estimated' ELSE 'actual' END,
                        cost_is_estimated, currency, wait_timeout, expires_at, attempts_used,
                        error_code, error_message, webhook_url, metadata_json,
                        created_at, updated_at, completed_at
                    FROM _sms_orders_v1
                    """
                )
                if had_messages:
                    await _rebuild_sms_messages_in_tx(conn)
                if had_activations:
                    await _rebuild_order_activations_in_tx(conn)
                await conn.execute("DROP TABLE _sms_orders_v1")

            await _ensure_v2_tables_and_indexes_in_tx(conn)
            await _verify_schema_integrity(conn)

            if 1 not in applied:
                await conn.execute(
                    "INSERT OR IGNORE INTO schema_version (version, description) VALUES (?, ?)",
                    (1, "SMS Hub core tables"),
                )
            await conn.execute(
                "INSERT INTO schema_version (version, description) VALUES (?, ?)",
                (2, "Scoped idempotency, order_activations, CAS/settlement/refund fields, webhook_deliveries outbox, project blacklist"),
            )
            await conn.commit()
            logger.info("database migration v2 applied and verified")
        except BaseException:
            await conn.rollback()
            raise
    finally:
        await conn.execute("PRAGMA foreign_keys=ON")


async def _migrate_v2_to_v3(conn: aiosqlite.Connection) -> None:
    """v3 前向修复迁移：
    1. 自动修复先前 v2 迁移中可能遗留的坏外键（如 sms_messages 指向已删除的 _sms_orders_v1）；
    2. 为 webhook_deliveries 补齐 contains_code / payload_scrubbed 字段与 order_id 索引；
    3. 修正历史 v1 迁移误标为 refund_state='confirmed' 且无激活确认证据的 cancelled 订单为 unknown；
    4. 校验 PRAGMA foreign_key_check 无误后才写入版本号 3。
    """
    await conn.commit()
    await conn.execute("PRAGMA foreign_keys=OFF")
    try:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            # 1. 检查并修复 sms_messages / order_activations 外键
            if await _table_exists(conn, "sms_messages"):
                msg_targets = await _fk_targets(conn, "sms_messages")
                msg_ddl = await _table_ddl(conn, "sms_messages")
                if msg_targets != {"sms_orders"} or "_sms_orders_v1" in msg_ddl:
                    await _rebuild_sms_messages_in_tx(conn)
                    logger.info("v3 migration rebuilt sms_messages foreign key -> sms_orders(order_id)")

            if await _table_exists(conn, "order_activations"):
                act_targets = await _fk_targets(conn, "order_activations")
                act_ddl = await _table_ddl(conn, "order_activations")
                if act_targets != {"sms_orders"} or "_sms_orders_v1" in act_ddl:
                    await _rebuild_order_activations_in_tx(conn)
                    logger.info("v3 migration rebuilt order_activations foreign key -> sms_orders(order_id)")

            # 2. 确保 v2/v3 基础表存在，再补齐 webhook_deliveries 新字段与索引
            await _ensure_v2_tables_and_indexes_in_tx(conn)
            wh_cols = await _table_columns(conn, "webhook_deliveries")
            if wh_cols:
                if "contains_code" not in wh_cols:
                    await conn.execute("ALTER TABLE webhook_deliveries ADD COLUMN contains_code INTEGER NOT NULL DEFAULT 0")
                if "payload_scrubbed" not in wh_cols:
                    await conn.execute("ALTER TABLE webhook_deliveries ADD COLUMN payload_scrubbed INTEGER NOT NULL DEFAULT 0")
                await conn.execute(
                    "UPDATE webhook_deliveries SET contains_code=1 WHERE event_type='order.code_received'"
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_order ON webhook_deliveries(order_id)"
                )

            # 3. 严格依据可审计激活台账校正历史无凭据的 confirmed 退款记录（修复 v3 复核 P1-02）
            await _reconcile_unverified_confirmed_refunds_in_tx(conn)

            await _verify_schema_integrity(conn)
            await conn.execute(
                "INSERT INTO schema_version (version, description) VALUES (?, ?)",
                (3, "Repair child table FKs, add webhook outbox code-scrubbing columns, fix legacy refund_state"),
            )
            await conn.commit()
            logger.info("database migration v3 applied and verified")
        except BaseException:
            await conn.rollback()
            raise
    finally:
        await conn.execute("PRAGMA foreign_keys=ON")


async def _reconcile_unverified_confirmed_refunds_in_tx(conn: aiosqlite.Connection) -> None:
    """仅当存在匹配 activation_id 且 state='refund_confirmed' 的 order_activations 台账凭据时，
    才允许保留历史订单的 refund_state='confirmed'；无激活 ID 归为 none，有激活但缺退款凭据归为 unknown。"""
    await conn.execute(
        """
        UPDATE sms_orders
        SET refund_state='none'
        WHERE status IN ('cancelled', 'expired')
          AND refund_state='confirmed'
          AND COALESCE(activation_id, '') = ''
        """
    )
    await conn.execute(
        """
        UPDATE sms_orders
        SET refund_state='unknown'
        WHERE status IN ('cancelled', 'expired')
          AND refund_state='confirmed'
          AND COALESCE(activation_id, '') != ''
          AND NOT EXISTS (
              SELECT 1 FROM order_activations oa
              WHERE oa.order_id = sms_orders.order_id
                AND oa.activation_id = sms_orders.activation_id
                AND oa.state = 'refund_confirmed'
          )
        """
    )


async def _migrate_v3_to_v4(conn: aiosqlite.Connection) -> None:
    """v4 前向校正迁移：清除旧 v2/v3 遗留的无 order_activations 退款凭据的 confirmed 标记（含 USER_CANCELLED 等）。"""
    await conn.commit()
    await conn.execute("PRAGMA foreign_keys=OFF")
    try:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await _ensure_v2_tables_and_indexes_in_tx(conn)
            await _reconcile_unverified_confirmed_refunds_in_tx(conn)
            await _verify_schema_integrity(conn)
            await conn.execute(
                "INSERT INTO schema_version (version, description) VALUES (?, ?)",
                (4, "Require auditable order_activations refund_confirmed evidence for historical confirmed refunds"),
            )
            await conn.commit()
            logger.info("database migration v4 applied and verified")
        except BaseException:
            await conn.rollback()
            raise
    finally:
        await conn.execute("PRAGMA foreign_keys=ON")


async def _migrate_v4_to_v5(conn: aiosqlite.Connection) -> None:
    """v5 前向迁移（修复补充评估 P1-02 / P1-03 / P1-05）：
    1. 新增永久项目注册表 projects 并从 api_keys / sms_orders / blacklist / webhook_deliveries 回填全部历史 project_id；
    2. 为 api_keys 增加 key_version 支持密钥轮转与 SSE 实时撤销检测；
    3. 为 sms_orders 增加 request_hash_version / country_input / idempotency_http_status / idempotency_retryable / async_acquire；
    4. 为 order_activations 增加 currency / cost_status / estimated_cost / refund_amount 支持精确财务台账与净支出统计。
    """
    await conn.commit()
    await conn.execute("PRAGMA foreign_keys=OFF")
    try:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await _ensure_v2_tables_and_indexes_in_tx(conn)
            await conn.execute(PROJECTS_DDL)

            # 1. api_keys 增加 key_version
            key_cols = await _table_columns(conn, "api_keys")
            if "key_version" not in key_cols:
                await conn.execute("ALTER TABLE api_keys ADD COLUMN key_version INTEGER NOT NULL DEFAULT 1")

            # 2. sms_orders 增加幂等与原始请求字段
            ord_cols = await _table_columns(conn, "sms_orders")
            ord_additions = (
                ("request_hash_version", "INTEGER NOT NULL DEFAULT 1"),
                ("country_input", "TEXT NOT NULL DEFAULT ''"),
                ("idempotency_http_status", "INTEGER NOT NULL DEFAULT 0"),
                ("idempotency_retryable", "INTEGER NOT NULL DEFAULT 0"),
                ("async_acquire", "INTEGER NOT NULL DEFAULT 0"),
            )
            for col_name, col_def in ord_additions:
                if col_name not in ord_cols:
                    await conn.execute(f"ALTER TABLE sms_orders ADD COLUMN {col_name} {col_def}")

            # 3. order_activations 增加精确财务台账列并按可信证据回填历史记录（修复 v5 复核 P1-04 / P1-05）
            act_cols = await _table_columns(conn, "order_activations")
            act_additions = (
                ("currency", "TEXT NOT NULL DEFAULT ''"),
                ("cost_status", "TEXT NOT NULL DEFAULT 'unknown'"),
                ("estimated_cost", "REAL"),
                ("refund_amount", "REAL"),
            )
            for col_name, col_def in act_additions:
                if col_name not in act_cols:
                    await conn.execute(f"ALTER TABLE order_activations ADD COLUMN {col_name} {col_def}")

            # 仅以父订单可信非空币种回填历史激活币种，证据不足归为 UNKNOWN（绝不默认填 USD）
            await conn.execute(
                """
                UPDATE order_activations
                SET currency = COALESCE(
                    (
                        SELECT CASE
                            WHEN UPPER(TRIM(COALESCE(o.currency, ''))) NOT IN ('', 'UNKNOWN')
                            THEN UPPER(TRIM(o.currency))
                            ELSE 'UNKNOWN'
                        END
                        FROM sms_orders o
                        WHERE o.order_id = order_activations.order_id
                    ),
                    'UNKNOWN'
                )
                WHERE TRIM(COALESCE(currency, '')) = ''
                """
            )

            # 仅对真实已购激活回填成本口径；明确未购买的尝试 (no_numbers / failed 且无 activation_id)
            # 保留 quoted_max_price 供诊断，绝不回填 estimated_cost；也不在缺证据时推定 refund_amount
            await conn.execute(
                """
                UPDATE order_activations
                SET cost_status = CASE
                        WHEN actual_cost IS NOT NULL THEN 'actual'
                        WHEN COALESCE(activation_id, '') != '' AND quoted_max_price IS NOT NULL THEN 'estimated'
                        WHEN COALESCE(activation_id, '') = '' AND state IN ('no_numbers', 'failed') THEN 'none'
                        ELSE 'unknown'
                    END,
                    estimated_cost = CASE
                        WHEN actual_cost IS NULL AND COALESCE(activation_id, '') != '' AND quoted_max_price IS NOT NULL
                        THEN quoted_max_price
                        ELSE NULL
                    END
                WHERE cost_status = 'unknown'
                """
            )

            # 4. 回填永久项目注册表 projects（现存密钥记 active，历史已删密钥但留存数据的项目记 deleted，永不复用）
            await conn.execute(
                """
                INSERT OR IGNORE INTO projects (project_id, name, status, created_at, updated_at)
                SELECT project_id, name, 'active', created_at, created_at FROM api_keys
                WHERE project_id != ''
                """
            )
            for tbl in ("sms_orders", "blacklist", "webhook_deliveries"):
                if await _table_exists(conn, tbl):
                    await conn.execute(
                        f"""
                        INSERT OR IGNORE INTO projects (project_id, name, status)
                        SELECT DISTINCT project_id, project_id, 'deleted'
                        FROM {tbl}
                        WHERE COALESCE(project_id, '') != ''
                          AND project_id != 'web-console'
                        """
                    )

            await _verify_schema_integrity(conn)
            await conn.execute(
                "INSERT INTO schema_version (version, description) VALUES (?, ?)",
                (5, "Permanent projects registry, api key_version, idempotency replay fields, activation financial ledger"),
            )
            await conn.commit()
            logger.info("database migration v5 applied and verified")
        except BaseException:
            await conn.rollback()
            raise
    finally:
        await conn.execute("PRAGMA foreign_keys=ON")


async def _migrate_v5_to_v6(conn: aiosqlite.Connection) -> None:
    """v6 前向校正迁移（修复 v5 复核 P1-01 / P1-04 / P1-05）：
    1. 为 sms_orders 增加独立的初次幂等错误快照列 idempotency_error_code / idempotency_error_message，
       并将历史 async_acquire=1 订单的 idempotency_http_status 校正为 202；
    2. 为 order_activations 增加 refund_amount_status / refund_amount_source，清除旧 v5 无证据推定的
       refund_amount = actual_cost，同时保留可核实的显式/部分退款金额；
    3. 校正旧 v5 对历史非美元/未知币种激活错填为 USD 的记录，以及无库存/失败未购尝试被误填 estimated_cost 的记录。
    """
    await conn.commit()
    await conn.execute("PRAGMA foreign_keys=OFF")
    try:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await _ensure_v2_tables_and_indexes_in_tx(conn)
            await conn.execute(PROJECTS_DDL)

            # 1. sms_orders 补齐不可变幂等错误快照列并回填历史记录（P1-01）
            ord_cols = await _table_columns(conn, "sms_orders")
            for col_name, col_def in (
                ("idempotency_error_code", "TEXT NOT NULL DEFAULT ''"),
                ("idempotency_error_message", "TEXT NOT NULL DEFAULT ''"),
            ):
                if col_name not in ord_cols:
                    await conn.execute(f"ALTER TABLE sms_orders ADD COLUMN {col_name} {col_def}")

            # 异步受理订单统一固定 202 幂等重放语义
            await conn.execute(
                """
                UPDATE sms_orders
                SET idempotency_http_status = 202,
                    idempotency_retryable = 0,
                    idempotency_error_code = '',
                    idempotency_error_message = ''
                WHERE async_acquire = 1
                """
            )
            # 同步失败订单回填初次业务错误快照：仅当 error_code 属于初次取号失败码时才复制，
            # 绝不把补偿/终态改写后的 COMPENSATED_CANCEL 等状态误填为初次业务错误（修复 v6 复核 P1-06）
            await conn.execute(
                """
                UPDATE sms_orders
                SET idempotency_error_code = CASE
                        WHEN idempotency_error_code != '' THEN idempotency_error_code
                        WHEN error_code IN (
                            'NO_NUMBERS', 'INSUFFICIENT_BALANCE', 'BLACKLISTED',
                            'BLACKLIST_REFUND_UNCONFIRMED', 'PRICE_EXCEEDED',
                            'PURCHASE_UNCERTAIN', 'PROVIDER_ERROR',
                            'ACQUIRE_POST_ERROR', 'INTERNAL_ERROR'
                        ) THEN error_code
                        ELSE 'HISTORICAL_ACQUIRE_ERROR'
                    END,
                    idempotency_error_message = CASE
                        WHEN idempotency_error_message != '' THEN idempotency_error_message
                        WHEN error_code IN (
                            'NO_NUMBERS', 'INSUFFICIENT_BALANCE', 'BLACKLISTED',
                            'BLACKLIST_REFUND_UNCONFIRMED', 'PRICE_EXCEEDED',
                            'PURCHASE_UNCERTAIN', 'PROVIDER_ERROR',
                            'ACQUIRE_POST_ERROR', 'INTERNAL_ERROR'
                        ) THEN error_message
                        ELSE '历史订单初次取号失败（后续已变更状态，请查询订单获取最新结果）'
                    END,
                    idempotency_http_status = CASE
                        WHEN idempotency_http_status >= 400 THEN idempotency_http_status
                        WHEN error_code = 'INSUFFICIENT_BALANCE' THEN 402
                        WHEN error_code IN ('NO_NUMBERS', 'BLACKLISTED', 'BLACKLIST_REFUND_UNCONFIRMED', 'PRICE_EXCEEDED') THEN 409
                        WHEN error_code IN ('ACQUIRE_POST_ERROR', 'INTERNAL_ERROR') THEN 500
                        ELSE 502
                    END
                WHERE async_acquire = 0
                  AND (idempotency_http_status >= 400 OR (idempotency_http_status = 0 AND status = 'failed'))
                """
            )
            # 历史空币种订单归一为 UNKNOWN（绝不隐式假设 USD）
            await conn.execute(
                "UPDATE sms_orders SET currency = 'UNKNOWN' WHERE TRIM(COALESCE(currency, '')) = ''"
            )

            # 2. order_activations 增加退款金额证据状态与来源列（P1-04）
            act_cols = await _table_columns(conn, "order_activations")
            added_refund_status = "refund_amount_status" not in act_cols
            for col_name, col_def in (
                ("refund_amount_status", "TEXT NOT NULL DEFAULT 'none'"),
                ("refund_amount_source", "TEXT NOT NULL DEFAULT ''"),
            ):
                if col_name not in act_cols:
                    await conn.execute(f"ALTER TABLE order_activations ADD COLUMN {col_name} {col_def}")

            if added_refund_status:
                # 可核实的部分退款或无 actual_cost 却显式记录的退款：保留金额并标 verified
                await conn.execute(
                    """
                    UPDATE order_activations
                    SET refund_amount_status = 'verified',
                        refund_amount_source = 'legacy_explicit'
                    WHERE state = 'refund_confirmed'
                      AND refund_amount IS NOT NULL
                      AND (actual_cost IS NULL OR ABS(refund_amount - actual_cost) > 1e-9)
                    """
                )
                # 旧 v5 迁移或运行时在无证据时自动填入 refund_amount = actual_cost：清除推定值并标 unknown
                await conn.execute(
                    """
                    UPDATE order_activations
                    SET refund_amount = NULL,
                        refund_amount_status = 'unknown',
                        refund_amount_source = ''
                    WHERE state = 'refund_confirmed'
                      AND refund_amount IS NOT NULL
                      AND actual_cost IS NOT NULL
                      AND ABS(refund_amount - actual_cost) <= 1e-9
                    """
                )
                # 已确认退款但无金额记录：明确标 unknown
                await conn.execute(
                    """
                    UPDATE order_activations
                    SET refund_amount_status = 'unknown',
                        refund_amount_source = ''
                    WHERE state = 'refund_confirmed'
                      AND refund_amount IS NULL
                    """
                )
                await conn.execute(
                    """
                    UPDATE order_activations
                    SET refund_amount_status = 'none',
                        refund_amount_source = ''
                    WHERE state != 'refund_confirmed'
                    """
                )

            # 3. 校正旧 v5 对无激活未购尝试误填的 estimated_cost（保留 quoted_max_price 供诊断，P1-05）
            await conn.execute(
                """
                UPDATE order_activations
                SET estimated_cost = NULL,
                    cost_status = CASE
                        WHEN state IN ('no_numbers', 'failed') THEN 'none'
                        ELSE 'unknown'
                    END
                WHERE COALESCE(activation_id, '') = ''
                  AND actual_cost IS NULL
                """
            )

            # 4. 仅补齐真正缺失的空币种，绝不盲目覆盖已有明确记录的激活币种（修复 v6 复核 P1-05）
            await conn.execute(
                """
                UPDATE order_activations
                SET currency = COALESCE(
                    (
                        SELECT CASE
                            WHEN UPPER(TRIM(COALESCE(o.currency, ''))) NOT IN ('', 'UNKNOWN')
                            THEN UPPER(TRIM(o.currency))
                            ELSE 'UNKNOWN'
                        END
                        FROM sms_orders o
                        WHERE o.order_id = order_activations.order_id
                    ),
                    'UNKNOWN'
                )
                WHERE TRIM(COALESCE(currency, '')) = ''
                """
            )
            await conn.execute(
                """
                UPDATE order_activations
                SET currency = UPPER(TRIM(currency))
                WHERE TRIM(COALESCE(currency, '')) != ''
                """
            )

            await _verify_schema_integrity(conn)
            await conn.execute(
                "INSERT INTO schema_version (version, description) VALUES (?, ?)",
                (6, "Immutable idempotency error snapshots, refund amount evidence status, safe currency and unpurchased cost repair"),
            )
            await conn.commit()
            logger.info("database migration v6 applied and verified")
        except BaseException:
            await conn.rollback()
            raise
    finally:
        await conn.execute("PRAGMA foreign_keys=ON")


async def _migrate_v6_to_v7(
    conn: aiosqlite.Connection,
    *,
    had_v5_before: bool,
    had_v6_before: bool,
) -> None:
    """v7 前向迁移（修复 v6 复核 P0-01 / P1-05 / P1-06）：
    1. sms_orders 增加 code_scrub_version 单调脱敏版本号，封堵旧码延迟推送与内存重试回写；
    2. order_activations 增加 currency_source / currency_review_required，保留已记录激活币种，
       对旧 v5 未记录来源的 USD 冲突行与已运行旧 v6 的受影响行标入人工核对队列；
    3. 校正旧 v6 误把 COMPENSATED_CANCEL 等补偿后错误码复制到 idempotency_error_code 的历史记录。
    """
    await conn.commit()
    await conn.execute("PRAGMA foreign_keys=OFF")
    try:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await _ensure_v2_tables_and_indexes_in_tx(conn)
            await conn.execute(PROJECTS_DDL)

            # 1. sms_orders 增加单调脱敏版本号 code_scrub_version（P0-01）
            ord_cols = await _table_columns(conn, "sms_orders")
            if "code_scrub_version" not in ord_cols:
                await conn.execute("ALTER TABLE sms_orders ADD COLUMN code_scrub_version INTEGER NOT NULL DEFAULT 0")
            await conn.execute(
                "UPDATE sms_orders SET code_scrub_version = 1 WHERE code_scrubbed = 1 AND code_scrub_version = 0"
            )

            # 2. 校正旧 v6 误把补偿/终态错误码复制进 idempotency_error_code 的记录（P1-06）
            await conn.execute(
                """
                UPDATE sms_orders
                SET idempotency_error_code = 'HISTORICAL_ACQUIRE_ERROR',
                    idempotency_error_message = '历史订单初次取号失败（后续已变更状态，请查询订单获取最新结果）'
                WHERE async_acquire = 0
                  AND idempotency_http_status >= 400
                  AND idempotency_error_code IN (
                      'COMPENSATED_CANCEL', 'CANCEL_UNREFUNDABLE', 'USER_CANCELLED',
                      'SMS_TIMEOUT', 'PROVIDER_CANCELLED', 'PROVIDER_EXPIRED',
                      'RESTART_RECOVERY', 'MAINTENANCE_RECOVERY', 'COMPLETE_RECOVERY',
                      'ACQUIRE_COMPENSATION_PENDING', 'PROVIDER_FINISHED'
                  )
                """
            )

            # 3. order_activations 增加币种来源与人工核对标记（P1-05）
            act_cols = await _table_columns(conn, "order_activations")
            for col_name, col_def in (
                ("currency_source", "TEXT NOT NULL DEFAULT ''"),
                ("currency_review_required", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if col_name not in act_cols:
                    await conn.execute(f"ALTER TABLE order_activations ADD COLUMN {col_name} {col_def}")

            # 真正缺失币种（空串）按父订单补齐并注明来源
            await conn.execute(
                """
                UPDATE order_activations
                SET currency = COALESCE(
                        (
                            SELECT CASE
                                WHEN UPPER(TRIM(COALESCE(o.currency, ''))) NOT IN ('', 'UNKNOWN')
                                THEN UPPER(TRIM(o.currency))
                                ELSE 'UNKNOWN'
                            END
                            FROM sms_orders o
                            WHERE o.order_id = order_activations.order_id
                        ),
                        'UNKNOWN'
                    ),
                    currency_source = CASE
                        WHEN EXISTS (
                            SELECT 1 FROM sms_orders o
                            WHERE o.order_id = order_activations.order_id
                              AND UPPER(TRIM(COALESCE(o.currency, ''))) NOT IN ('', 'UNKNOWN')
                        ) THEN 'migrated_from_order'
                        ELSE 'unknown'
                    END
                WHERE TRIM(COALESCE(currency, '')) = ''
                """
            )

            if had_v5_before and not had_v6_before:
                # 旧 v5 库升级：已记录的激活币种原样保留绝不覆盖；但对缺来源证据且与父订单不一致的 USD 激活标入核对
                await conn.execute(
                    """
                    UPDATE order_activations
                    SET currency_source = 'v5_unverified',
                        currency_review_required = 1
                    WHERE currency_source = ''
                      AND UPPER(TRIM(currency)) = 'USD'
                      AND (COALESCE(activation_id, '') != '' OR actual_cost IS NOT NULL)
                      AND (
                          NOT EXISTS (SELECT 1 FROM sms_orders o WHERE o.order_id = order_activations.order_id)
                          OR EXISTS (
                              SELECT 1 FROM sms_orders o
                              WHERE o.order_id = order_activations.order_id
                                AND UPPER(TRIM(COALESCE(o.currency, ''))) != 'USD'
                          )
                      )
                    """
                )
            elif had_v6_before:
                # 已经过旧 v6 迁移的库：旧 v6 曾把 USD 激活按非 USD/空父订单覆写，将无法区分的受影响记录标入核对
                await conn.execute(
                    """
                    UPDATE order_activations
                    SET currency_source = 'v6_migration_review',
                        currency_review_required = 1
                    WHERE currency_source = ''
                      AND (COALESCE(activation_id, '') != '' OR actual_cost IS NOT NULL)
                      AND (
                          (
                              UPPER(TRIM(currency)) = 'UNKNOWN'
                              AND EXISTS (
                                  SELECT 1 FROM sms_orders o
                                  WHERE o.order_id = order_activations.order_id
                                    AND UPPER(TRIM(COALESCE(o.currency, ''))) IN ('', 'UNKNOWN')
                              )
                          )
                          OR (
                              UPPER(TRIM(currency)) NOT IN ('', 'USD', 'UNKNOWN')
                              AND (
                                  LOWER(provider) IN ('smsbower', 'herosms')
                                  OR (
                                      SELECT COUNT(*) FROM order_activations oa2
                                      WHERE oa2.order_id = order_activations.order_id
                                        AND (COALESCE(oa2.activation_id, '') != '' OR oa2.actual_cost IS NOT NULL)
                                  ) > 1
                              )
                          )
                      )
                    """
                )

            await _verify_schema_integrity(conn)
            await conn.execute(
                "INSERT INTO schema_version (version, description) VALUES (?, ?)",
                (7, "SMS code_scrub_version epoch, non-destructive currency_source/review flags, historical acquire error cleanup"),
            )
            await conn.commit()
            logger.info("database migration v7 applied and verified")
        except BaseException:
            await conn.rollback()
            raise
    finally:
        await conn.execute("PRAGMA foreign_keys=ON")


async def _migrate_v7_to_v8(conn: aiosqlite.Connection) -> None:
    """v8 前向迁移：
    1. sms_orders 新增 resend_base_digest（支持脱敏后重发防重且不落盘明文）；
    2. 规范化历史 blacklist.provider 小写去重；
    3. 补齐统计、自动脱敏与对账高频查询索引。
    """
    await conn.commit()
    await conn.execute("PRAGMA foreign_keys=OFF")
    try:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await _ensure_v2_tables_and_indexes_in_tx(conn)
            await conn.execute(PROJECTS_DDL)

            ord_cols = await _table_columns(conn, "sms_orders")
            if "resend_base_digest" not in ord_cols:
                await conn.execute("ALTER TABLE sms_orders ADD COLUMN resend_base_digest TEXT NOT NULL DEFAULT ''")

            if await _table_exists(conn, "blacklist"):
                await conn.execute(
                    """
                    DELETE FROM blacklist
                    WHERE id NOT IN (
                        SELECT MIN(id)
                        FROM blacklist
                        GROUP BY phone, LOWER(TRIM(provider)), project_id
                    )
                    """
                )
                await conn.execute(
                    "UPDATE blacklist SET provider = LOWER(TRIM(provider)) WHERE provider != LOWER(TRIM(provider))"
                )

            for idx_sql in (
                "CREATE INDEX IF NOT EXISTS idx_sms_orders_created_utc ON sms_orders(strftime('%Y-%m-%dT%H:%M:%f', created_at))",
                "CREATE INDEX IF NOT EXISTS idx_sms_orders_unscrubbed ON sms_orders(code_scrubbed, status)",
                "CREATE INDEX IF NOT EXISTS idx_sms_orders_review_settlement ON sms_orders(settlement_state)",
                "CREATE INDEX IF NOT EXISTS idx_order_activations_ccy_review ON order_activations(currency_review_required) WHERE currency_review_required = 1",
            ):
                await conn.execute(idx_sql)

            await _verify_schema_integrity(conn)
            await conn.execute(
                "INSERT INTO schema_version (version, description) VALUES (?, ?)",
                (8, "Keyed resend_base_digest, lowercase blacklist provider normalization, and query performance indexes"),
            )
            await conn.commit()
            logger.info("database migration v8 applied and verified")
        except BaseException:
            await conn.rollback()
            raise
    finally:
        await conn.execute("PRAGMA foreign_keys=ON")


def _payload_contains_code(event_type: str, raw_payload: str) -> bool:
    if event_type == "order.code_received":
        return True
    if not raw_payload or raw_payload.startswith("e_") or raw_payload == "{}":
        return False
    try:
        parsed = json.loads(raw_payload)
        data = parsed.get("data") if isinstance(parsed, dict) else None
        if isinstance(data, dict) and str(data.get("latest_code") or "").strip():
            return True
    except Exception:
        pass
    return False


async def _encrypt_legacy_plaintext_secrets(conn: aiosqlite.Connection) -> None:
    """迁移检查：若历史库中残留未加密的验证码、Webhook 密钥或 Outbox 验证码负载，自动原地加密或脱敏。"""
    try:
        # 无论是否配置主密钥，先将已脱敏订单或已终态(delivered/failed/scrubbed)的 Outbox 验证码负载立即清空！
        cur = await conn.execute(
            """
            SELECT w.delivery_id, w.event_type, w.order_id, w.status, w.payload_json, w.contains_code,
                   COALESCE(o.code_scrubbed, 0) AS order_scrubbed
            FROM webhook_deliveries w
            LEFT JOIN sms_orders o ON o.order_id = w.order_id
            WHERE w.payload_scrubbed = 0 AND w.payload_json != '' AND w.payload_json != '{}'
              AND (w.contains_code = 1 OR w.event_type = 'order.code_received' OR w.payload_json LIKE '%latest_code%')
            """
        )
        wh_rows = [dict(r) for r in await cur.fetchall()]
        wh_scrubbed = 0
        wh_encrypted = 0
        for row in wh_rows:
            raw_json = str(row["payload_json"] or "")
            has_code = bool(row["contains_code"]) or _payload_contains_code(str(row["event_type"]), raw_json)
            if not has_code:
                continue
            status = str(row["status"] or "")
            if bool(row["order_scrubbed"]) or status in {"delivered", "failed", "scrubbed"}:
                new_status = "scrubbed" if status in {"pending", "delivering"} else status
                await conn.execute(
                    "UPDATE webhook_deliveries SET payload_json='{}', contains_code=1, payload_scrubbed=1, "
                    "status=?, lease_expires_at='' WHERE delivery_id=?",
                    (new_status, row["delivery_id"]),
                )
                wh_scrubbed += 1
            elif env.has_secret_key and not raw_json.startswith("e_"):
                enc_payload = f"e_{env.encrypt_value(raw_json)}"
                await conn.execute(
                    "UPDATE webhook_deliveries SET payload_json=?, contains_code=1 WHERE delivery_id=?",
                    (enc_payload, row["delivery_id"]),
                )
                wh_encrypted += 1

        orders_enc = 0
        msgs_enc = 0
        keys_enc = 0
        # 先清理已标记 code_scrubbed=1 却残留验证码字段的异常历史行
        await conn.execute(
            "UPDATE sms_orders SET latest_code='', resend_base_code='' "
            "WHERE code_scrubbed=1 AND (latest_code != '' OR resend_base_code != '')"
        )
        await conn.execute(
            "UPDATE sms_messages SET code='', raw_text='' "
            "WHERE (code != '' OR raw_text != '') "
            "AND order_id IN (SELECT order_id FROM sms_orders WHERE code_scrubbed=1)"
        )
        if env.has_secret_key:
            cur = await conn.execute(
                "SELECT order_id, latest_code, resend_base_code FROM sms_orders "
                "WHERE (latest_code != '' AND latest_code NOT LIKE 'e_%') "
                "   OR (resend_base_code != '' AND resend_base_code NOT LIKE 'e_%')"
            )
            rows = await cur.fetchall()
            for row in rows:
                lc = str(row["latest_code"] or "")
                rb = str(row["resend_base_code"] or "")
                enc_lc = f"e_{env.encrypt_value(lc)}" if lc and not lc.startswith("e_") else lc
                enc_rb = f"e_{env.encrypt_value(rb)}" if rb and not rb.startswith("e_") else rb
                await conn.execute(
                    "UPDATE sms_orders SET latest_code=?, resend_base_code=? WHERE order_id=?",
                    (enc_lc, enc_rb, row["order_id"]),
                )
            orders_enc = len(rows)

            cur = await conn.execute(
                "SELECT id, code, raw_text FROM sms_messages WHERE (code != '' AND code NOT LIKE 'e_%') OR (raw_text != '' AND raw_text NOT LIKE 'e_%')"
            )
            msg_rows = await cur.fetchall()
            for row in msg_rows:
                code = str(row["code"] or "")
                raw_text = str(row["raw_text"] or "")
                enc_code = f"e_{env.encrypt_value(code)}" if code and not code.startswith("e_") else code
                enc_raw = f"e_{env.encrypt_value(raw_text)}" if raw_text and not raw_text.startswith("e_") else raw_text
                await conn.execute(
                    "UPDATE sms_messages SET code=?, raw_text=? WHERE id=?",
                    (enc_code, enc_raw, row["id"]),
                )
            msgs_enc = len(msg_rows)

            cur = await conn.execute(
                "SELECT project_id, webhook_secret_enc FROM api_keys WHERE webhook_secret_enc != '' AND webhook_secret_enc NOT LIKE 'e_%'"
            )
            key_rows = await cur.fetchall()
            for row in key_rows:
                await conn.execute(
                    "UPDATE api_keys SET webhook_secret_enc=? WHERE project_id=?",
                    (f"e_{env.encrypt_value(str(row['webhook_secret_enc']))}", row["project_id"]),
                )
            keys_enc = len(key_rows)

        if getattr(conn, "in_transaction", False):
            await conn.commit()
        if orders_enc or msgs_enc or keys_enc or wh_scrubbed or wh_encrypted:
            logger.info(
                "历史敏感数据迁移完成: orders=%d messages=%d webhook_secrets=%d outbox_encrypted=%d outbox_scrubbed=%d",
                orders_enc,
                msgs_enc,
                keys_enc,
                wh_encrypted,
                wh_scrubbed,
            )
    except Exception:
        try:
            await conn.rollback()
        except Exception:
            pass
        logger.warning("历史明文敏感数据加密迁移异常", exc_info=True)


async def run_migrations(conn: aiosqlite.Connection) -> None:
    """幂等执行所有前向迁移，并在最后强制校验外键完整性。"""
    await conn.executescript(BASE_SCHEMA_SQL)
    await conn.commit()
    await _ensure_legacy_columns(conn)

    cursor = await conn.execute("SELECT version FROM schema_version")
    applied = {int(row["version"]) for row in await cursor.fetchall()}
    had_v5_before = 5 in applied
    had_v6_before = 6 in applied

    if 2 not in applied:
        await _migrate_v1_to_v2(conn, applied)
        applied.add(2)

    if 3 not in applied:
        await _migrate_v2_to_v3(conn)
        applied.add(3)

    if 4 not in applied:
        await _migrate_v3_to_v4(conn)
        applied.add(4)

    if 5 not in applied:
        await _migrate_v4_to_v5(conn)
        applied.add(5)

    if 6 not in applied:
        await _migrate_v5_to_v6(conn)
        applied.add(6)

    if 7 not in applied:
        await _migrate_v6_to_v7(conn, had_v5_before=had_v5_before, had_v6_before=had_v6_before)
        applied.add(7)

    if 8 not in applied:
        await _migrate_v7_to_v8(conn)
        applied.add(8)

    await _encrypt_legacy_plaintext_secrets(conn)
