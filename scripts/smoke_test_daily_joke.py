"""Smoke test：每天一个笑话（daily joke）。

不联网；DB 用临时文件。

覆盖：
1)  config envs 就位（enabled / hour / minute / tz / source_mode / recipients）
2)  _is_due：只在精确 hh:mm 触发
3)  resolve_recipients：
    - owner 来自 OWNER_CHAT_IDS；beibei 来自 meta.xiaopang_chat_id + DAILY_JOKE_BEIBEI_CHAT_IDS
    - 去重 / 过滤空
4)  run_daily_joke_once：
    - 禁用时不发送、不调模型
    - 启用 + 第一次：拉 joke + 给所有接收人发 + 写 last_sent
    - 同日重复调用幂等（除非 force=True）
    - 强制 force=True 时即便今日已写也会再发（用于命令式触发）
5)  get_daily_joke：
    - ai 模式：调一次 AI 原创
    - mixed 抓到网络文案：调一次润色
    - mixed 抓不到：回落 AI
    - network 抓不到：返回 AI_FALLBACK 文案、不调 AI
6)  scheduler.start() 在 disabled 时不创建后台 task
7)  scheduler.stop() 优雅退出
8)  Business 不走调度器路径：调度器本身不与 business_connection_id 关联（默认 send_message 不带）
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


async def main() -> None:
    tmpdir = tempfile.mkdtemp(prefix="daily_joke_smoke_")
    db_path = os.path.join(tmpdir, "smoke.sqlite3")
    os.environ["BOT_DB_PATH"] = db_path
    # 强制启用并固定时间，方便断言
    os.environ["DAILY_JOKE_ENABLED"] = "1"
    os.environ["DAILY_JOKE_HOUR"] = "21"
    os.environ["DAILY_JOKE_MINUTE"] = "0"
    os.environ["DAILY_JOKE_TZ"] = "Asia/Hong_Kong"
    os.environ["DAILY_JOKE_SOURCE_MODE"] = "mixed"
    os.environ["DAILY_JOKE_RECIPIENTS"] = "owner,beibei"
    os.environ["DAILY_JOKE_NETWORK_URLS"] = ""
    os.environ["OWNER_CHAT_IDS"] = "1111,2222"
    os.environ["DAILY_JOKE_BEIBEI_CHAT_IDS"] = "9999"

    # 干净加载
    for mod in (
        "config", "db.core",
        "services.openai_service",
        "services.xiaopang_service",
        "services.joke_service",
        "services.daily_joke_scheduler",
    ):
        sys.modules.pop(mod, None)

    import config
    from db.core import init_db, close_db
    await init_db()

    # ---------- 1) config envs ----------
    assert config.DAILY_JOKE_ENABLED is True
    assert config.DAILY_JOKE_HOUR == 21 and config.DAILY_JOKE_MINUTE == 0
    assert config.DAILY_JOKE_TZ == "Asia/Hong_Kong"
    assert config.DAILY_JOKE_SOURCE_MODE == "mixed"
    assert config.DAILY_JOKE_RECIPIENTS == {"owner", "beibei"}
    assert "1111" in config.OWNER_CHAT_IDS and "2222" in config.OWNER_CHAT_IDS
    assert config.DAILY_JOKE_BEIBEI_CHAT_IDS == ["9999"]
    print("[ok] config envs：enabled / 21:00 / Asia/Hong_Kong / mixed / owner+beibei 全部就位")

    # ---------- 2) _is_due 精准触发 ----------
    from services.daily_joke_scheduler import _is_due
    tz = ZoneInfo("Asia/Hong_Kong")
    assert _is_due(datetime(2026, 5, 17, 21, 0, tzinfo=tz), 21, 0) is True
    assert _is_due(datetime(2026, 5, 17, 21, 1, tzinfo=tz), 21, 0) is False
    assert _is_due(datetime(2026, 5, 17, 20, 59, tzinfo=tz), 21, 0) is False
    assert _is_due(datetime(2026, 5, 17, 9, 0, tzinfo=tz), 9, 0) is True
    print("[ok] _is_due 只在精确 hh:mm 触发")

    # ---------- 3) resolve_recipients ----------
    from services.daily_joke_scheduler import resolve_recipients
    from services.xiaopang_service import meta_set

    # 先 meta 里设个贝贝 chat_id
    await meta_set("xiaopang_chat_id", "8888")
    recips = await resolve_recipients()
    assert "1111" in recips and "2222" in recips, f"应含 OWNER_CHAT_IDS: {recips}"
    assert "8888" in recips, f"应含 meta.xiaopang_chat_id: {recips}"
    assert "9999" in recips, f"应含 DAILY_JOKE_BEIBEI_CHAT_IDS: {recips}"
    # 去重
    assert len(recips) == len(set(recips))
    print(f"[ok] resolve_recipients：去重合并 owner(1111,2222) + beibei(meta 8888 + env 9999) = {recips}")

    # 只 owner
    only_owner = await resolve_recipients(recipients=["owner"])
    assert set(only_owner) == {"1111", "2222"}
    # 只 beibei
    only_bb = await resolve_recipients(recipients=["beibei"])
    assert set(only_bb) == {"8888", "9999"}
    # 未知关键字忽略
    none_r = await resolve_recipients(recipients=["nope"])
    assert none_r == []
    print("[ok] resolve_recipients：owner-only / beibei-only / 未知关键字 都正确")

    # ---------- 4) run_daily_joke_once：禁用时不发 ----------
    import services.daily_joke_scheduler as sched_mod
    bot = MagicMock()
    bot.send_message = AsyncMock()

    with patch.object(sched_mod, "DAILY_JOKE_ENABLED", False):
        res = await sched_mod.run_daily_joke_once(bot)
    assert res["sent"] == 0 and res["skipped"] is True
    assert bot.send_message.await_count == 0
    print("[ok] 禁用时 run_daily_joke_once 不调 bot.send_message")

    # ---------- 5) run_daily_joke_once：第一次发送 → 4 个接收人都收到 ----------
    bot.send_message.reset_mock()
    # 强制 joke 文案，避开真正网络/AI 调用
    with patch.object(sched_mod, "DAILY_JOKE_ENABLED", True), \
         patch("services.joke_service.fetch_joke_from_network", AsyncMock(return_value=None)), \
         patch("services.joke_service._ai_generate_joke", AsyncMock(return_value="这是一个测试段子")):
        res = await sched_mod.run_daily_joke_once(bot)
    assert res["sent"] == 4, f"应给 4 个接收人各发一条: {res}"
    assert bot.send_message.await_count == 4
    # 验证 send_message 调用都不带 business_connection_id
    for call in bot.send_message.await_args_list:
        assert "business_connection_id" not in call.kwargs, "调度器不应携带 business_connection_id"
    # 文案
    sent_texts = [c.kwargs.get("text", "") for c in bot.send_message.await_args_list]
    assert all("这是一个测试段子" in t for t in sent_texts)
    print("[ok] 启用 + 首发：bot.send_message 调用 4 次，全是普通私信、不带 business_connection_id")

    # ---------- 6) 同日幂等：第二次调用应 skipped ----------
    bot.send_message.reset_mock()
    with patch.object(sched_mod, "DAILY_JOKE_ENABLED", True), \
         patch("services.joke_service.fetch_joke_from_network", AsyncMock(return_value=None)), \
         patch("services.joke_service._ai_generate_joke", AsyncMock(return_value="不应该被发")):
        res2 = await sched_mod.run_daily_joke_once(bot)
    assert res2["skipped"] is True and res2["sent"] == 0, f"同日重复应 skip: {res2}"
    assert bot.send_message.await_count == 0
    print("[ok] 同日重复调用 run_daily_joke_once 幂等：sent=0、不调 send_message")

    # ---------- 7) force=True：跳过幂等，强制发一次 ----------
    bot.send_message.reset_mock()
    with patch.object(sched_mod, "DAILY_JOKE_ENABLED", True), \
         patch("services.joke_service.fetch_joke_from_network", AsyncMock(return_value=None)), \
         patch("services.joke_service._ai_generate_joke", AsyncMock(return_value="强制段子")):
        res3 = await sched_mod.run_daily_joke_once(bot, force=True)
    assert res3["sent"] == 4 and res3["skipped"] is False
    assert bot.send_message.await_count == 4
    print("[ok] force=True 跳过幂等，强制发送")

    # ---------- 8) get_daily_joke 行为：ai/mixed/network ----------
    import services.joke_service as joke_mod
    # ai：直接调 _ai_generate_joke，不去 fetch
    with patch.object(joke_mod, "fetch_joke_from_network", AsyncMock(side_effect=AssertionError("ai 模式不应抓取"))), \
         patch.object(joke_mod, "_ai_generate_joke", AsyncMock(return_value="AI 原创段子")):
        text = await joke_mod.get_daily_joke(source_mode="ai")
    assert "AI 原创段子" in text
    # mixed 抓到：润色后返回
    with patch.object(joke_mod, "fetch_joke_from_network", AsyncMock(return_value="原始素材，乱码符号//*&^")), \
         patch.object(joke_mod, "_polish_with_model", AsyncMock(return_value="润色后的段子")), \
         patch.object(joke_mod, "_ai_generate_joke", AsyncMock(side_effect=AssertionError("抓到时不应回落 AI"))):
        text = await joke_mod.get_daily_joke(source_mode="mixed")
    assert text == "润色后的段子"
    # mixed 抓不到：回落 AI
    with patch.object(joke_mod, "fetch_joke_from_network", AsyncMock(return_value=None)), \
         patch.object(joke_mod, "_ai_generate_joke", AsyncMock(return_value="兜底 AI 段子")):
        text = await joke_mod.get_daily_joke(source_mode="mixed")
    assert text == "兜底 AI 段子"
    # network 抓不到：返回 fallback，不调 AI
    with patch.object(joke_mod, "fetch_joke_from_network", AsyncMock(return_value=None)), \
         patch.object(joke_mod, "_ai_generate_joke", AsyncMock(side_effect=AssertionError("network 模式不应调 AI"))):
        text = await joke_mod.get_daily_joke(source_mode="network")
    assert text == joke_mod._AI_FALLBACK
    print("[ok] get_daily_joke：ai / mixed-fetch-成功 / mixed-fetch-失败 / network 路径全部正确")

    # ---------- 9) DailyJokeScheduler.start() disabled 时不创建 task ----------
    from services.daily_joke_scheduler import DailyJokeScheduler
    sched_disabled = DailyJokeScheduler(bot, enabled=False)
    sched_disabled.start()
    assert sched_disabled.is_running is False
    await sched_disabled.stop()
    print("[ok] DailyJokeScheduler.start() 在 disabled 时 noop，不创建 task")

    # ---------- 10) start() enabled 真创建 task + stop() 优雅退出 ----------
    sched_on = DailyJokeScheduler(bot, hour=21, minute=0, tz="Asia/Hong_Kong", enabled=True)
    sched_on.start()
    assert sched_on.is_running is True
    # 不让它真等到 21:00 —— 立刻 stop
    await sched_on.stop()
    assert sched_on.is_running is False
    print("[ok] DailyJokeScheduler.start() 真创建 task；stop() 优雅退出")

    # ---------- 11) 无接收人时不写 last_sent、不发 ----------
    # 重置 meta last_sent 与 owner/beibei 全清空
    await meta_set("daily_joke_last_sent", "")
    await meta_set("xiaopang_chat_id", "")
    bot.send_message.reset_mock()
    with patch.object(sched_mod, "DAILY_JOKE_ENABLED", True), \
         patch.object(sched_mod, "OWNER_CHAT_IDS", []), \
         patch.object(sched_mod, "DAILY_JOKE_BEIBEI_CHAT_IDS", []), \
         patch("services.joke_service.fetch_joke_from_network", AsyncMock(return_value=None)), \
         patch("services.joke_service._ai_generate_joke", AsyncMock(return_value="不应该被发出")):
        res4 = await sched_mod.run_daily_joke_once(bot, force=True)
    assert res4["sent"] == 0 and res4["skipped"] is True, f"无接收人应直接 skip: {res4}"
    assert bot.send_message.await_count == 0
    from services.xiaopang_service import meta_get
    assert (await meta_get("daily_joke_last_sent", "")) == "", "无接收人不应写 last_sent"
    print("[ok] 无接收人时 run_daily_joke_once 不发送、不写 last_sent")

    # ---------- 12) Business 不触发 daily joke：调度器与 business_connection_id 解耦 ----------
    # 这里不能真起 polling；仅验证 _send_joke_to_all 永远走 普通 send_message
    bot.send_message.reset_mock()
    sent = await sched_mod._send_joke_to_all(bot, "测试", ["1111", "2222"])
    assert sent == 2
    for call in bot.send_message.await_args_list:
        # 绝不传 business_connection_id
        assert "business_connection_id" not in call.kwargs
        # 是 chat_id 命名参数
        assert "chat_id" in call.kwargs
    print("[ok] _send_joke_to_all 不携带 business_connection_id（Business 路径隔离）")

    # ---------- 13) 任意 bot.send_message 失败也不会 crash 整个任务 ----------
    fail_bot = MagicMock()
    async def fake_send(chat_id, text, **kw):
        if chat_id == 1111:
            raise RuntimeError("forced send fail")
        return None
    fail_bot.send_message = AsyncMock(side_effect=fake_send)
    sent = await sched_mod._send_joke_to_all(fail_bot, "测试", ["1111", "2222"])
    assert sent == 1, "一个失败一个成功时返回成功数 1"
    print("[ok] 单个 send_message 失败不中断后续接收人")

    await close_db()
    try:
        os.remove(db_path)
    except Exception:
        pass
    print("\nALL DAILY JOKE SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
