"""R 级互动剧情系统 —— 数据驱动存储模块。

用户最终决定：剧情系统从硬编码 FSM 重构为**数据驱动 FSM**。剧本/角色/场景/转移规则
全部存在 DB 表里，引擎从 DB 读规则推进；存储仍独立、自包含，不混进主业务表逻辑
（db/core.py）。

本模块复用现有 SQLite 访问模式（全局单连接 + asyncio.Lock 串行化 + WAL），但连接、
锁、建表、读写 API 全部独立。启动时用 executescript 跑 db/rstory_seed.sql（建表 +
种子，全部 IF NOT EXISTS / INSERT OR IGNORE，幂等）。

数据库文件：默认 config.RSTORY_DB_PATH（缺省回落主库 DB_PATH）。沿用既有 rstory 独立库
约定——可通过 RSTORY_DB_PATH 指向独立文件；不引入孤立 bot.db。

表（见 db/rstory_seed.sql）：
- scripts / characters / script_characters / scenes：静态剧情内容（数据驱动）。
- fsm_transitions：数据驱动转移规则（trigger_type/condition_json/effect_json/priority）。
- user_game_state：用户在某剧本的当前 FSM 状态 + 历史。
- user_char_relation：角色情感数值状态机（affection/trust/desire/dominance/flags…）。
- unlock_products / user_unlocks：解锁产品（USDT 计价）与用户已解锁记录。
- stat_history / content_access_log：数值变更与分级访问审计。
- users：年龄验证权威来源（age_verified）。
- rstory_charges：OxaPay 支付订单/对账（track_id / payment_url / status 流转），复用既有逻辑。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from config import RSTORY_DB_PATH
from utils.logger import setup_logging

logger = setup_logging()


# 支付订单生命周期状态。
CHARGE_PENDING = "pending"
CHARGE_CONFIRMED = "confirmed"
CHARGE_PAID = "paid"  # OxaPay Webhook 验签确认到账时的终态。
CHARGE_FAILED = "failed"

# 解锁来源默认值（统一 USDT / OxaPay）。
UNLOCK_SOURCE_OXAPAY = "oxapay"

_SEED_SQL_PATH = Path(__file__).resolve().parent.parent / "db" / "rstory_seed.sql"


# ---------------- 独立连接状态（与 db.core 完全分开）----------------

_db: aiosqlite.Connection | None = None
_db_lock = asyncio.Lock()
_init_lock = asyncio.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_uid(user_id: int | str) -> int:
    """统一把 user_id 存成整数（schema 里 user_id INTEGER）。"""
    return int(user_id)


async def _ensure_conn() -> aiosqlite.Connection:
    """确保本模块全局连接存在；首次调用建连 + PRAGMA + 跑 seed（建表 + 种子）。幂等。"""
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
        await conn.execute("PRAGMA foreign_keys=ON;")
        seed_sql = _SEED_SQL_PATH.read_text(encoding="utf-8")
        await conn.executescript(seed_sql)
        await conn.commit()
        _db = conn
        logger.info("rstory store opened | path=%s", RSTORY_DB_PATH)
        return _db


async def init_store() -> None:
    """初始化连接 + 建表 + 种子。可重复调用，幂等。"""
    await _ensure_conn()


async def close_store() -> None:
    """关闭连接。可重复调用，幂等。"""
    global _db
    async with _init_lock:
        if _db is None:
            return
        try:
            await _db.close()
        except Exception as e:  # noqa: BLE001
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
class Script:
    script_id: str
    title: str
    description: str | None
    entry_state: str
    is_active: int


@dataclass
class Character:
    char_id: str
    name: str
    base_prompt: str
    r_prompt: str | None
    nsfw_prompt: str | None
    devoted_prompt: str | None
    content_level: int


@dataclass
class Scene:
    scene_id: str
    script_id: str
    state_type: str  # normal / payment_gate / age_gate / end
    scene_type: str  # narrate / ai_free / gate
    title: str | None
    fixed_text: str | None
    choices: list[dict]  # 由 choices_json 解析
    content_level: int
    char_id: str | None


@dataclass
class Transition:
    id: int
    script_id: str
    from_state: str
    to_state: str
    trigger_type: str  # choice / auto / payment / age_verify
    trigger_value: str | None
    condition: dict | None  # 由 condition_json 解析
    effect: dict | None  # 由 effect_json 解析
    priority: int


@dataclass
class GameState:
    user_id: int
    script_id: str
    current_fsm_state: str
    current_char_id: str | None
    history: list[str] = field(default_factory=list)


@dataclass
class Relation:
    user_id: int
    char_id: str
    affection: int
    trust: int
    desire: int
    dominance: int
    relationship: str
    current_mood: str
    flags: dict
    total_messages: int


@dataclass
class UnlockProduct:
    unlock_id: str
    title: str
    description: str | None
    content_level: int
    usdt_amount: float
    char_id: str | None
    is_active: int


@dataclass
class Charge:
    charge_id: str
    user_id: str
    unlock_id: str
    usdt_amount: float
    provider: str
    status: str
    pay_address: str | None
    pay_info: str | None
    track_id: str | None
    payment_url: str | None
    created_at: str
    confirmed_at: str | None


# ---------------- scripts / characters / scenes（静态内容读）----------------

async def get_script(script_id: str) -> Script | None:
    row = await _fetchone(
        "SELECT script_id, title, description, entry_state, is_active "
        "FROM scripts WHERE script_id = ?",
        (script_id,),
    )
    if not row:
        return None
    return Script(
        script_id=row["script_id"],
        title=row["title"],
        description=row["description"],
        entry_state=row["entry_state"],
        is_active=row["is_active"],
    )


async def get_character(char_id: str) -> Character | None:
    row = await _fetchone(
        "SELECT char_id, name, base_prompt, r_prompt, nsfw_prompt, devoted_prompt, content_level "
        "FROM characters WHERE char_id = ?",
        (char_id,),
    )
    if not row:
        return None
    return Character(
        char_id=row["char_id"],
        name=row["name"],
        base_prompt=row["base_prompt"],
        r_prompt=row["r_prompt"],
        nsfw_prompt=row["nsfw_prompt"],
        devoted_prompt=row["devoted_prompt"],
        content_level=row["content_level"],
    )


def _parse_json(raw: str | None, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


async def get_scene(scene_id: str) -> Scene | None:
    row = await _fetchone(
        "SELECT scene_id, script_id, state_type, scene_type, title, fixed_text, "
        "choices_json, content_level, char_id FROM scenes WHERE scene_id = ?",
        (scene_id,),
    )
    if not row:
        return None
    return Scene(
        scene_id=row["scene_id"],
        script_id=row["script_id"],
        state_type=row["state_type"],
        scene_type=row["scene_type"],
        title=row["title"],
        fixed_text=row["fixed_text"],
        choices=_parse_json(row["choices_json"], []),
        content_level=row["content_level"],
        char_id=row["char_id"],
    )


async def list_transitions(script_id: str, from_state: str) -> list[Transition]:
    """取某剧本某状态出发的全部转移，按 priority 降序（引擎据此优先匹配）。"""
    rows = await _fetchall(
        "SELECT id, script_id, from_state, to_state, trigger_type, trigger_value, "
        "condition_json, effect_json, priority FROM fsm_transitions "
        "WHERE script_id = ? AND from_state = ? ORDER BY priority DESC, id ASC",
        (script_id, from_state),
    )
    return [
        Transition(
            id=r["id"],
            script_id=r["script_id"],
            from_state=r["from_state"],
            to_state=r["to_state"],
            trigger_type=r["trigger_type"],
            trigger_value=r["trigger_value"],
            condition=_parse_json(r["condition_json"], None),
            effect=_parse_json(r["effect_json"], None),
            priority=r["priority"],
        )
        for r in rows
    ]


# ---------------- user_game_state（FSM 状态）----------------

async def get_game_state(user_id: int | str, script_id: str) -> GameState | None:
    row = await _fetchone(
        "SELECT user_id, script_id, current_fsm_state, current_char_id, history_json "
        "FROM user_game_state WHERE user_id = ? AND script_id = ?",
        (_norm_uid(user_id), script_id),
    )
    if not row:
        return None
    return GameState(
        user_id=row["user_id"],
        script_id=row["script_id"],
        current_fsm_state=row["current_fsm_state"],
        current_char_id=row["current_char_id"],
        history=_parse_json(row["history_json"], []),
    )


async def set_game_state(
    user_id: int | str,
    script_id: str,
    current_fsm_state: str,
    current_char_id: str | None,
    history: list[str],
) -> None:
    await _execute(
        "INSERT INTO user_game_state "
        "(user_id, script_id, current_fsm_state, current_char_id, history_json, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, script_id) DO UPDATE SET "
        "current_fsm_state = excluded.current_fsm_state, "
        "current_char_id = excluded.current_char_id, "
        "history_json = excluded.history_json, updated_at = excluded.updated_at",
        (
            _norm_uid(user_id),
            script_id,
            current_fsm_state,
            current_char_id,
            json.dumps(history, ensure_ascii=False),
            _now_iso(),
        ),
    )


# ---------------- user_char_relation（数值状态机）----------------

async def get_or_create_relation(user_id: int | str, char_id: str) -> Relation:
    """取用户↔角色关系；不存在则按 schema 默认值创建。"""
    uid = _norm_uid(user_id)
    row = await _fetchone(
        "SELECT user_id, char_id, affection, trust, desire, dominance, relationship, "
        "current_mood, flags, total_messages FROM user_char_relation "
        "WHERE user_id = ? AND char_id = ?",
        (uid, char_id),
    )
    if row is None:
        await _execute(
            "INSERT OR IGNORE INTO user_char_relation (user_id, char_id, last_active) "
            "VALUES (?, ?, ?)",
            (uid, char_id, _now_iso()),
        )
        row = await _fetchone(
            "SELECT user_id, char_id, affection, trust, desire, dominance, relationship, "
            "current_mood, flags, total_messages FROM user_char_relation "
            "WHERE user_id = ? AND char_id = ?",
            (uid, char_id),
        )
    return Relation(
        user_id=row["user_id"],
        char_id=row["char_id"],
        affection=row["affection"],
        trust=row["trust"],
        desire=row["desire"],
        dominance=row["dominance"],
        relationship=row["relationship"],
        current_mood=row["current_mood"],
        flags=_parse_json(row["flags"], {}),
        total_messages=row["total_messages"],
    )


# 允许 *_delta 写入的数值字段（白名单，防止 effect_json 注入任意列名）。
_NUMERIC_STATS = ("affection", "trust", "desire", "dominance")


async def apply_relation_changes(
    user_id: int | str,
    char_id: str,
    *,
    deltas: dict[str, int] | None = None,
    set_flags: list[str] | None = None,
    relationship: str | None = None,
    scene_id: str | None = None,
    reason: str | None = None,
) -> Relation:
    """对关系数值/flag/关系阶段应用一组变更，并把每个数值变更写入 stat_history。

    deltas：{stat_name: delta}，只允许 _NUMERIC_STATS 内的字段。
    set_flags：把这些 flag 置 True（写入 flags JSON）。
    relationship：改关系阶段。
    """
    rel = await get_or_create_relation(user_id, char_id)
    uid = _norm_uid(user_id)

    if deltas:
        for stat, delta in deltas.items():
            if stat not in _NUMERIC_STATS or delta == 0:
                continue
            new_val = getattr(rel, stat) + int(delta)
            setattr(rel, stat, new_val)
            await _execute(
                f"UPDATE user_char_relation SET {stat} = ? WHERE user_id = ? AND char_id = ?",  # nosec B608
                (new_val, uid, char_id),
            )
            await _execute(
                "INSERT INTO stat_history (user_id, char_id, stat_name, delta, reason, scene_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (uid, char_id, stat, int(delta), reason, scene_id, _now_iso()),
            )

    if set_flags:
        for flag in set_flags:
            rel.flags[flag] = True
        await _execute(
            "UPDATE user_char_relation SET flags = ? WHERE user_id = ? AND char_id = ?",
            (json.dumps(rel.flags, ensure_ascii=False), uid, char_id),
        )

    if relationship:
        rel.relationship = relationship
        await _execute(
            "UPDATE user_char_relation SET relationship = ? WHERE user_id = ? AND char_id = ?",
            (relationship, uid, char_id),
        )

    await _execute(
        "UPDATE user_char_relation SET last_active = ? WHERE user_id = ? AND char_id = ?",
        (_now_iso(), uid, char_id),
    )
    return rel


async def list_stat_history(user_id: int | str, char_id: str) -> list[aiosqlite.Row]:
    return await _fetchall(
        "SELECT stat_name, delta, reason, scene_id, created_at FROM stat_history "
        "WHERE user_id = ? AND char_id = ? ORDER BY id ASC",
        (_norm_uid(user_id), char_id),
    )


# ---------------- users / 年龄验证 ----------------

async def ensure_user(user_id: int | str, username: str | None = None) -> None:
    await _execute(
        "INSERT OR IGNORE INTO users (user_id, username, created_at) VALUES (?, ?, ?)",
        (_norm_uid(user_id), username, _now_iso()),
    )


async def is_age_verified(user_id: int | str) -> bool:
    row = await _fetchone(
        "SELECT age_verified FROM users WHERE user_id = ?",
        (_norm_uid(user_id),),
    )
    return bool(row and row["age_verified"])


async def set_age_verified(user_id: int | str, username: str | None = None) -> None:
    """置 users.age_verified=1（幂等）。先 ensure_user 保证行存在。"""
    await ensure_user(user_id, username)
    await _execute(
        "UPDATE users SET age_verified = 1 WHERE user_id = ?",
        (_norm_uid(user_id),),
    )


async def log_content_access(
    user_id: int | str, content_level: int, scene_id: str | None, age_verified: bool
) -> None:
    await _execute(
        "INSERT INTO content_access_log (user_id, content_level, scene_id, age_verified, accessed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (_norm_uid(user_id), int(content_level), scene_id, 1 if age_verified else 0, _now_iso()),
    )


async def list_content_access(user_id: int | str) -> list[aiosqlite.Row]:
    return await _fetchall(
        "SELECT content_level, scene_id, age_verified, accessed_at FROM content_access_log "
        "WHERE user_id = ? ORDER BY id ASC",
        (_norm_uid(user_id),),
    )


# ---------------- unlock_products / user_unlocks ----------------

def _row_to_product(row: aiosqlite.Row) -> UnlockProduct:
    return UnlockProduct(
        unlock_id=row["unlock_id"],
        title=row["title"],
        description=row["description"],
        content_level=row["content_level"],
        usdt_amount=float(row["usdt_amount"]),
        char_id=row["char_id"],
        is_active=row["is_active"],
    )


async def get_unlock_product(unlock_id: str) -> UnlockProduct | None:
    row = await _fetchone(
        "SELECT unlock_id, title, description, content_level, usdt_amount, char_id, is_active "
        "FROM unlock_products WHERE unlock_id = ?",
        (unlock_id,),
    )
    return _row_to_product(row) if row else None


async def get_product_for_level(content_level: int, char_id: str | None = None) -> UnlockProduct | None:
    """按内容分级（可选限定角色）找解锁产品。gate 场景据此定位要解锁的 unlock_id。"""
    if char_id is not None:
        row = await _fetchone(
            "SELECT unlock_id, title, description, content_level, usdt_amount, char_id, is_active "
            "FROM unlock_products WHERE content_level = ? AND char_id = ? AND is_active = 1 "
            "ORDER BY unlock_id LIMIT 1",
            (int(content_level), char_id),
        )
        if row:
            return _row_to_product(row)
    row = await _fetchone(
        "SELECT unlock_id, title, description, content_level, usdt_amount, char_id, is_active "
        "FROM unlock_products WHERE content_level = ? AND is_active = 1 "
        "ORDER BY (char_id IS NULL), unlock_id LIMIT 1",
        (int(content_level),),
    )
    return _row_to_product(row) if row else None


async def is_unlocked(user_id: int | str, unlock_id: str) -> bool:
    row = await _fetchone(
        "SELECT 1 FROM user_unlocks WHERE user_id = ? AND unlock_id = ?",
        (_norm_uid(user_id), unlock_id),
    )
    return row is not None


async def is_level_unlocked(user_id: int | str, content_level: int) -> bool:
    """该内容分级是否已解锁（有任一对应 content_level 的产品被该用户解锁）。

    condition_json 的 content_level_unlocked 求值用。
    """
    row = await _fetchone(
        "SELECT 1 FROM user_unlocks u JOIN unlock_products p ON u.unlock_id = p.unlock_id "
        "WHERE u.user_id = ? AND p.content_level = ? LIMIT 1",
        (_norm_uid(user_id), int(content_level)),
    )
    return row is not None


async def record_unlock(
    user_id: int | str, unlock_id: str, *, source: str = UNLOCK_SOURCE_OXAPAY, charge_id: str | None = None
) -> bool:
    """写一条解锁记录。幂等：已存在则不重复插，返回 False。返回 True 表示本次新增。"""
    uid = _norm_uid(user_id)
    if await is_unlocked(uid, unlock_id):
        return False
    await _execute(
        "INSERT OR IGNORE INTO user_unlocks (user_id, unlock_id, unlocked_at, source, charge_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (uid, unlock_id, _now_iso(), source, charge_id),
    )
    return True


async def list_unlocked(user_id: int | str) -> list[str]:
    rows = await _fetchall(
        "SELECT unlock_id FROM user_unlocks WHERE user_id = ? ORDER BY unlock_id",
        (_norm_uid(user_id),),
    )
    return [r["unlock_id"] for r in rows]


# ---------------- rstory_charges（OxaPay 订单/对账，复用既有逻辑）----------------

_CHARGE_COLS = (
    "charge_id, user_id, unlock_id, usdt_amount, provider, status, "
    "pay_address, pay_info, track_id, payment_url, created_at, confirmed_at"
)


def _row_to_charge(row: aiosqlite.Row) -> Charge:
    return Charge(
        charge_id=row["charge_id"],
        user_id=row["user_id"],
        unlock_id=row["unlock_id"],
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


async def create_charge_record(
    *,
    charge_id: str,
    user_id: int | str,
    unlock_id: str,
    usdt_amount: float,
    provider: str,
    pay_address: str | None = None,
    pay_info: str | None = None,
    track_id: str | None = None,
    payment_url: str | None = None,
) -> Charge:
    now = _now_iso()
    await _execute(
        "INSERT INTO rstory_charges "
        "(charge_id, user_id, unlock_id, usdt_amount, provider, status, "
        "pay_address, pay_info, track_id, payment_url, created_at, confirmed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        (
            charge_id,
            str(_norm_uid(user_id)),
            unlock_id,
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
        user_id=str(_norm_uid(user_id)),
        unlock_id=unlock_id,
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


async def get_charge(charge_id: str) -> Charge | None:
    row = await _fetchone(
        f"SELECT {_CHARGE_COLS} FROM rstory_charges WHERE charge_id = ?",  # nosec B608
        (charge_id,),
    )
    return _row_to_charge(row) if row else None


async def get_charge_by_track_id(track_id: str) -> Charge | None:
    if not track_id:
        return None
    row = await _fetchone(
        f"SELECT {_CHARGE_COLS} FROM rstory_charges WHERE track_id = ? "  # nosec B608
        "ORDER BY created_at DESC LIMIT 1",
        (track_id,),
    )
    return _row_to_charge(row) if row else None


async def set_charge_track(charge_id: str, track_id: str | None, payment_url: str | None) -> None:
    await _execute(
        "UPDATE rstory_charges SET track_id = ?, payment_url = ? WHERE charge_id = ?",
        (track_id, payment_url, charge_id),
    )


_CONFIRMED_STATUSES = {CHARGE_CONFIRMED, CHARGE_PAID}


async def update_charge_status(charge_id: str, status: str) -> None:
    """更新支付状态；confirmed/paid 时写 confirmed_at（COALESCE 不覆盖，重复回调不刷新）。"""
    confirmed_at = _now_iso() if status in _CONFIRMED_STATUSES else None
    await _execute(
        "UPDATE rstory_charges SET status = ?, "
        "confirmed_at = COALESCE(confirmed_at, ?) WHERE charge_id = ?",
        (status, confirmed_at, charge_id),
    )
