"""Smoke test：私信「文案优化」（/文案优化 · copyfix）。

覆盖：
1)  copywriting_service.extract_signals：识别 emoji / 占位符 / 自定义 emoji 实体 / 贴纸描述，
    并能拼出 to_prompt_hint；空文本无信号时 hint 为空。
2)  copywriting_service.optimize_copy：空文本走引导文案；有文本时调 client.chat 并把
    表达意图提示拼进 user content；主模型异常时回落 BACKUP_MODEL。
3)  private 路由：/文案优化 命令识别为 copyfix（中文别名 → _normalize_tool_name），
    且属于 _TEXT_TOOLS。
4)  private 菜单：小工具二级菜单含「📝 文案优化 /文案优化」（play:copyfix），
    且 _TOOL_HINTS["copyfix"] 存在；play:copyfix 回调只显示用法、不触发生成。
5)  _send_text_tool("copyfix", 文案)：先回 STATUS_TEXT_TOOL，再调 optimize_copy，
    且 entities 被传进 extract_signals。
6)  /文案优化 无内容时：发用法提示 + 登记 pending(copyfix)。
7)  pending(copyfix) 后续文本：_maybe_consume_pending_for_text 把整条文本交给
    optimize_copy（不拼 style 前缀）。
8)  media 路由：private 里 pending(copyfix) 时单独发贴纸 → 回提示「把文字发我」，
    且不消费 pending、不调用模型回复。
9)  隔离性：business 模式 sticker 不会触发 copyfix 提示。

不联网；DB 用临时文件；不读写任何密钥。
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


def _fake_private_message(text, *, user_id=7777, username="u_copy", entities=None):
    chat = SimpleNamespace(id=user_id, type="private")
    from_user = SimpleNamespace(id=user_id, username=username, is_bot=False)
    msg = SimpleNamespace(
        chat=chat,
        from_user=from_user,
        business_connection_id=None,
        sender_business_bot=None,
        text=text,
        entities=entities,
        photo=None,
        sticker=None,
        animation=None,
        voice=None,
        video=None,
        caption=None,
        message_id=42,
    )
    msg.answer = AsyncMock()
    msg.bot = MagicMock()
    return msg


def _fake_sticker_message(*, user_id=7777, username="u_copy", business=False):
    chat = SimpleNamespace(id=user_id, type="private")
    from_user = SimpleNamespace(id=user_id, username=username, is_bot=False)
    sticker = SimpleNamespace(
        emoji="😂", set_name="FunnyPack", is_animated=False, is_video=False,
        type="regular", file_unique_id="uniq-1", file_id="fid-1",
    )
    msg = SimpleNamespace(
        chat=chat,
        from_user=from_user,
        business_connection_id=("bc-x" if business else None),
        sender_business_bot=None,
        text=None,
        entities=None,
        photo=None,
        sticker=sticker,
        animation=None,
        voice=None,
        video=None,
        caption=None,
        message_id=43,
    )
    msg.answer = AsyncMock()
    msg.bot = MagicMock()
    return msg


def _fake_chat_completion(content):
    choice = SimpleNamespace(message=SimpleNamespace(content=content))
    return SimpleNamespace(choices=[choice])


async def run():
    tmpdir = tempfile.mkdtemp(prefix="copyfix_smoke_")
    db_path = os.path.join(tmpdir, "smoke.sqlite3")
    os.environ["BOT_DB_PATH"] = db_path

    from db.core import init_db, close_db
    await init_db()

    import services.copywriting_service as cw
    import services.pending_style_service as pending_mod
    import routers.private as private_mod
    import routers.media as media_mod

    # ---- 1) extract_signals ----
    sig = cw.extract_signals(
        "🔥今日上新！全场五折 [贴纸] :fire: 手慢无～",
        entities=[SimpleNamespace(type="custom_emoji"), SimpleNamespace(type="bold")],
        sticker_descs=["贴纸、emoji=😂"],
    )
    assert "🔥" in sig.emojis, f"应识别 emoji：{sig.emojis}"
    assert sig.custom_emoji_count == 1, f"应识别 1 个 custom_emoji：{sig.custom_emoji_count}"
    assert any("贴纸" in p or "fire" in p for p in sig.placeholders), f"应识别占位/表情码：{sig.placeholders}"
    assert sig.sticker_descs, "应保留贴纸描述"
    hint = sig.to_prompt_hint()
    assert hint and "表达意图" in hint, f"应拼出表达意图提示：{hint!r}"

    empty_sig = cw.extract_signals("普通广告，没有表情符号", entities=None)
    assert not empty_sig.has_any(), "无表情时 has_any 应为 False"
    assert empty_sig.to_prompt_hint() == "", "无表情时 hint 应为空串"
    print("[ok] extract_signals 识别 emoji/自定义emoji/占位/贴纸描述，并拼出/省略 hint")

    # ---- 2) optimize_copy ----
    # 空文本：走引导，不调模型
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock())))
    with patch.object(cw, "client", fake_client):
        guide = await cw.optimize_copy("   ")
    assert "发给我" in guide, f"空文本应回引导：{guide}"
    assert fake_client.chat.completions.create.await_count == 0, "空文本不应调用模型"

    # 有文本 + 信号：调用模型，user content 含表达意图提示
    create_mock = AsyncMock(return_value=_fake_chat_completion("✨今日上新｜全场五折\n点下方立即抢～"))
    fake_client2 = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create_mock)))
    with patch.object(cw, "client", fake_client2):
        out = await cw.optimize_copy("🔥五折促销", cw.extract_signals("🔥五折促销"))
    assert "五折" in out
    sent_user = create_mock.call_args.kwargs["messages"][1]["content"]
    assert "待优化文案" in sent_user and "🔥" in sent_user, f"user content 应含文案与表达意图：{sent_user!r}"
    print("[ok] optimize_copy：空文本走引导；有文本调模型并拼入表达意图提示")

    # 主模型异常 → 回落 BACKUP_MODEL
    calls = {"n": 0}

    async def flaky_create(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("primary down")
        return _fake_chat_completion("备用结果")

    fake_client3 = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=flaky_create)))
    with patch.object(cw, "client", fake_client3), \
         patch.object(cw, "CORE_MODEL", "model-a"), \
         patch.object(cw, "BACKUP_MODEL", "model-b"):
        out2 = await cw.optimize_copy("一些文案")
    assert out2 == "备用结果", f"主模型失败应回落 BACKUP：{out2}"
    assert calls["n"] == 2
    print("[ok] optimize_copy 主模型失败回落 BACKUP_MODEL")

    # ---- 3) 命令识别 ----
    assert "copyfix" in private_mod._TEXT_TOOLS
    tool, arg = private_mod._detect_tool_command("/文案优化 帮我优化这段")
    assert tool == "copyfix" and arg == "帮我优化这段", f"中文命令应识别为 copyfix：{tool},{arg}"
    tool2, _ = private_mod._detect_tool_command("/copyfix 内容")
    assert tool2 == "copyfix", "英文别名 /copyfix 也应识别"
    print("[ok] /文案优化 与 /copyfix 都识别为 copyfix，且属于 _TEXT_TOOLS")

    # ---- 4) 菜单 + hint + play:copyfix 回调 ----
    kb = private_mod._build_tools_keyboard()
    flat = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "play:copyfix" in flat, f"小工具菜单应含 play:copyfix：{flat}"
    assert "copyfix" in private_mod._TOOL_HINTS and "文案优化" in private_mod._TOOL_HINTS["copyfix"]

    forbid = AsyncMock(side_effect=AssertionError("play:copyfix 不应触发生成"))
    q_msg = SimpleNamespace(answer=AsyncMock())
    query = SimpleNamespace(data="play:copyfix", message=q_msg, answer=AsyncMock())
    with patch.object(private_mod, "run_text_tool", forbid), \
         patch.object(private_mod, "optimize_copy", forbid):
        await private_mod.play_callback(query)
    assert q_msg.answer.await_count == 1, "play:copyfix 应只回用法说明"
    shown = q_msg.answer.call_args.args[0]
    assert "文案优化" in shown
    print("[ok] 小工具菜单含 文案优化，play:copyfix 只显示用法、不触发生成")

    # ---- 5) _send_text_tool(copyfix) 走 optimize_copy 并传 entities ----
    sent = []

    async def cap_send(bot, chat_id, text):
        sent.append(text)

    opt_mock = AsyncMock(return_value="优化后的频道文案")
    msg = _fake_private_message("/文案优化 🔥五折", entities=[SimpleNamespace(type="custom_emoji")])
    bot = MagicMock()
    with patch.object(private_mod, "send_long_text", side_effect=cap_send), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(private_mod, "optimize_copy", opt_mock), \
         patch.object(private_mod, "run_text_tool", forbid):
        await private_mod._send_text_tool(bot, msg, "copyfix", "🔥五折促销")
    assert private_mod.STATUS_TEXT_TOOL in sent, f"应先回状态提示：{sent}"
    assert "优化后的频道文案" in sent, "应输出优化结果"
    assert opt_mock.await_count == 1, "应调用 optimize_copy 而非 run_text_tool"
    passed_signals = opt_mock.call_args.args[1]
    assert passed_signals.custom_emoji_count == 1, "entities 应被读进 signals"
    print("[ok] _send_text_tool(copyfix) 走 optimize_copy 并把 entities 读进信号")

    # ---- 6) /文案优化 无内容 → 提示 + 登记 pending ----
    pending_mod.clear_pending_style(7777)
    sent.clear()
    msg2 = _fake_private_message("/文案优化")
    with patch.object(private_mod, "send_long_text", side_effect=cap_send), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()):
        await private_mod._send_text_tool(bot, msg2, "copyfix", "")
    assert any("文案优化" in s for s in sent), f"无内容应回用法：{sent}"
    p = pending_mod.get_pending_style(7777)
    assert p is not None and p.tool == "copyfix", "无内容时应登记 pending(copyfix)"
    print("[ok] /文案优化 无内容：发用法提示 + 登记 pending(copyfix)")

    # ---- 7) pending(copyfix) 后续文本 → optimize_copy（不拼 style 前缀）----
    sent.clear()
    opt_mock2 = AsyncMock(return_value="结果2")
    msg3 = _fake_private_message("🔥夏季大促，全场三折！")
    with patch.object(private_mod, "send_long_text", side_effect=cap_send), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(private_mod, "optimize_copy", opt_mock2):
        consumed = await private_mod._maybe_consume_pending_for_text(bot, msg3, "🔥夏季大促，全场三折！")
    assert consumed is True, "pending(copyfix) 应消费这条文本"
    assert opt_mock2.await_count == 1
    fed_text = opt_mock2.call_args.args[0]
    assert fed_text == "🔥夏季大促，全场三折！", f"应原样把文案交给 optimize_copy，不拼前缀：{fed_text!r}"
    assert pending_mod.get_pending_style(7777) is None, "消费后应清空 pending"
    print("[ok] pending(copyfix) 后续文本原样进入 optimize_copy，并清空 pending")

    # ---- 8) media：private pending(copyfix) + 单发贴纸 → 提示、不消费、不回复模型 ----
    pending_mod.set_pending_style(7777, "copyfix", "频道发布")
    stk = _fake_sticker_message()
    media_bot = MagicMock()
    media_bot.send_message = AsyncMock()
    forbid_call = AsyncMock(side_effect=AssertionError("贴纸提示阶段不应调用模型"))
    with patch.object(media_mod, "should_skip_message", lambda m: False), \
         patch.object(media_mod, "get_chat_mode", lambda m: "private"), \
         patch.object(media_mod, "is_xiaopang", AsyncMock(return_value=False)), \
         patch.object(media_mod, "xiaopang_scope", AsyncMock(return_value="default")), \
         patch.object(media_mod, "store_message", AsyncMock()), \
         patch.object(media_mod, "record_incoming_media", AsyncMock()), \
         patch.object(media_mod, "_business_self_check", AsyncMock(return_value=False)), \
         patch.object(media_mod, "_owner_self_sticker_or_gif_check", AsyncMock(return_value=False)), \
         patch.object(media_mod, "_business_non_contact_check", AsyncMock(return_value=False)), \
         patch.object(media_mod, "call_openai", forbid_call):
        await media_mod._handle_sticker_or_gif(stk, media_bot)
    assert media_bot.send_message.await_count == 1, "应给用户发一条「把文字发我」提示"
    prompt_text = media_bot.send_message.call_args.args[1]
    assert "文字" in prompt_text or "文案" in prompt_text, f"提示应引导发文案：{prompt_text}"
    assert pending_mod.get_pending_style(7777) is not None, "贴纸阶段不应消费 pending"
    print("[ok] private pending(copyfix) 单发贴纸 → 提示发文案，不消费 pending、不调模型")

    # ---- 9) 隔离：无 pending(copyfix) 的用户不触发提示 ----
    # 用一个全新 user_id（无 pending），即便误调用 helper 也应返回 False。
    other_bot = MagicMock()
    other_bot.send_message = AsyncMock()
    other_stk = _fake_sticker_message(user_id=8888, username="other", business=True)
    res = await media_mod._maybe_prompt_copyfix_for_sticker(other_stk, other_bot, "default")
    assert res is False, "无 pending(copyfix) 的用户不应被 copyfix 提示打扰"
    assert other_bot.send_message.await_count == 0, "无 pending 时不应发任何提示"
    print("[ok] 无 pending(copyfix) 时不触发提示（business / 普通用户隔离）")

    await close_db()
    try:
        os.remove(db_path)
    except Exception:
        pass
    print("\nALL COPYWRITING SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(run())
