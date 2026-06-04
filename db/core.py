"""SQLite 持久化层。

性能优化点：
- 全局复用一个 aiosqlite 连接，避免每次 execute/fetch 重新建连
- 通过 asyncio.Lock 串行化连接上的 execute/fetch，保证 aiosqlite 单连接并发安全
- init_db 中完成建连 + PRAGMA + schema 初始化
- close_db 用于优雅关闭（app.py polling 结束 finally 调用）
- 保持 row_factory = aiosqlite.Row（fetchone/fetchall 行为不变）
- 公共函数签名 (init_db / execute / fetchone / fetchall) 完全保留，不破坏调用方

为何要加锁：
    aiosqlite 把 sqlite3 的同步调用放到一个后台线程上跑，单连接同一时刻只能跑一条
    语句。多个 coroutine 并发执行 execute / fetch 时若不串行化，cursor / commit 会
    彼此踩到。这里用一个全局 asyncio.Lock 包住所有访问点，保证一致性。

注意：
    如果在 init_db 之前调用了 execute/fetch（理论上不会，但保底），会自动 lazy init。
"""

from __future__ import annotations

import asyncio

import aiosqlite

from config import DB_PATH
from utils.logger import setup_logging

logger = setup_logging()

INIT_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS message_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    scope TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    username TEXT,
    direction TEXT NOT NULL,
    mode TEXT NOT NULL,
    content_type TEXT NOT NULL,
    content_text TEXT,
    raw_json TEXT
);
CREATE TABLE IF NOT EXISTS reminder_hits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    scope TEXT NOT NULL,
    keyword TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    content_text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS relationship_profiles (
    scope TEXT PRIMARY KEY,
    profile_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    scope TEXT NOT NULL,
    summary_text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    category TEXT DEFAULT 'general',
    status TEXT DEFAULT 'todo',
    priority INTEGER DEFAULT 3,
    due_date TEXT,
    owner TEXT DEFAULT 'owner',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    content TEXT NOT NULL,
    source_chat_id TEXT,
    source_message_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(plan_id) REFERENCES plans(id)
);
CREATE TABLE IF NOT EXISTS daily_focus (
    day TEXT PRIMARY KEY,
    focus_text TEXT,
    top_tasks_json TEXT,
    mood_note TEXT,
    updated_at TEXT NOT NULL
);
-- self_media_assets：斗图弹药库。同时采集两个方向的素材：
--   * direction='outgoing' / source_owner=1：阿君（owner）自己发的贴纸/GIF/表情/图片/语音
--   * direction='incoming' / source_owner=0：对方发来的贴纸/GIF 等，也当素材入库
-- 只存元数据（file_id / file_unique_id / emoji / set_name / size / duration），不存媒体本体。
-- 采集控制到调用处：collect_now=true、reuse_in_same_turn=false（同一轮不拿对方刚发的 file_id 回发给对方）。
-- file_unique_id 是 Telegram 在不同 bot 之间也稳定的唯一标识，用作去重键。
CREATE TABLE IF NOT EXISTS self_media_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    media_type TEXT NOT NULL,           -- sticker / animation / photo / voice / video
    file_id TEXT NOT NULL,              -- bot 本身下一次发用的 file_id
    file_unique_id TEXT,                -- 跨 bot 稳定，UNIQUE 去重
    chat_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    username TEXT,
    mode TEXT NOT NULL,                 -- business / private
    business_connection_id TEXT,
    direction TEXT DEFAULT 'outgoing',  -- outgoing=owner自发；incoming=对方发过来
    source_owner INTEGER DEFAULT 1,     -- 1=owner自发；0=对方发来
    source_username TEXT,               -- 发送者的 username（与 username 重复但保留，方便检索）
    emoji TEXT,                         -- sticker.emoji 或 owner 手动标注
    set_name TEXT,                      -- sticker.set_name
    is_animated INTEGER DEFAULT 0,
    is_video INTEGER DEFAULT 0,
    duration INTEGER,                   -- animation/voice/video 时长秒
    width INTEGER,
    height INTEGER,
    file_size INTEGER,
    description TEXT,                   -- 可选补充描述，供后续检索
    tags TEXT,                          -- 逗号分隔的标签，预留扣图用
    use_count INTEGER DEFAULT 0,        -- bot 回发过多少次
    last_used_at TEXT,
    UNIQUE(file_unique_id, media_type)
);
CREATE INDEX IF NOT EXISTS idx_self_media_assets_type ON self_media_assets(media_type);
CREATE INDEX IF NOT EXISTS idx_self_media_assets_emoji ON self_media_assets(emoji);
CREATE INDEX IF NOT EXISTS idx_self_media_assets_ts ON self_media_assets(ts);
CREATE INDEX IF NOT EXISTS idx_self_media_assets_funique ON self_media_assets(file_unique_id);
"""


# ---------------- 全局连接状态 ----------------

_db: aiosqlite.Connection | None = None
_db_lock = asyncio.Lock()
# 初始化锁单独一个，避免多 coroutine 同时进入 lazy-init 时打架
_init_lock = asyncio.Lock()


async def _ensure_conn() -> aiosqlite.Connection:
    """确保全局连接存在，第一次调用时建连并设置 PRAGMA / schema。"""
    global _db
    if _db is not None:
        return _db
    async with _init_lock:
        if _db is not None:
            return _db
        conn = await aiosqlite.connect(DB_PATH)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA synchronous=NORMAL;")
        await conn.execute("PRAGMA foreign_keys=ON;")
        await conn.executescript(INIT_SQL)
        # 老库兼容：self_media_assets 表可能是早期创建的，缺 direction/source_owner/source_username。
        # SQLite 不支持 "ADD COLUMN IF NOT EXISTS"，这里先查 PRAGMA 再补列，不报错。
        try:
            async with conn.execute("PRAGMA table_info(self_media_assets)") as cur:
                cols = {row[1] for row in await cur.fetchall()}
            patch_cols = [
                ("direction", "TEXT DEFAULT 'outgoing'"),
                ("source_owner", "INTEGER DEFAULT 1"),
                ("source_username", "TEXT"),
            ]
            for cname, ctype in patch_cols:
                if cname not in cols:
                    try:
                        await conn.execute(
                            f"ALTER TABLE self_media_assets ADD COLUMN {cname} {ctype}"
                        )
                        logger.info("self_media_assets patched | added=%s", cname)
                    except Exception as e:
                        logger.warning("self_media_assets ADD COLUMN failed | col=%s | err=%s", cname, e)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_self_media_assets_direction ON self_media_assets(direction)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_self_media_assets_source_owner ON self_media_assets(source_owner)"
            )
        except Exception as e:
            logger.warning("self_media_assets schema patch skipped | err=%s", e)
        await conn.commit()
        _db = conn
        logger.info("db connection opened | path=%s", DB_PATH)
        return _db


async def init_db():
    """初始化全局连接 + schema。可重复调用，幂等。"""
    logger.info("db init start | path=%s", DB_PATH)
    await _ensure_conn()
    logger.info("db init done | path=%s", DB_PATH)


async def close_db():
    """关闭全局连接（polling 结束时 finally 调用）。可重复调用，幂等。"""
    global _db
    async with _init_lock:
        if _db is None:
            return
        try:
            await _db.close()
            logger.info("db connection closed | path=%s", DB_PATH)
        except Exception as e:
            logger.warning("db close failed | err=%s", e)
        finally:
            _db = None


async def execute(query: str, params: tuple = ()):
    conn = await _ensure_conn()
    async with _db_lock:
        await conn.execute(query, params)
        await conn.commit()


async def fetchone(query: str, params: tuple = ()):
    conn = await _ensure_conn()
    async with _db_lock:
        async with conn.execute(query, params) as cur:
            return await cur.fetchone()


async def fetchall(query: str, params: tuple = ()):
    conn = await _ensure_conn()
    async with _db_lock:
        async with conn.execute(query, params) as cur:
            return await cur.fetchall()
