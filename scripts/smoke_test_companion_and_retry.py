"""Smoke test：
Part A — 图像生成失败保留照片 + /继续 重试 + 「图片呢」状态回答
Part B — 贝贝陪伴公开命令 /宝宝 等 + /想你 通知 owner + /晚安 分数状态机

不联网；DB 用临时文件。
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


def _fake_private_message(text: str, *, user_id: int = 7777, username: str = "u_test"):
    chat = SimpleNamespace(id=user_id, type="private")
    from_user = SimpleNamespace(id=user_id, username=username, is_bot=False)
    msg = SimpleNamespace(
        chat=chat,
        from_user=from_user,
        business_connection_id=None,
        sender_business_bot=None,
        text=text,
        photo=None,
        sticker=None,
        animation=None,
        voice=None,
        video=None,
        caption=None,
        message_id=31,
    )
    msg.answer = AsyncMock()
    msg.bot = MagicMock()
    return msg


def _fake_callback(data: str, *, user_id: int = 7777):
    chat = SimpleNamespace(id=user_id, type="private")
    from_user = SimpleNamespace(id=user_id, username="u_test", is_bot=False)
    inner_msg = SimpleNamespace(chat=chat, from_user=from_user)
    inner_msg.answer = AsyncMock()
    inner_msg.bot = MagicMock()
    cb = SimpleNamespace(id="cbid", data=data, from_user=from_user, message=inner_msg)
    cb.answer = AsyncMock()
    return cb


async def main() -> None:
    tmpdir = tempfile.mkdtemp(prefix="comp_retry_smoke_")
    db_path = os.path.join(tmpdir, "smoke.sqlite3")
    os.environ["BOT_DB_PATH"] = db_path
    os.environ["OWNER_CHAT_IDS"] = "1001,1002"
    os.environ.setdefault("PLOG_CACHE_DIR", os.path.join(tmpdir, "plog_cache"))

    for mod in (
        "config", "db.core",
        "services.openai_service",
        "services.xiaopang_service",
        "services.alert_service",
        "services.plog_service",
        "services.pending_style_service",
        "services.pending_retry_service",
        "services.beibei_companion_service",
        "services.joke_service",
        "services.daily_joke_scheduler",
        "routers.business", "routers.media", "routers.private",
    ):
        sys.modules.pop(mod, None)

    import config  # noqa: F401
    from db.core import init_db, close_db
    await init_db()

    import routers.private as private_mod
    import services.plog_service as plog_svc
    import services.pending_retry_service as retry_svc
    import services.beibei_companion_service as bb_mod

    # ============================================
    # Part A — 图像生成失败 / 重试 / 状态查询
    # ============================================

    # ---------- A1) 失败时不消费照片、记 retry_task、给重试提示 ----------
    plog_svc.clear_pending_photo(7777)
    retry_svc.clear_task(7777)
    fake_photo_path = os.path.join(tmpdir, "for_retry.jpg")
    open(fake_photo_path, "wb").write(b"\xff\xd8\xff\xe0fake")
    plog_svc.remember_photo(7777, file_path=fake_photo_path, file_id="x", caption=None)

    msg = _fake_private_message("/plog 奶油手账", user_id=7777)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()

    sent_msgs: list[str] = []
    async def fake_send_long_text(b, cid, text, business_connection_id=None):
        sent_msgs.append(text)

    fail_result = {"ok": False, "error": "API timeout"}
    with patch.object(private_mod, "send_long_text", side_effect=fake_send_long_text), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(private_mod, "generate_plog_image", AsyncMock(return_value=fail_result)), \
         patch.object(private_mod, "is_xiaopang", AsyncMock(return_value=False)):
        await private_mod.run_plog_for_user(bot, msg, "奶油手账")

    # 照片应仍在缓存里（失败不消费）
    pending = plog_svc.get_pending_photo(7777)
    assert pending is not None and pending.file_path == fake_photo_path, (
        f"失败时照片应保留: {pending}"
    )
    # retry_task 应记上 failed
    task = retry_svc.get_task(7777)
    assert task is not None and task.tool == "plog" and task.style == "奶油手账", f"应记 retry_task: {task}"
    assert task.status == "failed"
    # 给用户的失败文案：必须含「记着」「/继续」类语义
    failed_sent = [c.args[1] for c in bot.send_message.await_args_list]
    assert any("记着" in t and "/继续" in t for t in failed_sent), f"应给重试提示文案: {failed_sent}"
    # 失败文案应带 InlineKeyboard 含 home:retry_image
    for call in bot.send_message.await_args_list:
        kb = call.kwargs.get("reply_markup")
        if kb:
            cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
            assert "home:retry_image" in cbs
            break
    print("[ok] A1: /plog 失败时保留照片、记 retry_task=failed、给 /继续 + 「再试一次」按钮")

    # ---------- A2) 「图片呢」类问题在有 failed task 时给状态回答（不走聊天） ----------
    sent_msgs.clear()
    bot.send_message.reset_mock()
    text_msg = _fake_private_message("图片呢？", user_id=7777)
    handled = await private_mod._maybe_answer_image_status(bot, text_msg, "图片呢？")
    assert handled is True
    # 应至少发出一条带 retry kb 的消息
    assert bot.send_message.await_count >= 1
    bodies = [c.args[1] for c in bot.send_message.await_args_list]
    assert any("奶油手账" in b and "再发一遍" in b for b in bodies), f"状态回答应说明上次失败的风格: {bodies}"
    print("[ok] A2: 「图片呢？」在有 failed task 时返回任务状态 + 重试按钮，不走聊天")

    # ---------- A3) /继续 → 重跑 run_plog_for_user(silent_status=True) ----------
    captured_retry = {}
    async def fake_retry_run_plog(b, m, arg, *, silent_status=False):
        captured_retry["plog"] = (arg, silent_status)
    # 仍有 retry_task 与照片
    assert retry_svc.has_failed_task(7777)
    msg_continue = _fake_private_message("/继续", user_id=7777)
    bot.send_message.reset_mock()
    with patch.object(private_mod, "run_plog_for_user", side_effect=fake_retry_run_plog):
        await private_mod._retry_last_image_task(bot, msg_continue)
    assert captured_retry.get("plog") == ("奶油手账", True), f"应调 run_plog_for_user 重试: {captured_retry}"
    print("[ok] A3: /继续 → 重跑 run_plog_for_user(style='奶油手账', silent_status=True)")

    # ---------- A4) home:retry_image 回调走重试 ----------
    bot.send_message.reset_mock()
    captured_retry.clear()
    cb_retry = _fake_callback("home:retry_image", user_id=7777)
    fake_state = MagicMock()
    fake_state.clear = AsyncMock()
    fake_state.set_state = AsyncMock()
    with patch.object(private_mod, "run_plog_for_user", side_effect=fake_retry_run_plog):
        await private_mod.home_callback(cb_retry, fake_state)
    assert captured_retry.get("plog") == ("奶油手账", True), f"home:retry_image 应触发重试: {captured_retry}"
    print("[ok] A4: home:retry_image 回调触发 _retry_last_image_task")

    # ---------- A5) 成功路径：清掉照片 + 清掉 retry_task ----------
    plog_svc.remember_photo(7777, file_path=fake_photo_path, file_id="x", caption=None)
    retry_svc.clear_task(7777)
    bot.send_message.reset_mock()
    bot.send_photo.reset_mock()
    ok_result = {"ok": True, "url": "https://x/p.png", "data": None, "error": None}
    with patch.object(private_mod, "send_long_text", side_effect=fake_send_long_text), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(private_mod, "generate_plog_image", AsyncMock(return_value=ok_result)), \
         patch.object(private_mod, "is_xiaopang", AsyncMock(return_value=False)):
        await private_mod.run_plog_for_user(bot, _fake_private_message("/plog 奶油手账", user_id=7777), "奶油手账")
    assert bot.send_photo.await_count == 1, "成功路径应发图"
    assert plog_svc.get_pending_photo(7777) is None, "成功路径应清掉照片"
    assert retry_svc.get_task(7777) is None, "成功路径应清掉 retry_task"
    print("[ok] A5: 成功路径才清照片 + retry_task")

    # ---------- A6) /继续 没有任务时给「没任务可重试」 ----------
    retry_svc.clear_task(7777)
    bot.send_message.reset_mock()
    ok = await private_mod._retry_last_image_task(bot, _fake_private_message("/继续", user_id=7777))
    assert ok is False
    print("[ok] A6: 无 retry_task 时 _retry_last_image_task 返回 False")

    # ---------- A7) /继续 有任务但照片丢失：明确提示 ----------
    retry_svc.mark_failed(7777, "plog", "奶油手账", reason="API timeout")
    plog_svc.clear_pending_photo(7777)
    bot.send_message.reset_mock()
    msg_continue2 = _fake_private_message("/继续", user_id=7777)
    ok = await private_mod._retry_last_image_task(bot, msg_continue2)
    assert ok is True  # 已发了「要先发一张照片」
    bodies = [c.args[1] for c in bot.send_message.await_args_list]
    assert any("先发一张照片" in b for b in bodies), f"应提示先发照片: {bodies}"
    print("[ok] A7: /继续 有 retry_task 但照片丢失 → 明确提示先发照片")

    # ============================================
    # Part B — 贝贝陪伴
    # ============================================

    # ---------- B1) P0：/宝宝 不再弹菜单，只回一句关系唤醒短句 ----------
    bb_msg = _fake_private_message("/宝宝", user_id=8888)
    bot.send_message.reset_mock()
    await bb_mod.handle_baobao(bot, bb_msg)
    assert bot.send_message.await_count == 1
    call = bot.send_message.await_args
    text = call.args[1] if len(call.args) >= 2 else call.kwargs.get("text", "")
    kb = call.kwargs.get("reply_markup")
    # FINAL P0：不再有 InlineKeyboard
    assert kb is None, f"/宝宝 不应再有按钮菜单：{kb}"
    # 是短句，含「我在」语义
    assert "我在" in text and len(text) <= 28, f"/宝宝 应短句关系唤醒：{text!r}"
    # 旧菜单 helper 仍存在但仅 owner 预览用
    assert hasattr(bb_mod, "handle_baobao_legacy_menu")
    print(f"[ok] B1: P0 /宝宝 返回关系唤醒短句，不再弹菜单：{text!r}")

    # ---------- B2) /烦 弹第一层 6 个选项（FINAL: 工作/情绪/关系/钱/心累/不想说）----------
    bot.send_message.reset_mock()
    await bb_mod.handle_trouble_first_layer(bot, bb_msg)
    title = bot.send_message.await_args.args[1]
    assert title == "是工作、情绪、关系，还是单纯烦？", f"/烦 标题应严格匹配 FINAL SPEC：{title}"
    kb = bot.send_message.await_args.kwargs.get("reply_markup")
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    for need in ("bb:trouble_pick:工作", "bb:trouble_pick:情绪", "bb:trouble_pick:关系",
                 "bb:trouble_pick:钱", "bb:trouble_pick:心累", "bb:trouble_pick:不想说"):
        assert need in cbs, f"/烦 缺选项 {need}"
    # FINAL SPEC 文案校对
    expected = {
        "工作": "事情再多也不是一下做完的。先告诉我最卡你的那件事。",
        "情绪": "今天发生了哪件让你不舒服的小事？",
        "关系": "如果是关于我们，别憋着。你慢慢说，我听着。",
        "钱": "压力是真的，但它只是阶段，不是结局。你不用一个人扛。",
        "心累": "那今天先别讲道理。先歇一会，我陪你。",
        "不想说": "好，那就先不说。我陪你安静一会。",
    }
    for kind, exp in expected.items():
        bot.send_message.reset_mock()
        await bb_mod.handle_trouble_pick(bot, bb_msg.chat.id, kind)
        sent = bot.send_message.await_args.args[1]
        assert sent == exp, f"/烦[{kind}] FINAL SPEC 文案不一致：{sent} vs {exp}"
    print("[ok] B2: /烦 弹 6 个一级分类，每个 FINAL SPEC 文案严格一致")

    # ---------- B3) /抱抱 / /早安 / /偏爱值 / /委屈 / /骂我 / /在哪 全部返回非空中文 ----------
    for fn, label in [
        (bb_mod.handle_hug, "/抱抱"),
        (bb_mod.handle_morning, "/早安"),
        (bb_mod.handle_favor_level, "/偏爱值"),
        (bb_mod.handle_grieved, "/委屈"),
        (bb_mod.handle_scold_me, "/骂我"),
        (bb_mod.handle_where, "/在哪"),
    ]:
        bot.send_message.reset_mock()
        await fn(bot, bb_msg)
        t = bot.send_message.await_args.args[1]
        assert isinstance(t, str) and t.strip(), f"{label} 返回为空"
    print("[ok] B3: /抱抱 /早安 /偏爱值 /委屈 /骂我 /在哪 都返回非空中文")

    # ---------- B4) /哄我 弹 3 风格按钮 + 每个风格短语都能渲染 ----------
    bot.send_message.reset_mock()
    await bb_mod.handle_soothe_menu(bot, bb_msg)
    kb = bot.send_message.await_args.kwargs.get("reply_markup")
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert set(cbs) == {"bb:soothe:gentle", "bb:soothe:firm", "bb:soothe:playful"}
    for kind in ("gentle", "firm", "playful"):
        bot.send_message.reset_mock()
        await bb_mod.handle_soothe_pick(bot, 8888, kind)
        assert bot.send_message.await_count == 1
    print("[ok] B4: /哄我 弹 3 风格 + 每个风格都给出文案")

    # ---------- B5) /想你：贝贝侧得到回应 + dedup_alert 给 owner 通知（FINAL SPEC 文案）----------
    import services.alert_service as alert_mod
    alert_mod._alert_sent_cache.clear()
    bot.send_message.reset_mock()
    captured_alerts = []
    async def fake_alert(b, t):
        captured_alerts.append((None, t))
    with patch.object(alert_mod, "alert_owner", side_effect=fake_alert):
        await bb_mod.handle_miss(bot, bb_msg)
    # 给贝贝的温暖回应：随机来自 MISS_REPLIES 池
    bb_replies = [c.args[1] for c in bot.send_message.await_args_list]
    assert bb_replies and any(r in bb_mod.MISS_REPLIES for r in bb_replies), f"应发 MISS_REPLIES 中一条: {bb_replies}"
    # FINAL（更新版）：alert 是 status-only，不再要求阿君几分钟内回复，不暗示 bot 等他
    assert any("贝贝刚刚点了 /想你" in t and "状态通报" in t and "正常陪她" in t
               for _, t in captured_alerts), f"owner 应收到 status-only 通知: {captured_alerts}"
    # 不应再出现「5 分钟内回复」「不要让机器人」之类要求性文案
    for _, t in captured_alerts:
        assert "5 分钟" not in t, f"FINAL 更新版不应再出现「5 分钟」要求性文案: {t}"
        assert "不要让机器人" not in t, f"不应再出现「不要让机器人」: {t}"
    # 同 5 分钟窗口 dedup
    cnt_before = len(captured_alerts)
    with patch.object(alert_mod, "alert_owner", side_effect=fake_alert):
        await bb_mod.handle_miss(bot, bb_msg)
    assert len(captured_alerts) == cnt_before, "同 5 分钟窗口内 /想你 应 dedup，不重复通知"
    # 贝贝看不到任何「已通知阿君」字样
    bb_replies2 = [c.args[1] for c in bot.send_message.await_args_list]
    assert not any("通知" in r or "阿君" in r for r in bb_replies2)
    print("[ok] B5: /想你 FINAL SPEC alert 文案 + dedup 通过；贝贝看不到通知")

    # ---------- B6) /晚安 状态机：FINAL SPEC ----------
    bot.send_message.reset_mock()
    await bb_mod.handle_night_ask_score(bot, bb_msg)
    asked = bot.send_message.await_args.args[1]
    assert asked == "今天开心值几分？1 到 10。", f"/晚安 问分文案应严格匹配 FINAL：{asked}"
    assert bb_mod.has_pending_night_score(8888)

    # 低分（1-4）：NIGHT_LOW_REPLY
    bot.send_message.reset_mock()
    consumed = await bb_mod.maybe_consume_night_score(bot, _fake_private_message("3", user_id=8888), "3")
    assert consumed is True
    body = bot.send_message.await_args.args[1]
    assert body == bb_mod.NIGHT_LOW_REPLY, f"低分应返回 NIGHT_LOW_REPLY，实际：{body}"
    assert not bb_mod.has_pending_night_score(8888)

    # 中分（5-7）：NIGHT_MID_REPLY
    await bb_mod.handle_night_ask_score(bot, _fake_private_message("/晚安", user_id=8888))
    bot.send_message.reset_mock()
    consumed = await bb_mod.maybe_consume_night_score(bot, _fake_private_message("6", user_id=8888), "6")
    assert consumed is True
    body = bot.send_message.await_args.args[1]
    assert body == bb_mod.NIGHT_MID_REPLY, f"中分应返回 NIGHT_MID_REPLY，实际：{body}"

    # 高分（8-10）：NIGHT_HIGH_REPLY
    await bb_mod.handle_night_ask_score(bot, _fake_private_message("/晚安", user_id=8888))
    bot.send_message.reset_mock()
    consumed = await bb_mod.maybe_consume_night_score(bot, _fake_private_message("9", user_id=8888), "9")
    assert consumed is True
    body = bot.send_message.await_args.args[1]
    assert body == bb_mod.NIGHT_HIGH_REPLY, f"高分应返回 NIGHT_HIGH_REPLY，实际：{body}"

    # 「不想打分 / 算了」：清状态 + NIGHT_NOSCORE_REPLY
    await bb_mod.handle_night_ask_score(bot, _fake_private_message("/晚安", user_id=8888))
    for refuse in ("不打分", "算了", "不想打分"):
        await bb_mod.handle_night_ask_score(bot, _fake_private_message("/晚安", user_id=8888))
        bot.send_message.reset_mock()
        consumed = await bb_mod.maybe_consume_night_score(bot, _fake_private_message(refuse, user_id=8888), refuse)
        assert consumed is True
        body = bot.send_message.await_args.args[1]
        assert body == bb_mod.NIGHT_NOSCORE_REPLY, f"{refuse} 应返回 NIGHT_NOSCORE_REPLY，实际：{body}"
        assert not bb_mod.has_pending_night_score(8888), f"{refuse} 后应清状态"

    # 非法数字：不消费状态
    await bb_mod.handle_night_ask_score(bot, _fake_private_message("/晚安", user_id=8888))
    bot.send_message.reset_mock()
    consumed = await bb_mod.maybe_consume_night_score(bot, _fake_private_message("呃", user_id=8888), "呃")
    assert consumed is True
    assert bb_mod.has_pending_night_score(8888), "非法输入不消费状态"
    print("[ok] B6: /晚安 FINAL SPEC：1-4/5-7/8-10 + 不打分清状态 + 非法不消费")

    # ---------- B7) /记得：meta 没存就如实告知；有条目时 "【回忆 NN】..." 格式 ----------
    bot.send_message.reset_mock()
    from services.xiaopang_service import meta_set
    await meta_set("xiaopang:memories", "")
    await bb_mod.handle_remember(bot, bb_msg)
    body = bot.send_message.await_args.args[1]
    assert "还没攒到真实记忆" in body or "才会留着" in body, f"空 memories 应如实告知: {body}"
    # 填一些后能拿出来；输出格式为「【回忆 NN】<内容>」
    await meta_set("xiaopang:memories", "你不爱吃香菜,你怕黑")
    bot.send_message.reset_mock()
    await bb_mod.handle_remember(bot, bb_msg)
    body = bot.send_message.await_args.args[1]
    assert body.startswith("【回忆 ") and ("【回忆 01】" in body or "【回忆 02】" in body), (
        f"应使用 FINAL 格式【回忆 NN】<内容>：{body}"
    )
    assert ("不爱吃香菜" in body) or ("怕黑" in body)
    print("[ok] B7: /记得 空时如实告知；有条目时按 FINAL 格式「【回忆 NN】...」输出")

    # ---------- B8) 公开命令集合与隐藏 owner 命令不相交 ----------
    from services.xiaopang_service import XIAOPANG_OWNER_COMMANDS
    overlap = set(bb_mod.COMPANION_COMMANDS) & set(XIAOPANG_OWNER_COMMANDS)
    assert not overlap, f"公开陪伴命令与隐藏 owner 命令重叠: {overlap}"
    print("[ok] B8: 公开陪伴命令与 XIAOPANG_OWNER_COMMANDS 集合互不相交")

    # ---------- B9) dispatch_companion_command 路由全部 22 个命令 ----------
    alert_mod._alert_sent_cache.clear()
    with patch.object(alert_mod, "alert_owner", AsyncMock()):
        for cmd in bb_mod.COMPANION_COMMANDS:
            bot.send_message.reset_mock()
            ok = await bb_mod.dispatch_companion_command(bot, _fake_private_message(cmd, user_id=8888), cmd)
            assert ok is True, f"dispatch_companion_command 应返回 True for {cmd}"
            assert bot.send_message.await_count >= 1, f"{cmd} 应至少回一条"
    assert len(bb_mod.COMPANION_COMMANDS) == 22, f"FINAL SPEC 共 22 个命令: 实际 {len(bb_mod.COMPANION_COMMANDS)}"
    print(f"[ok] B9: dispatch_companion_command 路由覆盖全部 {len(bb_mod.COMPANION_COMMANDS)} 个公开命令")

    # ---------- B9b) /今天像不像我：只返回 STYLE_LIBRARY 一句，不解释 ----------
    bot.send_message.reset_mock()
    await bb_mod.handle_today_like_me(bot, bb_msg)
    body = bot.send_message.await_args.args[1]
    assert body in bb_mod.STYLE_LIBRARY, f"/今天像不像我 应只返 STYLE_LIBRARY 一句：{body}"
    assert len(body) <= 12, f"必须短：{body}"
    print(f"[ok] B9b: /今天像不像我 返回 STYLE_LIBRARY 一句短话「{body}」，不解释")

    # ---------- B9c) /委屈 /想哭 各自走告警 + FINAL SPEC 文案 ----------
    alert_mod._alert_sent_cache.clear()
    captured = []
    async def _cap_alert(b, t):
        captured.append(t)
    with patch.object(alert_mod, "alert_owner", side_effect=_cap_alert):
        await bb_mod.handle_grieved(bot, _fake_private_message("/委屈", user_id=8888))
    # FINAL（更新版）：status-only，不出现「不要讲道理」「候选回复」之类指挥性文案
    assert any("贝贝刚刚点了 /委屈" in t and "状态通报" in t and "正常陪她" in t for t in captured), (
        f"/委屈 应触发 status-only alert: {captured}"
    )
    for t in captured:
        assert "候选回复" not in t and "建议真人" not in t and "不要讲道理" not in t, (
            f"/委屈 alert 不应再有指挥真人的文案: {t}"
        )
    captured.clear()
    alert_mod._alert_sent_cache.clear()
    with patch.object(alert_mod, "alert_owner", side_effect=_cap_alert):
        await bb_mod.handle_cry(bot, _fake_private_message("/想哭", user_id=8888))
    assert any("贝贝刚刚点了 /想哭" in t and "状态通报" in t and "正常陪她" in t for t in captured), (
        f"/想哭 应触发 status-only alert: {captured}"
    )
    for t in captured:
        assert "建议真人" not in t and "不要让 bot 连续哄" not in t and "候选回复" not in t, (
            f"/想哭 alert 不应再有指挥真人的文案: {t}"
        )
    print("[ok] B9c: /委屈 + /想哭 触发 status-only owner alert（不指挥真人）")

    # ---------- B10) bb:* callback dispatch ----------
    for data in ("bb:hug", "bb:trouble", "bb:trouble_pick:工作", "bb:soothe:gentle",
                 "bb:miss", "bb:night", "bb:talk", "bb:surprise"):
        cb = _fake_callback(data, user_id=8888)
        with patch.object(alert_mod, "alert_owner", AsyncMock()):
            await bb_mod.dispatch_companion_callback(bot, cb)
    print("[ok] B10: bb:* callback 路径全部能正常执行（不抛、不暴露隐藏）")

    # ---------- B11) Business 不触发陪伴命令 ----------
    # 通过 text_handler 直接验证：mode=business 时应直接 return
    biz_msg = _fake_private_message("/宝宝", user_id=88001)
    biz_msg.business_connection_id = "bc-x"
    biz_bot = MagicMock()
    biz_bot.send_message = AsyncMock()
    with patch.object(private_mod, "get_chat_mode", lambda m: "business"):
        await private_mod.text_handler(biz_msg, biz_bot)
    # text_handler 在 business 模式应 return；biz_bot.send_message 不应被调
    assert biz_bot.send_message.await_count == 0, "Business 模式下 /宝宝 绝不应触发陪伴命令"
    print("[ok] B11: Business 模式 /宝宝 不触发陪伴命令")

    # ---------- B12) Gating：陌生人在 private 发 /宝宝 等命令不响应 ----------
    # 模拟 not Beibei + not owner 的私信用户 → 应该不触发陪伴 dispatch；走普通聊天路径
    stranger_msg = _fake_private_message("/宝宝", user_id=999111)
    stranger_msg.business_connection_id = None
    stranger_bot = MagicMock()
    stranger_bot.send_message = AsyncMock()
    # 让聊天回退路径也不真调模型；把 call_openai mock 一下
    chat_calls = []
    async def fake_chat(messages, model, mode, **_kw):
        chat_calls.append((model, mode))
        return {"reply_text": "嗯。", "sticker_type": None}
    with patch.object(private_mod, "get_chat_mode", lambda m: "private"), \
         patch.object(private_mod, "should_skip_message", lambda m: False), \
         patch.object(private_mod, "is_xiaopang", AsyncMock(return_value=False)), \
         patch.object(private_mod, "is_owner", lambda m: False), \
         patch.object(private_mod, "send_reply", AsyncMock()), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(private_mod, "call_openai", side_effect=fake_chat), \
         patch.object(private_mod, "build_system_prompt_with_xiaopang", AsyncMock(return_value="sys")), \
         patch.object(private_mod, "store_message", AsyncMock()), \
         patch.object(private_mod, "dispatch_companion_command",
                      AsyncMock(side_effect=AssertionError("陌生人不应触发陪伴命令"))):
        try:
            await private_mod.text_handler(stranger_msg, stranger_bot)
        except AssertionError:
            raise
    # 走的是普通聊天路径
    assert len(chat_calls) == 1, f"陌生人应走普通 chat 路径: {chat_calls}"
    print("[ok] B12: Gating — 陌生人在 private 发 /宝宝 不响应陪伴模块，走普通聊天")

    # ---------- B13) /play 给贝贝侧文案：关键词触发版（不再提示 / 命令）----------
    from routers.private import BEIBEI_PLAY_MENU_TEXT
    # 关键词触发版：BEIBEI_PLAY_MENU_TEXT 不再提示任何 / 命令
    for forb in ("/宝宝", "/img", "/meme", "/plog", "/小胖", "/学习小胖", "管理面板", "授权"):
        assert forb not in BEIBEI_PLAY_MENU_TEXT, f"贝贝侧菜单不应出现 {forb}：{BEIBEI_PLAY_MENU_TEXT}"
    # 含「直接说」/「我在」等关系唤醒短句
    assert "我在" in BEIBEI_PLAY_MENU_TEXT and ("直接说" in BEIBEI_PLAY_MENU_TEXT or "你说" in BEIBEI_PLAY_MENU_TEXT)
    print("[ok] B13: 贝贝侧 /play 文案关键词触发版：无任何 / 命令、含关系唤醒短句")

    # ============================================
    # Part C — 风控告警（risk_alert_service）
    # ============================================

    import services.risk_alert_service as risk_mod
    alert_mod._alert_sent_cache.clear()

    # ---------- C1) 高风险关键词检测 ----------
    assert risk_mod.hits_high_risk_keywords("我们是不是该分开了") == ["我们", "分开"] or \
           set(risk_mod.hits_high_risk_keywords("我们是不是该分开了")) >= {"我们", "分开"}
    assert risk_mod.hits_high_risk_keywords("没事") == []
    print("[ok] C1: 高风险关键词 hit/miss 正确")

    # ---------- C2) FINAL（更新版）business 高风险：NOT safe_reply_only；alert 是 status-only；
    #               context_for_model 给 gpt-5.5 用 ----------
    bizbot = MagicMock()
    bizbot.send_message = AsyncMock()
    captured_risk = []
    async def _cap2(b, t):
        captured_risk.append(t)
    with patch.object(alert_mod, "alert_owner", side_effect=_cap2):
        res = await risk_mod.check_and_alert(
            bizbot, user_id=111, sender_label="yj_syj", text="我们以后是不是不能继续了",
            is_business=True,
        )
    # safe_reply_only 永远 False
    assert res.safe_reply_only is False, "FINAL 更新版 safe_reply_only 必须永远 False"
    # alert 是 status-only
    assert any("高风险关键词" in t and "状态通报" in t and "正常陪她" in t for t in captured_risk), (
        f"alert 应是 status-only: {captured_risk}"
    )
    # alert 中不应再有指挥真人的字样
    for t in captured_risk:
        for forbid in ("5 分钟", "建议真人", "切到一句安全回复", "不要让"):
            assert forbid not in t, f"alert 不应含 {forbid!r}: {t}"
    # context_for_model 非空，应为内部状态摘要
    assert res.context_for_model and "内部状态" in res.context_for_model, (
        f"应返回 context_for_model 给 gpt-5.5: {res.context_for_model[:80]}"
    )
    # context 中不应外露关键词「我们」「以后」原文（让模型不要复述）
    assert "请用最短" in res.context_for_model and "不许下任何承诺" in res.context_for_model
    print("[ok] C2: business 高风险 → safe_reply_only=False；alert status-only；context_for_model 注入")

    # ---------- C3) private 路径：触发 status-only alert；不降级；不返回 safe_reply_only ----------
    captured_risk.clear()
    alert_mod._alert_sent_cache.clear()
    with patch.object(alert_mod, "alert_owner", side_effect=_cap2):
        res = await risk_mod.check_and_alert(
            bizbot, user_id=112, sender_label="yj_syj_priv", text="别烦我",
            is_business=False,
        )
    assert res.safe_reply_only is False
    assert any("高风险" in t and "状态通报" in t for t in captured_risk)
    print("[ok] C3: private 模式触发 status-only alert；不降级")

    # ---------- C4) 冷回 3 次连击：第 3 次触发告警；后续状态被重置 ----------
    captured_risk.clear()
    alert_mod._alert_sent_cache.clear()
    with patch.object(alert_mod, "alert_owner", side_effect=_cap2):
        await risk_mod.check_and_alert(bizbot, user_id=222, sender_label="yj_cold", text="嗯", is_business=True)
        await risk_mod.check_and_alert(bizbot, user_id=222, sender_label="yj_cold", text="哦", is_business=True)
        # 第 3 次应触发
        res = await risk_mod.check_and_alert(bizbot, user_id=222, sender_label="yj_cold", text="好", is_business=True)
    assert any("偏冷淡、防御感较高" in t and "状态通报" in t for t in captured_risk), (
        f"3 次冷回应触发 status-only 告警: {captured_risk}"
    )
    assert res.cold_streak == 3
    # 触发过后状态会清零
    captured_risk.clear()
    alert_mod._alert_sent_cache.clear()
    with patch.object(alert_mod, "alert_owner", side_effect=_cap2):
        await risk_mod.check_and_alert(bizbot, user_id=222, sender_label="yj_cold", text="嗯", is_business=True)
    # 此时连击重置为 1，不应再发
    assert not any("偏冷淡、防御感较高" in t for t in captured_risk), "重置后单次冷回不应告警"
    print("[ok] C4: 冷回 3 次连击触发 status-only dedup 告警；触发后状态重置")

    # ---------- C5) 非冷回打断连击 ----------
    captured_risk.clear()
    alert_mod._alert_sent_cache.clear()
    with patch.object(alert_mod, "alert_owner", side_effect=_cap2):
        await risk_mod.check_and_alert(bizbot, user_id=333, sender_label="x", text="嗯", is_business=True)
        await risk_mod.check_and_alert(bizbot, user_id=333, sender_label="x", text="今天有点事", is_business=True)
        res = await risk_mod.check_and_alert(bizbot, user_id=333, sender_label="x", text="好", is_business=True)
    assert res.cold_streak == 1, f"非冷回应该把连击打断重置: {res.cold_streak}"
    assert not any("偏冷淡、防御感较高" in t for t in captured_risk)
    print("[ok] C5: 非冷回打断连击")

    # ---------- C6) 深夜低落关键词：仅在 deep_night 时触发告警 ----------
    captured_risk.clear()
    alert_mod._alert_sent_cache.clear()
    from datetime import datetime as _dt
    # 强制深夜
    with patch.object(risk_mod, "is_deep_night", return_value=True), \
         patch.object(alert_mod, "alert_owner", side_effect=_cap2):
        await risk_mod.check_and_alert(bizbot, user_id=444, sender_label="x", text="今天好累睡不着", is_business=True)
    assert any("贝贝深夜情绪偏低" in t for t in captured_risk), f"深夜低落应告警: {captured_risk}"

    captured_risk.clear()
    alert_mod._alert_sent_cache.clear()
    # 非深夜不告警
    with patch.object(risk_mod, "is_deep_night", return_value=False), \
         patch.object(alert_mod, "alert_owner", side_effect=_cap2):
        await risk_mod.check_and_alert(bizbot, user_id=445, sender_label="y", text="今天好累睡不着", is_business=True)
    assert not any("深夜情绪偏低" in t for t in captured_risk), "白天不应告警"
    print("[ok] C6: 深夜低落关键词只在 deep_night 时告警")

    # ---------- C7) FINAL Business prompt 包含 BEIBEI_FINAL_PERSONA_BLOCK 关键字 ----------
    from config import BEIBEI_FINAL_PERSONA_BLOCK
    for kw in ("数字分身", "不是恋爱话术机", "不是情绪治疗师", "不要堆称呼", "好，我不逼你"):
        assert kw in BEIBEI_FINAL_PERSONA_BLOCK, f"FINAL persona block 缺关键词 {kw}"
    print("[ok] C7: BEIBEI_FINAL_PERSONA_BLOCK 含 FINAL 关键规则")

    # ---------- C8) HIGH_RISK_SAFE_REPLY 常量仍保留（极端兜底用，但路由层不再主动用）----------
    assert risk_mod.HIGH_RISK_SAFE_REPLY == "好，我不逼你。你先缓缓，我在。"
    print(f"[ok] C8: HIGH_RISK_SAFE_REPLY 常量保留作兜底：{risk_mod.HIGH_RISK_SAFE_REPLY}")

    # ---------- C9) business 路由高风险关键词 → 仍调 gpt-5.5；不发硬编码 safe reply ----------
    # 直接 patch call_openai 与 send_reply，验证模型被调用且 model=CORE_MODEL；同时验证
    # 输入消息里的 system prompt 包含从 risk_alert_service 注入的内部状态摘要。
    import routers.business as biz_mod
    from config import CORE_MODEL as _CORE
    biz_message = SimpleNamespace(
        chat=SimpleNamespace(id=42, type="private"),
        from_user=SimpleNamespace(id=42, username="yj_syj", is_bot=False),
        business_connection_id="bc-final",
        sender_business_bot=None,
        text="我们以后是不是不能继续了",
        photo=None, sticker=None, animation=None, voice=None, video=None,
        caption=None, message_id=9001,
    )
    captured_models = []
    captured_systems = []
    captured_send_replies = []
    async def fake_call(messages, model, mode, **_kw):
        captured_models.append(model)
        # 抓 system prompt 内容
        for m in messages:
            if m.get("role") == "system":
                captured_systems.append(m.get("content", ""))
                break
        return {"reply_text": "嗯，我在。", "sticker_type": None, "should_reply": True, "risk_note": ""}
    async def fake_send_reply(bot, chat_id, result, model_used, business_connection_id=None):
        captured_send_replies.append((result.get("reply_text"), model_used))
    with patch.object(biz_mod, "should_skip_message", lambda m: False), \
         patch.object(biz_mod, "get_chat_mode", lambda m: "business"), \
         patch.object(biz_mod, "is_self_message", lambda m: False), \
         patch.object(biz_mod, "is_in_self_silence", lambda m: False), \
         patch.object(biz_mod, "is_in_owner_cooldown", lambda m: False), \
         patch.object(biz_mod, "is_xiaopang", AsyncMock(return_value=True)), \
         patch.object(biz_mod, "ad_keyword_hit", lambda t: None), \
         patch.object(biz_mod, "maybe_hit_xiaopang_reminders", AsyncMock()), \
         patch.object(biz_mod, "xiaopang_scope", AsyncMock(return_value="xiaopang")), \
         patch.object(biz_mod, "store_message", AsyncMock()), \
         patch.object(biz_mod, "build_system_prompt_with_xiaopang",
                      AsyncMock(return_value="SYS_PROMPT")), \
         patch.object(biz_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(biz_mod, "human_typing_delay", AsyncMock()), \
         patch.object(biz_mod, "call_openai", side_effect=fake_call), \
         patch.object(biz_mod, "send_reply", side_effect=fake_send_reply), \
         patch.object(biz_mod, "get_history", lambda uid: []), \
         patch.object(biz_mod, "save_history", lambda uid, t, r: None), \
         patch.object(alert_mod, "alert_owner", side_effect=_cap2):
        captured_risk.clear()
        await biz_mod.text_handler(biz_message, MagicMock(send_message=AsyncMock()))
    # call_openai 被调用了 1 次，model 应为高配核心（CORE_MODEL 或 atree_models resolver 拨出的核心模型）；
    # 不允许是 LIGHT_MODEL/mini。
    from config import LIGHT_MODEL as _LIGHT_C9
    assert len(captured_models) == 1, f"business 应调用模型一次：{captured_models}"
    assert captured_models[0] != _LIGHT_C9 and "mini" not in captured_models[0].split("-")[-1], (
        f"business 不应降级到 LIGHT/mini：{captured_models}"
    )
    # P0 重设计：注入的 system prompt 应包含 companion_engine 的 mode addendum；
    # 「我们以后是不是不能继续了」命中 risk_support 模式（hits_high_risk_keywords：我们/以后），
    # 故 system prompt 应含 risk_support 关键字与软语气约束。
    sys_p = captured_systems[0] if captured_systems else ""
    assert "当前回复模式" in sys_p and "risk_support" in sys_p, (
        f"system prompt 应被注入 mode addendum：{sys_p[-300:] if sys_p else None}"
    )
    assert "继续陪、不抽离" in sys_p or "不要长篇" in sys_p, (
        f"risk_support addendum 应含软语气约束：{sys_p[-300:]}"
    )
    # send_reply 用模型给的 reply_text，而不是 HIGH_RISK_SAFE_REPLY 硬码
    assert captured_send_replies and captured_send_replies[0][0] == "嗯，我在。", (
        f"应把模型回复发出去（非硬编码 safe reply）: {captured_send_replies}"
    )
    for r, _ in captured_send_replies:
        assert r != risk_mod.HIGH_RISK_SAFE_REPLY, "FINAL 更新版 business 不应直接发 HIGH_RISK_SAFE_REPLY"
    print("[ok] C9: business 高风险 → call_openai(CORE_MODEL=gpt-5.5) + 风险 context 注入；不发硬编码 safe reply")

    # ---------- C10) 阿君私信窗口普通短消息 → 走 CORE_MODEL（gpt-5.5）不降级到 LIGHT ----------
    import routers.private as priv_mod
    from config import CORE_MODEL as _CORE2, LIGHT_MODEL as _LIGHT
    captured_models2 = []
    async def fake_call2(messages, model, mode, **_kw):
        captured_models2.append(model)
        return {"reply_text": "嗯", "sticker_type": None}

    owner_msg_short = _fake_private_message("嗨", user_id=999111)  # 短文本
    with patch.object(priv_mod, "should_skip_message", lambda m: False), \
         patch.object(priv_mod, "get_chat_mode", lambda m: "private"), \
         patch.object(priv_mod, "is_owner", lambda m: True), \
         patch.object(priv_mod, "is_xiaopang", AsyncMock(return_value=False)), \
         patch.object(priv_mod, "handle_owner_plan_command", AsyncMock(return_value=None)), \
         patch.object(priv_mod, "_maybe_answer_image_status", AsyncMock(return_value=False)), \
         patch.object(priv_mod, "_maybe_consume_pending_for_text", AsyncMock(return_value=False)), \
         patch.object(priv_mod, "build_system_prompt_with_xiaopang", AsyncMock(return_value="sys")), \
         patch.object(priv_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(priv_mod, "send_reply", AsyncMock()), \
         patch.object(priv_mod, "store_message", AsyncMock()), \
         patch.object(priv_mod, "call_openai", side_effect=fake_call2):
        await priv_mod.text_handler(owner_msg_short, MagicMock())
    # 阿君私信短文本：不允许降级到 LIGHT_MODEL；模型可以是 CORE_MODEL 或 resolver 拨出的高配核心。
    assert len(captured_models2) == 1, captured_models2
    assert captured_models2[0] != _LIGHT, f"阿君私信不应降级到 {_LIGHT}: {captured_models2}"
    assert "mini" not in captured_models2[0].split("-")[-1], f"阿君私信不应是 mini: {captured_models2}"
    print(f"[ok] C10: 阿君私信短文本聊天 → 高配核心 {captured_models2[0]}（非降级 {_LIGHT}）")

    # ============================================
    # Part D — Beibei 陪伴 P0 重设计（mode router + 不展示菜单 + 后处理）
    # ============================================

    import services.companion_mode_router as cm_mod
    import services.companion_engine as ce_mod

    # ---------- D1) /宝宝 P0：返回关系唤醒短句，不带 InlineKeyboard ----------
    bb_msg2 = _fake_private_message("/宝宝", user_id=8888)
    bot.send_message.reset_mock()
    await bb_mod.handle_baobao(bot, bb_msg2)
    call = bot.send_message.await_args
    text_d1 = call.args[1]
    kb_d1 = call.kwargs.get("reply_markup")
    assert kb_d1 is None
    assert "我在" in text_d1 and "陪你做什么" not in text_d1
    print(f"[ok] D1: /宝宝 P0 关系唤醒短句「{text_d1}」无菜单")

    # ---------- D2) /宝宝 按最近 last_mode 返回不同短句 ----------
    cm_mod.get_session_state(8888).last_mode = "comfort_hold"
    bot.send_message.reset_mock()
    await bb_mod.handle_baobao(bot, bb_msg2)
    assert "今天先不硬撑" in bot.send_message.await_args.args[1]
    cm_mod.get_session_state(8888).last_mode = "space_respect"
    bot.send_message.reset_mock()
    await bb_mod.handle_baobao(bot, bb_msg2)
    assert "不吵你" in bot.send_message.await_args.args[1]
    cm_mod.get_session_state(8888).last_mode = "playful_light"
    bot.send_message.reset_mock()
    await bb_mod.handle_baobao(bot, bb_msg2)
    assert "宝宝到了" in bot.send_message.await_args.args[1]
    print("[ok] D2: /宝宝 根据 last_mode 返回不同关系唤醒短句（comfort/space/playful）")

    # ---------- D3) 模式路由：分类器对典型语料正确 ----------
    cases = [
        ("好累", cm_mod.MODE_COMFORT_HOLD),
        ("我今天烦死了", cm_mod.MODE_COMFORT_HOLD),
        ("嗯", cm_mod.MODE_SPACE_RESPECT),
        ("。", cm_mod.MODE_SPACE_RESPECT),
        ("随便你", cm_mod.MODE_REPAIR_GENTLE),
        ("你怎么看这件事", cm_mod.MODE_SERIOUS_ANSWER),
        ("我们以后是不是不能继续了", cm_mod.MODE_RISK_SUPPORT),
        ("嘿嘿嘿哄哄我", cm_mod.MODE_PLAYFUL_LIGHT),
        ("今天去吃饭", cm_mod.MODE_PRESENCE_SOFT),
    ]
    for txt, expected in cases:
        r = cm_mod.classify(user_id=12345, text=txt)
        assert r.mode == expected, f"分类失败：{txt!r} → {r.mode}（应为 {expected}）"
    print("[ok] D3: 模式分类器对 9 个典型样本全部命中正确模式")

    # ---------- D4) post_process_reply：长度裁切 + emoji 限制 + 追问熔断 ----------
    cls_cold = cm_mod.classify(user_id=12346, text="嗯")  # space_respect
    long_emoji_reply = "我看到了哦！这件事呢从恋爱角度来看其实你应该多关心一下自己😘😍😘😍😘😍😘😍" * 3
    out = ce_mod.post_process_reply(long_emoji_reply, cls_cold)
    assert len(out) <= 28, f"space_respect 应裁到 28 字内：{out!r}"
    # space_respect 不允许 emoji
    assert "😘" not in out and "😍" not in out
    # 不允许追问，输出不能以问号结尾
    assert not (out.endswith("?") or out.endswith("？"))

    cls_serious = cm_mod.classify(user_id=12347, text="你觉得我应不应该辞职")
    out2 = ce_mod.post_process_reply("我先说结论：不一定。你今天为什么想到这个？", cls_serious)
    # serious_answer 长度上限 60；问句允许是否依据 ask_budget，但首轮 budget=1 应允许问句保留
    assert len(out2) <= 60
    print("[ok] D4: post_process_reply 裁长 + 去 emoji + 追问熔断 正确")

    # ---------- D5) build_ajun_alert：4 段 status-only；不含「请立刻 / 务必 / 5 分钟」 ----------
    cls_risk = cm_mod.classify(user_id=12348, text="我们以后是不是不能继续了")
    alert = ce_mod.build_ajun_alert(cls_risk, "yj_syj")
    assert alert and alert.should_alert
    for forbid in ("请立刻", "务必", "5 分钟", "5分钟"):
        assert forbid not in alert.text, f"alert 不应含 {forbid!r}：{alert.text}"
    for must in ("状态", "依据", "机器人已做", "你如果想接"):
        assert must in alert.text, f"alert 4 段缺 {must}：{alert.text}"
    assert "仅为状态通报" in alert.text
    print("[ok] D5: 阿君状态通报 4 段式 status-only；不指挥真人")

    # ---------- D6) Beibei /play 不再弹娱乐菜单（无 InlineKeyboard）----------
    from routers.private import BEIBEI_PLAY_MENU_TEXT
    bb_play_msg = _fake_private_message("/play", user_id=8800)
    bb_play_msg.answer = AsyncMock()
    with patch.object(priv_mod, "should_skip_message", lambda m: False), \
         patch.object(priv_mod, "get_chat_mode", lambda m: "private"), \
         patch.object(priv_mod, "is_xiaopang", AsyncMock(return_value=True)):
        await priv_mod.play_handler(bb_play_msg)
    assert bb_play_msg.answer.await_count == 1
    play_call = bb_play_msg.answer.await_args
    # 贝贝侧不带 reply_markup（按钮）
    assert play_call.kwargs.get("reply_markup") is None, "贝贝侧 /play 不应弹按钮菜单"
    sent = play_call.args[0]
    assert "我在" in sent
    # 关键词触发版：不再提示 / 命令（包括 /宝宝）
    for forbid in ("/宝宝", "/img", "/meme", "/plog", "/magnet", "/y2k", "/poster", "🎀 好玩"):
        assert forbid not in sent, f"贝贝侧 /play 不应再出现 / 命令 {forbid}：{sent}"
    # 文案鼓励她直接说
    assert "直接说" in sent or "你说" in sent
    print("[ok] D6: 贝贝 /play 不再弹菜单、不再提示任何 / 命令；只一句关系唤醒")

    # ---------- D7) Beibei 隐藏陪伴命令（如 /烦 /抱抱）不再展示菜单 ----------
    # text_handler 收到贝贝发 /烦 时 → 不应调 dispatch_companion_command（即不弹菜单）
    bb_extra = _fake_private_message("/烦", user_id=8801)
    biz_dispatch_called = []
    async def _fake_disp(b, m, c):
        biz_dispatch_called.append(c)
        return True
    captured_chat = []
    async def _cap_chat(messages, model, mode, **_kw):
        captured_chat.append(model)
        return {"reply_text": "嗯，我在。", "sticker_type": None}
    with patch.object(priv_mod, "should_skip_message", lambda m: False), \
         patch.object(priv_mod, "get_chat_mode", lambda m: "private"), \
         patch.object(priv_mod, "is_xiaopang", AsyncMock(return_value=True)), \
         patch.object(priv_mod, "is_owner", lambda m: False), \
         patch.object(priv_mod, "dispatch_companion_command", side_effect=_fake_disp), \
         patch.object(priv_mod, "_maybe_answer_image_status", AsyncMock(return_value=False)), \
         patch.object(priv_mod, "remember_xiaopang_identity", AsyncMock()), \
         patch.object(priv_mod, "xiaopang_scope", AsyncMock(return_value="xiaopang")), \
         patch.object(priv_mod, "store_message", AsyncMock()), \
         patch.object(priv_mod, "maybe_hit_xiaopang_reminders", AsyncMock()), \
         patch.object(priv_mod, "risk_check_and_alert", AsyncMock()), \
         patch.object(priv_mod, "xiaopang_block_owner_command_for_private", AsyncMock(return_value=None)), \
         patch.object(priv_mod, "_maybe_consume_pending_for_text", AsyncMock(return_value=False)), \
         patch.object(priv_mod, "handle_xiaopang_private_setting", AsyncMock(return_value=None)), \
         patch.object(priv_mod, "xiaopang_fixed_privacy_reply", AsyncMock(return_value=None)), \
         patch.object(priv_mod, "xiaopang_blocklist_hit", AsyncMock(return_value=False)), \
         patch.object(priv_mod, "build_system_prompt_with_xiaopang", AsyncMock(return_value="sys")), \
         patch.object(priv_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(priv_mod, "send_reply", AsyncMock()), \
         patch.object(priv_mod, "call_openai", side_effect=_cap_chat):
        await priv_mod.text_handler(bb_extra, MagicMock())
    assert biz_dispatch_called == [], f"贝贝发 /烦 不应触发 dispatch_companion_command：{biz_dispatch_called}"
    # 阿树关键词路径：/烦 命中 annoyed → 安全短句直接发送给贝贝；不再走 gpt-5.5
    # （比之前的 LLM 路径更稳：纯规则、不依赖模型创作。仍不弹菜单。）
    assert captured_chat == [], f"贝贝 /烦 → 阿树短句路径，不应调 LLM：{captured_chat}"
    print("[ok] D7: 贝贝发隐藏陪伴命令 /烦 不弹菜单；落到阿树关键词安全短句")

    # ---------- D8) Owner 预览：发 /烦 仍可弹菜单 ----------
    owner_msg_for_companion = _fake_private_message("/烦", user_id=999111)
    owner_dispatch_called = []
    async def _fake_disp_owner(b, m, c):
        owner_dispatch_called.append(c)
        return True
    with patch.object(priv_mod, "should_skip_message", lambda m: False), \
         patch.object(priv_mod, "get_chat_mode", lambda m: "private"), \
         patch.object(priv_mod, "is_xiaopang", AsyncMock(return_value=False)), \
         patch.object(priv_mod, "is_owner", lambda m: True), \
         patch.object(priv_mod, "_maybe_answer_image_status", AsyncMock(return_value=False)), \
         patch.object(priv_mod, "handle_owner_plan_command", AsyncMock(return_value=None)), \
         patch.object(priv_mod, "dispatch_companion_command", side_effect=_fake_disp_owner), \
         patch.object(priv_mod, "_maybe_consume_pending_for_text", AsyncMock(return_value=False)), \
         patch.object(priv_mod, "build_system_prompt_with_xiaopang", AsyncMock(return_value="sys")), \
         patch.object(priv_mod, "store_message", AsyncMock()), \
         patch.object(priv_mod, "send_reply", AsyncMock()), \
         patch.object(priv_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(priv_mod, "call_openai", AsyncMock(return_value={"reply_text": "嗯", "sticker_type": None})):
        await priv_mod.text_handler(owner_msg_for_companion, MagicMock())
    assert owner_dispatch_called == ["/烦"], f"Owner 发 /烦 仍应触发 dispatch_companion_command（预览/调试）：{owner_dispatch_called}"
    print("[ok] D8: Owner 预览 /烦 仍可走 dispatch_companion_command（不影响调试）")

    # ---------- D9) 贝贝普通文本走 CORE_MODEL + mode addendum 注入 system prompt ----------
    captured_models_d9 = []
    captured_systems_d9 = []
    async def fake_call_d9(messages, model, mode, **_kw):
        captured_models_d9.append(model)
        for m in messages:
            if m.get("role") == "system":
                captured_systems_d9.append(m.get("content", ""))
                break
        return {"reply_text": "嗯，我在。", "sticker_type": None}

    bb_normal = _fake_private_message("好累，今天什么都不想做", user_id=8802)
    with patch.object(priv_mod, "should_skip_message", lambda m: False), \
         patch.object(priv_mod, "get_chat_mode", lambda m: "private"), \
         patch.object(priv_mod, "is_xiaopang", AsyncMock(return_value=True)), \
         patch.object(priv_mod, "is_owner", lambda m: False), \
         patch.object(priv_mod, "_maybe_answer_image_status", AsyncMock(return_value=False)), \
         patch.object(priv_mod, "remember_xiaopang_identity", AsyncMock()), \
         patch.object(priv_mod, "xiaopang_scope", AsyncMock(return_value="xiaopang")), \
         patch.object(priv_mod, "store_message", AsyncMock()), \
         patch.object(priv_mod, "maybe_hit_xiaopang_reminders", AsyncMock()), \
         patch.object(priv_mod, "risk_check_and_alert", AsyncMock()), \
         patch.object(priv_mod, "xiaopang_block_owner_command_for_private", AsyncMock(return_value=None)), \
         patch.object(priv_mod, "_maybe_consume_pending_for_text", AsyncMock(return_value=False)), \
         patch.object(priv_mod, "handle_xiaopang_private_setting", AsyncMock(return_value=None)), \
         patch.object(priv_mod, "xiaopang_fixed_privacy_reply", AsyncMock(return_value=None)), \
         patch.object(priv_mod, "xiaopang_blocklist_hit", AsyncMock(return_value=False)), \
         patch.object(priv_mod, "build_system_prompt_with_xiaopang", AsyncMock(return_value="BASE_SYS")), \
         patch.object(priv_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(priv_mod, "send_reply", AsyncMock()), \
         patch.object(priv_mod, "call_openai", side_effect=fake_call_d9):
        await priv_mod.text_handler(bb_normal, MagicMock())
    # 阿树关键词路径优先：「好累」命中 tired → 安全短句直接发，不走 gpt-5.5。
    # 这是 P0 spec 的设计：高频情绪靠规则池，低频/中性文本才让 gpt-5.5 自由发挥。
    assert captured_models_d9 == [], f"贝贝『好累』→ 阿树池，不应调 LLM：{captured_models_d9}"
    print("[ok] D9: 贝贝『好累』走阿树池；不调 LLM（spec 改动后预期）")

    # ---------- D10) business 贝贝 + 模式 addendum 注入 ----------
    import routers.business as biz_mod2
    biz_message_d10 = SimpleNamespace(
        chat=SimpleNamespace(id=42, type="private"),
        from_user=SimpleNamespace(id=42, username="yj_syj", is_bot=False),
        business_connection_id="bc-final2",
        sender_business_bot=None,
        text="好累",
        photo=None, sticker=None, animation=None, voice=None, video=None,
        caption=None, message_id=9100,
    )
    captured_models_d10 = []
    captured_systems_d10 = []
    async def fake_call_d10(messages, model, mode, **_kw):
        captured_models_d10.append(model)
        for m in messages:
            if m.get("role") == "system":
                captured_systems_d10.append(m.get("content", ""))
                break
        return {"reply_text": "嗯，我在。今天先不硬撑。", "sticker_type": None, "should_reply": True, "risk_note": ""}
    with patch.object(biz_mod2, "should_skip_message", lambda m: False), \
         patch.object(biz_mod2, "get_chat_mode", lambda m: "business"), \
         patch.object(biz_mod2, "is_self_message", lambda m: False), \
         patch.object(biz_mod2, "is_in_self_silence", lambda m: False), \
         patch.object(biz_mod2, "is_in_owner_cooldown", lambda m: False), \
         patch.object(biz_mod2, "is_xiaopang", AsyncMock(return_value=True)), \
         patch.object(biz_mod2, "ad_keyword_hit", lambda t: None), \
         patch.object(biz_mod2, "maybe_hit_xiaopang_reminders", AsyncMock()), \
         patch.object(biz_mod2, "xiaopang_scope", AsyncMock(return_value="xiaopang")), \
         patch.object(biz_mod2, "store_message", AsyncMock()), \
         patch.object(biz_mod2, "build_system_prompt_with_xiaopang", AsyncMock(return_value="SYS_BIZ")), \
         patch.object(biz_mod2, "send_chat_action_safe", AsyncMock()), \
         patch.object(biz_mod2, "human_typing_delay", AsyncMock()), \
         patch.object(biz_mod2, "call_openai", side_effect=fake_call_d10), \
         patch.object(biz_mod2, "send_reply", AsyncMock()), \
         patch.object(biz_mod2, "get_history", lambda uid: []), \
         patch.object(biz_mod2, "save_history", lambda uid, t, r: None), \
         patch.object(biz_mod2, "risk_check_and_alert", AsyncMock()):
        await biz_mod2.text_handler(biz_message_d10, MagicMock(send_message=AsyncMock()))
    # D10: 贝贝 business 用 atree_models resolver 拨号到高配核心；只验证非 mini/LIGHT。
    assert len(captured_models_d10) == 1 and "mini" not in captured_models_d10[0].split("-")[-1], (
        f"business 贝贝不应降级到 mini：{captured_models_d10}"
    )
    sys_d10 = captured_systems_d10[0] if captured_systems_d10 else ""
    assert "当前回复模式" in sys_d10 and "comfort_hold" in sys_d10
    print(f"[ok] D10: business 贝贝消息走高配核心 {captured_models_d10[0]} + mode addendum 注入")

    # ============================================
    # Part E — 贝贝自然关键词触发器（关键词触发版 / 不用 / 命令）
    # ============================================
    import services.beibei_keyword_trigger as bbkw

    # ---------- E1) 关键词分类正确 ----------
    cases_E1 = [
        ("在吗", "presence_soft"),
        ("你在吗", "presence_soft"),
        ("陪我一下", "presence_soft"),
        ("宝宝", "presence_soft"),
        ("好烦", "comfort_hold"),
        ("烦死了", "comfort_hold"),
        ("破防了", "comfort_hold"),
        ("抱抱", "comfort_hold"),
        ("想抱一下", "comfort_hold"),
        ("想你", "playful_light"),
        ("我想你了", "playful_light"),
        ("晚安", "presence_soft"),
        ("我要睡了", "presence_soft"),
        ("早安", "presence_soft"),
        ("委屈", "comfort_hold"),
        ("我想哭", "comfort_hold"),
        ("撑不住", "comfort_hold"),
        ("不想说", "space_respect"),
        ("算了", "space_respect"),
        ("骂我", "playful_light"),
        ("偏爱值", "playful_light"),
        ("今天我乖吗", "playful_light"),
    ]
    for txt, exp in cases_E1:
        intent = bbkw.detect_intent(txt)
        assert intent is not None, f"应命中关键词触发器：{txt!r}"
        assert intent.mode == exp, f"{txt!r} 应进入 {exp}，实际 {intent.mode}"
    print(f"[ok] E1: 关键词触发器分类正确（{len(cases_E1)} 个样本）")

    # ---------- E2) /宝宝 兼容：作为「宝宝」关键词处理 ----------
    intent = bbkw.detect_intent("/宝宝")
    assert intent is not None and intent.mode == "presence_soft"
    assert intent.short_reply and "我在" in intent.short_reply
    intent2 = bbkw.detect_intent("/宝宝@some_bot")
    assert intent2 is not None and intent2.mode == "presence_soft"
    print("[ok] E2: 旧 /宝宝 兼容为关键词「宝宝」，不当 slash 命令处理")

    # ---------- E3) 「想你」触发 status-only alert，贝贝收到短回复 ----------
    intent = bbkw.detect_intent("想你了")
    assert intent.needs_ajun_alert is True
    assert intent.short_reply and len(intent.short_reply) <= 16
    print("[ok] E3: 「想你」短回复 + status-only alert（dedup 5 分钟）")

    # ---------- E4) 私信 text_handler：贝贝发「在吗」走关键词路径，不调 dispatch_companion_command ----------
    bb_kw_msg = _fake_private_message("在吗", user_id=8901)
    sent_kw_replies = []
    async def _send_msg_cap(chat_id, t, **kw):
        sent_kw_replies.append(t)
    bb_kw_msg.bot = MagicMock()
    bot_kw = MagicMock()
    bot_kw.send_message = AsyncMock(side_effect=_send_msg_cap)
    disp_called = []
    async def _fake_disp_kw(b, m, c):
        disp_called.append(c)
        return True
    chat_called = []
    async def _cap_chat_kw(messages, model, mode, **_kw):
        chat_called.append(model)
        return {"reply_text": "嗯，我在。", "sticker_type": None}
    with patch.object(priv_mod, "should_skip_message", lambda m: False), \
         patch.object(priv_mod, "get_chat_mode", lambda m: "private"), \
         patch.object(priv_mod, "is_xiaopang", AsyncMock(return_value=True)), \
         patch.object(priv_mod, "is_owner", lambda m: False), \
         patch.object(priv_mod, "_maybe_answer_image_status", AsyncMock(return_value=False)), \
         patch.object(priv_mod, "dispatch_companion_command", side_effect=_fake_disp_kw), \
         patch.object(priv_mod, "call_openai", side_effect=_cap_chat_kw):
        await priv_mod.text_handler(bb_kw_msg, bot_kw)
    # 应已发短关系感回复（不调 LLM；不调 dispatch_companion_command）
    assert sent_kw_replies, f"应已发短关键词回复：{sent_kw_replies}"
    assert disp_called == []
    # 「在吗」短回复路径不需要 LLM；call_openai 不应被调
    assert chat_called == [], f"「在吗」关键词应直接发短句、不调 LLM：{chat_called}"
    # 短回复极短
    assert all(len(r) <= 18 for r in sent_kw_replies)
    print(f"[ok] E4: 贝贝发「在吗」 → 关键词路径直接短回复，不调 LLM、不调 dispatch")

    # ---------- E5) 贝贝发「烦」 → 关键词触发 comfort_hold + 走 LLM（无短句） ----------
    bb_kw2 = _fake_private_message("烦死了", user_id=8902)
    bot_kw2 = MagicMock()
    bot_kw2.send_message = AsyncMock()
    chat_called2 = []
    async def _cap_chat_kw2(messages, model, mode, **_kw):
        chat_called2.append((model, [m for m in messages if m.get("role") == "system"][0]["content"]))
        return {"reply_text": "嗯，到我这儿。", "sticker_type": None}
    with patch.object(priv_mod, "should_skip_message", lambda m: False), \
         patch.object(priv_mod, "get_chat_mode", lambda m: "private"), \
         patch.object(priv_mod, "is_xiaopang", AsyncMock(return_value=True)), \
         patch.object(priv_mod, "is_owner", lambda m: False), \
         patch.object(priv_mod, "_maybe_answer_image_status", AsyncMock(return_value=False)), \
         patch.object(priv_mod, "build_system_prompt_with_xiaopang", AsyncMock(return_value="BASE_BB")), \
         patch.object(priv_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(priv_mod, "send_reply", AsyncMock()), \
         patch.object(priv_mod, "call_openai", side_effect=_cap_chat_kw2):
        await priv_mod.text_handler(bb_kw2, bot_kw2)
    # 阿树关键词路径：「烦死了」命中 annoyed → 安全短句，不再走 LLM
    assert chat_called2 == [], f"贝贝『烦死了』→ 阿树池，不应调 LLM：{chat_called2}"
    print("[ok] E5: 贝贝发「烦死了」 → 阿树关键词池直接发，不调 LLM")

    # ---------- E6) 贝贝发「想你」 → 阿树短回复 + alert_owner 中性提醒（贝贝看不到）----------
    captured_alerts_e = []
    async def _fake_alert_owner_e(b, t):
        captured_alerts_e.append(t)
        return None
    bb_kw3 = _fake_private_message("想你了", user_id=8903)
    bot_kw3 = MagicMock()
    bot_kw3.send_message = AsyncMock()
    # 重置 atree cooldown 以确保 alert 一定发
    from services.atree_cooldown import reset as _atree_cd_reset
    _atree_cd_reset()
    with patch.object(priv_mod, "should_skip_message", lambda m: False), \
         patch.object(priv_mod, "get_chat_mode", lambda m: "private"), \
         patch.object(priv_mod, "is_xiaopang", AsyncMock(return_value=True)), \
         patch.object(priv_mod, "is_owner", lambda m: False), \
         patch.object(priv_mod, "_maybe_answer_image_status", AsyncMock(return_value=False)), \
         patch("services.alert_service.alert_owner", side_effect=_fake_alert_owner_e):
        await priv_mod.text_handler(bb_kw3, bot_kw3)
    # 贝贝侧短回复（阿树安全池）
    bb_visible_replies = [c.args[1] for c in bot_kw3.send_message.await_args_list]
    assert bb_visible_replies, f"应已发短关键词回复：{bb_visible_replies}"
    # 阿君通知：内容中性、不出现后台词，不带原话（想你属 medium，不 forward）
    assert captured_alerts_e, f"应触发阿君通知：{captured_alerts_e}"
    for t in captured_alerts_e:
        for forb in ("状态通报", "真人接管", "请立刻", "5 分钟", "务必"):
            assert forb not in t, f"阿君通知不应含 {forb}：{t}"
        assert "想你了" not in t, f"medium 通知不带原话：{t}"
    # 贝贝看不到任何「通知」「阿君」「状态」字样
    for r in bb_visible_replies:
        for forb in ("通知", "阿君", "状态通报", "仅为状态"):
            assert forb not in r, f"贝贝侧不应泄漏后台 alert：{r}"
    print("[ok] E6: 「想你」→ 贝贝阿树短回复 + 阿君中性通知（贝贝看不到，不带原话）")

    # ---------- E7) 陌生人发「在吗」 → 不走关键词触发器（gating） ----------
    stranger_msg = _fake_private_message("在吗", user_id=70001)
    bot_s = MagicMock()
    bot_s.send_message = AsyncMock()
    chat_called_s = []
    async def _cap_chat_s(messages, model, mode, **_kw):
        chat_called_s.append(model)
        return {"reply_text": "嗯。", "sticker_type": None}
    with patch.object(priv_mod, "should_skip_message", lambda m: False), \
         patch.object(priv_mod, "get_chat_mode", lambda m: "private"), \
         patch.object(priv_mod, "is_xiaopang", AsyncMock(return_value=False)), \
         patch.object(priv_mod, "is_owner", lambda m: False), \
         patch.object(priv_mod, "_maybe_answer_image_status", AsyncMock(return_value=False)), \
         patch.object(priv_mod, "_maybe_consume_pending_for_text", AsyncMock(return_value=False)), \
         patch.object(priv_mod, "store_message", AsyncMock()), \
         patch.object(priv_mod, "build_system_prompt_with_xiaopang", AsyncMock(return_value="sys")), \
         patch.object(priv_mod, "send_reply", AsyncMock()), \
         patch.object(priv_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(priv_mod, "call_openai", side_effect=_cap_chat_s):
        await priv_mod.text_handler(stranger_msg, bot_s)
    # 陌生人不应触发关键词短回复（短回复来自直接 bot.send_message；这条会从普通聊天发 send_reply）
    # 关键词触发器是 Beibei-only，所以陌生人不应被关键词路径接住
    direct_sends = [c.args for c in bot_s.send_message.await_args_list]
    assert not direct_sends, f"陌生人不应被关键词触发器直接短回复：{direct_sends}"
    # 应落到普通聊天 fallback
    assert chat_called_s == ["gpt-5.5"]
    print("[ok] E7: 陌生人发「在吗」不走关键词触发器（仅贝贝生效）；走普通聊天")

    # ---------- E8) Owner 仍可走 / 命令调试（不被关键词触发器吞掉）----------
    # 已由 D8 覆盖；这里再确认 owner 发「在吗」走普通聊天，不弹关键词短句
    owner_kw_msg = _fake_private_message("在吗", user_id=999111)
    bot_o = MagicMock()
    bot_o.send_message = AsyncMock()
    chat_called_o = []
    async def _cap_chat_o(messages, model, mode, **_kw):
        chat_called_o.append(model)
        return {"reply_text": "在", "sticker_type": None}
    with patch.object(priv_mod, "should_skip_message", lambda m: False), \
         patch.object(priv_mod, "get_chat_mode", lambda m: "private"), \
         patch.object(priv_mod, "is_xiaopang", AsyncMock(return_value=False)), \
         patch.object(priv_mod, "is_owner", lambda m: True), \
         patch.object(priv_mod, "_maybe_answer_image_status", AsyncMock(return_value=False)), \
         patch.object(priv_mod, "handle_owner_plan_command", AsyncMock(return_value=None)), \
         patch.object(priv_mod, "_maybe_consume_pending_for_text", AsyncMock(return_value=False)), \
         patch.object(priv_mod, "build_system_prompt_with_xiaopang", AsyncMock(return_value="sys")), \
         patch.object(priv_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(priv_mod, "send_reply", AsyncMock()), \
         patch.object(priv_mod, "store_message", AsyncMock()), \
         patch.object(priv_mod, "call_openai", side_effect=_cap_chat_o):
        await priv_mod.text_handler(owner_kw_msg, bot_o)
    # owner 不被关键词触发器接住；走普通聊天
    direct_sends_o = [c.args for c in bot_o.send_message.await_args_list]
    assert not direct_sends_o, f"owner 不应被关键词触发器吞掉：{direct_sends_o}"
    # owner 走 resolver 拨号的高配核心；只要不是 LIGHT/mini 即可
    assert len(chat_called_o) == 1 and "mini" not in chat_called_o[0].split("-")[-1], chat_called_o
    print(f"[ok] E8: Owner 发「在吗」走高配核心 {chat_called_o[0]}；关键词触发器仅贝贝生效")

    # ---------- E9) 业务窗口贝贝发「想你」 → 关键词调整模式 + status-only alert ----------
    import routers.business as biz_mod_e
    captured_models_e = []
    captured_systems_e = []
    captured_alerts_biz = []
    async def fake_call_biz_e(messages, model, mode, **_kw):
        captured_models_e.append(model)
        for m in messages:
            if m.get("role") == "system":
                captured_systems_e.append(m.get("content", ""))
                break
        return {"reply_text": "嗯。", "sticker_type": None, "should_reply": True, "risk_note": ""}
    async def fake_dedup_biz(b, key, t):
        captured_alerts_biz.append((key, t))
        return None
    biz_msg_e = SimpleNamespace(
        chat=SimpleNamespace(id=42, type="private"),
        from_user=SimpleNamespace(id=42, username="yj_syj", is_bot=False),
        business_connection_id="bc-kw",
        sender_business_bot=None, text="想你了",
        photo=None, sticker=None, animation=None, voice=None, video=None,
        caption=None, message_id=9301,
    )
    with patch.object(biz_mod_e, "should_skip_message", lambda m: False), \
         patch.object(biz_mod_e, "get_chat_mode", lambda m: "business"), \
         patch.object(biz_mod_e, "is_self_message", lambda m: False), \
         patch.object(biz_mod_e, "is_in_self_silence", lambda m: False), \
         patch.object(biz_mod_e, "is_in_owner_cooldown", lambda m: False), \
         patch.object(biz_mod_e, "is_xiaopang", AsyncMock(return_value=True)), \
         patch.object(biz_mod_e, "ad_keyword_hit", lambda t: None), \
         patch.object(biz_mod_e, "maybe_hit_xiaopang_reminders", AsyncMock()), \
         patch.object(biz_mod_e, "xiaopang_scope", AsyncMock(return_value="xiaopang")), \
         patch.object(biz_mod_e, "store_message", AsyncMock()), \
         patch.object(biz_mod_e, "build_system_prompt_with_xiaopang", AsyncMock(return_value="SYS")), \
         patch.object(biz_mod_e, "send_chat_action_safe", AsyncMock()), \
         patch.object(biz_mod_e, "human_typing_delay", AsyncMock()), \
         patch.object(biz_mod_e, "call_openai", side_effect=fake_call_biz_e), \
         patch.object(biz_mod_e, "send_reply", AsyncMock()), \
         patch.object(biz_mod_e, "get_history", lambda uid: []), \
         patch.object(biz_mod_e, "save_history", lambda uid, t, r: None), \
         patch.object(biz_mod_e, "risk_check_and_alert", AsyncMock()), \
         patch.object(biz_mod_e, "dedup_alert", side_effect=fake_dedup_biz):
        await biz_mod_e.text_handler(biz_msg_e, MagicMock(send_message=AsyncMock()))
    # business 贝贝走 resolver 拨号的高配核心；不允许 LIGHT/mini
    assert len(captured_models_e) == 1 and "mini" not in captured_models_e[0].split("-")[-1], (
        f"business 贝贝不应降级到 mini：{captured_models_e}"
    )
    sys_e = captured_systems_e[0] if captured_systems_e else ""
    assert "playful_light" in sys_e, f"business 关键词「想你」应调整为 playful_light：{sys_e[-300:]}"
    # alert
    assert any("状态：撒娇/想你" in t for _, t in captured_alerts_biz)
    print(f"[ok] E9: business 贝贝「想你」 → 高配核心 {captured_models_e[0]} + 模式 addendum + status-only alert")

    await close_db()
    try:
        os.remove(db_path)
    except Exception:
        pass
    print("\nALL COMPANION + RETRY SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
