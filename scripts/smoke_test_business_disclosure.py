"""普通用户 Business 机器人助手身份披露 + 紧急关键词告警 smoke。

不联网。验证：
- BUSINESS_SYSTEM_PROMPT 含『机器人助手 / AI 助手』身份披露段
- BUSINESS_SYSTEM_PROMPT 含紧急关键词处理指引（紧急 / 叫醒 / 急事 / 反复提醒）
- ATREE_SYSTEM_PROMPT 明确禁止暴露『机器人 / 系统 / AI』
- sanitize_visible_reply 仍能在贝贝侧把模型输出的『我是阿君的机器人助手』洗掉
- 普通用户业务窗口收到紧急关键词 → 触发 dedup_alert（不抛、不阻断后续 LLM）

跑法：python3 scripts/smoke_test_business_disclosure.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _ok(name, cond, detail=""):
    print(f"[{'OK' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        sys.exit(1)


def test_prompt_disclosure():
    import config
    p = config.BUSINESS_SYSTEM_PROMPT
    # 身份披露段
    _ok("BUSINESS_SYSTEM_PROMPT 含『机器人助手』或『AI 助手』",
        ("机器人助手" in p) or ("AI 助手" in p) or ("AI助手" in p))
    _ok("披露段说『暂时不在 / 我先帮他接』",
        "暂时不在" in p or "先帮他接" in p)
    _ok("仍要求不冒充阿君本人",
        "不要冒充" in p or "不冒充" in p)
    # 紧急关键词处理
    _ok("含『紧急 / 急事 / 叫醒阿君』指引", "紧急" in p and "叫醒阿君" in p)
    _ok("含『反复提醒 / 多发几条』话术", "反复" in p or "多发几条" in p)
    _ok("不夸大承诺打电话", "打电话" in p)  # 出现在『不要承诺打电话』里
    _ok("含『继续说清楚事情』话术", "说清楚" in p)


def test_atree_overrides_robot_disclosure():
    from services.atree_persona import ATREE_SYSTEM_PROMPT, ATREE_VISIBLE_FORBIDDEN_WORDS
    # ATREE_SYSTEM_PROMPT 明确禁词
    _ok("ATREE_SYSTEM_PROMPT 含『不许出现』+ 机器人", "不许出现" in ATREE_SYSTEM_PROMPT and "机器人" in ATREE_SYSTEM_PROMPT)
    # forbidden 词表里有机器人 / 系统 / AI
    _ok("forbidden 词含『机器人』", "机器人" in ATREE_VISIBLE_FORBIDDEN_WORDS)
    _ok("forbidden 词含『系统』", "系统" in ATREE_VISIBLE_FORBIDDEN_WORDS)
    _ok("forbidden 词含『AI』", "AI" in ATREE_VISIBLE_FORBIDDEN_WORDS)


def test_sanitize_scrubs_robot_for_beibei():
    from services.atree_persona import sanitize_visible_reply
    bad = "我是阿君的机器人助手，他暂时不在。"
    out = sanitize_visible_reply(bad)
    # 命中后台词应直接走硬兜底
    _ok("贝贝侧『机器人助手』被 sanitize 兜底", out == "嗯，我在。")


async def test_urgent_alert_fires_for_ordinary_user():
    """普通用户业务窗口发紧急关键词 → 触发 dedup_alert；不抛；不阻断后续 LLM。"""
    # 隔离 import 重新加载 router 模块
    for mod in (
        "config", "db.core",
        "services.openai_service",
        "services.contact_service",
        "services.context_service",
        "services.message_service",
        "services.alert_service",
        "services.atree_keyword_trigger",
        "services.atree_owner_alert",
        "services.atree_persona",
        "services.xiaopang_service",
        "routers.business",
    ):
        sys.modules.pop(mod, None)

    import config  # noqa: F401
    # 不真正 init_db；store_message 等 DB 写都被 mock 掉。

    import routers.business as biz

    sent_alerts: list[tuple[str, str]] = []
    async def fake_dedup(b, key, t):
        sent_alerts.append((key, t))

    call_log: list[tuple] = []
    async def fake_call(messages, model, mode, response_json=True, **_kw):
        call_log.append((model, mode))
        return {"reply_text": "我先记下来，让阿君上来看。", "sticker_type": None,
                "should_reply": True, "risk_note": ""}

    msg = SimpleNamespace(
        chat=SimpleNamespace(id=88001, type="private"),
        from_user=SimpleNamespace(id=88001, username="some_friend", is_bot=False),
        business_connection_id="bc-x",
        sender_business_bot=None,
        text="紧急！能不能叫醒阿君，我现在就要找他",
        photo=None, sticker=None, animation=None, voice=None, video=None,
        caption=None, message_id=5001,
    )

    with patch.object(biz, "should_skip_message", lambda m: False), \
         patch.object(biz, "get_chat_mode", lambda m: "business"), \
         patch.object(biz, "is_self_message", lambda m: False), \
         patch.object(biz, "is_in_self_silence", lambda m: False), \
         patch.object(biz, "is_in_owner_cooldown", lambda m: False), \
         patch.object(biz, "is_xiaopang", AsyncMock(return_value=False)), \
         patch.object(biz, "ad_keyword_hit", lambda t: None), \
         patch.object(biz, "store_message", AsyncMock()), \
         patch.object(biz, "build_system_prompt_with_xiaopang", AsyncMock(return_value="SYS")), \
         patch.object(biz, "send_chat_action_safe", AsyncMock()), \
         patch.object(biz, "human_typing_delay", AsyncMock()), \
         patch.object(biz, "send_reply", AsyncMock()), \
         patch.object(biz, "get_history", lambda uid: []), \
         patch.object(biz, "save_history", lambda uid, t, r: None), \
         patch.object(biz, "dedup_alert", side_effect=fake_dedup), \
         patch.object(biz, "call_openai", side_effect=fake_call):
        await biz.text_handler(msg, MagicMock(send_message=AsyncMock()))

    _ok("普通用户紧急关键词 → 触发 dedup_alert", len(sent_alerts) >= 1)
    found_urgent = any(
        ("urgent:" in k) and ("紧急" in t or "叫醒" in t)
        for k, t in sent_alerts
    )
    _ok("alert key 含 urgent: 前缀且文案中性", found_urgent, f"alerts={sent_alerts}")
    _ok("LLM 仍被调用（紧急不阻断回复）", len(call_log) >= 1)
    # alert 文本只截 200 字摘要
    for _, t in sent_alerts:
        _ok("告警文本不超过 600 字", len(t) <= 600)


async def test_no_urgent_alert_for_beibei():
    for mod in (
        "config", "db.core",
        "services.openai_service",
        "services.contact_service",
        "services.context_service",
        "services.message_service",
        "services.alert_service",
        "services.atree_keyword_trigger",
        "services.atree_owner_alert",
        "services.atree_persona",
        "services.xiaopang_service",
        "routers.business",
    ):
        sys.modules.pop(mod, None)

    import config  # noqa: F401
    from db.core import init_db
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="biz_urg_bb_")
    os.environ["BOT_DB_PATH"] = os.path.join(tmpdir, "db.sqlite3")
    await init_db()

    import routers.business as biz

    sent_alerts: list[tuple[str, str]] = []
    async def fake_dedup(b, key, t):
        sent_alerts.append((key, t))

    async def fake_call(messages, model, mode, response_json=True, **_kw):
        return {"reply_text": "嗯，我在。", "sticker_type": None,
                "should_reply": True, "risk_note": ""}

    msg = SimpleNamespace(
        chat=SimpleNamespace(id=42, type="private"),
        from_user=SimpleNamespace(id=42, username="yj_syj", is_bot=False),
        business_connection_id="bc-x",
        sender_business_bot=None,
        text="紧急！",  # 贝贝侧不触发普通用户 urgent 分支
        photo=None, sticker=None, animation=None, voice=None, video=None,
        caption=None, message_id=5002,
    )

    with patch.object(biz, "should_skip_message", lambda m: False), \
         patch.object(biz, "get_chat_mode", lambda m: "business"), \
         patch.object(biz, "is_self_message", lambda m: False), \
         patch.object(biz, "is_in_self_silence", lambda m: False), \
         patch.object(biz, "is_in_owner_cooldown", lambda m: False), \
         patch.object(biz, "is_xiaopang", AsyncMock(return_value=True)), \
         patch.object(biz, "ad_keyword_hit", lambda t: None), \
         patch.object(biz, "store_message", AsyncMock()), \
         patch.object(biz, "build_system_prompt_with_xiaopang", AsyncMock(return_value="SYS")), \
         patch.object(biz, "send_chat_action_safe", AsyncMock()), \
         patch.object(biz, "human_typing_delay", AsyncMock()), \
         patch.object(biz, "send_reply", AsyncMock()), \
         patch.object(biz, "get_history", lambda uid: []), \
         patch.object(biz, "save_history", lambda uid, t, r: None), \
         patch.object(biz, "dedup_alert", side_effect=fake_dedup), \
         patch.object(biz, "maybe_hit_xiaopang_reminders", AsyncMock()), \
         patch.object(biz, "xiaopang_scope", AsyncMock(return_value="xiaopang")), \
         patch.object(biz, "risk_check_and_alert", AsyncMock()), \
         patch.object(biz, "call_openai", side_effect=fake_call):
        await biz.text_handler(msg, MagicMock(send_message=AsyncMock()))

    # 贝贝侧只走阿树通道；不应触发『普通用户 urgent』分支的 urgent: 前缀告警
    urgent_keys = [k for k, _ in sent_alerts if k.startswith("urgent:")]
    _ok("贝贝侧不触发 urgent: 告警", len(urgent_keys) == 0, f"keys={urgent_keys}")


async def _async_main():
    test_prompt_disclosure()
    test_atree_overrides_robot_disclosure()
    test_sanitize_scrubs_robot_for_beibei()
    await test_urgent_alert_fires_for_ordinary_user()
    await test_no_urgent_alert_for_beibei()
    # 关 DB（init_db 在 import 时被间接拉起；不关会留 aiosqlite 后台任务）
    try:
        from db.core import close_db
        await close_db()
    except Exception:
        pass
    print("ALL BUSINESS DISCLOSURE SMOKE OK")


if __name__ == "__main__":
    asyncio.run(_async_main())
