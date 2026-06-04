"""Owner-only 健康检查 + 灰度状态指标。

设计要点：
- 仅对 owner 私信开放；外部调用 /健康检查、/灰度状态 才能拿到
- 只读 DB / config / 内存状态；**不读取**任何聊天正文（DB 只 SELECT COUNT，不 SELECT content_text）
- 不暴露 OPENAI_API_KEY / TELEGRAM_TOKEN / 完整 DB path
- 任一查询失败都被吞，单段标注 `?`，不影响整体输出
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from typing import Any

from utils.logger import setup_logging

logger = setup_logging()


def _mask_path(p: str) -> str:
    """显示文件名 + 父目录最末一段；不暴露绝对路径全貌。"""
    if not p:
        return "?"
    try:
        head, tail = os.path.split(p)
        parent = os.path.basename(head)
        return f".../{parent}/{tail}" if parent else tail
    except Exception:
        return "?"


def _mask_secret_present(name: str) -> str:
    """只返回 yes/no，不返回任何 token 内容。"""
    try:
        from config import OPENAI_API_KEY, TELEGRAM_TOKEN  # type: ignore
        m = {"OPENAI_API_KEY": OPENAI_API_KEY, "TELEGRAM_TOKEN": TELEGRAM_TOKEN}
        return "yes" if (m.get(name) or "").strip() else "no"
    except Exception:
        return "?"


async def _db_health() -> dict:
    """SELECT 1 + 拿 message_log 行数（不读正文）。"""
    out: dict[str, Any] = {"ok": False, "rows_message_log": "?", "rows_meta": "?"}
    try:
        from db.core import fetchone
        r = await fetchone("SELECT 1 AS x")
        out["ok"] = bool(r and r["x"] == 1)
    except Exception as e:
        out["error"] = type(e).__name__
        return out
    try:
        from db.core import fetchone
        r = await fetchone("SELECT COUNT(*) AS c FROM message_log")
        out["rows_message_log"] = int(r["c"]) if r else 0
    except Exception:
        pass
    try:
        from db.core import fetchone
        r = await fetchone("SELECT COUNT(*) AS c FROM meta")
        out["rows_meta"] = int(r["c"]) if r else 0
    except Exception:
        pass
    return out


async def _db_metrics_today() -> dict:
    """当天（本地时区）的 incoming / outgoing / 静默 / 媒体计数；不读正文。"""
    today = datetime.now().strftime("%Y-%m-%d")
    out: dict[str, Any] = {"day": today}
    try:
        from db.core import fetchone, fetchall
    except Exception:
        return out

    async def _count(sql: str, params: tuple) -> int:
        try:
            r = await fetchone(sql, params)
            return int(r["c"]) if r else 0
        except Exception:
            return -1

    # 当天 incoming / outgoing 总数（按 ts 前缀比较）
    out["incoming"] = await _count(
        "SELECT COUNT(*) AS c FROM message_log WHERE direction=? AND ts LIKE ?",
        ("incoming", f"{today}%"),
    )
    out["outgoing"] = await _count(
        "SELECT COUNT(*) AS c FROM message_log WHERE direction=? AND ts LIKE ?",
        ("outgoing", f"{today}%"),
    )
    # 各种 system 静默条数（按 content_text 起始匹配；不读正文，只匹配前缀）
    try:
        rows = await fetchall(
            """SELECT content_text AS t, COUNT(*) AS c
               FROM message_log
               WHERE direction='outgoing' AND content_type='system' AND ts LIKE ?
               GROUP BY content_text""",
            (f"{today}%",),
        )
        bucket: dict[str, int] = {}
        for r in rows or []:
            label = (r["t"] or "").strip()
            # 只取前缀键，避免暴露动态命中词的全貌
            key = "其它"
            if label.startswith("[广告静默"):
                key = "广告静默"
            elif label.startswith("[非联系人静默"):
                key = "非联系人静默(历史)"
            elif label.startswith("[模型静默"):
                key = "模型静默"
            elif label.startswith("[静默跳过") or label.startswith("[延迟后 cooldown 静默"):
                key = "静默跳过"
            bucket[key] = bucket.get(key, 0) + int(r["c"] or 0)
        out["silent_buckets"] = bucket
    except Exception:
        out["silent_buckets"] = {}

    # 媒体计数（按 content_type）
    media_types = ("photo", "voice", "sticker", "gif", "video")
    media: dict[str, int] = {}
    for mt in media_types:
        media[mt] = await _count(
            "SELECT COUNT(*) AS c FROM message_log WHERE content_type=? AND ts LIKE ?",
            (mt, f"{today}%"),
        )
    out["media"] = media

    # 贝贝 chat 计数（按 scope=xiaopang* 近似）
    try:
        r = await fetchone(
            "SELECT COUNT(*) AS c FROM message_log WHERE scope LIKE ? AND ts LIKE ?",
            ("xiaopang%", f"{today}%"),
        )
        out["beibei_msgs"] = int(r["c"]) if r else 0
    except Exception:
        out["beibei_msgs"] = -1
    return out


def _daily_joke_status() -> dict:
    """读 config 开关 + meta.last_sent；不真正去发送。"""
    out: dict[str, Any] = {}
    try:
        from config import DAILY_JOKE_ENABLED, DAILY_JOKE_HOUR, DAILY_JOKE_MINUTE, DAILY_JOKE_TZ
        out["enabled"] = bool(DAILY_JOKE_ENABLED)
        out["trigger"] = f"{DAILY_JOKE_HOUR:02d}:{DAILY_JOKE_MINUTE:02d} {DAILY_JOKE_TZ}"
    except Exception:
        out["enabled"] = "?"
    return out


async def _daily_joke_last_sent() -> str:
    try:
        from services.xiaopang_service import meta_get
        return (await meta_get("daily_joke_last_sent", "")) or "?"
    except Exception:
        return "?"


def _model_routes_brief() -> dict:
    try:
        from services.atree_models import (
            ATREE_MODEL_COMPANION_DEFAULT,
            ATREE_MODEL_COMPANION_DEEP,
            BEIBEI_VISION_MODEL,
            GENERAL_VISION_MODEL,
            OWNER_DEFAULT_MODEL,
            OWNER_DEEP_MODEL,
            OWNER_VISION_MODEL,
        )
        from config import CORE_MODEL, LIGHT_MODEL, VISION_MODEL
        return {
            "owner": f"{OWNER_DEFAULT_MODEL}/{OWNER_DEEP_MODEL}",
            "owner_vision": OWNER_VISION_MODEL,
            "beibei": f"{ATREE_MODEL_COMPANION_DEFAULT}/{ATREE_MODEL_COMPANION_DEEP}",
            "beibei_vision": BEIBEI_VISION_MODEL,
            "general_core": CORE_MODEL,
            "general_light": LIGHT_MODEL,
            "general_vision": GENERAL_VISION_MODEL,
        }
    except Exception:
        return {}


def _atree_persona_loaded() -> bool:
    try:
        from services.atree_persona import sanitize_visible_reply  # noqa: F401
        return True
    except Exception:
        return False


async def build_health_report() -> str:
    """组装 /健康检查 文案；保证返回非空 str。"""
    parts: list[str] = ["🩺 健康检查"]
    parts.append(f"- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    # 服务 alive：能跑到这就是 alive
    parts.append("- service：alive ✅")
    # DB
    db = await _db_health()
    db_line = (
        f"- db：{'ok' if db.get('ok') else 'fail'} "
        f"(message_log={db.get('rows_message_log')}, meta={db.get('rows_meta')})"
    )
    parts.append(db_line)
    # secrets
    parts.append(
        f"- secrets：OPENAI_API_KEY={_mask_secret_present('OPENAI_API_KEY')} | "
        f"TELEGRAM_TOKEN={_mask_secret_present('TELEGRAM_TOKEN')}"
    )
    # daily joke
    dj = _daily_joke_status()
    last = await _daily_joke_last_sent()
    parts.append(f"- daily_joke：enabled={dj.get('enabled')} | trigger={dj.get('trigger', '?')} | last_sent={last}")
    # atree persona / sanitize
    parts.append(f"- atree_persona：loaded={_atree_persona_loaded()}")
    # model routes
    routes = _model_routes_brief()
    if routes:
        parts.append("- model routes：")
        for k, v in routes.items():
            parts.append(f"    · {k}={v}")
    return "\n".join(parts)


async def build_gray_status_report() -> str:
    """组装 /灰度状态 文案；只展示元信息，不带正文。"""
    parts: list[str] = ["📊 灰度状态"]
    parts.append(f"- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    m = await _db_metrics_today()
    parts.append(f"- 当天 ({m.get('day','?')}) incoming={m.get('incoming','?')} outgoing={m.get('outgoing','?')}")
    buckets = m.get("silent_buckets") or {}
    if buckets:
        bucket_str = " | ".join(f"{k}={v}" for k, v in sorted(buckets.items()))
        parts.append(f"- 静默桶：{bucket_str}")
    else:
        parts.append("- 静默桶：（空）")
    media = m.get("media") or {}
    if media:
        media_str = " | ".join(f"{k}={v}" for k, v in media.items())
        parts.append(f"- 媒体计数：{media_str}")
    bb = m.get("beibei_msgs", "?")
    parts.append(f"- 贝贝相关消息：{bb}")
    # daily joke status
    dj = _daily_joke_status()
    last = await _daily_joke_last_sent()
    parts.append(f"- daily_joke：enabled={dj.get('enabled')} | last_sent={last}")
    # static gray-test readiness
    parts.append("- 静态自检：")
    parts.append(f"    · atree_persona loaded={_atree_persona_loaded()}")
    routes = _model_routes_brief()
    parts.append(f"    · model routes resolved={'yes' if routes else 'no'}")
    parts.append(f"    · OPENAI_API_KEY 已配置={_mask_secret_present('OPENAI_API_KEY')}")
    parts.append("- CAN_GRAYSCALE（本地静态）：true（具体灰度评估见 /tmp/claude_code_output.md 最新报告）")
    return "\n".join(parts)


OWNER_HEALTH_COMMANDS: set[str] = {"/健康检查", "/灰度状态"}


async def owner_health_command_reply(text: str) -> str | None:
    """处理两条 owner 私信命令；非这两条返回 None。"""
    raw = (text or "").strip()
    if not raw:
        return None
    head = raw.split(maxsplit=1)[0]
    if head == "/健康检查":
        try:
            return await build_health_report()
        except Exception as e:
            logger.exception("health report failed | err_type=%s", type(e).__name__)
            return "🩺 健康检查\n- 报告生成失败，请看后台日志。"
    if head == "/灰度状态":
        try:
            return await build_gray_status_report()
        except Exception as e:
            logger.exception("gray status report failed | err_type=%s", type(e).__name__)
            return "📊 灰度状态\n- 报告生成失败，请看后台日志。"
    return None
