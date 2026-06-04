"""斗图弹药库 / 媒体素材采集层。

采集范围（两个方向都采）：
- direction='outgoing' / source_owner=1：阿君（owner）自发的 sticker / animation / photo / voice / video，
  在 business 或 private 任一窗口里机器人都静默采集，不调用模型也不回复。
- direction='incoming' / source_owner=0：对方发过来的 sticker / animation 等，在走正常回复逻辑的同时
  同步入库，作为未来反手的“弹药”。

采集控制与使用约束：
- collect_now=true：收到的一刻就入库。
- reuse_in_same_turn=false：同一轮对话里，绝不能拿“对方刚发的同一个 file_id / file_unique_id”
  反手发回给对方。该保护在选材函数 pick_media_asset 里靠 exclude_file_unique_id 实现。

存储说明：
- 只存元数据（file_id / file_unique_id / emoji / set_name / 尺寸 / 时长 / 文件大小 /
  business_connection_id / direction / source_owner / source_username），不存媒体本体。
- 通过 file_unique_id + media_type 联合唯一去重；ON CONFLICT 只更新 file_id / ts 不干扰 direction。
- 任何异常都吞掉只记日志，不影响主链路。
"""

from datetime import datetime

from aiogram.types import Message

from db.core import execute, fetchall, fetchone
from utils.logger import setup_logging

logger = setup_logging()


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _extract_media_meta(message: Message) -> dict | None:
    """从 message 中抽取媒体元数据；不识别的媒体返回 None。

    优先级：animation > sticker > photo > voice > video。
    （animation 在 aiogram 里独立于 sticker，所以先判 animation。）
    """
    # animation / GIF
    if getattr(message, "animation", None) is not None:
        a = message.animation
        return {
            "media_type": "animation",
            "file_id": getattr(a, "file_id", "") or "",
            "file_unique_id": getattr(a, "file_unique_id", None),
            "emoji": None,
            "set_name": None,
            "is_animated": 0,
            "is_video": 0,
            "duration": getattr(a, "duration", None),
            "width": getattr(a, "width", None),
            "height": getattr(a, "height", None),
            "file_size": getattr(a, "file_size", None),
            "description": (getattr(a, "file_name", None) or "").strip() or None,
        }

    # sticker
    if getattr(message, "sticker", None) is not None:
        s = message.sticker
        return {
            "media_type": "sticker",
            "file_id": getattr(s, "file_id", "") or "",
            "file_unique_id": getattr(s, "file_unique_id", None),
            "emoji": (getattr(s, "emoji", None) or None),
            "set_name": (getattr(s, "set_name", None) or None),
            "is_animated": 1 if getattr(s, "is_animated", False) else 0,
            "is_video": 1 if getattr(s, "is_video", False) else 0,
            "duration": None,
            "width": getattr(s, "width", None),
            "height": getattr(s, "height", None),
            "file_size": getattr(s, "file_size", None),
            "description": None,
        }

    # photo（取最大尺寸那张）
    if getattr(message, "photo", None):
        ph = message.photo[-1]
        return {
            "media_type": "photo",
            "file_id": getattr(ph, "file_id", "") or "",
            "file_unique_id": getattr(ph, "file_unique_id", None),
            "emoji": None,
            "set_name": None,
            "is_animated": 0,
            "is_video": 0,
            "duration": None,
            "width": getattr(ph, "width", None),
            "height": getattr(ph, "height", None),
            "file_size": getattr(ph, "file_size", None),
            "description": (getattr(message, "caption", None) or None),
        }

    # voice
    if getattr(message, "voice", None) is not None:
        v = message.voice
        return {
            "media_type": "voice",
            "file_id": getattr(v, "file_id", "") or "",
            "file_unique_id": getattr(v, "file_unique_id", None),
            "emoji": None,
            "set_name": None,
            "is_animated": 0,
            "is_video": 0,
            "duration": getattr(v, "duration", None),
            "width": None,
            "height": None,
            "file_size": getattr(v, "file_size", None),
            "description": None,
        }

    # video
    if getattr(message, "video", None) is not None:
        vid = message.video
        return {
            "media_type": "video",
            "file_id": getattr(vid, "file_id", "") or "",
            "file_unique_id": getattr(vid, "file_unique_id", None),
            "emoji": None,
            "set_name": None,
            "is_animated": 0,
            "is_video": 0,
            "duration": getattr(vid, "duration", None),
            "width": getattr(vid, "width", None),
            "height": getattr(vid, "height", None),
            "file_size": getattr(vid, "file_size", None),
            "description": (getattr(message, "caption", None) or None),
        }

    return None


async def record_self_media(
    message: Message,
    mode: str,
    direction: str = "outgoing",
    source_owner: bool = True,
) -> bool:
    """把媒体落到 self_media_assets。同时适用 owner 自发与对方发过来的采集。

    参数：
    - mode: "business" 或 "private"。
    - direction: "outgoing"（owner 自发）或 "incoming"（对方发来）。
    - source_owner: True 表示是 owner 自发；False 表示对方发来。

    返回 True 表示采集逻辑正常（插入或去重更新）；False 表示出异常。
    任何异常都吞掉、不抛出。
    """
    try:
        meta = _extract_media_meta(message)
        if not meta or not meta.get("file_id"):
            return False

        from_user = message.from_user
        username = (getattr(from_user, "username", None) or "") if from_user else ""
        chat_id = str(message.chat.id) if message.chat else ""
        user_id = str(getattr(from_user, "id", "")) if from_user else ""

        await execute(
            """
            INSERT INTO self_media_assets(
                ts, media_type, file_id, file_unique_id,
                chat_id, user_id, username, mode, business_connection_id,
                direction, source_owner, source_username,
                emoji, set_name, is_animated, is_video,
                duration, width, height, file_size, description
            ) VALUES(?, ?, ?, ?,  ?, ?, ?, ?, ?,  ?, ?, ?,  ?, ?, ?, ?,  ?, ?, ?, ?, ?)
            ON CONFLICT(file_unique_id, media_type) DO UPDATE SET
                file_id=excluded.file_id,
                last_used_at=excluded.ts
            """,
            (
                _now_str(),
                meta["media_type"],
                meta["file_id"],
                meta.get("file_unique_id"),
                chat_id,
                user_id,
                username,
                mode,
                str(getattr(message, "business_connection_id", "") or "") or None,
                direction,
                1 if source_owner else 0,
                username or None,
                meta.get("emoji"),
                meta.get("set_name"),
                int(meta.get("is_animated") or 0),
                int(meta.get("is_video") or 0),
                meta.get("duration"),
                meta.get("width"),
                meta.get("height"),
                meta.get("file_size"),
                meta.get("description"),
            ),
        )
        logger.info(
            "media asset recorded | type=%s | mode=%s | dir=%s | owner=%s | chat_id=%s | emoji=%s | set=%s",
            meta["media_type"],
            mode,
            direction,
            int(bool(source_owner)),
            chat_id,
            meta.get("emoji"),
            meta.get("set_name"),
        )
        return True
    except Exception as e:
        logger.warning("record_self_media failed | err=%s", e)
        return False


async def record_incoming_media(message: Message, mode: str) -> bool:
    """对方发来的 sticker/animation/photo/voice/video 入库（弹药库采集）。

    仅入库，不影响主链路的回复逻辑；是实现“collect_now=true”语义的调用点。
    """
    return await record_self_media(message, mode, direction="incoming", source_owner=False)


async def list_self_media(
    media_type: str | None = None,
    emoji: str | None = None,
    limit: int = 20,
    direction: str | None = None,
) -> list[dict]:
    """查询素材库，供后续斗图选择使用。过滤：媒体类型 / emoji / direction。"""
    where = []
    params: list = []
    if media_type:
        where.append("media_type = ?")
        params.append(media_type)
    if emoji:
        where.append("emoji = ?")
        params.append(emoji)
    if direction:
        where.append("direction = ?")
        params.append(direction)
    sql = "SELECT * FROM self_media_assets"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(int(limit))
    rows = await fetchall(sql, tuple(params))
    return [dict(r) for r in rows]


async def pick_media_asset(
    media_type: str,
    exclude_file_unique_id: str | None = None,
    emoji: str | None = None,
    prefer_owner: bool = True,
) -> dict | None:
    """从弹药库里挑一个可发的素材。

    硬约束（reuse_in_same_turn=false）：如果传了 exclude_file_unique_id（通常就是对方
    本轮刚发过来的那个），永远不要选中同一个 file_unique_id 的贴纸/GIF 回发。

    优先策略：
      1) prefer_owner=True 时优先 source_owner=1 的素材（阿君自己的表情包更贴近他本人风格）
      2) use_count 越小优先（避免总发同一张）
      3) ts 越新优先
    找不到返回 None。调用方要能接受 None（表示不发素材，仅文字/emoji 回）。
    """
    try:
        where = ["media_type = ?"]
        params: list = [media_type]
        if exclude_file_unique_id:
            where.append("(file_unique_id IS NULL OR file_unique_id != ?)")
            params.append(exclude_file_unique_id)
        if emoji:
            where.append("emoji = ?")
            params.append(emoji)
        sql = "SELECT * FROM self_media_assets WHERE " + " AND ".join(where)
        if prefer_owner:
            # SQLite 里拿 source_owner=1 排前：ORDER BY source_owner DESC
            sql += " ORDER BY source_owner DESC, use_count ASC, ts DESC LIMIT 1"
        else:
            sql += " ORDER BY use_count ASC, ts DESC LIMIT 1"
        row = await fetchone(sql, tuple(params))
        return dict(row) if row else None
    except Exception as e:
        logger.warning("pick_media_asset failed | err=%s", e)
        return None


async def bump_media_use(file_unique_id: str, media_type: str) -> None:
    """选中某素材准备发出时调用，累加 use_count 并更新 last_used_at。出错不报。"""
    if not file_unique_id:
        return
    try:
        await execute(
            "UPDATE self_media_assets SET use_count = COALESCE(use_count,0) + 1, last_used_at = ? "
            "WHERE file_unique_id = ? AND media_type = ?",
            (_now_str(), file_unique_id, media_type),
        )
    except Exception as e:
        logger.warning("bump_media_use failed | err=%s", e)
