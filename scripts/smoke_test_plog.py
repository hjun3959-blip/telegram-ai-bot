"""Smoke test：/plog（AI大头贴+生活小报）+ /magnet（AI冰箱贴海报）。

两个独立公开功能，命令、菜单、prompt、提示分开；共用最近一张照片缓存与图像生成封装。

覆盖：
1) /play /help /贝贝菜单文案：包含 /plog、/magnet 与中文展示名
2) /play /help 文案不暴露隐藏管理命令
3) 文本命令 /plog：无照片时温柔提示先发照片，不调用图片生成
4) 文本命令 /magnet：无照片时温柔提示先发照片，不调用图片生成
5) 私信发照片后再发 /plog：会调用 plog_service.generate_plog_image，参数包含 reference_path 与风格
6) 私信发照片后再发 /magnet：会调用 magnet_service.generate_magnet_image，传入 reference_path 与地点
7) 私信 photo caption 直接 /plog：跳过 VISION 聊天，直接走 plog 流程
8) 私信 photo caption 直接 /magnet：跳过 VISION 聊天，直接走 magnet 流程
9) Business 模式 photo：不缓存照片，不触发 /plog 或 /magnet 自动生成
10) plog_service 缓存隔离：不同 user_id 互不干扰；同一 user 覆盖
11) plog_service.consume_pending_photo 后再 get 返回 None
12) plog prompt 含手绘注解模板关键句、magnet prompt 含建筑冰箱贴关键句；两者不互窜
13) /plog 风格识别：贝贝风格、Y2K 等关键词命中各自分支
14) generate_image_from_reference 在 client.images.edit 不存在时降级到 generate_image

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


def _fake_private_text_message(text: str, *, user_id: int = 7001, username: str = "u_tester"):
    chat = SimpleNamespace(id=user_id, type="private")
    from_user = SimpleNamespace(id=user_id, username=username, is_bot=False)
    return SimpleNamespace(
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
        message_id=1,
        bot=None,
    )


def _fake_private_photo_message(*, caption: str | None = None, user_id: int = 7001, username: str = "u_tester"):
    chat = SimpleNamespace(id=user_id, type="private")
    from_user = SimpleNamespace(id=user_id, username=username, is_bot=False)
    photo = [SimpleNamespace(file_id="fake_photo_id", file_unique_id="fpuid", width=512, height=512)]
    return SimpleNamespace(
        chat=chat,
        from_user=from_user,
        business_connection_id=None,
        sender_business_bot=None,
        text=None,
        photo=photo,
        sticker=None,
        animation=None,
        voice=None,
        video=None,
        caption=caption,
        message_id=2,
    )


def _fake_business_photo_message(*, caption: str | None = None, user_id: int = 42, username: str = "yj_syj"):
    chat = SimpleNamespace(id=user_id, type="private")
    from_user = SimpleNamespace(id=user_id, username=username, is_bot=False)
    photo = [SimpleNamespace(file_id="biz_photo_id", file_unique_id="bfpuid", width=512, height=512)]
    return SimpleNamespace(
        chat=chat,
        from_user=from_user,
        business_connection_id="bc-x",
        sender_business_bot=None,
        text=None,
        photo=photo,
        sticker=None,
        animation=None,
        voice=None,
        video=None,
        caption=caption,
        message_id=3,
    )


async def main() -> None:
    tmpdir = tempfile.mkdtemp(prefix="plog_smoke_")
    db_path = os.path.join(tmpdir, "smoke.sqlite3")
    os.environ["BOT_DB_PATH"] = db_path
    os.environ["PLOG_CACHE_DIR"] = os.path.join(tmpdir, "plog_cache")
    os.environ.setdefault("BUSINESS_REPLY_DELAY_MIN", "0.0")
    os.environ.setdefault("BUSINESS_REPLY_DELAY_MAX", "0.0")
    os.environ.setdefault("BUSINESS_REPLY_DELAY_PER_CHAR", "0.0")
    os.environ.setdefault("BUSINESS_REPLY_DELAY_JITTER", "0.0")

    for mod in (
        "config",
        "db.core",
        "services.business_memory_service",
        "services.contact_service",
        "services.context_service",
        "services.history_service",
        "services.message_service",
        "services.openai_service",
        "services.xiaopang_service",
        "services.self_media_service",
        "services.plog_service",
        "services.magnet_service",
        "services.image_generation_service",
        "routers.business",
        "routers.media",
        "routers.private",
    ):
        sys.modules.pop(mod, None)

    import config  # noqa: F401
    from db.core import init_db, close_db
    await init_db()

    # ---------- 1) /play /help / HOW_TO_USE_TEXT / _TOOL_HINTS 覆盖图片创作命令 ----------
    #
    # 菜单二次收口后：PLAY_MENU_TEXT / BEIBEI_PLAY_MENU_TEXT 是首页极简文案，
    # 不再直接铺命令；具体命令汇总到 HOW_TO_USE_TEXT 与 _TOOL_HINTS 里。
    # HELP_TEXT 仍含完整 PLAY_MENU_TEXT 内容做兼容。
    from routers.private import (
        PLAY_MENU_TEXT,
        HELP_TEXT,
        BEIBEI_PLAY_MENU_TEXT,
        BEIBEI_HELP_TEXT,
        HOW_TO_USE_TEXT,
        _TOOL_HINTS,
    )
    # HOW_TO_USE_TEXT 应汇总所有图片创作命令（含 /starposter 至少别名提及）
    for kw in ("/plog", "/magnet", "/fridge", "/y2k", "/poster", "/starposter"):
        assert kw in HOW_TO_USE_TEXT, f"HOW_TO_USE_TEXT 缺少 {kw}"
    # _TOOL_HINTS 必须含每个命令对应的 hint 文案
    for key in ("plog", "magnet", "fridge", "y2k", "poster", "starposter"):
        hint = _TOOL_HINTS.get(key, "")
        assert hint, f"_TOOL_HINTS 缺少 {key}"
        assert f"/{key}" in hint, f"_TOOL_HINTS[{key}] 应提到 /{key}"
    print("[ok] HOW_TO_USE_TEXT + _TOOL_HINTS 覆盖 /plog /magnet /fridge /y2k /poster /starposter 命令名")

    # ---------- 2) /play /help 不暴露隐藏管理 ----------
    hidden = ["/小胖", "/学习小胖", "/新计划", "/计划列表", "/联系人列表", "/添加联系人", "/删除联系人", "管理面板", "授权"]
    for text in (PLAY_MENU_TEXT, HELP_TEXT, BEIBEI_PLAY_MENU_TEXT, BEIBEI_HELP_TEXT, HOW_TO_USE_TEXT):
        for tok in hidden:
            assert tok not in text, f"菜单暴露了隐藏功能 {tok}"
    print("[ok] /play /help 文案未暴露任何隐藏管理命令")

    # ---------- 3) 文本命令 /plog 无照片：温柔提示先发照片，不调用图片生成 ----------
    import routers.private as private_mod
    import services.plog_service as plog_svc

    # 清干净缓存
    plog_svc.clear_pending_photo(7001)

    sent_msgs: list[str] = []
    async def fake_send_long_text(bot, chat_id, text, business_connection_id=None):
        sent_msgs.append(text)

    bot = MagicMock()
    bot.send_photo = AsyncMock()
    msg = _fake_private_text_message("/plog 可爱手账风")
    plog_gen_mock = AsyncMock(return_value={"ok": True, "url": "https://x/y.png", "data": None, "error": None})
    with patch.object(private_mod, "send_long_text", side_effect=fake_send_long_text), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(private_mod, "generate_plog_image", plog_gen_mock):
        await private_mod.run_plog_for_user(bot, msg, "可爱手账风")
    assert any("先发" in m and "/plog" in m for m in sent_msgs), f"应提示先发照片: {sent_msgs}"
    assert plog_gen_mock.await_count == 0, "无照片时不应调用图片生成"
    assert bot.send_photo.await_count == 0
    print("[ok] /plog 无照片时提示先发照片，不调用图片生成")

    # ---------- 4) 文本命令 /magnet 无照片：温柔提示，不调用 ----------
    sent_msgs.clear()
    plog_svc.clear_pending_photo(7001)
    magnet_gen_mock = AsyncMock(return_value={"ok": True, "url": "https://x/z.png", "data": None, "error": None, "location": "", "yyyymm": ""})
    with patch.object(private_mod, "send_long_text", side_effect=fake_send_long_text), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(private_mod, "generate_magnet_image", magnet_gen_mock):
        await private_mod.run_magnet_for_user(bot, msg, "巴黎")
    assert any("先发" in m and "/magnet" in m for m in sent_msgs), f"应提示先发照片: {sent_msgs}"
    assert magnet_gen_mock.await_count == 0
    print("[ok] /magnet 无照片时提示先发照片，不调用图片生成")

    # ---------- 5) 有照片 → /plog：调用 plog generate；参考图与风格传对 ----------
    # 先放一张模拟“最近照片”到缓存
    fake_photo_path = os.path.join(tmpdir, "fake_photo.jpg")
    with open(fake_photo_path, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0fake-jpg-bytes")
    plog_svc.remember_photo(7001, file_path=fake_photo_path, file_id="fake_photo_id", caption=None)

    captured_plog_calls: list = []
    async def fake_generate_plog_image(**kw):
        captured_plog_calls.append(kw)
        return {"ok": True, "url": "https://x/plog.png", "data": None, "error": None, "style": "可爱手账风"}

    bot.send_photo.reset_mock()
    sent_msgs.clear()
    with patch.object(private_mod, "send_long_text", side_effect=fake_send_long_text), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(private_mod, "generate_plog_image", side_effect=fake_generate_plog_image):
        await private_mod.run_plog_for_user(bot, msg, "可爱手账风")

    assert len(captured_plog_calls) == 1, f"应调用一次 plog 生成，得到 {len(captured_plog_calls)}"
    args = captured_plog_calls[0]
    assert args.get("reference_path") == fake_photo_path, f"reference_path 错误: {args}"
    assert args.get("style_raw") == "可爱手账风"
    # 发出了照片
    assert bot.send_photo.await_count == 1, "应发出 1 张生成的照片"
    # 缓存已清空
    assert plog_svc.get_pending_photo(7001) is None
    print("[ok] 有照片时 /plog 会调用 generate_plog_image 并发出图片，缓存被清理")

    # ---------- 6) 有照片 → /magnet：调用 magnet generate；地点 raw_arg 传对 ----------
    plog_svc.remember_photo(7001, file_path=fake_photo_path, file_id="fake_photo_id", caption=None)
    captured_magnet_calls: list = []
    async def fake_generate_magnet_image(**kw):
        captured_magnet_calls.append(kw)
        return {"ok": True, "url": "https://x/magnet.png", "data": None, "error": None, "location": "巴黎", "yyyymm": "2026.05"}

    bot.send_photo.reset_mock()
    sent_msgs.clear()
    with patch.object(private_mod, "send_long_text", side_effect=fake_send_long_text), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(private_mod, "generate_magnet_image", side_effect=fake_generate_magnet_image):
        await private_mod.run_magnet_for_user(bot, msg, "巴黎")

    assert len(captured_magnet_calls) == 1
    margs = captured_magnet_calls[0]
    assert margs.get("reference_path") == fake_photo_path
    assert margs.get("raw_arg") == "巴黎"
    assert bot.send_photo.await_count == 1
    assert plog_svc.get_pending_photo(7001) is None
    print("[ok] 有照片时 /magnet 会调用 generate_magnet_image 并发出图片，缓存被清理")

    # ---------- 7) caption=/plog 直接走 plog 流程，跳过 VISION ----------
    import routers.media as media_mod

    # 重建一张文件让 media 流程能读
    with open(fake_photo_path, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0fake-jpg-bytes")

    plog_run_mock = AsyncMock()
    magnet_run_mock = AsyncMock()
    visual_summary_mock = AsyncMock(return_value="some visual summary")
    final_core_mock = AsyncMock(return_value={"reply_text": "x", "sticker_type": None, "should_reply": True, "risk_note": ""})
    call_openai_trap = AsyncMock(return_value={"reply_text": "y", "sticker_type": None})

    download_mock = AsyncMock()
    async def fake_download_file(path, dst):
        # 落地一个伪 jpg，让后续 cache 拷贝成功
        with open(dst, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0downloaded")
    bot.get_file = AsyncMock(return_value=SimpleNamespace(file_path="s/p.jpg"))
    bot.download_file = AsyncMock(side_effect=fake_download_file)

    photo_msg = _fake_private_photo_message(caption="/plog 甜点小报", user_id=7001)
    with patch.object(media_mod, "encode_image_to_base64", AsyncMock(return_value="ZmFrZQ==")), \
         patch.object(media_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(media_mod, "_visual_summary_via_vision", visual_summary_mock), \
         patch.object(media_mod, "_final_reply_via_core_model", final_core_mock), \
         patch.object(media_mod, "call_openai", call_openai_trap), \
         patch.object(media_mod, "send_reply", AsyncMock()), \
         patch.object(media_mod, "store_message", AsyncMock()), \
         patch.object(media_mod, "safe_remove", AsyncMock()), \
         patch.object(private_mod, "run_plog_for_user", plog_run_mock), \
         patch.object(private_mod, "run_magnet_for_user", magnet_run_mock):
        await media_mod._handle_photo(photo_msg, bot)

    assert plog_run_mock.await_count == 1, "caption=/plog 应触发 run_plog_for_user"
    assert magnet_run_mock.await_count == 0
    # VISION 与 chat call 不应被触发用于聊天回复
    assert call_openai_trap.await_count == 0, f"caption=/plog 不应触发 private 聊天 call_openai；得到 {call_openai_trap.await_args_list}"
    print("[ok] private photo caption=/plog 跳过 VISION 聊天，直接进入 plog 流程")

    # ---------- 8) caption=/magnet 直接走 magnet 流程 ----------
    plog_run_mock.reset_mock()
    magnet_run_mock.reset_mock()
    call_openai_trap.reset_mock()
    photo_msg2 = _fake_private_photo_message(caption="/magnet 京都 2025.10", user_id=7001)
    with patch.object(media_mod, "encode_image_to_base64", AsyncMock(return_value="ZmFrZQ==")), \
         patch.object(media_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(media_mod, "_visual_summary_via_vision", visual_summary_mock), \
         patch.object(media_mod, "_final_reply_via_core_model", final_core_mock), \
         patch.object(media_mod, "call_openai", call_openai_trap), \
         patch.object(media_mod, "send_reply", AsyncMock()), \
         patch.object(media_mod, "store_message", AsyncMock()), \
         patch.object(media_mod, "safe_remove", AsyncMock()), \
         patch.object(private_mod, "run_plog_for_user", plog_run_mock), \
         patch.object(private_mod, "run_magnet_for_user", magnet_run_mock):
        await media_mod._handle_photo(photo_msg2, bot)
    assert magnet_run_mock.await_count == 1
    assert plog_run_mock.await_count == 0
    assert call_openai_trap.await_count == 0
    # 拿到风格参数
    call_args = magnet_run_mock.await_args
    assert call_args.args[2] == "京都 2025.10", f"raw_arg 透传错误: {call_args.args}"
    print("[ok] private photo caption=/magnet 跳过 VISION 聊天，直接进入 magnet 流程，参数正确透传")

    # ---------- 9) Business 模式 photo：不缓存，不触发自动生成 ----------
    plog_run_mock.reset_mock()
    magnet_run_mock.reset_mock()
    plog_svc.clear_pending_photo(42)
    biz_photo_msg = _fake_business_photo_message(caption="/plog 可爱手账风", user_id=42)
    final_core_mock_biz = AsyncMock(return_value={"reply_text": "嗯", "sticker_type": None, "should_reply": True, "risk_note": ""})
    with patch.object(media_mod, "encode_image_to_base64", AsyncMock(return_value="ZmFrZQ==")), \
         patch.object(media_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(media_mod, "_visual_summary_via_vision", visual_summary_mock), \
         patch.object(media_mod, "_final_reply_via_core_model", final_core_mock_biz), \
         patch.object(media_mod, "human_typing_delay", AsyncMock()), \
         patch.object(media_mod, "send_reply", AsyncMock()), \
         patch.object(media_mod, "store_message", AsyncMock()), \
         patch.object(media_mod, "safe_remove", AsyncMock()), \
         patch.object(private_mod, "run_plog_for_user", plog_run_mock), \
         patch.object(private_mod, "run_magnet_for_user", magnet_run_mock):
        await media_mod._handle_photo(biz_photo_msg, bot)

    assert plog_run_mock.await_count == 0, "Business 模式绝不可触发 /plog 自动生成"
    assert magnet_run_mock.await_count == 0, "Business 模式绝不可触发 /magnet 自动生成"
    assert plog_svc.get_pending_photo(42) is None, "Business 模式不应缓存照片到 plog 池"
    print("[ok] Business 模式 photo 不缓存、不触发 /plog 或 /magnet 自动生成")

    # ---------- 10) 缓存隔离 + 覆盖 ----------
    plog_svc.clear_pending_photo(1001)
    plog_svc.clear_pending_photo(1002)
    p1 = os.path.join(tmpdir, "u1.jpg"); open(p1, "wb").write(b"a")
    p2 = os.path.join(tmpdir, "u2.jpg"); open(p2, "wb").write(b"b")
    p3 = os.path.join(tmpdir, "u1b.jpg"); open(p3, "wb").write(b"c")
    plog_svc.remember_photo(1001, file_path=p1, file_id=None)
    plog_svc.remember_photo(1002, file_path=p2, file_id=None)
    assert plog_svc.get_pending_photo(1001).file_path == p1
    assert plog_svc.get_pending_photo(1002).file_path == p2
    # 覆盖
    plog_svc.remember_photo(1001, file_path=p3, file_id=None)
    assert plog_svc.get_pending_photo(1001).file_path == p3
    # 原 p1 应被删
    assert not os.path.exists(p1), "覆盖时旧本地文件应被清理"
    print("[ok] plog 缓存：用户隔离 + 同用户覆盖时旧本地文件被清理")

    # ---------- 11) consume 后 get 返回 None ----------
    plog_svc.consume_pending_photo(1001)
    assert plog_svc.get_pending_photo(1001) is None
    print("[ok] consume_pending_photo 后 get_pending_photo 返回 None")

    # ---------- 12) plog prompt 与 magnet prompt 关键句独立、不互窜 ----------
    from services.plog_service import build_plog_prompt, PLOG_HAND_DRAWN_ANNOTATION_TEMPLATE
    from services.magnet_service import (
        build_magnet_prompt,
        MAGNET_TOP_HALF_DIRECTIVE,
        MAGNET_BOTTOM_HALF_DIRECTIVE,
        MAGNET_HARD_NEGATIVES,
    )

    plog_p = build_plog_prompt("可爱手账风", beibei=False)
    magnet_p = build_magnet_prompt("巴黎 2025.10", beibei=False)

    # plog 含手绘注解模板的关键词
    for kw in [
        "手绘",
        "手写",
        "白色笔画",
        "饮料",
        "食物",
        "今天有点幸福",
    ]:
        assert kw in plog_p, f"plog prompt 缺少关键词 {kw}"
    # plog 不应混入冰箱贴/海报相关
    for kw in ["冰箱贴", "上半 50%", "下半 50%", "YYYY.MM"]:
        assert kw not in plog_p, f"plog prompt 不应包含 magnet 关键词 {kw}"
    # magnet 含建筑冰箱贴/上下半结构关键词
    for kw in ["冰箱贴", "上半", "下半", "纯色背景", "建筑", "2025.10"]:
        assert kw in magnet_p, f"magnet prompt 缺少关键词 {kw}"
    # magnet 不应把自己定位成 plog/手账风：检查这些 plog 关键词没有出现在
    # magnet 的“正向风格描述区”里。允许在硬禁忌里出现“不要大头贴/手账”这种否定句。
    magnet_style_block = magnet_p.split("硬禁忌")[0]
    for kw in ["plog", "Y2K", "小分身"]:
        assert kw not in magnet_style_block, f"magnet 正向描述里不应出现 plog 关键词 {kw}"
    # 模板常量本身不互相依赖
    assert "手绘" in PLOG_HAND_DRAWN_ANNOTATION_TEMPLATE
    assert "冰箱贴" in MAGNET_TOP_HALF_DIRECTIVE
    assert "原照片" in MAGNET_BOTTOM_HALF_DIRECTIVE
    assert "乱码" in MAGNET_HARD_NEGATIVES
    print("[ok] plog / magnet prompt 关键句独立、不互窜")

    # ---------- 13) plog 风格识别 ----------
    from services.plog_service import resolve_style
    # "贝贝风格" 应命中贝贝同义词组 -> 同义词组第一项是 "贝贝"
    name_b, frag_b = resolve_style("贝贝风格")
    assert name_b == "贝贝", f"贝贝风格应命中贝贝同义词组，得到 {name_b}"
    assert "贝贝" in frag_b or "粉白" in frag_b
    # 用纯 "Y2K"（避免与“拼贴”首先命中“可爱手账”同义词组）
    name_y, frag_y = resolve_style("Y2K")
    assert "Y2K" in frag_y or "千禧" in frag_y or "果冻" in frag_y
    name_d, frag_d = resolve_style("甜点小报")
    assert "甜点" in frag_d or "奶油" in frag_d
    print("[ok] plog 风格关键词识别：贝贝/Y2K/甜点 都命中各自分支")

    # ---------- 13b) /plog Q 版分身 sub-mode：完全独立于手绘注解模板 ----------
    from services.plog_service import (
        PLOG_Q_VERSION_TEMPLATE,
        _matches_q_version,
        Q_VERSION_SYNONYMS,
    )

    # 关键词识别命中
    for kw in ["q版", "Q版", "q版 一起来玩", "分身", "大头贴", "sd公仔", "chibi", "q版手账", "手账照"]:
        assert _matches_q_version(kw), f"应命中 Q 版 sub-mode: {kw}"
    for kw in ["可爱手账风", "甜点小报", "Y2K少女拼贴", "巴黎", ""]:
        assert not _matches_q_version(kw), f"不应命中 Q 版 sub-mode: {kw!r}"

    # Q 版 prompt 内容：当前正式模板=「Q版分身手账照」
    # 关键词：保留真人 + 5-8 个 chibi 分身 + 手账涂鸦 + 手写短句 + 主题动作
    qp = build_plog_prompt("q版", beibei=False)
    for kw in ["Q版分身手账照", "5-8", "chibi", "保留", "脸部", "手写", "涂鸦", "贴纸", "工作", "自拍", "运动"]:
        assert kw in qp, f"Q 版 prompt 缺少关键词 {kw}"
    # Q 版分支绝不能混入手绘注解模板的关键词
    for kw in ["饮料 -> 味道", "白色笔画的细线手绘线条", "今天有点幸福", "小红书/生活 plog 风格的拼贴图"]:
        assert kw not in qp, f"Q 版 prompt 不应包含手绘注解关键词 {kw}"

    # 反过来：默认手绘注解分支不能混入 Q 版关键词
    annot_p = build_plog_prompt("可爱手账风", beibei=False)
    for kw in ["Q版分身手账照", "5-8 个", "chibi 贴纸风格"]:
        assert kw not in annot_p, f"手绘注解 prompt 不应包含 Q 版关键词 {kw}"

    # user_caption 也可以触发 Q 版（用户在 caption 里写 "q版" 也算）
    qp_cap = build_plog_prompt(None, user_caption="q版")
    assert "Q版分身手账照" in qp_cap and "饮料 -> 味道" not in qp_cap

    # 模板常量本身不互相依赖
    assert "Q版分身手账照" in PLOG_Q_VERSION_TEMPLATE
    assert "饮料 -> 味道" not in PLOG_Q_VERSION_TEMPLATE
    assert "q版" in Q_VERSION_SYNONYMS

    # 新增：「Q版分身手账照」官方模板常量公开可访问
    from services.plog_service import PLOG_Q_VERSION_HANDBOOK_TEMPLATE
    for kw in ["Q版分身手账照", "保留原图", "5-8", "chibi", "手写", "贴纸呈现", "手绘涂鸦", "构图", "避免"]:
        assert kw in PLOG_Q_VERSION_HANDBOOK_TEMPLATE, f"Q版分身手账照 模板缺少关键词 {kw}"

    # chibi / 手账照 这两个关键词应该单独可触发
    assert _matches_q_version("chibi")
    assert _matches_q_version("手账照")
    print("[ok] /plog Q 版分身手账照 模板生效；与手绘注解 sub-mode 完全独立")

    # ---------- 14) generate_image_from_reference 降级 ----------
    import services.image_generation_service as igs
    # 强行让 client.images.edit 不存在：把 client 替换成 mock
    fake_client = SimpleNamespace(images=SimpleNamespace())  # 没有 edit 属性
    with patch.object(igs, "client", fake_client), \
         patch.object(igs, "generate_image", AsyncMock(return_value={"ok": True, "url": "x", "data": None, "error": None})) as gen_mock:
        out = await igs.generate_image_from_reference(prompt="hello", reference_path=fake_photo_path)
        assert out["ok"] is True
        # 必须降级调用 generate_image
        assert gen_mock.await_count == 1, "无 images.edit 时应降级到 generate_image"
    # reference_path 为 None 时也走 generate_image
    with patch.object(igs, "generate_image", AsyncMock(return_value={"ok": True, "url": "y", "data": None, "error": None})) as gen_mock2:
        out2 = await igs.generate_image_from_reference(prompt="hi", reference_path=None)
        assert out2["ok"] is True and gen_mock2.await_count == 1
    print("[ok] generate_image_from_reference 在 images.edit 不可用 / 无参考图时自动降级")

    # ---------- 15) /y2k 与 /poster 独立底层 service ----------
    # 15a) build_y2k_prompt 含用户提供的正式拼贴海报关键词，不混入 plog/magnet/poster
    from services.y2k_service import build_y2k_prompt, Y2K_COLLAGE_TEMPLATE
    y2k_p = build_y2k_prompt("粉色少女拼贴")
    for kw in ["Y2K美学", "剪贴簿", "拼贴海报", "韩国少女", "拍立得", "泡泡糖", "SO CUTE!", "199X!", "GIRL VIBES"]:
        assert kw in y2k_p, f"y2k prompt 缺少关键词 {kw}"
    # y2k 不能混入别的功能的关键句
    for kw in ["冰箱贴", "上半 50%", "手绘风注解", "在周围生成 8 个迷你分身", "明星拼贴海报"]:
        assert kw not in y2k_p, f"y2k prompt 不应包含别的功能关键词 {kw}"
    assert "拼贴海报" in Y2K_COLLAGE_TEMPLATE
    print("[ok] /y2k prompt 独立且包含用户正式拼贴海报关键词，不与 plog/magnet/poster 混用")

    # 15b) build_poster_prompt 含用户提供的明星拼贴海报关键词，不混入别的功能
    from services.poster_service import build_poster_prompt, POSTER_CORE_DIRECTIVE, POSTER_HARD_NEGATIVES
    poster_p = build_poster_prompt("甜酷复古")
    for kw in [
        "粉色系",
        "拼贴艺术",
        "明星",
        "网红",
        "时尚模特",
        "浅粉",
        "桃粉",
        "玫瑰粉",
        "金属光泽",
        "渐变字体",
        "镭射",
        "荧光",
        "甜美",
        "酷帅",
        "复古",
    ]:
        assert kw in poster_p, f"poster prompt 缺少关键词 {kw}"
    # poster 不能混入别的功能的关键句
    for kw in [
        "冰箱贴",
        "上半 50%",
        "下半 50%",
        "手绘风注解",
        "在周围生成 8 个迷你分身",
        "今天有点幸福",
    ]:
        assert kw not in poster_p, f"poster prompt 不应包含别的功能关键词 {kw}"
    assert "粉色系" in POSTER_CORE_DIRECTIVE
    assert "乱码" in POSTER_HARD_NEGATIVES
    print("[ok] /poster prompt 独立且包含用户正式明星拼贴海报关键词，不与别的功能混用")

    # 15c) /y2k 无照片时提示先发照片，不调用生成
    sent_msgs.clear()
    plog_svc.clear_pending_photo(7001)
    y2k_gen_mock = AsyncMock(return_value={"ok": True, "url": "https://x/y2k.png", "data": None, "error": None, "fallback_to_text2image": False})
    with patch.object(private_mod, "send_long_text", side_effect=fake_send_long_text), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(private_mod, "generate_y2k_image", y2k_gen_mock):
        await private_mod.run_y2k_for_user(bot, msg, "粉色少女拼贴")
    assert any("先发" in m and "/y2k" in m for m in sent_msgs), f"应提示先发照片: {sent_msgs}"
    assert y2k_gen_mock.await_count == 0
    print("[ok] /y2k 无照片时提示先发照片，不调用图片生成")

    # 15d) /poster 无照片时提示先发照片，不调用生成
    sent_msgs.clear()
    plog_svc.clear_pending_photo(7001)
    poster_gen_mock = AsyncMock(return_value={"ok": True, "url": "https://x/poster.png", "data": None, "error": None, "fallback_to_text2image": False})
    with patch.object(private_mod, "send_long_text", side_effect=fake_send_long_text), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(private_mod, "generate_poster_image", poster_gen_mock):
        await private_mod.run_poster_for_user(bot, msg, "甜酷复古")
    assert any("先发" in m and "/poster" in m for m in sent_msgs), f"应提示先发照片: {sent_msgs}"
    assert poster_gen_mock.await_count == 0
    print("[ok] /poster 无照片时提示先发照片，不调用图片生成")

    # 15e) 有照片 → /y2k：调用 y2k generate，参考图与 raw_arg 传对
    with open(fake_photo_path, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0fake-jpg-bytes")
    plog_svc.remember_photo(7001, file_path=fake_photo_path, file_id="fake_photo_id", caption=None)
    captured_y2k_calls: list = []
    async def fake_generate_y2k_image(**kw):
        captured_y2k_calls.append(kw)
        return {"ok": True, "url": "https://x/y2k.png", "data": None, "error": None, "fallback_to_text2image": False}
    bot.send_photo.reset_mock()
    sent_msgs.clear()
    with patch.object(private_mod, "send_long_text", side_effect=fake_send_long_text), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(private_mod, "generate_y2k_image", side_effect=fake_generate_y2k_image):
        await private_mod.run_y2k_for_user(bot, msg, "粉色少女拼贴")
    assert len(captured_y2k_calls) == 1
    yargs = captured_y2k_calls[0]
    assert yargs.get("reference_path") == fake_photo_path
    assert yargs.get("raw_arg") == "粉色少女拼贴"
    assert bot.send_photo.await_count == 1
    assert plog_svc.get_pending_photo(7001) is None
    print("[ok] 有照片时 /y2k 会调用 generate_y2k_image 并发出图片，缓存被清理")

    # 15f) 有照片 → /poster：调用 poster generate，参考图与 raw_arg 传对
    plog_svc.remember_photo(7001, file_path=fake_photo_path, file_id="fake_photo_id", caption=None)
    captured_poster_calls: list = []
    async def fake_generate_poster_image(**kw):
        captured_poster_calls.append(kw)
        return {"ok": True, "url": "https://x/poster.png", "data": None, "error": None, "fallback_to_text2image": False}
    bot.send_photo.reset_mock()
    sent_msgs.clear()
    with patch.object(private_mod, "send_long_text", side_effect=fake_send_long_text), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(private_mod, "generate_poster_image", side_effect=fake_generate_poster_image):
        await private_mod.run_poster_for_user(bot, msg, "甜酷复古")
    assert len(captured_poster_calls) == 1
    pargs = captured_poster_calls[0]
    assert pargs.get("reference_path") == fake_photo_path
    assert pargs.get("raw_arg") == "甜酷复古"
    assert bot.send_photo.await_count == 1
    assert plog_svc.get_pending_photo(7001) is None
    print("[ok] 有照片时 /poster 会调用 generate_poster_image 并发出图片，缓存被清理")

    # 15g) caption=/y2k 跳过 VISION，走 y2k 流程
    with open(fake_photo_path, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0fake-jpg-bytes")
    y2k_run_mock = AsyncMock()
    poster_run_mock = AsyncMock()
    plog_run_mock2 = AsyncMock()
    magnet_run_mock2 = AsyncMock()
    visual_summary_mock2 = AsyncMock(return_value="visual")
    final_core_mock2 = AsyncMock(return_value={"reply_text": "x", "sticker_type": None, "should_reply": True, "risk_note": ""})
    call_openai_trap2 = AsyncMock(return_value={"reply_text": "y", "sticker_type": None})
    bot.get_file = AsyncMock(return_value=SimpleNamespace(file_path="s/p.jpg"))
    async def fake_download_file2(path, dst):
        with open(dst, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0downloaded")
    bot.download_file = AsyncMock(side_effect=fake_download_file2)
    photo_msg_y2k = _fake_private_photo_message(caption="/y2k 粉色少女", user_id=7001)
    with patch.object(media_mod, "encode_image_to_base64", AsyncMock(return_value="ZmFrZQ==")), \
         patch.object(media_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(media_mod, "_visual_summary_via_vision", visual_summary_mock2), \
         patch.object(media_mod, "_final_reply_via_core_model", final_core_mock2), \
         patch.object(media_mod, "call_openai", call_openai_trap2), \
         patch.object(media_mod, "send_reply", AsyncMock()), \
         patch.object(media_mod, "store_message", AsyncMock()), \
         patch.object(media_mod, "safe_remove", AsyncMock()), \
         patch.object(private_mod, "run_plog_for_user", plog_run_mock2), \
         patch.object(private_mod, "run_magnet_for_user", magnet_run_mock2), \
         patch.object(private_mod, "run_y2k_for_user", y2k_run_mock), \
         patch.object(private_mod, "run_poster_for_user", poster_run_mock):
        await media_mod._handle_photo(photo_msg_y2k, bot)
    assert y2k_run_mock.await_count == 1
    assert poster_run_mock.await_count == 0
    assert plog_run_mock2.await_count == 0
    assert magnet_run_mock2.await_count == 0
    assert call_openai_trap2.await_count == 0, "caption=/y2k 不应触发 private 聊天 call_openai"
    print("[ok] private photo caption=/y2k 跳过 VISION聊天，直接进入 y2k 流程")

    # 15h) caption=/poster 跳过 VISION，走 poster 流程
    y2k_run_mock.reset_mock()
    poster_run_mock.reset_mock()
    plog_run_mock2.reset_mock()
    magnet_run_mock2.reset_mock()
    call_openai_trap2.reset_mock()
    photo_msg_poster = _fake_private_photo_message(caption="/poster 甜酷复古", user_id=7001)
    with patch.object(media_mod, "encode_image_to_base64", AsyncMock(return_value="ZmFrZQ==")), \
         patch.object(media_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(media_mod, "_visual_summary_via_vision", visual_summary_mock2), \
         patch.object(media_mod, "_final_reply_via_core_model", final_core_mock2), \
         patch.object(media_mod, "call_openai", call_openai_trap2), \
         patch.object(media_mod, "send_reply", AsyncMock()), \
         patch.object(media_mod, "store_message", AsyncMock()), \
         patch.object(media_mod, "safe_remove", AsyncMock()), \
         patch.object(private_mod, "run_plog_for_user", plog_run_mock2), \
         patch.object(private_mod, "run_magnet_for_user", magnet_run_mock2), \
         patch.object(private_mod, "run_y2k_for_user", y2k_run_mock), \
         patch.object(private_mod, "run_poster_for_user", poster_run_mock):
        await media_mod._handle_photo(photo_msg_poster, bot)
    assert poster_run_mock.await_count == 1
    assert y2k_run_mock.await_count == 0
    assert plog_run_mock2.await_count == 0
    assert magnet_run_mock2.await_count == 0
    assert call_openai_trap2.await_count == 0
    # /starposter 别名
    poster_run_mock.reset_mock()
    photo_msg_star = _fake_private_photo_message(caption="/starposter", user_id=7001)
    with patch.object(media_mod, "encode_image_to_base64", AsyncMock(return_value="ZmFrZQ==")), \
         patch.object(media_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(media_mod, "_visual_summary_via_vision", visual_summary_mock2), \
         patch.object(media_mod, "_final_reply_via_core_model", final_core_mock2), \
         patch.object(media_mod, "call_openai", call_openai_trap2), \
         patch.object(media_mod, "send_reply", AsyncMock()), \
         patch.object(media_mod, "store_message", AsyncMock()), \
         patch.object(media_mod, "safe_remove", AsyncMock()), \
         patch.object(private_mod, "run_plog_for_user", plog_run_mock2), \
         patch.object(private_mod, "run_magnet_for_user", magnet_run_mock2), \
         patch.object(private_mod, "run_y2k_for_user", y2k_run_mock), \
         patch.object(private_mod, "run_poster_for_user", poster_run_mock):
        await media_mod._handle_photo(photo_msg_star, bot)
    assert poster_run_mock.await_count == 1, "/starposter 别名应触发 run_poster_for_user"
    print("[ok] private photo caption=/poster 与 /starposter 都跳过 VISION 聊天，直接进入 poster 流程")

    # 15i) Business 模式 photo (caption=/y2k or /poster) 不触发生成
    y2k_run_mock.reset_mock()
    poster_run_mock.reset_mock()
    plog_svc.clear_pending_photo(42)
    biz_photo_y2k = _fake_business_photo_message(caption="/y2k 粉色少女", user_id=42)
    final_core_mock_biz2 = AsyncMock(return_value={"reply_text": "嗯", "sticker_type": None, "should_reply": True, "risk_note": ""})
    with patch.object(media_mod, "encode_image_to_base64", AsyncMock(return_value="ZmFrZQ==")), \
         patch.object(media_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(media_mod, "_visual_summary_via_vision", visual_summary_mock2), \
         patch.object(media_mod, "_final_reply_via_core_model", final_core_mock_biz2), \
         patch.object(media_mod, "human_typing_delay", AsyncMock()), \
         patch.object(media_mod, "send_reply", AsyncMock()), \
         patch.object(media_mod, "store_message", AsyncMock()), \
         patch.object(media_mod, "safe_remove", AsyncMock()), \
         patch.object(private_mod, "run_y2k_for_user", y2k_run_mock), \
         patch.object(private_mod, "run_poster_for_user", poster_run_mock):
        await media_mod._handle_photo(biz_photo_y2k, bot)
    assert y2k_run_mock.await_count == 0, "Business 模式绝不可触发 /y2k 自动生成"
    assert poster_run_mock.await_count == 0, "Business 模式绝不可触发 /poster 自动生成"
    assert plog_svc.get_pending_photo(42) is None
    print("[ok] Business 模式 photo caption=/y2k or /poster 不缓存、不触发自动生成")

    # 15j) image_generation_service 降级时 fallback_to_text2image=True，路由层应发送诚实提示
    plog_svc.remember_photo(7001, file_path=fake_photo_path, file_id="fake_photo_id", caption=None)
    async def fake_generate_poster_fallback(**kw):
        return {"ok": True, "url": "https://x/poster_t2i.png", "data": None, "error": None, "fallback_to_text2image": True}
    bot.send_photo.reset_mock()
    sent_msgs.clear()
    with patch.object(private_mod, "send_long_text", side_effect=fake_send_long_text), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(private_mod, "generate_poster_image", side_effect=fake_generate_poster_fallback):
        await private_mod.run_poster_for_user(bot, msg, "甜酷复古")
    # 发了图 + 多发了一条诚实提示
    assert bot.send_photo.await_count == 1
    assert any("无法严格保留" in m or "图生图" in m or "原图" in m for m in sent_msgs), f"应附一条降级说明: {sent_msgs}"
    print("[ok] /poster 在 text2image 降级时额外发一条诚实提示")

    await close_db()
    try:
        os.remove(db_path)
    except Exception:
        pass
    print("\nALL PLOG/MAGNET SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
