"""R 级互动剧情系统 —— 独立存储模块。

用户明确要求：剧情系统的存储独立、自包含，不要混进主业务表逻辑（db/core.py）里。
本模块复用现有 SQLite 的访问模式（全局单连接 + asyncio.Lock 串行化 + WAL），但
连接、锁、建表、读写 API 全部独立，互不影响主库的初始化流程。

默认数据库文件与主库相同（config.RSTORY_DB_PATH，缺省回落到 DB_PATH），但表名带
rstory_ 前缀，与主业务表隔离；也可通过 RSTORY_DB_PATH 指向独立文件。

三张表：
- rstory_progress：用户在某角色剧情的当前进度（stage / node）。
  主键 (user_id, character)。
- rstory_unlocks：用户已解锁的阶段记录（解锁即写一条，幂等：付费一次解锁该阶段）。
  唯一键 (user_id, character, stage)，存在即视为已解锁，不重复收费。
- rstory_charges：解锁支付/交易记录（charge_id / 阶段 / usdt 金额 / provider /
  status / 创建与确认时间）。charge_id 主键。

所有时间用 ISO8601 UTC 字符串（与主库 ts 字段风格一致）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import aiosqlite

from config import RSTORY_DB_PATH
from utils.logger import setup_logging

logger = setup_logging()


# 状态常量：支付订单生命周期。
CHARGE_PENDING = "pending"
CHARGE_CONFIRMED = "confirmed"
CHARGE_PAID = "paid"  # 真实渠道（OxaPay）Webhook 回调确认到账时的终态。
CHARGE_FAILED = "failed"


INIT_SQL = """
CREATE TABLE IF NOT EXISTS rstory_progress (
    user_id TEXT NOT NULL,
    character TEXT NOT NULL,
    stage INTEGER NOT NULL,
    node TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, character)
);
CREATE TABLE IF NOT EXISTS rstory_unlocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    character TEXT NOT NULL,
    stage INTEGER NOT NULL,
    charge_id TEXT,
    unlocked_at TEXT NOT NULL,
    UNIQUE (user_id, character, stage)
);
CREATE TABLE IF NOT EXISTS rstory_charges (
    charge_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    character TEXT NOT NULL,
    stage INTEGER NOT NULL,
    usdt_amount REAL NOT NULL,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,
    pay_address TEXT,
    pay_info TEXT,
    track_id TEXT,
    payment_url TEXT,
    created_at TEXT NOT NULL,
    confirmed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_rstory_charges_user ON rstory_charges(user_id, character);
CREATE INDEX IF NOT EXISTS idx_rstory_charges_track ON rstory_charges(track_id);
CREATE INDEX IF NOT EXISTS idx_rstory_unlocks_user ON rstory_unlocks(user_id, character);
"""


async def _migrate_charges_columns(conn: aiosqlite.Connection) -> None:
    """为已存在的旧 rstory_charges 表补 track_id / payment_url 列（幂等）。

    新建库走 INIT_SQL 已含这些列；旧库（commit 9d14a62 建的）缺列，这里 ALTER 补上。
    SQLite 没有 ADD COLUMN IF NOT EXISTS，先查 PRAGMA table_info 再决定。
    """
    async with conn.execute("PRAGMA table_info(rstory_charges)") as cur:
        cols = {row[1] for row in await cur.fetchall()}
    for col in ("track_id", "payment_url"):
        if col not in cols:
            await conn.execute(f"ALTER TABLE rstory_charges ADD COLUMN {col} TEXT")


# ---------------- 独立连接状态（与 db.core 完全分开）----------------

_db: aiosqlite.Connection | None = None
_db_lock = asyncio.Lock()
_init_lock = asyncio.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _ensure_conn() -> aiosqlite.Connection:
    """确保本模块的全局连接存在；首次调用建连 + PRAGMA + 建表。幂等。"""
    global _db
    if _db is not None:
        return _db
    async with _init_lock:
        if _db is not None:
            return _db
        conn = await aiosqlite.connect(RSTORY_DB_PATH)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA synchronous=NORMAL;")
        await conn.executescript(INIT_SQL)
        await _migrate_charges_columns(conn)
        await conn.commit()
        _db = conn
        logger.info("rstory store opened | path=%s", RSTORY_DB_PATH)
        return _db


async def init_store() -> None:
    """初始化本模块连接 + 建表。可重复调用，幂等。"""
    await _ensure_conn()


async def close_store() -> None:
    """关闭本模块连接。可重复调用，幂等。"""
    global _db
    async with _init_lock:
        if _db is None:
            return
        try:
            await _db.close()
        except Exception as e:
            logger.warning("rstory store close failed | err=%s", e)
        finally:
            _db = None


async def _execute(query: str, params: tuple = ()) -> None:
    conn = await _ensure_conn()
    async with _db_lock:
        await conn.execute(query, params)
        await conn.commit()


async def _fetchone(query: str, params: tuple = ()) -> aiosqlite.Row | None:
    conn = await _ensure_conn()
    async with _db_lock:
        async with conn.execute(query, params) as cur:
            return await cur.fetchone()


async def _fetchall(query: str, params: tuple = ()) -> list[aiosqlite.Row]:
    conn = await _ensure_conn()
    async with _db_lock:
        async with conn.execute(query, params) as cur:
            return list(await cur.fetchall())


# ---------------- 数据载体 ----------------

@dataclass
class Progress:
    user_id: str
    character: str
    stage: int
    node: str
    updated_at: str


@dataclass
class Charge:
    charge_id: str
    user_id: str
    character: str
    stage: int
    usdt_amount: float
    provider: str
    status: str
    pay_address: str | None
    pay_info: str | None
    track_id: str | None
    payment_url: str | None
    created_at: str
    confirmed_at: str | None


def _norm_uid(user_id: int | str) -> str:
    """统一把 user_id 存成字符串（与主库 user_id TEXT 风格一致）。"""
    return str(user_id)


# ---------------- 进度读写 ----------------

async def get_progress(user_id: int | str, character: str) -> Progress | None:
    row = await _fetchone(
        "SELECT user_id, character, stage, node, updated_at "
        "FROM rstory_progress WHERE user_id = ? AND character = ?",
        (_norm_uid(user_id), character),
    )
    if not row:
        return None
    return Progress(
        user_id=row["user_id"],
        character=row["character"],
        stage=row["stage"],
        node=row["node"],
        updated_at=row["updated_at"],
    )


async def set_progress(user_id: int | str, character: str, stage: int, node: str) -> None:
    """写入/更新进度（upsert）。"""
    await _execute(
        "INSERT INTO rstory_progress (user_id, character, stage, node, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, character) DO UPDATE SET "
        "stage = excluded.stage, node = excluded.node, updated_at = excluded.updated_at",
        (_norm_uid(user_id), character, int(stage), node, _now_iso()),
    )


# ---------------- 解锁读写 ----------------

async def is_stage_unlocked(user_id: int | str, character: str, stage: int) -> bool:
    """阶段是否已解锁。存在解锁记录即视为已付费解锁。"""
    row = await _fetchone(
        "SELECT 1 FROM rstory_unlocks WHERE user_id = ? AND character = ? AND stage = ?",
        (_norm_uid(user_id), character, int(stage)),
    )
    return row is not None


async def record_unlock(
    user_id: int | str, character: str, stage: int, charge_id: str | None
) -> bool:
    """写一条解锁记录。幂等：已存在则不重复插入，返回 False（表示本次未新增）。

    返回 True 表示新增了解锁记录。
    """
    uid = _norm_uid(user_id)
    if await is_stage_unlocked(uid, character, stage):
        return False
    await _execute(
        "INSERT OR IGNORE INTO rstory_unlocks "
        "(user_id, character, stage, charge_id, unlocked_at) VALUES (?, ?, ?, ?, ?)",
        (uid, character, int(stage), charge_id, _now_iso()),
    )
    return True


async def list_unlocked_stages(user_id: int | str, character: str) -> list[int]:
    rows = await _fetchall(
        "SELECT stage FROM rstory_unlocks WHERE user_id = ? AND character = ? ORDER BY stage",
        (_norm_uid(user_id), character),
    )
    return [int(r["stage"]) for r in rows]


# ---------------- 支付记录读写 ----------------

async def create_charge_record(
    *,
    charge_id: str,
    user_id: int | str,
    character: str,
    stage: int,
    usdt_amount: float,
    provider: str,
    pay_address: str | None = None,
    pay_info: str | None = None,
    track_id: str | None = None,
    payment_url: str | None = None,
) -> Charge:
    """写入一条 pending 支付记录。"""
    now = _now_iso()
    await _execute(
        "INSERT INTO rstory_charges "
        "(charge_id, user_id, character, stage, usdt_amount, provider, status, "
        "pay_address, pay_info, track_id, payment_url, created_at, confirmed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        (
            charge_id,
            _norm_uid(user_id),
            character,
            int(stage),
            float(usdt_amount),
            provider,
            CHARGE_PENDING,
            pay_address,
            pay_info,
            track_id,
            payment_url,
            now,
        ),
    )
    return Charge(
        charge_id=charge_id,
        user_id=_norm_uid(user_id),
        character=character,
        stage=int(stage),
        usdt_amount=float(usdt_amount),
        provider=provider,
        status=CHARGE_PENDING,
        pay_address=pay_address,
        pay_info=pay_info,
        track_id=track_id,
        payment_url=payment_url,
        created_at=now,
        confirmed_at=None,
    )


_CHARGE_COLS = (
    "charge_id, user_id, character, stage, usdt_amount, provider, status, "
    "pay_address, pay_info, track_id, payment_url, created_at, confirmed_at"
)


def _row_to_charge(row: aiosqlite.Row) -> Charge:
    return Charge(
        charge_id=row["charge_id"],
        user_id=row["user_id"],
        character=row["character"],
        stage=row["stage"],
        usdt_amount=row["usdt_amount"],
        provider=row["provider"],
        status=row["status"],
        pay_address=row["pay_address"],
        pay_info=row["pay_info"],
        track_id=row["track_id"],
        payment_url=row["payment_url"],
        created_at=row["created_at"],
        confirmed_at=row["confirmed_at"],
    )


async def get_charge(charge_id: str) -> Charge | None:
    row = await _fetchone(
        f"SELECT {_CHARGE_COLS} FROM rstory_charges WHERE charge_id = ?",
        (charge_id,),
    )
    return _row_to_charge(row) if row else None


async def get_charge_by_track_id(track_id: str) -> Charge | None:
    """按 OxaPay track_id 找订单（Webhook 对账用，order_id 缺失时的兜底）。"""
    if not track_id:
        return None
    row = await _fetchone(
        f"SELECT {_CHARGE_COLS} FROM rstory_charges WHERE track_id = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (track_id,),
    )
    return _row_to_charge(row) if row else None


async def set_charge_track(charge_id: str, track_id: str | None, payment_url: str | None) -> None:
    """回填 OxaPay 返回的 track_id / payment_url（创建发票成功后调用）。"""
    await _execute(
        "UPDATE rstory_charges SET track_id = ?, payment_url = ? WHERE charge_id = ?",
        (track_id, payment_url, charge_id),
    )


# status 在这两个值时写 confirmed_at（视为已到账确认）。
_CONFIRMED_STATUSES = {CHARGE_CONFIRMED, CHARGE_PAID}


async def update_charge_status(charge_id: str, status: str) -> None:
    """更新支付状态；status=confirmed/paid 时同时写 confirmed_at（幂等：COALESCE 不覆盖已有时间）。"""
    confirmed_at = _now_iso() if status in _CONFIRMED_STATUSES else None
    await _execute(
        "UPDATE rstory_charges SET status = ?, "
        "confirmed_at = COALESCE(confirmed_at, ?) WHERE charge_id = ?",
        (status, confirmed_at, charge_id),
    )
