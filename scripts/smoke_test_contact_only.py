"""Smoke test：Business 联系人白名单是「辅助标注」，不是「硬性拦截」。

行为变更（本次）：
- Business 不再做「非联系人 → 直接静默」。是否回复交给模型（BUSINESS_SYSTEM_PROMPT 已加陌生人/广告判别规则）。
- 广告关键词仍优先静默 + 给 owner 告警。
- 阿君自发消息、owner cooldown、self-silence、阿树系统继续生效。

覆盖：
1) config 暴露 CONTACT_USERNAMES / CONTACT_USER_IDS，默认带贝贝三账号
2) contact_service.is_contact 判定仍保留：贝贝/env/meta 命中 True；陌生人 False
3) add_contact / remove_contact / list_contacts_text 正常工作并持久化到 meta 表
4) routers.business.text_handler 在「非白名单普通文本」时**会进入模型流程**（不再硬静默）
5) routers.business.text_handler 在「广告关键词命中」时仍不调模型，记 [广告静默] + dedup_alert
6) 贝贝消息能继续走完代聊流程（不会被静默挡住）
7) 私信路由不受影响：is_contact 只是模块级 helper，private text_handler 没引入这个分支
8) /play /help 文案不暴露 /联系人列表 /添加联系人 /删除联系人 这些隐藏命令
9) CONTACT_OWNER_COMMANDS 集合保留正确命令名
10) BUSINESS_SYSTEM_PROMPT 含陌生人/广告判别规则

不联网，不真发；DB 用临时文件。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _fake_business_message(
    *,
    chat_id: int | None = None,
    user_id: int = 9001,
    username: str | None = "stranger",
    text: str = "hi",
    conn_id: str = "bc-x",
    is_bot: bool = False,
) -> SimpleNamespace:
    # business 私聊里 chat.id 等于对方 user.id；不等会被 is_self_message 抢先误判 self
    if chat_id is None:
        chat_id = user_id
    chat = SimpleNamespace(id=chat_id, type="private")
    from_user = SimpleNamespace(id=user_id, username=username, is_bot=is_bot)
    return SimpleNamespace(
        chat=chat,
        from_user=from_user,
        business_connection_id=conn_id,
        sender_business_bot=None,
        text=text,
        sticker=None,
        animation=None,
        photo=None,
        voice=None,
        video=None,
        caption=None,
        message_id=42,
    )


def _fake_private_message(user_id: int = 1001) -> SimpleNamespace:
    chat = SimpleNamespace(id=user_id, type="private")
    from_user = SimpleNamespace(id=user_id, username="some_random", is_bot=False)
    return SimpleNamespace(
        chat=chat,
        from_user=from_user,
        business_connection_id=None,
        sender_business_bot=None,
        text="hello",
        sticker=None,
        animation=None,
        photo=None,
        voice=None,
        video=None,
        caption=None,
        message_id=11,
    )


async def main() -> None:
    tmpdir = tempfile.mkdtemp(prefix="contact_smoke_")
    db_path = os.path.join(tmpdir, "smoke.sqlite3")
    os.environ["BOT_DB_PATH"] = db_path
    # 在 import config 前预置一个 env 白名单：username=envfriend, user_id=12345
    os.environ["CONTACT_USERNAMES"] = "envfriend"
    os.environ["CONTACT_USER_IDS"] = "12345"

    # 清缓存，确保新 env 被读进来
    for mod in (
        "config",
        "db.core",
        "services.contact_service",
        "services.context_service",
        "services.message_service",
        "services.xiaopang_service",
        "routers.business",
        "routers.media",
        "routers.private",
    ):
        sys.modules.pop(mod, None)

    import config
    from db.core import init_db, close_db, fetchone

    await init_db()

    # ---------- 1) config 暴露 ----------
    assert hasattr(config, "CONTACT_USERNAMES"), "缺 config.CONTACT_USERNAMES"
    assert hasattr(config, "CONTACT_USER_IDS"), "缺 config.CONTACT_USER_IDS"
    # 默认贝贝三账号仍在 env 默认值；这里被 env CONTACT_USERNAMES=envfriend 覆盖
    # 所以贝贝默认走 contact_service 的硬编码兜底
    assert "envfriend" in config.CONTACT_USERNAMES
    assert "12345" in config.CONTACT_USER_IDS
    print("[ok] config CONTACT_USERNAMES / CONTACT_USER_IDS 字段就位且 env 生效")

    from services.contact_service import (
        is_contact,
        add_contact,
        remove_contact,
        list_contacts_text,
        owner_contact_command_reply,
        CONTACT_OWNER_COMMANDS,
    )

    # ---------- 2) is_contact 判定 ----------
    # 贝贝（默认硬编码兜底，即便 env 覆盖也保留）
    beibei = _fake_business_message(user_id=42, username="yj_syj")
    assert await is_contact(beibei) is True, "贝贝必须默认是联系人"
    beibei2 = _fake_business_message(user_id=43, username="i_q772")
    assert await is_contact(beibei2) is True
    beibei3 = _fake_business_message(user_id=44, username="Zp7987")  # 大小写
    assert await is_contact(beibei3) is True

    # env username
    env_msg = _fake_business_message(user_id=900, username="envfriend")
    assert await is_contact(env_msg) is True, "env CONTACT_USERNAMES 应命中"

    # env user_id
    env_id_msg = _fake_business_message(user_id=12345, username="someone_random")
    assert await is_contact(env_id_msg) is True, "env CONTACT_USER_IDS 应命中"

    # 陌生人
    stranger = _fake_business_message(user_id=9999, username="totally_unknown")
    assert await is_contact(stranger) is False, "陌生人不应命中"

    # 无 username 的陌生人
    no_name = _fake_business_message(user_id=9998, username=None)
    assert await is_contact(no_name) is False
    print("[ok] is_contact：贝贝/env username/env user_id 命中；陌生人/无 username 不命中")

    # ---------- 3) meta 白名单 CRUD ----------
    ok, msg = await add_contact("@new_friend")
    assert ok is True and "new_friend" in msg
    # 再加一次：去重
    ok2, _ = await add_contact("new_friend")
    assert ok2 is False
    # 加 numeric id
    ok3, _ = await add_contact("77777")
    assert ok3 is True

    meta_user = _fake_business_message(user_id=8000, username="new_friend")
    assert await is_contact(meta_user) is True, "meta 白名单 username 应命中"
    meta_uid = _fake_business_message(user_id=77777, username="rand")
    assert await is_contact(meta_uid) is True, "meta 白名单 user_id 应命中"

    # 删除
    ok_rm, _ = await remove_contact("new_friend")
    assert ok_rm is True
    assert await is_contact(meta_user) is False, "删除后不再命中"
    ok_rm2, _ = await remove_contact("not_exists")
    assert ok_rm2 is False

    # list_contacts_text 展示能跑通且包含默认/env/meta 三段
    text = await list_contacts_text()
    for tok in ("默认", "env CONTACT_USERNAMES", "env CONTACT_USER_IDS", "meta usernames", "meta user_ids"):
        assert tok in text, f"list_contacts_text 缺 {tok}"
    # owner 命令分发
    r = await owner_contact_command_reply("/联系人列表")
    assert "联系人白名单" in r
    r2 = await owner_contact_command_reply("/添加联系人 @again_one")
    assert "again_one" in r2
    r3 = await owner_contact_command_reply("/删除联系人 again_one")
    assert "again_one" in r3
    print("[ok] add/remove/list/owner_contact_command_reply CRUD + 持久化 OK")

    # CONTACT_OWNER_COMMANDS 是预期三个
    assert CONTACT_OWNER_COMMANDS == {"/联系人列表", "/添加联系人", "/删除联系人"}
    print("[ok] CONTACT_OWNER_COMMANDS 集合正确")

    # ---------- 4) routers.business.text_handler 非白名单普通文本 → 进入模型流程 ----------
    import routers.business as biz

    call_openai_mock = AsyncMock(
        return_value={"reply_text": "嗯", "should_reply": True, "sticker_type": None, "risk_note": ""}
    )
    send_reply_mock = AsyncMock()
    store_message_mock = AsyncMock()

    stranger_text = _fake_business_message(
        user_id=10101, username="strange_user", text="你好"
    )
    bot_mock = MagicMock()

    with patch.object(biz, "call_openai", call_openai_mock), \
         patch.object(biz, "send_reply", send_reply_mock), \
         patch.object(biz, "store_message", store_message_mock), \
         patch.object(biz, "send_chat_action_safe", AsyncMock()), \
         patch.object(biz, "human_typing_delay", AsyncMock()):
        await biz.text_handler(stranger_text, bot_mock)

    # 非白名单普通文本：进入模型流程
    assert call_openai_mock.await_count == 1, "非白名单普通文本应进入模型流程"
    # 不应再有 [非联系人静默]
    contents = [c.args[2] for c in store_message_mock.await_args_list]
    assert not any("非联系人静默" in str(c) for c in contents), (
        f"应已移除非联系人硬静默，得到 {contents}"
    )
    print("[ok] business 非白名单普通文本进入模型流程；不再 [非联系人静默]")

    # ---------- 4b) routers.business.text_handler 广告关键词 → 仍静默 + alert ----------
    call_openai_mock_ad = AsyncMock(return_value={"reply_text": "x", "should_reply": True})
    send_reply_mock_ad = AsyncMock()
    store_message_mock_ad = AsyncMock()
    dedup_alert_mock_ad = AsyncMock()
    ad_text = _fake_business_message(
        user_id=20202, username="ads_bot_xx", text="加微信送返利兼职刷单"
    )
    with patch.object(biz, "call_openai", call_openai_mock_ad), \
         patch.object(biz, "send_reply", send_reply_mock_ad), \
         patch.object(biz, "store_message", store_message_mock_ad), \
         patch.object(biz, "dedup_alert", dedup_alert_mock_ad), \
         patch.object(biz, "send_chat_action_safe", AsyncMock()), \
         patch.object(biz, "human_typing_delay", AsyncMock()):
        await biz.text_handler(ad_text, bot_mock)
    assert call_openai_mock_ad.await_count == 0, "广告消息仍不应进入模型"
    assert send_reply_mock_ad.await_count == 0, "广告消息不应发送回复"
    ad_contents = [c.args[2] for c in store_message_mock_ad.await_args_list]
    assert any("广告静默" in str(c) for c in ad_contents), f"应写 [广告静默:..]，得到 {ad_contents}"
    assert dedup_alert_mock_ad.await_count == 1, "广告消息应触发 owner alert"
    print("[ok] business 广告关键词仍静默 + 告警 owner")

    # ---------- 5) 贝贝消息能继续走流程 ----------
    # 重新清 mock；准备贝贝消息
    call_openai_mock2 = AsyncMock(
        return_value={"reply_text": "嗯，在的", "should_reply": True, "sticker_type": None, "risk_note": ""}
    )
    send_reply_mock2 = AsyncMock()
    store_message_mock2 = AsyncMock()
    beibei_text = _fake_business_message(
        user_id=42, username="yj_syj", text="在吗"
    )
    with patch.object(biz, "call_openai", call_openai_mock2), \
         patch.object(biz, "send_reply", send_reply_mock2), \
         patch.object(biz, "store_message", store_message_mock2), \
         patch.object(biz, "send_chat_action_safe", AsyncMock()), \
         patch.object(biz, "human_typing_delay", AsyncMock()):
        await biz.text_handler(beibei_text, bot_mock)

    assert call_openai_mock2.await_count == 1, "贝贝应该正常调模型"
    assert send_reply_mock2.await_count == 1, "贝贝应该正常发送回复"
    # 不应该出现 [非联系人静默]
    contents2 = [c.args[2] for c in store_message_mock2.await_args_list]
    assert not any("非联系人静默" in str(c) for c in contents2), "贝贝不应被非联系人静默挡下"
    print("[ok] 贝贝 business 消息走完代聊流程，未被非联系人静默挡下")

    # ---------- 6) /play /help 文案不暴露联系人维护命令 ----------
    import routers.private as priv
    forbidden_contact_tokens = ["/联系人列表", "/添加联系人", "/删除联系人", "联系人白名单"]
    for ftext in (priv.HELP_TEXT, priv.PLAY_MENU_TEXT, priv.HOW_TO_USE_TEXT,
                  priv.BEIBEI_PLAY_MENU_TEXT, priv.BEIBEI_HELP_TEXT):
        for tok in forbidden_contact_tokens:
            assert tok not in ftext, f"/play /help 暴露了联系人管理 {tok}"
    print("[ok] /play /help 文案不暴露 /联系人列表 等隐藏命令")

    # ---------- 7) private 路由：is_contact 不影响私信文本流 ----------
    # 同时验证 business / media 已移除「非联系人静默」硬拦截。
    biz_src = open(os.path.join(ROOT, "routers", "business.py"), "r", encoding="utf-8").read()
    media_src = open(os.path.join(ROOT, "routers", "media.py"), "r", encoding="utf-8").read()
    priv_src = open(os.path.join(ROOT, "routers", "private.py"), "r", encoding="utf-8").read()
    assert "非联系人静默" not in biz_src, "business.py 不应再有 [非联系人静默] 硬拦截"
    assert "非联系人静默" not in priv_src, "private 路由不应引入非联系人静默"
    # media 的 _business_non_contact_check 现在永远 False，函数体保留兼容
    print("[ok] business / media 已移除非联系人硬拦截；private 仍不受影响")

    # routers.private 模块里 owner 隐藏命令引入 CONTACT_OWNER_COMMANDS（保留：仅辅助标注）
    assert "CONTACT_OWNER_COMMANDS" in priv_src
    assert "owner_contact_command_reply" in priv_src
    print("[ok] routers/private.py 仍接入联系人维护命令（辅助标注，不再拦截 Business）")

    # ---------- 8) BUSINESS_SYSTEM_PROMPT 含陌生人/广告判别规则 ----------
    import config as cfg
    bsp = cfg.BUSINESS_SYSTEM_PROMPT
    for key in ("陌生人", "广告", "should_reply"):
        assert key in bsp, f"BUSINESS_SYSTEM_PROMPT 应包含 {key}：{bsp[-300:]}"
    print("[ok] BUSINESS_SYSTEM_PROMPT 注入陌生人/广告判别规则")

    await close_db()
    try:
        os.remove(db_path)
    except Exception:
        pass
    print("\nALL CONTACT-ONLY SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
