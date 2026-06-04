"""Smoke test：私信功能区菜单二次收口（首页 4 大入口 + 二级菜单 + 命令反馈）。

覆盖：
1)  首页文案：PLAY_MENU_TEXT / BEIBEI_PLAY_MENU_TEXT 不再直接铺 13 个命令；
    只包含「功能区 / 入口」类提示词；不暴露 /plog /magnet 等具体命令名。
2)  首页键盘 _build_play_keyboard：4 个大入口（home:make_image / home:fun /
    home:tools / home:howto），且每行各占一格；不直接含 play:* 按钮。
3)  二级菜单：_build_make_image_keyboard / _build_fun_keyboard / _build_tools_keyboard
    覆盖正确分组的 play:* 按钮 + 「⬅️ 返回首页」（home:back）。
4)  /help 文案：明确说 /play 或 /start 都会进首页大入口。
5)  /start 在 private 弹首页（带 home 键盘）；不直接铺娱乐按钮。
6)  /play 在 private 弹首页（带 home 键盘）。
7)  贝贝侧 /start /play 弹温柔版首页（带 home 键盘，仍是 4 个大入口）。
8)  callback home:make_image / home:fun / home:tools 各自弹对应二级菜单（带返回首页）。
9)  callback home:howto 弹 HOW_TO_USE_TEXT；home:back 回首页。
10) callback play:xxx 只显示用法文案（带返回首页），不调用任何生成接口。
11) 隐藏小胖/贝贝管理命令不在任何菜单/hint/标签里出现。
12) Business 模式 /start /play /help 全部直接 return，不弹菜单不发文本。
13) 命令反馈机制（P0）：
    - run_plog_for_user 无照片时回 STATUS_NEED_PHOTO_PLOG（含「先发一张照片」）
    - run_plog_for_user 有照片时先回 STATUS_IMAGE_TOOL 再生成
    - 同样验证 magnet / y2k / poster
    - _send_image_tool（/img /meme）有参数时先回 STATUS_IMAGE_TOOL
    - _send_text_tool（文本类）有参数时先回 STATUS_TEXT_TOOL

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


def _fake_private_message(text: str, *, user_id: int = 9001, username: str = "u_ent"):
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
        message_id=21,
    )
    msg.answer = AsyncMock()
    msg.bot = MagicMock()
    return msg


def _fake_business_message(text: str, *, user_id: int = 999, username: str = "biz_u"):
    chat = SimpleNamespace(id=user_id, type="private")
    from_user = SimpleNamespace(id=user_id, username=username, is_bot=False)
    msg = SimpleNamespace(
        chat=chat,
        from_user=from_user,
        business_connection_id="bc-ent",
        sender_business_bot=None,
        text=text,
        photo=None,
        sticker=None,
        animation=None,
        voice=None,
        video=None,
        caption=None,
        message_id=22,
    )
    msg.answer = AsyncMock()
    msg.bot = MagicMock()
    return msg


def _fake_callback(data: str, *, user_id: int = 9001):
    chat = SimpleNamespace(id=user_id, type="private")
    from_user = SimpleNamespace(id=user_id, username="u_ent", is_bot=False)
    inner_msg = SimpleNamespace(chat=chat, from_user=from_user)
    inner_msg.answer = AsyncMock()
    cb = SimpleNamespace(
        id="cbid",
        data=data,
        from_user=from_user,
        message=inner_msg,
    )
    cb.answer = AsyncMock()
    return cb


def _all_callback_data(kb) -> list[str]:
    out = []
    for row in kb.inline_keyboard:
        for btn in row:
            cb = getattr(btn, "callback_data", None)
            if cb:
                out.append(cb)
    return out


def _all_labels(kb) -> list[str]:
    out = []
    for row in kb.inline_keyboard:
        for btn in row:
            label = getattr(btn, "text", "")
            if label:
                out.append(label)
    return out


async def main() -> None:
    tmpdir = tempfile.mkdtemp(prefix="ent_smoke_")
    db_path = os.path.join(tmpdir, "smoke.sqlite3")
    os.environ["BOT_DB_PATH"] = db_path
    os.environ.setdefault("PLOG_CACHE_DIR", os.path.join(tmpdir, "plog_cache"))
    os.environ.setdefault("BUSINESS_REPLY_DELAY_MIN", "0.0")
    os.environ.setdefault("BUSINESS_REPLY_DELAY_MAX", "0.0")
    os.environ.setdefault("BUSINESS_REPLY_DELAY_PER_CHAR", "0.0")
    os.environ.setdefault("BUSINESS_REPLY_DELAY_JITTER", "0.0")

    for mod in (
        "config", "db.core",
        "services.context_service", "services.history_service",
        "services.message_service", "services.openai_service",
        "services.xiaopang_service", "services.plog_service",
        "services.magnet_service", "services.y2k_service", "services.poster_service",
        "services.image_generation_service",
        "routers.business", "routers.media", "routers.private",
    ):
        sys.modules.pop(mod, None)

    import config  # noqa: F401
    from db.core import init_db, close_db
    await init_db()

    from aiogram.types import InlineKeyboardMarkup
    import routers.private as private_mod
    from routers.private import (
        PLAY_MENU_TEXT,
        HELP_TEXT,
        BEIBEI_PLAY_MENU_TEXT,
        BEIBEI_HELP_TEXT,
        HOW_TO_USE_TEXT,
        HOME_MENU_HEADER,
        BEIBEI_HOME_MENU_HEADER,
        SUB_MAKE_IMAGE_TITLE,
        SUB_FUN_TITLE,
        SUB_TOOLS_TITLE,
        STATUS_TEXT_TOOL,
        STATUS_IMAGE_TOOL,
        STATUS_NEED_PHOTO_PLOG,
        STATUS_NEED_PHOTO_MAGNET,
        STATUS_NEED_PHOTO_Y2K,
        STATUS_NEED_PHOTO_POSTER,
        _build_play_keyboard,
        _build_home_keyboard,
        _build_make_image_keyboard,
        _build_fun_keyboard,
        _build_tools_keyboard,
        _build_back_home_keyboard,
        _TOOL_HINTS,
    )
    import services.plog_service as plog_svc

    # ---------- 1) 首页文案：极简，不直接铺命令 ----------
    # 首页不应直接列出 /plog /magnet 等命令名（用户反馈：太乱）
    for cmd in ("/plog", "/magnet", "/y2k", "/poster", "/img", "/meme", "/eat", "/reply"):
        assert cmd not in PLAY_MENU_TEXT, f"首页应保持简洁，不应直接列出 {cmd}"
        assert cmd not in BEIBEI_PLAY_MENU_TEXT, f"贝贝首页应保持简洁，不应直接列出 {cmd}"
    # 但要保留功能区入口语
    assert "功能区" in PLAY_MENU_TEXT or "入口" in PLAY_MENU_TEXT
    assert ("发文字" in PLAY_MENU_TEXT) and ("图片" in PLAY_MENU_TEXT or "语音" in PLAY_MENU_TEXT)
    # 贝贝侧 P0（关键词触发版）：极简关系唤醒（「我在」+「你说/直接说」）；
    # 不再提示任何 / 命令（包括 /宝宝），改为自然关键词触发
    assert "我在" in BEIBEI_PLAY_MENU_TEXT and ("直接说" in BEIBEI_PLAY_MENU_TEXT or "你说" in BEIBEI_PLAY_MENU_TEXT)
    assert "/宝宝" not in BEIBEI_PLAY_MENU_TEXT, "贝贝侧 /play 不应再提示 /宝宝"
    print("[ok] 首页文案极简、不直接铺命令列表，但保留功能区/直接发文字提示")

    # ---------- 2) 首页键盘：4 个大入口 ----------
    home_kb = _build_play_keyboard()
    home_cbs = _all_callback_data(home_kb)
    assert set(home_cbs) == {"home:make_image", "home:fun", "home:tools", "home:howto"}, (
        f"首页键盘应只有 4 个大入口 callback_data，实际：{home_cbs}"
    )
    home_labels = " ".join(_all_labels(home_kb))
    for kw in ["做点图", "好玩一下", "小工具", "怎么用"]:
        assert kw in home_labels, f"首页缺少入口 {kw}"
    # 首页不应出现具体 play:* 按钮
    for cb in home_cbs:
        assert not cb.startswith("play:"), f"首页不应直接含 play:* 按钮: {cb}"
    print("[ok] 首页键盘只含 4 大入口：📸 做点图 / 🎀 好玩一下 / 🧰 小工具 / 📖 怎么用")

    # ---------- 3) 二级菜单分组正确 + 都带返回首页 ----------
    mk = _build_make_image_keyboard()
    mk_cbs = _all_callback_data(mk)
    expected_image_keys = {"play:plog", "play:magnet", "play:y2k", "play:poster", "play:img", "play:meme"}
    assert expected_image_keys <= set(mk_cbs), f"做点图二级菜单缺按钮：{expected_image_keys - set(mk_cbs)}"
    assert "home:back" in mk_cbs, "做点图二级菜单缺「返回首页」"
    # 不应窜到文本工具
    for forbid in ("play:eat", "play:reply", "play:eli5", "play:excel", "play:polish", "play:tldr"):
        assert forbid not in mk_cbs, f"做点图二级菜单不应含 {forbid}"

    fun = _build_fun_keyboard()
    fun_cbs = _all_callback_data(fun)
    expected_fun_keys = {"play:eat", "play:reply", "play:eli5"}
    assert expected_fun_keys <= set(fun_cbs), f"好玩一下二级菜单缺按钮：{expected_fun_keys - set(fun_cbs)}"
    assert "home:back" in fun_cbs
    for forbid in ("play:plog", "play:magnet", "play:excel", "play:tldr", "play:polish"):
        assert forbid not in fun_cbs, f"好玩一下二级菜单不应含 {forbid}"

    tools = _build_tools_keyboard()
    tools_cbs = _all_callback_data(tools)
    expected_tools_keys = {"play:excel", "play:tldr", "play:polish"}
    assert expected_tools_keys <= set(tools_cbs), f"小工具二级菜单缺按钮：{expected_tools_keys - set(tools_cbs)}"
    assert "home:back" in tools_cbs
    for forbid in ("play:plog", "play:magnet", "play:eat", "play:img", "play:meme"):
        assert forbid not in tools_cbs, f"小工具二级菜单不应含 {forbid}"

    # 返回首页键盘
    back = _build_back_home_keyboard()
    assert _all_callback_data(back) == ["home:back"]
    print("[ok] 二级菜单：做点图 6 + 好玩一下 3 + 小工具 3，每个都带「⬅️ 返回首页」")

    # ---------- 4) 不暴露隐藏管理命令 ----------
    hidden = ["/小胖", "/学习小胖", "/新计划", "/计划列表", "/联系人列表", "/添加联系人", "/删除联系人", "管理面板", "授权"]
    for text in (PLAY_MENU_TEXT, HELP_TEXT, BEIBEI_PLAY_MENU_TEXT, BEIBEI_HELP_TEXT, HOW_TO_USE_TEXT):
        for tok in hidden:
            assert tok not in text, f"菜单/帮助暴露了 {tok}"
    for kb in (home_kb, mk, fun, tools, back):
        for label in _all_labels(kb):
            for tok in hidden:
                assert tok not in label, f"按钮 {label} 暴露了 {tok}"
        for cb in _all_callback_data(kb):
            for tok in hidden:
                assert tok not in cb, f"callback_data {cb} 暴露了 {tok}"
    print("[ok] 菜单/帮助/按钮均不暴露任何隐藏管理命令")

    # ---------- 5) /help 文案：含 /play /start 与「首页」/「入口」 ----------
    for kw in ["/play", "/start"]:
        assert kw in HELP_TEXT, f"/help 缺少 {kw}"
    assert "首页" in HELP_TEXT or "入口" in HELP_TEXT
    assert "做点图" in HELP_TEXT and "怎么用" in HELP_TEXT
    print("[ok] /help 文案说明 /play 与 /start 都会进首页大入口")

    # ---------- 6) /start private 弹首页 ----------
    msg = _fake_private_message("/start")
    with patch.object(private_mod, "is_xiaopang", AsyncMock(return_value=False)), \
         patch.object(private_mod, "should_skip_message", lambda m: False), \
         patch.object(private_mod, "get_chat_mode", lambda m: "private"):
        await private_mod.start_handler(msg)
    assert msg.answer.await_count == 1
    call = msg.answer.await_args
    sent_text = call.args[0] if call.args else call.kwargs.get("text", "")
    sent_markup = call.kwargs.get("reply_markup")
    assert isinstance(sent_markup, InlineKeyboardMarkup)
    assert set(_all_callback_data(sent_markup)) == {"home:make_image", "home:fun", "home:tools", "home:howto"}
    # 不应直接铺命令
    for cmd in ("/plog", "/magnet", "/y2k", "/poster"):
        assert cmd not in sent_text, f"/start 首页消息不应直接列出 {cmd}"
    print("[ok] /start 在 private 弹首页（4 大入口，不铺命令）")

    # ---------- 7) /play private 弹首页 ----------
    msg2 = _fake_private_message("/play")
    with patch.object(private_mod, "is_xiaopang", AsyncMock(return_value=False)), \
         patch.object(private_mod, "should_skip_message", lambda m: False), \
         patch.object(private_mod, "get_chat_mode", lambda m: "private"):
        await private_mod.play_handler(msg2)
    assert msg2.answer.await_count == 1
    call = msg2.answer.await_args
    sent_markup = call.kwargs.get("reply_markup")
    assert isinstance(sent_markup, InlineKeyboardMarkup)
    assert set(_all_callback_data(sent_markup)) == {"home:make_image", "home:fun", "home:tools", "home:howto"}
    print("[ok] /play 在 private 弹首页（4 大入口）")

    # ---------- 8) 贝贝侧 /start /play P0（关键词触发版）：不弹菜单、不再提示任何 / 命令 ----------
    for cmd in ("/start", "/play"):
        m = _fake_private_message(cmd)
        with patch.object(private_mod, "is_xiaopang", AsyncMock(return_value=True)), \
             patch.object(private_mod, "should_skip_message", lambda m: False), \
             patch.object(private_mod, "get_chat_mode", lambda m: "private"):
            if cmd == "/start":
                await private_mod.start_handler(m)
            else:
                await private_mod.play_handler(m)
        assert m.answer.await_count == 1
        c = m.answer.await_args
        text = c.args[0] if c.args else c.kwargs.get("text", "")
        # 贝贝侧：不弹按钮
        assert c.kwargs.get("reply_markup") is None, f"贝贝侧 {cmd} 不应再弹按钮菜单"
        # 关键词触发版：不再有任何 / 命令（包括 /宝宝）
        for forbid in ("/宝宝", "/img", "/meme", "/plog", "/magnet", "/y2k", "/poster"):
            assert forbid not in text, f"贝贝侧 {cmd} 不应列 {forbid}"
        # 仍含「我在 / 你说 / 直接说」关系感
        assert "我在" in text and ("直接说" in text or "你说" in text)
        # 不会出现隐藏管理词
        for tok in hidden:
            assert tok not in text
    print("[ok] 贝贝侧 /start /play 不弹菜单、不提示任何 / 命令；只关系唤醒 + 「你直接说就行」")

    # ---------- 9) home:* callback：弹对应二级菜单 / howto / 返回首页 ----------
    for key, expected_sub_cbs in [
        ("make_image", expected_image_keys),
        ("fun", expected_fun_keys),
        ("tools", expected_tools_keys),
    ]:
        cb = _fake_callback(f"home:{key}")
        with patch.object(private_mod, "is_xiaopang", AsyncMock(return_value=False)):
            await private_mod.home_callback(cb)
        assert cb.message.answer.await_count == 1
        sub_call = cb.message.answer.await_args
        sub_kb = sub_call.kwargs.get("reply_markup")
        assert isinstance(sub_kb, InlineKeyboardMarkup)
        sub_cbs = set(_all_callback_data(sub_kb))
        assert expected_sub_cbs <= sub_cbs, f"home:{key} 应弹对应二级菜单，实际 {sub_cbs}"
        assert "home:back" in sub_cbs

    # home:howto
    cb_h = _fake_callback("home:howto")
    with patch.object(private_mod, "is_xiaopang", AsyncMock(return_value=False)):
        await private_mod.home_callback(cb_h)
    h_call = cb_h.message.answer.await_args
    h_text = h_call.args[0] if h_call.args else h_call.kwargs.get("text", "")
    assert h_text and "使用说明" in h_text
    assert "home:back" in _all_callback_data(h_call.kwargs["reply_markup"])

    # home:back
    cb_back = _fake_callback("home:back")
    with patch.object(private_mod, "is_xiaopang", AsyncMock(return_value=False)):
        await private_mod.home_callback(cb_back)
    back_call = cb_back.message.answer.await_args
    back_kb = back_call.kwargs.get("reply_markup")
    assert set(_all_callback_data(back_kb)) == {"home:make_image", "home:fun", "home:tools", "home:howto"}
    print("[ok] home:make_image / home:fun / home:tools / home:howto / home:back 行为正确")

    # ---------- 10a) play:<image_tool> & play:<fun_text_tool> 应打开风格子菜单 ----------
    from routers.private import _STYLE_PRESETS, _build_style_picker_keyboard, _resolve_style_name, _style_usage_text
    image_tool_keys = ["plog", "magnet", "y2k", "poster", "img", "meme"]
    fun_text_tool_keys = ["eat", "reply", "eli5"]  # 好玩一下的 3 个文本工具，也走风格菜单
    forbid = AsyncMock(side_effect=AssertionError("按钮不应触发生成接口"))
    with patch.object(private_mod, "generate_plog_image", forbid), \
         patch.object(private_mod, "generate_magnet_image", forbid), \
         patch.object(private_mod, "generate_y2k_image", forbid), \
         patch.object(private_mod, "generate_poster_image", forbid), \
         patch.object(private_mod, "generate_image", forbid), \
         patch.object(private_mod, "run_text_tool", forbid), \
         patch.object(private_mod, "_send_image_tool", forbid):
        for key in image_tool_keys + fun_text_tool_keys:
            cb = _fake_callback(f"play:{key}")
            await private_mod.play_callback(cb)
            assert cb.message.answer.await_count == 1
            args = cb.message.answer.await_args
            title = args.args[0] if args.args else ""
            kb = args.kwargs.get("reply_markup")
            assert isinstance(kb, InlineKeyboardMarkup)
            preset = _STYLE_PRESETS[key]
            assert title == preset["title"], f"play:{key} 应展示风格标题 {preset['title']!r}，得到 {title!r}"
            # 键盘里必须有所有 stylepick:<key>:<idx>
            cbs = _all_callback_data(kb)
            for i in range(len(preset["styles"])):
                assert f"stylepick:{key}:{i}" in cbs, f"风格菜单缺 stylepick:{key}:{i}"
            # 返回按钮：image kind 回「做点图」，text kind 回「好玩一下」
            if key in fun_text_tool_keys:
                assert "home:fun" in cbs, f"play:{key}（好玩一下）应有「返回好玩一下」按钮"
            else:
                assert "home:make_image" in cbs, f"play:{key}（做点图）应有「返回做点图」按钮"
            assert "home:back" in cbs
            # 风格按钮标签必须是中文短词
            labels = _all_labels(kb)
            for s in preset["styles"]:
                assert s in labels, f"风格菜单缺标签 {s}"
            for tok in hidden:
                assert tok not in title and tok not in " ".join(labels)
    total_styles = sum(len(_STYLE_PRESETS[k]["styles"]) for k in image_tool_keys + fun_text_tool_keys)
    print(f"[ok] {len(image_tool_keys)} 个图像工具 + {len(fun_text_tool_keys)} 个好玩文本工具 都打开「风格子菜单」（共 {total_styles} 个风格按钮）")

    # ---------- 10b) stylepick:<tool>:<idx>：新流程 ----------
    # need_photo=True 工具：无照片时显示「记住了风格，发照片后我继续」+ set pending
    # need_photo=False（img/meme）：始终 set pending + 显示「下一条文字会作为描述」
    import services.pending_style_service as pend_svc

    plog_svc.clear_pending_photo(9001)
    pend_svc.clear_pending_style(9001)

    for key in image_tool_keys + fun_text_tool_keys:
        preset = _STYLE_PRESETS[key]
        kind = preset.get("kind", "image")
        need_photo = preset["need_photo"]
        # 选第 0 个风格
        style_name = preset["styles"][0]
        cb = _fake_callback(f"stylepick:{key}:0")
        # 这一阶段（无照片 / img/meme / 文本工具）不应触发生成
        with patch.object(private_mod, "generate_plog_image", forbid), \
             patch.object(private_mod, "generate_magnet_image", forbid), \
             patch.object(private_mod, "generate_y2k_image", forbid), \
             patch.object(private_mod, "generate_poster_image", forbid), \
             patch.object(private_mod, "generate_image", forbid), \
             patch.object(private_mod, "run_text_tool", forbid):
            await private_mod.style_pick_callback(cb)
        assert cb.message.answer.await_count == 1, f"stylepick:{key}:0 应只发一条消息"
        usage = cb.message.answer.await_args.args[0]
        assert style_name in usage, f"风格说明应含 {style_name}: {usage[:60]}"
        if kind == "text":
            # 好玩一下：提示「把xxx发给我就行」+ 「你下一条文字…」
            assert "发给我就行" in usage, f"文本工具风格应提示「把xxx发给我就行」: {usage}"
            assert "下一条文字" in usage
        elif need_photo:
            assert "先发一张照片" in usage and "记住" in usage, f"需要照片但没缓存时应提示「记住了，发照片继续」: {usage}"
        else:
            assert "描述" in usage, f"/img /meme 风格应提示「把描述发给我」: {usage}"
        # set pending
        pending = pend_svc.get_pending_style(9001)
        assert pending is not None and pending.tool == key and pending.style == style_name, (
            f"应已 set_pending_style: {pending}"
        )
        # 尾部按钮
        cbs = _all_callback_data(cb.message.answer.await_args.kwargs["reply_markup"])
        assert f"style:{key}" in cbs and "home:back" in cbs
        for tok in hidden:
            assert tok not in usage
        # 清掉等下一轮
        pend_svc.clear_pending_style(9001)
    print("[ok] stylepick:<tool>:0（图像+好玩文本工具）显示新文案 + set_pending_style；不触发生成")

    # ---------- 10b-2) stylepick:plog:0 有照片缓存时直接触发 run_plog_for_user ----------
    plog_svc.clear_pending_photo(9001)
    pend_svc.clear_pending_style(9001)
    fake_photo_path = os.path.join(tmpdir, "for_style.jpg")
    open(fake_photo_path, "wb").write(b"\xff\xd8\xff\xe0fake")
    plog_svc.remember_photo(9001, file_path=fake_photo_path, file_id="x", caption=None)

    captured_runner_args: dict = {}
    async def fake_run_plog(bot_, msg_, arg_, *, silent_status=False):
        captured_runner_args["plog"] = (arg_, silent_status)
    cb_plog = _fake_callback("stylepick:plog:0")
    with patch.object(private_mod, "run_plog_for_user", side_effect=fake_run_plog), \
         patch.object(private_mod, "generate_plog_image", forbid):
        await private_mod.style_pick_callback(cb_plog)
    # 应至少发一条「我按「xx」帮你出图」状态
    sent_msgs_plog = [c.args[0] for c in cb_plog.message.answer.call_args_list]
    expected_style = _STYLE_PRESETS["plog"]["styles"][0]
    assert any(expected_style in m and "帮你出图" in m for m in sent_msgs_plog), (
        f"应发风格感知状态文案: {sent_msgs_plog}"
    )
    assert captured_runner_args.get("plog") == (expected_style, True), (
        f"应调 run_plog_for_user(style={expected_style!r}, silent_status=True)，得到 {captured_runner_args}"
    )
    # pending 应被清掉
    assert pend_svc.get_pending_style(9001) is None
    print("[ok] 有照片缓存时 stylepick:plog:0 直接调 run_plog_for_user 并清空 pending")

    # 同样验证 magnet/y2k/poster 各自的 runner 都能被直接触发
    plog_svc.clear_pending_photo(9001)
    plog_svc.remember_photo(9001, file_path=fake_photo_path, file_id="x", caption=None)
    captured_runner_args.clear()
    async def fake_run_magnet(bot_, msg_, arg_, *, silent_status=False):
        captured_runner_args["magnet"] = (arg_, silent_status)
    async def fake_run_y2k(bot_, msg_, arg_, *, silent_status=False):
        captured_runner_args["y2k"] = (arg_, silent_status)
    async def fake_run_poster(bot_, msg_, arg_, *, silent_status=False):
        captured_runner_args["poster"] = (arg_, silent_status)
    for tool, runner_name in [
        ("magnet", "run_magnet_for_user"),
        ("y2k", "run_y2k_for_user"),
        ("poster", "run_poster_for_user"),
    ]:
        plog_svc.clear_pending_photo(9001)
        plog_svc.remember_photo(9001, file_path=fake_photo_path, file_id="x", caption=None)
        cb_x = _fake_callback(f"stylepick:{tool}:0")
        with patch.object(private_mod, runner_name,
                          side_effect={"magnet": fake_run_magnet, "y2k": fake_run_y2k, "poster": fake_run_poster}[tool]):
            await private_mod.style_pick_callback(cb_x)
        expected = _STYLE_PRESETS[tool]["styles"][0]
        assert captured_runner_args.get(tool) == (expected, True), (
            f"{tool} 有照片时应直接触发 {runner_name}(style={expected!r}, silent_status=True)"
        )
    print("[ok] 有照片缓存时 magnet/y2k/poster 风格按钮也直接触发对应 runner")

    # ---------- 10b-3) /img 风格 → 下一条文字自动触发 _send_image_tool ----------
    pend_svc.clear_pending_style(9001)
    img_cb = _fake_callback("stylepick:img:0")
    with patch.object(private_mod, "generate_image", forbid):
        await private_mod.style_pick_callback(img_cb)
    pending = pend_svc.get_pending_style(9001)
    assert pending and pending.tool == "img"
    expected_img_style = _STYLE_PRESETS["img"]["styles"][0]
    assert pending.style == expected_img_style

    # 现在模拟下一条 private 文本，应触发 _send_image_tool(img, "<style> <desc>")
    captured_img_arg = {}
    async def fake_send_image_tool(bot_, msg_, tool_, arg_, *, silent_status=False):
        captured_img_arg["data"] = (tool_, arg_, silent_status)
    text_msg = _fake_private_message("一只小猫坐在窗边")
    bot = MagicMock()
    bot.send_photo = AsyncMock()
    async def _capture_send_long_text(bot__, chat_id, text, business_connection_id=None):
        # 状态文案
        pass
    with patch.object(private_mod, "_send_image_tool", side_effect=fake_send_image_tool), \
         patch.object(private_mod, "send_long_text", side_effect=_capture_send_long_text):
        consumed = await private_mod._maybe_consume_pending_for_text(bot, text_msg, "一只小猫坐在窗边")
    assert consumed is True
    tool_, arg_, silent_ = captured_img_arg["data"]
    assert tool_ == "img" and arg_.startswith(expected_img_style) and "一只小猫坐在窗边" in arg_ and silent_ is True
    assert pend_svc.get_pending_style(9001) is None, "消费后 pending 应清空"
    print("[ok] /img 风格 pending：下一条文字自动拼到风格后并触发 _send_image_tool")

    # /meme 同理
    pend_svc.clear_pending_style(9001)
    meme_cb = _fake_callback("stylepick:meme:0")
    with patch.object(private_mod, "generate_image", forbid):
        await private_mod.style_pick_callback(meme_cb)
    expected_meme_style = _STYLE_PRESETS["meme"]["styles"][0]
    assert pend_svc.get_pending_style(9001).style == expected_meme_style
    captured_img_arg.clear()
    text_msg2 = _fake_private_message("我真的会谢")
    with patch.object(private_mod, "_send_image_tool", side_effect=fake_send_image_tool), \
         patch.object(private_mod, "send_long_text", side_effect=_capture_send_long_text):
        consumed2 = await private_mod._maybe_consume_pending_for_text(bot, text_msg2, "我真的会谢")
    assert consumed2 is True
    tool_, arg_, silent_ = captured_img_arg["data"]
    assert tool_ == "meme" and expected_meme_style in arg_ and "我真的会谢" in arg_ and silent_ is True
    print("[ok] /meme 风格 pending：下一条文字自动拼到风格后并触发 _send_image_tool")

    # ---------- 10b-3.5) 好玩一下：eat / reply / eli5 风格 → 下一条文字自动触发 _send_text_tool ----------
    captured_text_arg: dict = {}
    async def fake_send_text_tool(bot_, msg_, tool_, arg_, *, silent_status=False):
        captured_text_arg["data"] = (tool_, arg_, silent_status)

    fun_pairs = [
        ("eat", "我今天好累", "深夜治愈"),
        ("reply", "在干嘛？", "幽默化解"),
        ("eli5", "量子纠缠", "五岁能懂"),
    ]
    for tool, user_text, expected_style in fun_pairs:
        # 用风格菜单设置 pending（验证 callback 路径也对）
        pend_svc.clear_pending_style(9001)
        idx_in_preset = _STYLE_PRESETS[tool]["styles"].index(expected_style)
        cb = _fake_callback(f"stylepick:{tool}:{idx_in_preset}")
        with patch.object(private_mod, "run_text_tool", forbid):
            await private_mod.style_pick_callback(cb)
        p = pend_svc.get_pending_style(9001)
        assert p and p.tool == tool and p.style == expected_style, f"set_pending 应有 {tool}={expected_style}"
        # 触发下一条 private 文本
        captured_text_arg.clear()
        text_msg_f = _fake_private_message(user_text)
        with patch.object(private_mod, "_send_text_tool", side_effect=fake_send_text_tool), \
             patch.object(private_mod, "send_long_text", side_effect=_capture_send_long_text):
            consumed = await private_mod._maybe_consume_pending_for_text(bot, text_msg_f, user_text)
        assert consumed is True, f"{tool} pending 应被消费"
        captured_tool, captured_arg, silent = captured_text_arg["data"]
        assert captured_tool == tool, f"应调 _send_text_tool({tool}, ...)"
        assert captured_arg.startswith(expected_style), f"arg 应以风格开头: {captured_arg!r}"
        assert user_text in captured_arg, f"arg 应包含用户输入 {user_text!r}: {captured_arg!r}"
        assert silent is True, f"应 silent_status=True"
        assert pend_svc.get_pending_style(9001) is None, "消费后 pending 应清空"
    print("[ok] 好玩一下 eat/reply/eli5 风格 pending：下一条非命令文本自动按「风格 + 输入」触发 _send_text_tool")

    # ---------- 10b-4) 命令文本不消费 pending ----------
    pend_svc.set_pending_style(9001, "img", "软萌头像")
    captured_img_arg.clear()
    with patch.object(private_mod, "_send_image_tool", side_effect=fake_send_image_tool):
        consumed3 = await private_mod._maybe_consume_pending_for_text(bot, _fake_private_message("/img 别的描述"), "/img 别的描述")
    assert consumed3 is False, "以 / 开头的命令应被视为显式调用，不消费 pending"
    assert "data" not in captured_img_arg
    assert pend_svc.get_pending_style(9001) is not None, "pending 不应被未消费的命令清掉"
    pend_svc.clear_pending_style(9001)
    print("[ok] 用户直接发命令（/img 别的）不消费 pending")

    # ---------- 10b-5) pending 覆盖 ----------
    pend_svc.clear_pending_style(9001)
    cb_a = _fake_callback("stylepick:img:0")
    cb_b = _fake_callback("stylepick:img:1")
    await private_mod.style_pick_callback(cb_a)
    await private_mod.style_pick_callback(cb_b)
    p = pend_svc.get_pending_style(9001)
    assert p and p.style == _STYLE_PRESETS["img"]["styles"][1], f"新选择应覆盖旧 pending: {p}"
    pend_svc.clear_pending_style(9001)
    print("[ok] 重新选择另一个风格会覆盖旧 pending")

    # ---------- 10b-6) 返回按钮不设置 pending ----------
    pend_svc.clear_pending_style(9001)
    cb_back = _fake_callback("home:back")
    with patch.object(private_mod, "is_xiaopang", AsyncMock(return_value=False)):
        await private_mod.home_callback(cb_back)
    assert pend_svc.get_pending_style(9001) is None, "返回首页不应 set pending"
    # 「⬅️ 返回风格」也不应 set pending
    cb_rs = _fake_callback("style:img")
    await private_mod.style_menu_callback(cb_rs)
    assert pend_svc.get_pending_style(9001) is None
    print("[ok] home:back 与 style:<tool>「返回风格」按钮都不会 set pending")

    # ---------- 10b-7) Business photo handler 不消费 pending ----------
    # 模拟：在 Business 模式收到照片，即便有 pending 也不应该触发生成
    pend_svc.set_pending_style(99988, "plog", "奶油手账")
    import routers.media as media_mod

    # 验证 media._handle_photo 在 business 模式时不会调 run_plog_for_user
    runner_called = {"count": 0}
    async def fake_business_runner(*a, **kw):
        runner_called["count"] += 1
    biz_photo = SimpleNamespace(
        chat=SimpleNamespace(id=99988, type="private"),
        from_user=SimpleNamespace(id=99988, username="biz_pend", is_bot=False),
        business_connection_id="bc-pend",
        sender_business_bot=None,
        text=None,
        photo=[SimpleNamespace(file_id="biz_pf", file_unique_id="bpfu", width=200, height=200)],
        sticker=None, animation=None, voice=None, video=None,
        caption=None,
        message_id=44,
    )
    bot2 = MagicMock()
    bot2.get_file = AsyncMock(return_value=SimpleNamespace(file_path="s/x.jpg"))
    async def _dl(p, d):
        open(d, "wb").write(b"\xff\xd8\xff\xe0biz")
    bot2.download_file = AsyncMock(side_effect=_dl)
    # 让 Business 自检直接 short-circuit：is_self_message=True 即可
    with patch.object(media_mod, "is_self_message", lambda m: True), \
         patch.object(media_mod, "mark_self_silence", lambda m: None), \
         patch.object(media_mod, "record_self_media", AsyncMock()), \
         patch.object(media_mod, "store_message", AsyncMock()), \
         patch.object(private_mod, "run_plog_for_user", side_effect=fake_business_runner):
        await media_mod._handle_photo(biz_photo, bot2)
    assert runner_called["count"] == 0, "Business 模式 photo 即使有 pending 也不能触发 runner"
    # pending 也不应被消费
    assert pend_svc.get_pending_style(99988) is not None, "Business 路径不应消费 pending"
    pend_svc.clear_pending_style(99988)
    print("[ok] Business 模式 photo handler 不消费 pending 风格、不触发生成")

    # ---------- 10c) style:<tool> 「返回风格」回到风格菜单（图像 + 好玩文本工具） ----------
    with patch.object(private_mod, "generate_plog_image", forbid), \
         patch.object(private_mod, "generate_image", forbid):
        for key in image_tool_keys + fun_text_tool_keys:
            cb = _fake_callback(f"style:{key}")
            await private_mod.style_menu_callback(cb)
            assert cb.message.answer.await_count == 1
            args = cb.message.answer.await_args
            assert args.args[0] == _STYLE_PRESETS[key]["title"]
            cbs = _all_callback_data(args.kwargs["reply_markup"])
            assert f"stylepick:{key}:0" in cbs
    print("[ok] style:<tool>「返回风格」按钮正确弹回风格子菜单（含好玩文本工具）")

    # ---------- 10d) play:* 其它（别名 fridge/starposter + 3 个非风格菜单文本工具）仍走原 hint 路径 ----------
    play_other_keys = ["fridge", "starposter", "excel", "polish", "tldr"]
    for key in play_other_keys:
        cb = _fake_callback(f"play:{key}")
        await private_mod.play_callback(cb)
        assert cb.message.answer.await_count == 1
        hint_text = cb.message.answer.await_args.args[0]
        assert hint_text and len(hint_text.strip()) > 0
        # 这类不应进风格菜单（标题里不应直接是某个风格标题）
        assert "选个风格" not in hint_text
        kb = cb.message.answer.await_args.kwargs.get("reply_markup")
        assert "home:back" in _all_callback_data(kb)
        for tok in hidden:
            assert tok not in hint_text
    # _TOOL_HINTS 仍覆盖所有命令键（含图像、好玩文本、3 个非风格菜单文本工具、别名）
    for key in image_tool_keys + fun_text_tool_keys + play_other_keys:
        assert key in _TOOL_HINTS and _TOOL_HINTS[key]
    print(f"[ok] 别名 fridge/starposter + excel/polish/tldr 仍走原 hint 路径")

    # ---------- 11) Business 模式 /start /play /help 不弹菜单 ----------
    biz_msg = _fake_business_message("/start")
    with patch.object(private_mod, "get_chat_mode", lambda m: "business"):
        await private_mod.start_handler(biz_msg)
    assert biz_msg.answer.await_count == 0

    biz_msg2 = _fake_business_message("/play")
    with patch.object(private_mod, "get_chat_mode", lambda m: "business"):
        await private_mod.play_handler(biz_msg2)
    assert biz_msg2.answer.await_count == 0

    biz_msg3 = _fake_business_message("/help")
    biz_help_send = AsyncMock()
    with patch.object(private_mod, "get_chat_mode", lambda m: "business"), \
         patch.object(private_mod, "send_long_text", biz_help_send):
        await private_mod.help_handler(biz_msg3)
    assert biz_help_send.await_count == 0
    print("[ok] Business 模式 /start /play /help 全部直接 return")

    # ---------- 12) 命令反馈机制（P0） ----------
    # 12a) 无照片：/plog /magnet /y2k /poster 都要回明确的「先发一张照片」状态提示
    plog_svc.clear_pending_photo(9001)
    sent_msgs: list[str] = []
    async def fake_send_long_text(bot, chat_id, text, business_connection_id=None):
        sent_msgs.append(text)

    test_msg = _fake_private_message("/plog 可爱手账风")
    bot = MagicMock()
    bot.send_photo = AsyncMock()
    with patch.object(private_mod, "send_long_text", side_effect=fake_send_long_text), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(private_mod, "generate_plog_image", AsyncMock()):
        await private_mod.run_plog_for_user(bot, test_msg, "可爱手账风")
    assert any("先发一张照片" in m for m in sent_msgs), f"/plog 无照片应明确提示先发照片：{sent_msgs}"

    sent_msgs.clear()
    with patch.object(private_mod, "send_long_text", side_effect=fake_send_long_text), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(private_mod, "generate_magnet_image", AsyncMock()):
        await private_mod.run_magnet_for_user(bot, test_msg, "巴黎")
    assert any("先发一张照片" in m for m in sent_msgs)

    sent_msgs.clear()
    with patch.object(private_mod, "send_long_text", side_effect=fake_send_long_text), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(private_mod, "generate_y2k_image", AsyncMock()):
        await private_mod.run_y2k_for_user(bot, test_msg, "粉色少女")
    assert any("先发一张照片" in m for m in sent_msgs)

    sent_msgs.clear()
    with patch.object(private_mod, "send_long_text", side_effect=fake_send_long_text), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(private_mod, "generate_poster_image", AsyncMock()):
        await private_mod.run_poster_for_user(bot, test_msg, "甜酷")
    assert any("先发一张照片" in m for m in sent_msgs)
    print("[ok] /plog /magnet /y2k /poster 无照片时均给出「先发一张照片」明确提示")

    # 12b) 有照片：先回 STATUS_IMAGE_TOOL 再生成
    photo_path = os.path.join(tmpdir, "fake.jpg")
    open(photo_path, "wb").write(b"\xff\xd8\xff\xe0fake")
    plog_svc.remember_photo(9001, file_path=photo_path, file_id=None, caption=None)

    sent_msgs.clear()
    bot.send_photo.reset_mock()
    plog_gen = AsyncMock(return_value={"ok": True, "url": "https://x/p.png", "data": None, "error": None})
    with patch.object(private_mod, "send_long_text", side_effect=fake_send_long_text), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(private_mod, "generate_plog_image", plog_gen):
        await private_mod.run_plog_for_user(bot, test_msg, "可爱手账风")
    assert any(STATUS_IMAGE_TOOL == m for m in sent_msgs), f"/plog 有照片时应先回 STATUS_IMAGE_TOOL：{sent_msgs}"
    assert plog_gen.await_count == 1
    assert bot.send_photo.await_count == 1

    # 12c) _send_image_tool：先回 STATUS_IMAGE_TOOL
    plog_svc.clear_pending_photo(9001)
    sent_msgs.clear()
    bot.send_photo.reset_mock()
    gen_image_mock = AsyncMock(return_value={"ok": True, "url": "https://x/i.png", "data": None, "error": None})
    with patch.object(private_mod, "send_long_text", side_effect=fake_send_long_text), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(private_mod, "generate_image", gen_image_mock):
        await private_mod._send_image_tool(bot, test_msg, "img", "一只穿西装的柴犬")
    assert STATUS_IMAGE_TOOL in sent_msgs, f"/img 应先回 STATUS_IMAGE_TOOL：{sent_msgs}"
    assert gen_image_mock.await_count == 1
    print("[ok] /img /meme /plog /magnet /y2k /poster 有参数/照片时都先回状态提示再生成")

    # 12d) _send_text_tool：先回 STATUS_TEXT_TOOL
    sent_msgs.clear()
    run_text_mock = AsyncMock(return_value="结果")
    with patch.object(private_mod, "send_long_text", side_effect=fake_send_long_text), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(private_mod, "run_text_tool", run_text_mock):
        await private_mod._send_text_tool(bot, test_msg, "tldr", "很长一段文字……")
    assert STATUS_TEXT_TOOL in sent_msgs, f"文本工具应先回 STATUS_TEXT_TOOL：{sent_msgs}"
    assert run_text_mock.await_count == 1
    print("[ok] 文本工具命令（/tldr /polish /eli5 /excel /eat /reply）先回 STATUS_TEXT_TOOL 再给结果")

    await close_db()
    try:
        os.remove(db_path)
    except Exception:
        pass
    print("\nALL ENTERTAINMENT MENU SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
