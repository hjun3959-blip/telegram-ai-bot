"""每天一个笑话（daily joke）调度器。

设计要点：
- 启动时立刻拉起后台 asyncio.Task，不阻塞 polling
- 每分钟 wake 一次，在目标 hour:minute 命中且当天还没发过时，触发一次发送
- 幂等：使用 meta 表的 daily_joke_last_sent 字段记 YYYY-MM-DD（按目标时区算），重复进程
  在同一天不会重发
- 接收人：
  * "owner" → OWNER_CHAT_IDS（来自 config，已去重）
  * "beibei" → meta.xiaopang_chat_id（如有）+ config.DAILY_JOKE_BEIBEI_CHAT_IDS（env 兜底）
- 任何环节出错都吞掉，写 logger.warning/exception，不让定时任务把进程拖垮
- 优雅停机：暴露 stop()；外部可以 await scheduler.stop()
- 严格隔离 Business：发送时不携带 business_connection_id；只走普通 send_message
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

from aiogram import Bot

from config import (
    DAILY_JOKE_BEIBEI_CHAT_IDS,
    DAILY_JOKE_ENABLED,
    DAILY_JOKE_HOUR,
    DAILY_JOKE_MINUTE,
    DAILY_JOKE_RECIPIENTS,
    DAILY_JOKE_TZ,
    OWNER_CHAT_IDS,
)
from utils.logger import setup_logging

logger = setup_logging()


# meta key 命名空间
_META_LAST_SENT_KEY = "daily_joke_last_sent"  # value: YYYY-MM-DD（目标时区）


def _now_in_tz(tz_name: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        # 时区名错配时回落到 UTC（最坏情况下时间不准，但不会 crash）
        logger.warning("daily joke: invalid tz %r, falling back to UTC", tz_name)
        return datetime.now(ZoneInfo("UTC"))


def _today_str_in_tz(tz_name: str) -> str:
    return _now_in_tz(tz_name).strftime("%Y-%m-%d")


def _is_due(now: datetime, hour: int, minute: int) -> bool:
    """命中触发分钟（精确到 hh:mm）。每分钟轮询一次时只需检查这一分钟。"""
    return now.hour == hour and now.minute == minute


async def _read_last_sent_day() -> str:
    """从 meta 读上次发送日期，失败返空串。"""
    try:
        from services.xiaopang_service import meta_get
        return await meta_get(_META_LAST_SENT_KEY, "")
    except Exception as e:
        logger.warning("daily joke: read meta last_sent failed | err=%s", e)
        return ""


async def _write_last_sent_day(day: str) -> None:
    try:
        from services.xiaopang_service import meta_set
        await meta_set(_META_LAST_SENT_KEY, day)
    except Exception as e:
        logger.warning("daily joke: write meta last_sent failed | err=%s", e)


async def _resolve_beibei_chat_ids() -> list[str]:
    """解析贝贝接收人列表：meta.xiaopang_chat_id + config 兜底。去重，过滤空串。"""
    ids: list[str] = []
    try:
        from services.xiaopang_service import meta_get
        xp = (await meta_get("xiaopang_chat_id", "")).strip()
        if xp:
            ids.append(xp)
    except Exception as e:
        logger.warning("daily joke: read xiaopang_chat_id failed | err=%s", e)
    for x in (DAILY_JOKE_BEIBEI_CHAT_IDS or []):
        sx = (x or "").strip()
        if sx and sx not in ids:
            ids.append(sx)
    return ids


async def resolve_recipients(
    *,
    recipients: Iterable[str] | None = None,
) -> list[str]:
    """根据配置返回最终去重的 chat_id 字符串列表。

    recipients 取值集合的元素：
      - "owner"  → OWNER_CHAT_IDS
      - "beibei" → meta.xiaopang_chat_id + DAILY_JOKE_BEIBEI_CHAT_IDS
    任何未知关键字忽略。
    """
    keys = {k.strip().lower() for k in (recipients or DAILY_JOKE_RECIPIENTS) if k}
    out: list[str] = []
    if "owner" in keys:
        for cid in OWNER_CHAT_IDS or []:
            s = str(cid).strip()
            if s and s not in out:
                out.append(s)
    if "beibei" in keys:
        for cid in await _resolve_beibei_chat_ids():
            if cid and cid not in out:
                out.append(cid)
    return out


_SAFE_FALLBACK_JOKE_FOR_BEIBEI = "嗯，今天的小段子被我吞了。明天再讲给你听。"


def _sanitize_joke_for_beibei(text: str) -> str:
    """贝贝侧笑话出站前过 sanitize_visible_reply（句数 / 字数 / 后台词 / 承诺词）。

    放宽 max_sentences=4 / max_chars=200，避免一段长笑话被硬截到「嗯，我在。」。
    若 sanitize 兜底掉到默认安全短句，则换成专属笑话兜底，避免每天发同一句「嗯，我在。」。
    """
    try:
        from services.atree_persona import sanitize_visible_reply
        cleaned = sanitize_visible_reply(text or "", max_sentences=4, max_chars=200)
        if not cleaned or cleaned.strip() == "嗯，我在。":
            return _SAFE_FALLBACK_JOKE_FOR_BEIBEI
        return cleaned
    except Exception:
        return _SAFE_FALLBACK_JOKE_FOR_BEIBEI


def _hit_redline_terms_in_joke(text: str) -> list[str]:
    """命中后台/承诺词，返回去重词表（不写正文）。"""
    if not text:
        return []
    try:
        from services.atree_persona import (
            ATREE_COMMITMENT_FORBIDDEN_WORDS,
            ATREE_VISIBLE_FORBIDDEN_WORDS,
        )
        terms = tuple(set(ATREE_VISIBLE_FORBIDDEN_WORDS) | set(ATREE_COMMITMENT_FORBIDDEN_WORDS))
        return sorted({w for w in terms if w and w in text})
    except Exception:
        return []


async def _maybe_alert_owner_redline(
    bot: Bot, *, hits: list[str], chat_id: str, scene: str
) -> None:
    """命中红线时尽力给 owner 一条状态通报；不写正文；失败被吞。"""
    if not hits:
        return
    try:
        from services.alert_service import dedup_alert
        key = f"daily_joke_redline:{scene}:{chat_id}:{','.join(hits)[:60]}"
        notice = (
            f"每日笑话出站红线命中（{scene}）\n"
            f"命中词：{', '.join(hits)}\n"
            f"已替换为兜底文案；这是状态通报。"
        )
        await dedup_alert(bot, key, notice)
    except Exception:
        try:
            logger.warning(
                "daily joke redline alert failed | scene=%s | chat_hash=%s | hit_count=%d",
                scene, chat_id, len(hits),
            )
        except Exception:
            pass


async def _send_joke_to_all(bot: Bot, text: str, chat_ids: list[str]) -> int:
    """逐个发送；每个发送都独立 try。返回成功数。

    - 隔离 Business：只用 bot.send_message(chat_id, text)，不传 business_connection_id。
    - 贝贝/小胖侧（在 DAILY_JOKE_BEIBEI 名单内的 chat_id）发送前**必须**过 sanitize；
      命中红线时还会尝试给 owner 一条状态通报，**不写正文**。
    - 普通用户 / owner 路径保持原样（owner 看到原话是设计意图）。
    """
    sent = 0
    try:
        beibei_ids = set(await _resolve_beibei_chat_ids())
    except Exception:
        beibei_ids = set()
    for cid in chat_ids:
        try:
            if cid in beibei_ids:
                hits = _hit_redline_terms_in_joke(text)
                safe_text = _sanitize_joke_for_beibei(text)
                if hits:
                    # 尽力告警；失败不影响发送
                    try:
                        await _maybe_alert_owner_redline(
                            bot, hits=hits, chat_id=str(cid), scene="daily_joke",
                        )
                    except Exception:
                        pass
                await bot.send_message(chat_id=int(cid), text=safe_text)
            else:
                await bot.send_message(chat_id=int(cid), text=text)
            sent += 1
        except Exception as e:
            logger.warning("daily joke send failed | chat_id=%s | err=%s", cid, e)
    return sent


async def run_daily_joke_once(
    bot: Bot,
    *,
    force: bool = False,
    recipients: Iterable[str] | None = None,
) -> dict:
    """触发一次发送（不判时间，由调用方决定是否发）。

    force=True 时跳过幂等检查；force=False 时如今日已发送，返回 {sent: 0, skipped: True}。
    返回 dict：{"ok": bool, "sent": int, "skipped": bool, "day": str, "text": str|None}
    """
    if not DAILY_JOKE_ENABLED:
        logger.info("daily joke: disabled by config, skip")
        return {"ok": False, "sent": 0, "skipped": True, "day": "", "text": None}

    day = _today_str_in_tz(DAILY_JOKE_TZ)
    if not force:
        last = await _read_last_sent_day()
        if last == day:
            logger.info("daily joke: already sent today (%s), skip", day)
            return {"ok": True, "sent": 0, "skipped": True, "day": day, "text": None}

    # 解析接收人；空就别发
    ids = await resolve_recipients(recipients=recipients)
    if not ids:
        logger.info("daily joke: no recipients resolved, skip")
        return {"ok": False, "sent": 0, "skipped": True, "day": day, "text": None}

    # 取 joke 文案
    try:
        from services.joke_service import get_daily_joke
        text = await get_daily_joke()
    except Exception as e:
        logger.exception("daily joke: get_daily_joke crashed | err=%s", e)
        return {"ok": False, "sent": 0, "skipped": False, "day": day, "text": None}

    sent = await _send_joke_to_all(bot, text, ids)
    if sent > 0 and not force:
        # 至少送出一条才算「今天发过了」，避免无接收人时也写入 last_sent
        await _write_last_sent_day(day)
    logger.info("daily joke sent | day=%s | sent=%d/%d | tz=%s", day, sent, len(ids), DAILY_JOKE_TZ)
    return {"ok": sent > 0, "sent": sent, "skipped": False, "day": day, "text": text}


class DailyJokeScheduler:
    """后台循环：每分钟检查一次是否到点。

    用法：
        sched = DailyJokeScheduler(bot)
        sched.start()
        ...
        await sched.stop()

    内部 task 完全在 event loop 上，不创建线程；shutdown 时取消 task 并等其退出。
    """

    def __init__(
        self,
        bot: Bot,
        *,
        hour: int | None = None,
        minute: int | None = None,
        tz: str | None = None,
        enabled: bool | None = None,
    ) -> None:
        self._bot = bot
        self._hour = DAILY_JOKE_HOUR if hour is None else hour
        self._minute = DAILY_JOKE_MINUTE if minute is None else minute
        self._tz = (tz or DAILY_JOKE_TZ or "Asia/Hong_Kong")
        self._enabled = DAILY_JOKE_ENABLED if enabled is None else enabled
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self._enabled:
            logger.info("daily joke scheduler: disabled, not starting")
            return
        if self.is_running:
            logger.info("daily joke scheduler: already running")
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="daily-joke-scheduler")
        logger.info(
            "daily joke scheduler started | trigger=%02d:%02d | tz=%s",
            self._hour, self._minute, self._tz,
        )

    async def stop(self) -> None:
        if not self._task:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None
        logger.info("daily joke scheduler stopped")

    async def _sleep_until_next_minute_boundary(self) -> None:
        """睡到下一分钟的 0 秒附近（最多 60 秒），由 _stop_event 提前打断。"""
        now = _now_in_tz(self._tz)
        # 下一个整分
        next_minute = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
        delay = max(1.0, (next_minute - now).total_seconds())
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    async def _tick(self) -> None:
        """每分钟检查一次。命中时间且今天没发就发一次。"""
        now = _now_in_tz(self._tz)
        if not _is_due(now, self._hour, self._minute):
            return
        try:
            await run_daily_joke_once(self._bot)
        except Exception as e:
            logger.exception("daily joke tick crashed | err=%s", e)

    async def _run_loop(self) -> None:
        # 启动后先对齐分钟边界，避免首分钟漂移多发
        try:
            await self._sleep_until_next_minute_boundary()
            while not self._stop_event.is_set():
                await self._tick()
                await self._sleep_until_next_minute_boundary()
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.exception("daily joke scheduler loop crashed | err=%s", e)
