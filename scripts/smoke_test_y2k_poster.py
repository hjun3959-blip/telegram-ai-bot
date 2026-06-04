"""Smoke test：/y2k（Y2K拼贴海报）+ /poster /starposter（明星拼贴海报）。

两个独立公开功能，命令、菜单、prompt、提示分开。

覆盖：
1) /play /help /贝贝菜单文案：包含 /y2k、/poster、/starposter 与中文展示名
2) /play /help 文案不暴露隐藏管理命令
3) 文本命令 /y2k 无照片：温柔提示先发照片，不调用图片生成
4) 文本命令 /poster /starposter 无照片：温柔提示，不调用图片生成
5) 有照片 → /y2k：调用 y2k generate；参考图与风格传对；缓存被清理
6) 有照片 → /poster：调用 poster generate；参考图与风格传对；缓存被清理
7) 有照片 → /starposter：与 /poster 走同一个 runner
8) caption=/y2k 直接走 y2k 流程
9) caption=/poster 直接走 poster 流程
10) Business 模式 photo（caption=/y2k 或 /poster）：不缓存、不触发自动生成
11) y2k prompt 含 Y2K 美学 / 拼贴海报 / 韩国少女 / SO CUTE 关键词；poster prompt 含粉色甜酷
    / 杂志拼贴 / 金属光泽；两者关键词独立，不互窜
12) image_generation_service 降级到 text2image 时，run_y2k_for_user / run_poster_for_user
    会附一句 T2I_FALLBACK_NOTE 诚实说明
13) 公开工具集合 _PHOTO_TOOLS 含 plog / magnet / fridge / y2k / poster / starposter

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


def _fake_private_text_message(text: str, *, user_id: int = 8001, username: str = "u_tester"):
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
        message_id=11,
        bot=None,
    )


def _fake_private_photo_message(*, caption: str | None = None, user_id: int = 8001, username: str = "u_tester"):
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
        message_id=12,
    )


def _fake_business_photo_message(*, caption: str | None = None, user_id: int = 88, username: str = "yj_syj"):
    chat = SimpleNamespace(id=user_id, type="private")
    from_user = SimpleNamespace(id=user_id, username=username, is_bot=False)
    photo = [SimpleNamespace(file_id="biz_photo_id", file_unique_id="bfpuid", width=512, height=512)]
    return SimpleNamespace(
        chat=chat,
        from_user=from_user,
        business_connection_id="bc-yp",
        sender_business_bot=None,
        text=None,
        photo=photo,
        sticker=None,
        animation=None,
        voice=None,
        video=None,
        caption=caption,
        message_id=13,
    )


async def main() -> None:
    tmpdir = tempfile.mkdtemp(prefix="yp_smoke_")
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
        "services.y2k_service",
        "services.poster_service",
        "services.image_generation_service",
        "routers.business",
        "routers.media",
        "routers.private",
    ):
        sys.modules.pop(mod, None)

    import config  # noqa: F401
    from db.core import init_db, close_db
    await init_db()

    # ---------- 1) 菜单文案：含 /y2k、/poster、/starposter 与中文展示名 ----------
    from routers.private import (
        PLAY_MENU_TEXT,
        HELP_TEXT,
        BEIBEI_PLAY_MENU_TEXT,
        BEIBEI_HELP_TEXT,
        HOW_TO_USE_TEXT,
        _PHOTO_TOOLS,
        _TOOL_HINTS,
    )
    # 菜单二次收口：PLAY_MENU_TEXT / BEIBEI_PLAY_MENU_TEXT 是极简首页文案，不直接铺命令；
    # 具体 /y2k /poster /starposter 命令名汇总到 HOW_TO_USE_TEXT 与 _TOOL_HINTS 中。
    for kw in ("/y2k", "/poster", "/starposter"):
        assert kw in HOW_TO_USE_TEXT, f"HOW_TO_USE_TEXT 缺 {kw}"
    for key in ("y2k", "poster", "starposter"):
        hint = _TOOL_HINTS.get(key, "")
        assert hint and f"/{key}" in hint, f"_TOOL_HINTS[{key}] 应提及 /{key}"
    print("[ok] HOW_TO_USE_TEXT + _TOOL_HINTS 覆盖 /y2k /poster /starposter 命令名")

    # ---------- 2) 不暴露隐藏管理命令 ----------
    hidden = ["/小胖", "/学习小胖", "/新计划", "/计划列表", "/联系人列表", "/添加联系人", "/删除联系人", "管理面板", "授权"]
    for text in (PLAY_MENU_TEXT, HELP_TEXT, BEIBEI_PLAY_MENU_TEXT, BEIBEI_HELP_TEXT, HOW_TO_USE_TEXT):
        for tok in hidden:
            assert tok not in text, f"菜单暴露了隐藏功能 {tok}"
    print("[ok] /play /help 不暴露任何隐藏管理命令")

    # ---------- 3) /y2k 无照片：温柔提示 ----------
    import routers.private as private_mod
    import services.plog_service as plog_svc

    plog_svc.clear_pending_photo(8001)

    sent_msgs: list[str] = []
    async def fake_send_long_text(bot, chat_id, text, business_connection_id=None):
        sent_msgs.append(text)

    bot = MagicMock()
    bot.send_photo = AsyncMock()

    y2k_gen_mock = AsyncMock(return_value={"ok": True, "url": "https://x/y2k.png", "data": None, "error": None})
    msg = _fake_private_text_message("/y2k 粉色少女")
    with patch.object(private_mod, "send_long_text", side_effect=fake_send_long_text), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(private_mod, "generate_y2k_image", y2k_gen_mock):
        await private_mod.run_y2k_for_user(bot, msg, "粉色少女")
    assert any("先发" in m and "/y2k" in m for m in sent_msgs), f"应提示先发照片: {sent_msgs}"
    assert y2k_gen_mock.await_count == 0
    print("[ok] /y2k 无照片时提示先发照片，不调用图片生成")

    # ---------- 4) /poster 与 /starposter 无照片：温柔提示 ----------
    sent_msgs.clear()
    poster_gen_mock = AsyncMock(return_value={"ok": True, "url": "https://x/poster.png", "data": None, "error": None})
    with patch.object(private_mod, "send_long_text", side_effect=fake_send_long_text), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(private_mod, "generate_poster_image", poster_gen_mock):
        await private_mod.run_poster_for_user(bot, msg, "甜酷复古")
    assert any("先发" in m and "/poster" in m for m in sent_msgs), f"应提示先发照片: {sent_msgs}"
    assert poster_gen_mock.await_count == 0
    print("[ok] /poster /starposter 无照片时提示先发照片，不调用图片生成")

    # ---------- 5) 有照片 → /y2k ----------
    fake_photo_path = os.path.join(tmpdir, "fake_photo.jpg")
    with open(fake_photo_path, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0fake-y2k-jpg")
    plog_svc.remember_photo(8001, file_path=fake_photo_path, file_id="fake_photo_id", caption=None)

    captured_y2k_calls: list = []
    async def fake_y2k_gen(**kw):
        captured_y2k_calls.append(kw)
        return {"ok": True, "url": "https://x/y2k_out.png", "data": None, "error": None, "fallback_to_text2image": False}

    bot.send_photo.reset_mock()
    sent_msgs.clear()
    with patch.object(private_mod, "send_long_text", side_effect=fake_send_long_text), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(private_mod, "generate_y2k_image", side_effect=fake_y2k_gen):
        await private_mod.run_y2k_for_user(bot, msg, "粉色少女")
    assert len(captured_y2k_calls) == 1
    args = captured_y2k_calls[0]
    assert args.get("reference_path") == fake_photo_path
    assert args.get("raw_arg") == "粉色少女"
    assert bot.send_photo.await_count == 1
    assert plog_svc.get_pending_photo(8001) is None
    # 没有 fallback，应该没附诚实说明
    assert not any("严格图像编辑" in m for m in sent_msgs)
    print("[ok] 有照片时 /y2k 调用 generate_y2k_image 并发出图片，缓存被清理")

    # ---------- 6) 有照片 → /poster ----------
    plog_svc.remember_photo(8001, file_path=fake_photo_path, file_id="fake_photo_id", caption=None)
    captured_poster_calls: list = []
    async def fake_poster_gen(**kw):
        captured_poster_calls.append(kw)
        return {"ok": True, "url": "https://x/poster_out.png", "data": None, "error": None, "fallback_to_text2image": False}

    bot.send_photo.reset_mock()
    sent_msgs.clear()
    with patch.object(private_mod, "send_long_text", side_effect=fake_send_long_text), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(private_mod, "generate_poster_image", side_effect=fake_poster_gen):
        await private_mod.run_poster_for_user(bot, msg, "甜酷复古")
    assert len(captured_poster_calls) == 1
    pargs = captured_poster_calls[0]
    assert pargs.get("reference_path") == fake_photo_path
    assert pargs.get("raw_arg") == "甜酷复古"
    assert bot.send_photo.await_count == 1
    assert plog_svc.get_pending_photo(8001) is None
    print("[ok] 有照片时 /poster 调用 generate_poster_image 并发出图片，缓存被清理")

    # ---------- 7) /starposter 与 /poster 共用 runner (通过 _detect_tool_command 验证) ----------
    from routers.private import _detect_tool_command
    tool, arg = _detect_tool_command("/starposter 复古")
    assert tool == "starposter" and arg == "复古"
    tool2, arg2 = _detect_tool_command("/poster")
    assert tool2 == "poster" and arg2 == ""
    tool3, arg3 = _detect_tool_command("/y2k 韩系")
    assert tool3 == "y2k" and arg3 == "韩系"
    print("[ok] _detect_tool_command 正确识别 /y2k /poster /starposter")

    # ---------- 8) caption=/y2k 直接走 y2k 流程 ----------
    import routers.media as media_mod

    with open(fake_photo_path, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0fake-y2k-jpg")

    y2k_run_mock = AsyncMock()
    poster_run_mock = AsyncMock()
    plog_run_mock = AsyncMock()
    magnet_run_mock = AsyncMock()
    visual_summary_mock = AsyncMock(return_value="some summary")
    final_core_mock = AsyncMock(return_value={"reply_text": "x", "sticker_type": None, "should_reply": True, "risk_note": ""})
    call_openai_trap = AsyncMock(return_value={"reply_text": "y", "sticker_type": None})

    async def fake_download_file(path, dst):
        with open(dst, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0downloaded")
    bot.get_file = AsyncMock(return_value=SimpleNamespace(file_path="s/p.jpg"))
    bot.download_file = AsyncMock(side_effect=fake_download_file)

    photo_msg = _fake_private_photo_message(caption="/y2k 粉色少女", user_id=8001)
    with patch.object(media_mod, "encode_image_to_base64", AsyncMock(return_value="ZmFrZQ==")), \
         patch.object(media_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(media_mod, "_visual_summary_via_vision", visual_summary_mock), \
         patch.object(media_mod, "_final_reply_via_core_model", final_core_mock), \
         patch.object(media_mod, "call_openai", call_openai_trap), \
         patch.object(media_mod, "send_reply", AsyncMock()), \
         patch.object(media_mod, "store_message", AsyncMock()), \
         patch.object(media_mod, "safe_remove", AsyncMock()), \
         patch.object(private_mod, "run_plog_for_user", plog_run_mock), \
         patch.object(private_mod, "run_magnet_for_user", magnet_run_mock), \
         patch.object(private_mod, "run_y2k_for_user", y2k_run_mock), \
         patch.object(private_mod, "run_poster_for_user", poster_run_mock):
        await media_mod._handle_photo(photo_msg, bot)

    assert y2k_run_mock.await_count == 1, "caption=/y2k 应触发 run_y2k_for_user"
    assert poster_run_mock.await_count == 0
    assert plog_run_mock.await_count == 0
    assert magnet_run_mock.await_count == 0
    assert call_openai_trap.await_count == 0
    call_args = y2k_run_mock.await_args
    assert call_args.args[2] == "粉色少女", f"raw_arg 透传错误: {call_args.args}"
    print("[ok] private photo caption=/y2k 跳过 VISION，直接进入 y2k 流程，参数正确透传")

    # ---------- 9) caption=/poster 与 /starposter 直接走 poster 流程 ----------
    for caption_in in ("/poster 甜酷", "/starposter 复古"):
        y2k_run_mock.reset_mock()
        poster_run_mock.reset_mock()
        plog_run_mock.reset_mock()
        magnet_run_mock.reset_mock()
        call_openai_trap.reset_mock()
        photo_msg2 = _fake_private_photo_message(caption=caption_in, user_id=8001)
        with patch.object(media_mod, "encode_image_to_base64", AsyncMock(return_value="ZmFrZQ==")), \
             patch.object(media_mod, "send_chat_action_safe", AsyncMock()), \
             patch.object(media_mod, "_visual_summary_via_vision", visual_summary_mock), \
             patch.object(media_mod, "_final_reply_via_core_model", final_core_mock), \
             patch.object(media_mod, "call_openai", call_openai_trap), \
             patch.object(media_mod, "send_reply", AsyncMock()), \
             patch.object(media_mod, "store_message", AsyncMock()), \
             patch.object(media_mod, "safe_remove", AsyncMock()), \
             patch.object(private_mod, "run_plog_for_user", plog_run_mock), \
             patch.object(private_mod, "run_magnet_for_user", magnet_run_mock), \
             patch.object(private_mod, "run_y2k_for_user", y2k_run_mock), \
             patch.object(private_mod, "run_poster_for_user", poster_run_mock):
            await media_mod._handle_photo(photo_msg2, bot)
        assert poster_run_mock.await_count == 1, f"caption={caption_in} 应触发 run_poster_for_user"
        assert y2k_run_mock.await_count == 0
        assert plog_run_mock.await_count == 0
        assert magnet_run_mock.await_count == 0
        assert call_openai_trap.await_count == 0
    print("[ok] private photo caption=/poster /starposter 都走 poster 流程")

    # ---------- 10) Business 模式 photo caption=/y2k /poster：不缓存、不触发 ----------
    y2k_run_mock.reset_mock()
    poster_run_mock.reset_mock()
    plog_svc.clear_pending_photo(88)
    biz_y2k_msg = _fake_business_photo_message(caption="/y2k 粉色", user_id=88)
    final_core_mock_biz = AsyncMock(return_value={"reply_text": "嗯", "sticker_type": None, "should_reply": True, "risk_note": ""})
    with patch.object(media_mod, "encode_image_to_base64", AsyncMock(return_value="ZmFrZQ==")), \
         patch.object(media_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(media_mod, "_visual_summary_via_vision", visual_summary_mock), \
         patch.object(media_mod, "_final_reply_via_core_model", final_core_mock_biz), \
         patch.object(media_mod, "human_typing_delay", AsyncMock()), \
         patch.object(media_mod, "send_reply", AsyncMock()), \
         patch.object(media_mod, "store_message", AsyncMock()), \
         patch.object(media_mod, "safe_remove", AsyncMock()), \
         patch.object(private_mod, "run_y2k_for_user", y2k_run_mock), \
         patch.object(private_mod, "run_poster_for_user", poster_run_mock):
        await media_mod._handle_photo(biz_y2k_msg, bot)
    assert y2k_run_mock.await_count == 0, "Business 模式绝不可触发 /y2k 自动生成"
    assert poster_run_mock.await_count == 0
    assert plog_svc.get_pending_photo(88) is None, "Business 模式不应缓存照片"

    biz_poster_msg = _fake_business_photo_message(caption="/poster 甜酷", user_id=88)
    with patch.object(media_mod, "encode_image_to_base64", AsyncMock(return_value="ZmFrZQ==")), \
         patch.object(media_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(media_mod, "_visual_summary_via_vision", visual_summary_mock), \
         patch.object(media_mod, "_final_reply_via_core_model", final_core_mock_biz), \
         patch.object(media_mod, "human_typing_delay", AsyncMock()), \
         patch.object(media_mod, "send_reply", AsyncMock()), \
         patch.object(media_mod, "store_message", AsyncMock()), \
         patch.object(media_mod, "safe_remove", AsyncMock()), \
         patch.object(private_mod, "run_y2k_for_user", y2k_run_mock), \
         patch.object(private_mod, "run_poster_for_user", poster_run_mock):
        await media_mod._handle_photo(biz_poster_msg, bot)
    assert y2k_run_mock.await_count == 0
    assert poster_run_mock.await_count == 0, "Business 模式绝不可触发 /poster 自动生成"
    assert plog_svc.get_pending_photo(88) is None
    print("[ok] Business 模式 photo caption=/y2k /poster 不缓存、不触发自动生成")

    # ---------- 11) y2k / poster prompt 关键句独立、不互窜 ----------
    from services.y2k_service import build_y2k_prompt, Y2K_COLLAGE_TEMPLATE
    from services.poster_service import build_poster_prompt, POSTER_CORE_DIRECTIVE

    y_p = build_y2k_prompt("粉色少女")
    p_p = build_poster_prompt("甜酷复古")

    for kw in ["Y2K美学", "拼贴海报", "韩国少女", "SO CUTE", "拍立得", "199X"]:
        assert kw in y_p, f"y2k prompt 缺少关键词 {kw}"
    for kw in ["粉色系", "拼贴", "金属光泽", "明星", "时尚"]:
        assert kw in p_p, f"poster prompt 缺少关键词 {kw}"

    # 关键模板常量
    assert "SO CUTE" in Y2K_COLLAGE_TEMPLATE
    assert "Aegyo" in Y2K_COLLAGE_TEMPLATE
    assert "粉色系" in POSTER_CORE_DIRECTIVE
    assert "拼贴" in POSTER_CORE_DIRECTIVE
    print("[ok] y2k / poster prompt 关键句独立、含正确关键词")

    # ---------- 12) fallback_to_text2image 时附诚实说明 ----------
    plog_svc.remember_photo(8001, file_path=fake_photo_path, file_id="fake_photo_id", caption=None)
    sent_msgs.clear()
    bot.send_photo.reset_mock()
    fallback_result = {"ok": True, "url": "https://x/y2k.png", "data": None, "error": None, "fallback_to_text2image": True}
    with patch.object(private_mod, "send_long_text", side_effect=fake_send_long_text), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(private_mod, "generate_y2k_image", AsyncMock(return_value=fallback_result)):
        await private_mod.run_y2k_for_user(bot, msg, "粉色少女")
    assert bot.send_photo.await_count == 1
    assert any("严格图像编辑" in m or "img2img" in m for m in sent_msgs), f"应附诚实说明: {sent_msgs}"
    print("[ok] /y2k 降级到 text2image 时附 T2I_FALLBACK_NOTE 诚实说明")

    # 同样验证 /poster
    plog_svc.remember_photo(8001, file_path=fake_photo_path, file_id="fake_photo_id", caption=None)
    sent_msgs.clear()
    bot.send_photo.reset_mock()
    fallback_result2 = {"ok": True, "url": "https://x/poster.png", "data": None, "error": None, "fallback_to_text2image": True}
    with patch.object(private_mod, "send_long_text", side_effect=fake_send_long_text), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(private_mod, "generate_poster_image", AsyncMock(return_value=fallback_result2)):
        await private_mod.run_poster_for_user(bot, msg, "甜酷")
    assert bot.send_photo.await_count == 1
    assert any("严格图像编辑" in m or "img2img" in m for m in sent_msgs), f"应附诚实说明: {sent_msgs}"
    print("[ok] /poster 降级到 text2image 时附 T2I_FALLBACK_NOTE 诚实说明")

    # ---------- 13) _PHOTO_TOOLS 覆盖 ----------
    for cmd in ("plog", "magnet", "fridge", "y2k", "poster", "starposter"):
        assert cmd in _PHOTO_TOOLS, f"_PHOTO_TOOLS 缺少 {cmd}"
    print("[ok] _PHOTO_TOOLS 覆盖 plog / magnet / fridge / y2k / poster / starposter")

    await close_db()
    try:
        os.remove(db_path)
    except Exception:
        pass
    print("\nALL Y2K / POSTER SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
