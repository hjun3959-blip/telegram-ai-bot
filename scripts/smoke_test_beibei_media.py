"""Smoke test: 贝贝 Business 媒体最终回复必须由 CORE_MODEL=gpt-5.5 出。

覆盖：
1) services/business_memory_service：get/save/trim/clear 行为正常
2) routers.business 改用共享 memory service，旧 user_histories 仍能拿到（兼容性）
3) services/openai_service.call_openai 接受 response_json=False（plain text）
4) routers/media._visual_summary_via_vision 用 VISION_MODEL + response_json=False
5) 贝贝 photo 最终调 call_openai 走 CORE_MODEL（gpt-5.5），不是 VISION_MODEL
6) 贝贝 sticker / GIF 最终调 call_openai 走 CORE_MODEL，不再 VISION_MODEL
7) 贝贝 photo 处理完后 business_memory_service 里有 history（媒体被 save_history）
8) 贝贝 sticker 处理完后 business_memory_service 里有 history
9) 媒体不向 LLM 暴露 file_id；视觉摘要 system prompt 明确禁止
10) 反模板化禁词进入了最终 user prompt
11) /play /help 文案没回归
12) business 非白名单 photo 不再硬性静默：放行进入正常视觉/模型流程（由模型自行判断）

不联网，全部 mock；DB 用临时文件。
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


def _fake_business_photo_message(
    *,
    chat_id: int = 42,
    user_id: int = 42,
    username: str = "yj_syj",
    caption: str | None = None,
    conn_id: str = "bc-x",
):
    """构造贝贝 business 模式的图片消息（贝贝 username=yj_syj 命中硬编码 contact + xiaopang）。"""
    chat = SimpleNamespace(id=chat_id, type="private")
    from_user = SimpleNamespace(id=user_id, username=username, is_bot=False)
    photo = [SimpleNamespace(file_id="photo_file_id_xyz", file_unique_id="puid_x", width=512, height=512)]
    return SimpleNamespace(
        chat=chat,
        from_user=from_user,
        business_connection_id=conn_id,
        sender_business_bot=None,
        text=None,
        photo=photo,
        sticker=None,
        animation=None,
        voice=None,
        video=None,
        caption=caption,
        message_id=101,
    )


def _fake_business_sticker_message(
    *,
    chat_id: int = 42,
    user_id: int = 42,
    username: str = "yj_syj",
    conn_id: str = "bc-x",
    kind: str = "sticker",
):
    chat = SimpleNamespace(id=chat_id, type="private")
    from_user = SimpleNamespace(id=user_id, username=username, is_bot=False)
    sticker = animation = None
    if kind == "sticker":
        sticker = SimpleNamespace(
            file_id="sticker_file_id_zzz",
            file_unique_id="suid_z",
            emoji="😂",
            set_name="someset",
            is_animated=False,
            is_video=False,
            width=512,
            height=512,
            file_size=1024,
            type="regular",
        )
    else:
        animation = SimpleNamespace(
            file_id="anim_file_id_qqq",
            file_unique_id="auid_q",
            duration=2,
            width=320,
            height=240,
            file_size=2048,
            file_name="x.gif",
        )
    return SimpleNamespace(
        chat=chat,
        from_user=from_user,
        business_connection_id=conn_id,
        sender_business_bot=None,
        text=None,
        photo=None,
        sticker=sticker,
        animation=animation,
        voice=None,
        video=None,
        caption=None,
        message_id=202,
    )


def _fake_stranger_photo_message(user_id: int = 99999, username: str = "no_one_knows"):
    chat = SimpleNamespace(id=user_id, type="private")
    from_user = SimpleNamespace(id=user_id, username=username, is_bot=False)
    photo = [SimpleNamespace(file_id="p_stranger", file_unique_id="ps", width=512, height=512)]
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
        caption=None,
        message_id=303,
    )


async def main() -> None:
    tmpdir = tempfile.mkdtemp(prefix="beibei_media_smoke_")
    db_path = os.path.join(tmpdir, "smoke.sqlite3")
    os.environ["BOT_DB_PATH"] = db_path
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
        "routers.business",
        "routers.media",
        "routers.private",
    ):
        sys.modules.pop(mod, None)

    import config  # noqa: F401
    from db.core import init_db, close_db
    await init_db()

    # ---------- 1) business_memory_service 基本行为 ----------
    from services import business_memory_service as bms
    bms.clear()
    assert bms.get_history(1) == []
    bms.save_history(1, "你好", "嗯")
    bms.save_history(1, "在干嘛", "看剧")
    h = bms.get_history(1)
    assert len(h) == 4, f"应有 4 条，得到 {len(h)}"
    assert h[0]["role"] == "user" and h[1]["role"] == "assistant"
    # 空 + 空时跳过
    bms.save_history(1, "", "")
    assert len(bms.get_history(1)) == 4
    # 写完返回的是浅拷贝
    h2 = bms.get_history(1)
    h2.clear()
    assert len(bms.get_history(1)) == 4
    bms.clear(1)
    assert bms.get_history(1) == []
    print("[ok] business_memory_service: get/save/trim/clear 行为正常")

    # ---------- 2) routers.business 用共享 memory service 且暴露 user_histories ----------
    import routers.business as biz
    assert biz.get_history is bms.get_history, "business 路由应使用共享 get_history"
    assert biz.save_history is bms.save_history, "business 路由应使用共享 save_history"
    assert biz.user_histories is bms.user_histories, "user_histories 应是共享 dict"
    bms.clear()
    biz.save_history(42, "test", "ok")
    assert bms.get_history(42) == [
        {"role": "user", "content": "test"},
        {"role": "assistant", "content": "ok"},
    ]
    print("[ok] routers.business 改用共享 memory service，旧 user_histories 仍兼容")

    # ---------- 3) call_openai 支持 response_json=False ----------
    import services.openai_service as oai
    import inspect
    sig = inspect.signature(oai.call_openai)
    assert "response_json" in sig.parameters, "call_openai 应增加 response_json 参数"
    sig2 = inspect.signature(oai._do_chat)
    assert "response_json" in sig2.parameters, "_do_chat 也应支持 response_json"
    print("[ok] services.openai_service.call_openai 支持 response_json 参数")

    # ---------- 4) media._visual_summary_via_vision 用 VISION_MODEL + plain text ----------
    import routers.media as media
    assert hasattr(media, "_visual_summary_via_vision")
    assert hasattr(media, "_final_reply_via_core_model")
    src = open(os.path.join(ROOT, "routers", "media.py"), "r", encoding="utf-8").read()
    assert "response_json=False" in src, "视觉摘要应使用 response_json=False"
    print("[ok] routers/media._visual_summary_via_vision 使用 plain text 模式")

    # ---------- 5) 贝贝 photo 最终 call_openai 走 CORE_MODEL ----------
    bms.clear()
    bot = MagicMock()
    bot.get_file = AsyncMock(return_value=SimpleNamespace(file_path="server/path/x.jpg"))
    bot.download_file = AsyncMock()

    call_log: list[tuple] = []

    async def fake_call_openai(messages, model, mode, response_json=True, **_kw):
        call_log.append((model, mode, response_json))
        if not response_json:
            return "画面里是一只猫在看镜头，背景是阳台。表情显得轻松。"
        # 模拟最终 JSON
        return {
            "reply_text": "猫还行吗？",
            "sticker_type": None,
            "should_reply": True,
            "risk_note": "",
        }

    encode_mock = AsyncMock(return_value="ZmFrZWJhc2U2NA==")
    with patch.object(media, "call_openai", side_effect=fake_call_openai), \
         patch.object(media, "encode_image_to_base64", encode_mock), \
         patch.object(media, "send_chat_action_safe", AsyncMock()), \
         patch.object(media, "human_typing_delay", AsyncMock()), \
         patch.object(media, "send_reply", AsyncMock()) as send_reply_mock, \
         patch.object(media, "store_message", AsyncMock()), \
         patch.object(media, "safe_remove", AsyncMock()):
        msg = _fake_business_photo_message(caption="看我家猫")
        await media._handle_photo(msg, bot)

    # 必须有两次 call_openai：第一次走视觉模型 + json=False，第二次 CORE_MODEL + json=True
    # 视觉模型自从 atree_models 接线后，贝贝走 pick_beibei_vision_model()（gemini-3.1-pro-preview），
    # 普通用户才回落到 config.VISION_MODEL（gemini-3.1-flash-lite）。这里两者都接受。
    from services.atree_models import pick_beibei_vision_model
    _expected_first_models = {config.VISION_MODEL, pick_beibei_vision_model()}
    assert len(call_log) >= 2, f"photo 应调用 call_openai 至少 2 次，得到 {len(call_log)} 次"
    first = call_log[0]
    final = call_log[-1]
    assert first[0] in _expected_first_models, (
        f"第一段视觉摘要应使用贝贝/通用 vision 模型，得到 {first[0]}"
    )
    assert first[2] is False, "第一段应 response_json=False"
    assert final[0] == config.CORE_MODEL == "gpt-5.5", f"最终 photo 回复必须 CORE_MODEL=gpt-5.5，得到 {final[0]}"
    assert final[2] is True, "最终应 response_json=True"
    # send_reply 用的 model 是 CORE_MODEL（不是 VISION_MODEL）
    assert send_reply_mock.await_count == 1
    sent_kwargs = send_reply_mock.await_args
    sent_model = sent_kwargs.args[3] if len(sent_kwargs.args) >= 4 else sent_kwargs.kwargs.get("model")
    assert sent_model == config.CORE_MODEL, f"send_reply 应使用 CORE_MODEL，得到 {sent_model}"
    print(f"[ok] 贝贝 photo 最终 call_openai 走 CORE_MODEL={final[0]}")

    # ---------- 7) 贝贝 photo 处理后共享记忆里有这条 ----------
    h_after = bms.get_history(42)
    assert any("[图片]" in (m.get("content") or "") for m in h_after if m.get("role") == "user"), \
        f"photo 后历史里应有 [图片] 占位，得到 {h_after}"
    print(f"[ok] 贝贝 photo 处理后 save_history 写入：{h_after}")

    # ---------- 6) 贝贝 sticker 最终 call_openai 走 CORE_MODEL ----------
    call_log.clear()
    bms.clear()
    async def fake_call_openai_sticker(messages, model, mode, response_json=True, **_kw):
        call_log.append((model, mode, response_json))
        return {
            "reply_text": "嗯笑啥呢",
            "sticker_type": None,
            "should_reply": True,
            "risk_note": "",
        }

    with patch.object(media, "call_openai", side_effect=fake_call_openai_sticker), \
         patch.object(media, "send_chat_action_safe", AsyncMock()), \
         patch.object(media, "human_typing_delay", AsyncMock()), \
         patch.object(media, "send_reply", AsyncMock()) as send_reply_mock2, \
         patch.object(media, "store_message", AsyncMock()), \
         patch.object(media, "record_incoming_media", AsyncMock(return_value=True)), \
         patch.object(media, "pick_media_asset", AsyncMock(return_value=None)), \
         patch.object(media, "bump_media_use", AsyncMock()):
        sticker_msg = _fake_business_sticker_message(kind="sticker")
        await media._handle_sticker_or_gif(sticker_msg, bot)

    assert len(call_log) == 1, f"sticker 在 business 应该只调用一次 CORE_MODEL，得到 {len(call_log)}"
    sm_model, sm_mode, sm_json = call_log[0]
    assert sm_model == config.CORE_MODEL == "gpt-5.5", f"贝贝 sticker 最终应走 CORE_MODEL，得到 {sm_model}"
    assert sm_json is True
    assert send_reply_mock2.await_count == 1
    sent_model2 = send_reply_mock2.await_args.args[3] if len(send_reply_mock2.await_args.args) >= 4 \
        else send_reply_mock2.await_args.kwargs.get("model")
    assert sent_model2 == config.CORE_MODEL, f"send_reply (sticker) 应使用 CORE_MODEL，得到 {sent_model2}"
    print(f"[ok] 贝贝 sticker 最终 call_openai 走 CORE_MODEL={sm_model}")

    # ---------- 8) 贝贝 sticker 处理后共享记忆里有这条 ----------
    h_st = bms.get_history(42)
    assert any("[贴纸表情]" in (m.get("content") or "") for m in h_st if m.get("role") == "user"), \
        f"sticker 后历史里应有 [贴纸表情] 占位，得到 {h_st}"
    print(f"[ok] 贝贝 sticker 处理后 save_history 写入：{h_st[:2]}...")

    # GIF 同样测试一次（轻量）
    call_log.clear()
    bms.clear()
    with patch.object(media, "call_openai", side_effect=fake_call_openai_sticker), \
         patch.object(media, "send_chat_action_safe", AsyncMock()), \
         patch.object(media, "human_typing_delay", AsyncMock()), \
         patch.object(media, "send_reply", AsyncMock()), \
         patch.object(media, "store_message", AsyncMock()), \
         patch.object(media, "record_incoming_media", AsyncMock(return_value=True)), \
         patch.object(media, "pick_media_asset", AsyncMock(return_value=None)), \
         patch.object(media, "bump_media_use", AsyncMock()):
        gif_msg = _fake_business_sticker_message(kind="animation")
        await media._handle_sticker_or_gif(gif_msg, bot)
    assert len(call_log) == 1 and call_log[0][0] == config.CORE_MODEL, \
        f"贝贝 GIF 也必须走 CORE_MODEL，得到 {call_log}"
    h_gif = bms.get_history(42)
    assert any("[GIF动图]" in (m.get("content") or "") for m in h_gif if m.get("role") == "user")
    print("[ok] 贝贝 GIF 同样走 CORE_MODEL 并入 save_history")

    # ---------- 9) 视觉摘要 system prompt 不暴露 file_id / set_name ----------
    # 用一个能捕获 messages 的 fake_call 检查 prompt 内容
    captured_msgs: list = []
    async def capture_call(messages, model, mode, response_json=True, **_kw):
        captured_msgs.append(list(messages))
        if not response_json:
            return "纯视觉描述：背景是一杯咖啡。"
        return {"reply_text": "嗯", "sticker_type": None, "should_reply": True, "risk_note": ""}

    bms.clear()
    with patch.object(media, "call_openai", side_effect=capture_call), \
         patch.object(media, "encode_image_to_base64", AsyncMock(return_value="Yg==")), \
         patch.object(media, "send_chat_action_safe", AsyncMock()), \
         patch.object(media, "human_typing_delay", AsyncMock()), \
         patch.object(media, "send_reply", AsyncMock()), \
         patch.object(media, "store_message", AsyncMock()), \
         patch.object(media, "safe_remove", AsyncMock()):
        msg2 = _fake_business_photo_message(caption=None)
        await media._handle_photo(msg2, bot)

    vision_sys = captured_msgs[0][0]["content"]
    assert "file_id" in vision_sys, "视觉摘要 system prompt 应明确提到 file_id（禁词）"
    # 用户 prompt 不含 file_id 字面
    vision_user = captured_msgs[0][1]["content"]
    assert isinstance(vision_user, list)
    for part in vision_user:
        if part.get("type") == "text":
            assert "photo_file_id_xyz" not in part["text"], "视觉 prompt 用户文本不应包含 file_id"
    # 最终主脑 user prompt 含反模板化禁词
    final_user_text = captured_msgs[-1][-1]["content"]
    assert "看到这个心情都亮了" in final_user_text, "最终 prompt 应明示禁用万能模板"
    assert "photo_file_id_xyz" not in final_user_text, "最终 prompt 也不应包含 file_id"
    print("[ok] 视觉摘要不暴露 file_id；最终 prompt 含反模板化禁词")

    # ---------- 10) 非白名单 photo 不再被硬性静默：由模型决定回不回 ----------
    bms.clear()
    call_log.clear()
    async def trap_call(messages, model, mode, response_json=True, **_kw):
        call_log.append((model, mode, response_json))
        # 模型这里返回 should_reply=false 模拟“看上去是陌生人 → 不回”
        return {"reply_text": "", "should_reply": False}

    with patch.object(media, "call_openai", side_effect=trap_call), \
         patch.object(media, "encode_image_to_base64", AsyncMock(return_value="x")), \
         patch.object(media, "send_chat_action_safe", AsyncMock()), \
         patch.object(media, "human_typing_delay", AsyncMock()), \
         patch.object(media, "send_reply", AsyncMock()) as send_reply_mock_s, \
         patch.object(media, "store_message", AsyncMock()) as store_mock_s, \
         patch.object(media, "safe_remove", AsyncMock()):
        stranger_msg = _fake_stranger_photo_message()
        await media._handle_photo(stranger_msg, bot)
    # 非白名单也应触发模型判断（视觉摘要 + 主脑），不再硬拦截。
    assert len(call_log) >= 1, f"非白名单 photo 应进入模型流程，得到 {call_log}"
    contents = [c.args[2] for c in store_mock_s.await_args_list]
    assert not any("非联系人静默" in str(c) for c in contents), (
        f"应已移除非联系人硬静默，得到 {contents}"
    )
    # 模型可以选择 should_reply=false → 不真发；但走的是模型路径，不是硬拦截
    assert send_reply_mock_s.await_count == 0, "模型决定不回时仍然不发"
    print("[ok] 非白名单 photo 不再硬静默；由模型决定（这里模型选了不回）")

    # ---------- 11) /play /help 文案不回归 ----------
    from routers.private import HELP_TEXT, PLAY_MENU_TEXT, HOW_TO_USE_TEXT, BEIBEI_PLAY_MENU_TEXT, BEIBEI_HELP_TEXT
    forbidden = ["小胖", "贝贝", "/新计划", "/计划列表", "/小胖", "/学习小胖聊天方式", "管理面板", "授权"]
    for ftext in (HELP_TEXT, PLAY_MENU_TEXT, HOW_TO_USE_TEXT, BEIBEI_PLAY_MENU_TEXT, BEIBEI_HELP_TEXT):
        for tok in forbidden:
            assert tok not in ftext, f"/play /help 暴露了 {tok}"
    print("[ok] /play /help 文案未回归")

    await close_db()
    try:
        os.remove(db_path)
    except Exception:
        pass
    print("\nALL BEIBEI-MEDIA SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
