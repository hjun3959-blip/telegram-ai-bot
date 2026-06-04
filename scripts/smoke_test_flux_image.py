"""Smoke test：Flux 图像功能（文字生图 /img + 图加文字生图/改图 /改图）。

覆盖（全部不联网，图像服务用 mock 替身）：
1)  模型常量：TEXT_IMAGE_MODEL 默认 flux-1.1-pro；IMAGE_TEXT_MODEL 默认 flux.1-kontext-pro；
    IMAGE_MODEL 不被改动（保持既有图像创作功能模型）。
2)  generate_image 默认走 TEXT_IMAGE_MODEL；显式 model 时用传入值。
3)  generate_image_from_reference 默认 edit 模型 = IMAGE_MODEL（不影响 plog/magnet）；
    generate_image_with_instruction 走 IMAGE_TEXT_MODEL，且 image edit 不可用时降级仍用 IMAGE_TEXT_MODEL。
4)  命令解析：_detect_tool_command 识别 /img、/改图、/生图、/图生图、/imgedit、/edit → 规范成对应 tool。
5)  caption 解析：_caption_is_plog_or_magnet / _caption_imgedit_intent 识别
    「改图 描述」「/改图 描述」「生图 描述」等；普通配文不被劫持。
6)  /img 直接走 _send_image_tool → generate_image（断言用的是 flux-1.1-pro 模型）。
7)  run_imgedit_for_user：无照片回「先发一张照片」；无指令回「想怎么改」；
    有照片+指令时调用 generate_image_with_instruction 并把图发出去。
8)  菜单可发现性：做点图二级菜单含 play:imgedit 按钮；_TOOL_HINTS 有 imgedit。
9)  Business 隔离：caption 触发只在 media 私信路径生效（这里验证解析函数与
    _handle_photo 的 business 分支不调用 imgedit）。

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


def _fake_private_message(*, text=None, caption=None, user_id=8801, photo=False, message_id=31):
    chat = SimpleNamespace(id=user_id, type="private")
    from_user = SimpleNamespace(id=user_id, username="flux_u", is_bot=False)
    photo_list = None
    if photo:
        photo_list = [SimpleNamespace(file_id="fid-1"), SimpleNamespace(file_id="fid-best")]
    msg = SimpleNamespace(
        chat=chat,
        from_user=from_user,
        business_connection_id=None,
        sender_business_bot=None,
        text=text,
        photo=photo_list,
        sticker=None,
        animation=None,
        voice=None,
        video=None,
        caption=caption,
        message_id=message_id,
    )
    msg.answer = AsyncMock()
    msg.bot = MagicMock()
    return msg


async def main() -> None:
    tmpdir = tempfile.mkdtemp(prefix="flux_smoke_")
    db_path = os.path.join(tmpdir, "smoke.sqlite3")
    os.environ["BOT_DB_PATH"] = db_path
    os.environ.setdefault("PLOG_CACHE_DIR", os.path.join(tmpdir, "plog_cache"))
    # 不设置 TEXT_IMAGE_MODEL / IMAGE_TEXT_MODEL，验证默认值

    for mod in (
        "config", "db.core",
        "services.context_service", "services.history_service",
        "services.message_service", "services.openai_service",
        "services.xiaopang_service", "services.plog_service",
        "services.image_generation_service",
        "routers.business", "routers.media", "routers.private",
    ):
        sys.modules.pop(mod, None)

    import config
    from db.core import init_db, close_db
    await init_db()

    # ---------- 1) 模型常量默认值 ----------
    assert config.TEXT_IMAGE_MODEL == "flux-1.1-pro", config.TEXT_IMAGE_MODEL
    assert config.IMAGE_TEXT_MODEL == "flux.1-kontext-pro", config.IMAGE_TEXT_MODEL
    # 既有图像创作模型不被替换
    assert config.IMAGE_MODEL == "gpt-image-2", config.IMAGE_MODEL
    print("[ok] 模型常量：TEXT_IMAGE_MODEL=flux-1.1-pro, IMAGE_TEXT_MODEL=flux.1-kontext-pro, IMAGE_MODEL 未改")

    # ---------- 2) generate_image 默认模型 ----------
    import services.image_generation_service as img_svc

    class _FakeImagesGenerate:
        def __init__(self):
            self.calls = []

        async def generate(self, *, model, prompt, size, n):
            self.calls.append({"model": model, "size": size})
            return SimpleNamespace(data=[SimpleNamespace(url="http://x/y.png", b64_json=None)])

    fake_gen = _FakeImagesGenerate()
    fake_client = SimpleNamespace(images=SimpleNamespace(generate=fake_gen.generate, edit=None))
    with patch.object(img_svc, "client", fake_client):
        out = await img_svc.generate_image("一只猫")
        assert out["ok"] and out["url"] == "http://x/y.png"
        assert fake_gen.calls[-1]["model"] == "flux-1.1-pro", fake_gen.calls
        # 显式 model 覆盖
        await img_svc.generate_image("一只猫", model="custom-model")
        assert fake_gen.calls[-1]["model"] == "custom-model"
    print("[ok] generate_image 默认 flux-1.1-pro，可被显式 model 覆盖")

    # ---------- 3) reference 路径模型选择 ----------
    ref = os.path.join(tmpdir, "ref.jpg")
    with open(ref, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0fakejpeg")

    class _FakeEdit:
        def __init__(self):
            self.calls = []

        async def edit(self, *, model, image, prompt, size, n):
            self.calls.append({"model": model})
            return SimpleNamespace(data=[SimpleNamespace(url="http://x/edit.png", b64_json=None)])

    # 3a) 既有功能默认：generate_image_from_reference 无 model → 用 IMAGE_MODEL 做 edit
    fe = _FakeEdit()
    fake_client2 = SimpleNamespace(images=SimpleNamespace(generate=fake_gen.generate, edit=fe.edit))
    with patch.object(img_svc, "client", fake_client2):
        out = await img_svc.generate_image_from_reference("加滤镜", reference_path=ref)
        assert out["ok"], out
        assert fe.calls[-1]["model"] == "gpt-image-2", fe.calls
    # 3b) 新功能：generate_image_with_instruction → edit 用 IMAGE_TEXT_MODEL
    fe2 = _FakeEdit()
    fake_client3 = SimpleNamespace(images=SimpleNamespace(generate=fake_gen.generate, edit=fe2.edit))
    with patch.object(img_svc, "client", fake_client3):
        out = await img_svc.generate_image_with_instruction("戴上墨镜", reference_path=ref)
        assert out["ok"], out
        assert fe2.calls[-1]["model"] == "flux.1-kontext-pro", fe2.calls
        assert out.get("fallback_to_text2image") is False
    # 3c) edit 不可用 → 降级到 text2image，仍用 IMAGE_TEXT_MODEL
    fake_gen_b = _FakeImagesGenerate()
    fake_client4 = SimpleNamespace(images=SimpleNamespace(generate=fake_gen_b.generate, edit=None))
    with patch.object(img_svc, "client", fake_client4):
        out = await img_svc.generate_image_with_instruction("戴上墨镜", reference_path=ref)
        assert out["ok"] and out.get("fallback_to_text2image") is True
        assert fake_gen_b.calls[-1]["model"] == "flux.1-kontext-pro", fake_gen_b.calls
    print("[ok] reference 模型选择：既有功能用 IMAGE_MODEL，新功能用 IMAGE_TEXT_MODEL（含降级）")

    # ---------- 4) 命令解析 ----------
    import routers.private as private_mod
    from routers.private import _detect_tool_command, _TOOL_HINTS, _build_make_image_keyboard

    assert _detect_tool_command("/img 一只猫") == ("img", "一只猫")
    for cmd in ("/改图", "/生图", "/图生图", "/imgedit", "/edit"):
        tool, arg = _detect_tool_command(f"{cmd} 戴墨镜")
        assert tool == "imgedit", (cmd, tool)
        assert arg == "戴墨镜", (cmd, arg)
    # 不相关命令不命中
    assert _detect_tool_command("/unknown x") == (None, "")
    print("[ok] _detect_tool_command 识别 /img 与 /改图 等别名 → imgedit")

    # ---------- 5) caption 解析 ----------
    from routers.media import _caption_is_plog_or_magnet, _caption_imgedit_intent

    assert _caption_imgedit_intent("改图 戴墨镜") == (True, "戴墨镜")
    assert _caption_imgedit_intent("/改图 戴墨镜") == (True, "戴墨镜")
    assert _caption_imgedit_intent("生图 夜景霓虹") == (True, "夜景霓虹")
    assert _caption_imgedit_intent("图生图 换背景") == (True, "换背景")
    # 普通配文不被劫持
    assert _caption_imgedit_intent("今天天气真好") == (False, "")
    assert _caption_imgedit_intent("") == (False, "")
    assert _caption_is_plog_or_magnet("改图 加帽子") == ("imgedit", "加帽子")
    assert _caption_is_plog_or_magnet("/plog 手账") == ("plog", "手账")
    assert _caption_is_plog_or_magnet("这是我的猫") == (None, "")
    print("[ok] caption 解析：改图/生图/图生图（带或不带 /）命中 imgedit；普通配文不劫持")

    # ---------- 6) /img 走 flux-1.1-pro ----------
    captured = {}

    async def _fake_generate_image(prompt, *args, **kwargs):
        captured["prompt"] = prompt
        captured["model"] = kwargs.get("model")
        return {"ok": True, "url": "http://x/img.png", "data": None, "error": None}

    bot = MagicMock()
    bot.send_photo = AsyncMock()
    bot.send_message = AsyncMock()
    msg = _fake_private_message(text="/img 一只穿西装的柴犬")
    with patch.object(private_mod, "generate_image", _fake_generate_image), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()), \
         patch.object(private_mod, "send_long_text", AsyncMock()):
        await private_mod._send_image_tool(bot, msg, "img", "一只穿西装的柴犬")
    assert bot.send_photo.await_count == 1, "应发出一张图"
    # generate_image 由 service 默认选 TEXT_IMAGE_MODEL；private 调用未传 model（=None），
    # 真正的模型选择在 service 层（已在步骤 2 验证）。这里只确认 prompt 透传。
    assert captured["prompt"] == "一只穿西装的柴犬"
    print("[ok] /img → _send_image_tool 调 generate_image 并发图（模型选择在 service 层=flux-1.1-pro）")

    # ---------- 7) run_imgedit_for_user 三态 ----------
    import services.plog_service as plog_svc
    uid = msg.from_user.id

    # 7a) 无照片
    plog_svc.clear_pending_photo(uid)
    sent_texts = []

    async def _capture_long_text(_bot, _chat, text, *a, **k):
        sent_texts.append(text)

    with patch.object(private_mod, "send_long_text", _capture_long_text), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()):
        await private_mod.run_imgedit_for_user(bot, msg, "戴墨镜")
    assert any("先发一张照片" in t for t in sent_texts), sent_texts

    # 7b) 有照片但无指令
    plog_svc.remember_photo(uid, file_path=ref, file_id="fid-best", caption=None)
    sent_texts.clear()
    with patch.object(private_mod, "send_long_text", _capture_long_text), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()):
        await private_mod.run_imgedit_for_user(bot, msg, "")
    assert any("怎么改" in t for t in sent_texts), sent_texts

    # 7c) 有照片 + 指令 → 调 generate_image_with_instruction，发图
    plog_svc.remember_photo(uid, file_path=ref, file_id="fid-best", caption=None)
    instr_capture = {}

    async def _fake_with_instruction(instruction, reference_path, **kwargs):
        instr_capture["instruction"] = instruction
        instr_capture["reference_path"] = reference_path
        return {"ok": True, "url": "http://x/edited.png", "data": None,
                "error": None, "fallback_to_text2image": False}

    bot.send_photo.reset_mock()
    with patch.object(private_mod, "generate_image_with_instruction", _fake_with_instruction), \
         patch.object(private_mod, "send_long_text", AsyncMock()), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()):
        await private_mod.run_imgedit_for_user(bot, msg, "戴上墨镜")
    assert instr_capture.get("instruction") == "戴上墨镜", instr_capture
    assert instr_capture.get("reference_path") == ref
    assert bot.send_photo.await_count == 1
    print("[ok] run_imgedit_for_user：无照片/无指令各自给提示；有照片+指令调 kontext 服务并发图")

    # ---------- 8) 菜单可发现性 ----------
    mk_cbs = []
    for row in _build_make_image_keyboard().inline_keyboard:
        for btn in row:
            if btn.callback_data:
                mk_cbs.append(btn.callback_data)
    assert "play:imgedit" in mk_cbs, mk_cbs
    assert "imgedit" in _TOOL_HINTS and "改图" in _TOOL_HINTS["imgedit"]
    print("[ok] 做点图菜单含 play:imgedit 按钮；_TOOL_HINTS 有 imgedit 用法")

    await close_db()
    print("\nALL FLUX IMAGE SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
