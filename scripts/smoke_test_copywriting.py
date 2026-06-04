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
10) ExpressiveSignals.has_any() 仅 custom_emoji_count > 0 时也返回 True。
11) to_prompt_hint() 四个字段全有时输出所有部分；截断逻辑（>20 emoji）。
12) extract_signals：CUSTOM_EMOJI 大写实体类型也被计数；None 文本当空串。
13) _build_user_content：无信号时不含表达意图；有信号时含两段。
14) optimize_copy：BACKUP_MODEL == CORE_MODEL 时不重试；两个模型都失败返回中文兜底。
15) optimize_copy：主模型返回空串时仍尝试 BACKUP_MODEL。
16) _normalize_tool_name：全部中文别名均规范成 copyfix。
17) _detect_tool_command：其余中文别名（优化文案/文案/改文案）也能识别。
18) _maybe_prompt_copyfix_for_sticker：from_user=None 返回 False；非 copyfix pending 返回 False。
19) _maybe_consume_pending_for_text：命令文本不消费 pending；空文本不消费。
20) copyfix 已在 _PENDING_TEXT_CONSUMABLE_TOOLS；COPYFIX_STICKER_PROMPT 含文字引导语。

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

    # ---- 10) ExpressiveSignals.has_any() 仅 custom_emoji_count > 0 ----
    sig_only_custom = cw.ExpressiveSignals(custom_emoji_count=3)
    assert sig_only_custom.has_any(), "仅 custom_emoji_count>0 时 has_any 应为 True"
    hint_only_custom = sig_only_custom.to_prompt_hint()
    assert "custom_emoji" in hint_only_custom or "自定义" in hint_only_custom, (
        f"hint 应提及自定义 emoji：{hint_only_custom!r}"
    )
    assert "表达意图" in hint_only_custom, "hint 应包含'表达意图'引导语"

    # 仅 sticker_descs
    sig_only_desc = cw.ExpressiveSignals(sticker_descs=["😂开心贴纸"])
    assert sig_only_desc.has_any(), "仅 sticker_descs 时 has_any 应为 True"
    # 仅 placeholders
    sig_only_ph = cw.ExpressiveSignals(placeholders=[":fire:"])
    assert sig_only_ph.has_any(), "仅 placeholders 时 has_any 应为 True"
    print("[ok] ExpressiveSignals.has_any() 对每种单独字段均返回 True")

    # ---- 11) to_prompt_hint() 四个字段全有 + emoji 截断 ----
    many_emojis = ["🔥"] * 25  # 超过 20 的上限
    sig_full = cw.ExpressiveSignals(
        emojis=many_emojis,
        custom_emoji_count=2,
        placeholders=[":fire:", "[sticker]", ":heart:"],
        sticker_descs=["开心贴纸", "大笑GIF"],
    )
    hint_full = sig_full.to_prompt_hint()
    # 所有 4 个部分都应出现
    assert "emoji" in hint_full.lower() or "🔥" in hint_full, "hint 应含 emoji 信息"
    assert "自定义" in hint_full, "hint 应含自定义 emoji 信息"
    assert "占位" in hint_full, "hint 应含占位符信息"
    assert "表情媒体" in hint_full, "hint 应含贴纸描述信息"
    # 截断：25 个 emoji 只展示 20 个，不能把所有 25 个都列出
    emoji_line = [line for line in hint_full.split("\n") if "🔥" in line]
    assert emoji_line, "hint 中应有 emoji 行"
    # 验证不超过 20 个（用计数 🔥 数量）
    shown_count = emoji_line[0].count("🔥")
    assert shown_count <= 20, f"emoji 截断后最多 20 个，实际：{shown_count}"
    print("[ok] to_prompt_hint() 四字段全有时输出所有部分，emoji 截断为 ≤20 个")

    # ---- 12) extract_signals 边界：CUSTOM_EMOJI 大写；None 文本 ----
    # 大写 CUSTOM_EMOJI 实体类型也应被计数
    sig_upper = cw.extract_signals(
        "测试文案",
        entities=[SimpleNamespace(type="CUSTOM_EMOJI"), SimpleNamespace(type="BOLD")],
    )
    assert sig_upper.custom_emoji_count == 1, (
        f"CUSTOM_EMOJI 大写类型应被计数：{sig_upper.custom_emoji_count}"
    )
    # None 文本当空串处理，不报错
    sig_none_text = cw.extract_signals(None)  # type: ignore[arg-type]
    assert not sig_none_text.has_any(), "None 文本应与空文本等效，has_any=False"
    assert sig_none_text.to_prompt_hint() == "", "None 文本无信号，hint 应为空串"
    # 空字符串文本
    sig_empty_text = cw.extract_signals("")
    assert not sig_empty_text.has_any(), "空字符串文本 has_any 应为 False"
    print("[ok] extract_signals：CUSTOM_EMOJI 大写被计数；None/空文本安全处理")

    # ---- 13) _build_user_content：无信号不含表达意图；有信号含两段 ----
    no_sig = cw.ExpressiveSignals()
    content_no_sig = cw._build_user_content("测试文案", no_sig)
    assert "待优化文案" in content_no_sig, "应含'待优化文案'前缀"
    assert "表达意图" not in content_no_sig, "无信号时不应含表达意图段"

    has_sig = cw.ExpressiveSignals(emojis=["🔥"])
    content_has_sig = cw._build_user_content("测试文案", has_sig)
    assert "待优化文案" in content_has_sig, "有信号时应含'待优化文案'"
    assert "表达意图" in content_has_sig, "有信号时应含表达意图段"
    print("[ok] _build_user_content：无信号省略表达意图；有信号拼入两段")

    # ---- 14) optimize_copy：BACKUP_MODEL==CORE_MODEL 不重试；两者都失败返回兜底 ----
    # BACKUP_MODEL == CORE_MODEL → 不应调第二次
    calls2 = {"n": 0}

    async def always_fail(**kwargs):
        calls2["n"] += 1
        raise RuntimeError("both down")

    fake_fail_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=always_fail)))
    with patch.object(cw, "client", fake_fail_client), \
         patch.object(cw, "CORE_MODEL", "same-model"), \
         patch.object(cw, "BACKUP_MODEL", "same-model"):
        result_same = await cw.optimize_copy("一段文案")
    # 同模型不重试：只调用 1 次
    assert calls2["n"] == 1, f"BACKUP==CORE 时应只调用 1 次，实际：{calls2['n']}"
    assert "卡" in result_same or "试试" in result_same, f"应返回中文兜底：{result_same!r}"

    # 两个不同模型都失败 → 返回中文兜底，不抛异常
    calls3 = {"n": 0}

    async def always_fail2(**kwargs):
        calls3["n"] += 1
        raise RuntimeError("everything down")

    fake_fail_client2 = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=always_fail2)))
    with patch.object(cw, "client", fake_fail_client2), \
         patch.object(cw, "CORE_MODEL", "model-x"), \
         patch.object(cw, "BACKUP_MODEL", "model-y"):
        result_fail = await cw.optimize_copy("再段文案")
    assert calls3["n"] == 2, f"两模型都失败时应调用 2 次，实际：{calls3['n']}"
    assert isinstance(result_fail, str) and result_fail, "兜底应返回非空字符串"
    print("[ok] optimize_copy：BACKUP==CORE 不重试；两者失败返回中文兜底")

    # ---- 15) optimize_copy：主模型返回空串 → 仍尝试 BACKUP_MODEL ----
    calls4 = {"n": 0}

    async def empty_then_result(**kwargs):
        calls4["n"] += 1
        if calls4["n"] == 1:
            return _fake_chat_completion("")  # 主模型返回空
        return _fake_chat_completion("来自备用模型的文案")

    fake_empty_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=empty_then_result)))
    with patch.object(cw, "client", fake_empty_client), \
         patch.object(cw, "CORE_MODEL", "model-a"), \
         patch.object(cw, "BACKUP_MODEL", "model-b"):
        result_empty = await cw.optimize_copy("有内容但主模型返空")
    assert calls4["n"] == 2, f"主模型返空时应尝试 BACKUP，实际调用次数：{calls4['n']}"
    assert result_empty == "来自备用模型的文案", f"应使用备用模型结果：{result_empty!r}"
    print("[ok] optimize_copy：主模型返回空串时仍尝试 BACKUP_MODEL")

    # ---- 16) _normalize_tool_name：所有中文别名 ----
    for alias in ("copyfix", "文案优化", "优化文案", "文案", "改文案"):
        normalized = private_mod._normalize_tool_name(alias)
        assert normalized == "copyfix", f"别名 '{alias}' 应规范为 'copyfix'，实际：{normalized!r}"
    # 确认不影响其他命令别名
    assert private_mod._normalize_tool_name("edit") == "imgedit", "imgedit 别名不应被影响"
    assert private_mod._normalize_tool_name("i2v") == "i2v", "i2v 不应被影响"
    assert private_mod._normalize_tool_name("polish") == "polish", "其他工具不应被影响"
    print("[ok] _normalize_tool_name 把所有 copyfix 别名规范成 'copyfix'，不影响其他")

    # ---- 17) _detect_tool_command：中文别名识别 ----
    for cmd in ("/优化文案", "/文案", "/改文案"):
        tool_d, _ = private_mod._detect_tool_command(f"{cmd} 内容文案")
        assert tool_d == "copyfix", f"命令 '{cmd}' 应识别为 copyfix，实际：{tool_d!r}"
    # 无参数版本也能识别
    tool_no_arg, arg_empty = private_mod._detect_tool_command("/文案优化")
    assert tool_no_arg == "copyfix", "无参数 /文案优化 也应识别为 copyfix"
    assert arg_empty == "", "无参数时 arg 应为空串"
    # 非命令不应识别
    tool_none, _ = private_mod._detect_tool_command("这不是命令")
    assert tool_none is None, "非命令不应被识别"
    print("[ok] _detect_tool_command 识别所有中文 copyfix 别名，非命令返回 None")

    # ---- 18) _maybe_prompt_copyfix_for_sticker 边界 ----
    # from_user=None → 返回 False
    msg_no_user = SimpleNamespace(chat=SimpleNamespace(id=9999), from_user=None, sticker=None, animation=None)
    dummy_bot = MagicMock()
    dummy_bot.send_message = AsyncMock()
    res_no_user = await media_mod._maybe_prompt_copyfix_for_sticker(msg_no_user, dummy_bot, "default")
    assert res_no_user is False, "from_user=None 时应返回 False"
    assert dummy_bot.send_message.await_count == 0, "from_user=None 时不应发消息"

    # 有 pending 但 tool != "copyfix" → 返回 False
    pending_mod.set_pending_style(6666, "eat", "广东菜")
    stk_eat = _fake_sticker_message(user_id=6666, username="u_eat")
    eat_bot = MagicMock()
    eat_bot.send_message = AsyncMock()
    res_eat = await media_mod._maybe_prompt_copyfix_for_sticker(stk_eat, eat_bot, "default")
    assert res_eat is False, "pending.tool=eat 时不应触发 copyfix 提示"
    assert eat_bot.send_message.await_count == 0, "非 copyfix pending 时不应发消息"
    pending_mod.clear_pending_style(6666)
    print("[ok] _maybe_prompt_copyfix_for_sticker：from_user=None 或非 copyfix pending 均返回 False")

    # ---- 19) _maybe_consume_pending_for_text 边界 ----
    # 设置 copyfix pending
    pending_mod.set_pending_style(7777, "copyfix", "频道发布")

    # 命令文本（以 / 开头）不消费 pending
    msg_cmd = _fake_private_message("/文案优化 补一段")
    not_consumed = await private_mod._maybe_consume_pending_for_text(bot, msg_cmd, "/文案优化 补一段")
    assert not_consumed is False, "以 / 开头的文本不应消费 pending"
    still_pending = pending_mod.get_pending_style(7777)
    assert still_pending is not None, "命令文本不应消费 pending，pending 应仍存在"

    # 空文本不消费 pending
    msg_empty2 = _fake_private_message("")
    not_consumed_empty = await private_mod._maybe_consume_pending_for_text(bot, msg_empty2, "")
    assert not_consumed_empty is False, "空文本不应消费 pending"
    assert pending_mod.get_pending_style(7777) is not None, "空文本不应消费 pending"
    pending_mod.clear_pending_style(7777)
    print("[ok] _maybe_consume_pending_for_text：命令文本和空文本均不消费 pending")

    # ---- 20) copyfix 在 _PENDING_TEXT_CONSUMABLE_TOOLS；COPYFIX_STICKER_PROMPT 含引导 ----
    assert "copyfix" in private_mod._PENDING_TEXT_CONSUMABLE_TOOLS, (
        "copyfix 应在 _PENDING_TEXT_CONSUMABLE_TOOLS 中"
    )
    prompt = media_mod.COPYFIX_STICKER_PROMPT
    assert prompt, "COPYFIX_STICKER_PROMPT 不应为空"
    assert "文案" in prompt or "文字" in prompt, (
        f"COPYFIX_STICKER_PROMPT 应引导用户发文案/文字：{prompt!r}"
    )
    # 确保 prompt 不含文件 ID 或密钥敏感词
    assert "file_id" not in prompt.lower(), "提示语不应含 file_id"
    print("[ok] copyfix 在 _PENDING_TEXT_CONSUMABLE_TOOLS；COPYFIX_STICKER_PROMPT 内容合规")

    await close_db()
    try:
        os.remove(db_path)
    except Exception:
        pass
    print("\nALL COPYWRITING SMOKE TESTS PASSED (20 sections)")



if __name__ == "__main__":

