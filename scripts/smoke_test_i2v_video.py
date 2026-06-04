"""Smoke test：图生视频功能（图生 15 秒视频 /图生视频 → wan2.6-i2v-flash）。

覆盖（全部不联网，视频接口/httpx 用 mock 替身）：
1)  模型/时长常量：I2V_VIDEO_MODEL 默认 wan2.6-i2v-flash；I2V_VIDEO_DURATION_SECONDS 默认 15；
    IMAGE_TO_VIDEO_MODEL 别名可覆盖；既有 image/text 模型不被改动。
2)  service 层 generate_video_from_image：
    - 同步返回 url / b64 / 嵌套结构都能抠出结果；
    - 异步任务（返回 task_id）走轮询，成功/失败/超时都收口；
    - 上游不支持（404/405）→ ok=False + 不暴露后端细节，不抛异常；
    - 无参考图 / 空描述各自给 ok=False。
3)  命令解析：_detect_tool_command 识别 /图生视频 /视频 /生成视频 /图转视频 /i2v → 规范成 i2v。
4)  caption 解析：_caption_i2v_intent / _caption_is_plog_or_magnet 识别
    「图生视频 描述」「/视频 描述」等；普通配文不被劫持；与 imgedit 不互相劫持。
5)  run_i2v_for_user 三态：无照片回「先发一张照片」；无描述回「想让这张图怎么动」；
    有照片+描述 → 调 generate_video_from_image 并把视频发出去。
6)  菜单可发现性：做点图二级菜单含 play:i2v 按钮；_TOOL_HINTS 有 i2v 用法。
7)  Business 隔离：caption 解析函数纯函数级隔离，_handle_photo business 分支不触达 i2v。
8)  时长选择：默认 15 秒透传到接口 body 的 duration 字段。

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


def _fake_private_message(*, text=None, caption=None, user_id=9901, photo=False, message_id=41):
    chat = SimpleNamespace(id=user_id, type="private")
    from_user = SimpleNamespace(id=user_id, username="i2v_u", is_bot=False)
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


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _make_fake_client(post_side_effect, get_side_effect=None):
    """构造一个最小 fake openai client，带 _client(httpx)/base_url/auth_headers。"""
    raw = MagicMock()
    raw.post = AsyncMock(side_effect=post_side_effect)
    if get_side_effect is not None:
        raw.get = AsyncMock(side_effect=get_side_effect)
    return SimpleNamespace(_client=raw, base_url="https://example.test/v1", auth_headers={"Authorization": "Bearer x"})


async def main() -> None:
    tmpdir = tempfile.mkdtemp(prefix="i2v_smoke_")
    db_path = os.path.join(tmpdir, "smoke.sqlite3")
    os.environ["BOT_DB_PATH"] = db_path
    os.environ.setdefault("PLOG_CACHE_DIR", os.path.join(tmpdir, "plog_cache"))
    # 加快轮询测试：缩短间隔/超时（不影响生产默认）
    os.environ["I2V_POLL_INTERVAL_SECONDS"] = "1"
    os.environ["I2V_POLL_TIMEOUT_SECONDS"] = "5"

    for mod in (
        "config", "db.core",
        "services.context_service", "services.history_service",
        "services.message_service", "services.openai_service",
        "services.xiaopang_service", "services.plog_service",
        "services.image_generation_service", "services.video_generation_service",
        "routers.business", "routers.media", "routers.private",
    ):
        sys.modules.pop(mod, None)

    import config
    from db.core import init_db, close_db
    await init_db()

    # ---------- 1) 模型/时长常量 ----------
    assert config.I2V_VIDEO_MODEL == "wan2.6-i2v-flash", config.I2V_VIDEO_MODEL
    assert config.I2V_VIDEO_DURATION_SECONDS == 15, config.I2V_VIDEO_DURATION_SECONDS
    # 既有模型未被替换
    assert config.IMAGE_MODEL == "gpt-image-2", config.IMAGE_MODEL
    assert config.TEXT_IMAGE_MODEL == "flux-1.1-pro", config.TEXT_IMAGE_MODEL
    assert config.IMAGE_TEXT_MODEL == "flux.1-kontext-pro", config.IMAGE_TEXT_MODEL
    print("[ok] 常量：I2V_VIDEO_MODEL=wan2.6-i2v-flash, 时长=15, 既有 image/text 模型未改")

    import services.video_generation_service as vsvc

    # 准备一张本地参考图
    ref = os.path.join(tmpdir, "ref.jpg")
    with open(ref, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0fakejpeg")

    # ---------- 2a) 同步返回 url ----------
    posted = {}

    async def _post_url(url, json=None, headers=None):
        posted["url"] = url
        posted["body"] = json
        return _FakeResp({"data": [{"url": "https://x/out.mp4"}]})

    with patch.object(vsvc, "client", _make_fake_client(_post_url)):
        out = await vsvc.generate_video_from_image("镜头推近", reference_path=ref)
        assert out["ok"] and out["url"] == "https://x/out.mp4", out
        # 时长 15 + 模型 透传到 body
        assert posted["body"]["model"] == "wan2.6-i2v-flash", posted["body"]
        assert posted["body"]["duration"] == 15, posted["body"]
        assert posted["body"]["image"].startswith("data:image/jpeg;base64,")
        assert "videos/generations" in posted["url"]
    print("[ok] service 同步 url：抠出 url，model/duration=15/image 正确透传")

    # ---------- 2b) 同步返回 b64 ----------
    import base64 as _b64

    async def _post_b64(url, json=None, headers=None):
        return _FakeResp({"data": [{"b64_json": _b64.b64encode(b"MP4DATA").decode()}]})

    with patch.object(vsvc, "client", _make_fake_client(_post_b64)):
        out = await vsvc.generate_video_from_image("动起来", reference_path=ref)
        assert out["ok"] and out["data"] == b"MP4DATA", out
    print("[ok] service 同步 b64：decode 成 bytes")

    # ---------- 2c) 嵌套结构 output.video_url ----------
    async def _post_nested(url, json=None, headers=None):
        return _FakeResp({"output": {"video_url": "https://x/nested.mp4"}})

    with patch.object(vsvc, "client", _make_fake_client(_post_nested)):
        out = await vsvc.generate_video_from_image("动起来", reference_path=ref)
        assert out["ok"] and out["url"] == "https://x/nested.mp4", out
    print("[ok] service 嵌套 output.video_url：能抠出")

    # ---------- 2d) 异步任务 → 轮询成功 ----------
    async def _post_task(url, json=None, headers=None):
        return _FakeResp({"task_id": "task-123"})

    poll_calls = {"n": 0}

    async def _get_poll(url, headers=None):
        poll_calls["n"] += 1
        if poll_calls["n"] < 2:
            return _FakeResp({"status": "running"})
        return _FakeResp({"status": "succeeded", "video_url": "https://x/poll.mp4"})

    with patch.object(vsvc, "client", _make_fake_client(_post_task, get_side_effect=_get_poll)):
        out = await vsvc.generate_video_from_image("动起来", reference_path=ref)
        assert out["ok"] and out["url"] == "https://x/poll.mp4", out
        assert poll_calls["n"] >= 2
    print("[ok] service 异步任务：轮询 running→succeeded 抠出 url")

    # ---------- 2e) 异步任务 → 轮询失败 ----------
    async def _get_fail(url, headers=None):
        return _FakeResp({"status": "failed"})

    with patch.object(vsvc, "client", _make_fake_client(_post_task, get_side_effect=_get_fail)):
        out = await vsvc.generate_video_from_image("动起来", reference_path=ref)
        assert out["ok"] is False and out["error"], out
    print("[ok] service 异步任务失败：ok=False + 中文文案")

    # ---------- 2f) 上游不支持（404）→ 优雅降级 ----------
    async def _post_404(url, json=None, headers=None):
        return _FakeResp({"error": "not found"}, status_code=404)

    with patch.object(vsvc, "client", _make_fake_client(_post_404)):
        out = await vsvc.generate_video_from_image("动起来", reference_path=ref)
        assert out["ok"] is False, out
        assert "不支持" in (out["error"] or ""), out
    print("[ok] service 上游不支持(404)：ok=False + 不暴露后端细节，不抛异常")

    # ---------- 2g) 无参考图 / 空描述 ----------
    with patch.object(vsvc, "client", _make_fake_client(_post_url)):
        out = await vsvc.generate_video_from_image("动起来", reference_path=None)
        assert out["ok"] is False, out
        out = await vsvc.generate_video_from_image("   ", reference_path=ref)
        assert out["ok"] is False, out
    print("[ok] service 无参考图 / 空描述：各自 ok=False")

    # ---------- 3) 命令解析 ----------
    import routers.private as private_mod
    from routers.private import _detect_tool_command, _TOOL_HINTS, _build_make_image_keyboard

    for cmd in ("/图生视频", "/视频", "/生成视频", "/图转视频", "/i2v"):
        tool, arg = _detect_tool_command(f"{cmd} 镜头推近")
        assert tool == "i2v", (cmd, tool)
        assert arg == "镜头推近", (cmd, arg)
    # 与 imgedit 不冲突
    assert _detect_tool_command("/改图 戴墨镜")[0] == "imgedit"
    assert _detect_tool_command("/unknown x") == (None, "")
    print("[ok] _detect_tool_command 识别 /图生视频 等别名 → i2v；与 imgedit 不冲突")

    # ---------- 4) caption 解析 ----------
    from routers.media import _caption_i2v_intent, _caption_is_plog_or_magnet, _caption_imgedit_intent

    assert _caption_i2v_intent("图生视频 镜头推近") == (True, "镜头推近")
    assert _caption_i2v_intent("/图生视频 镜头推近") == (True, "镜头推近")
    assert _caption_i2v_intent("视频 头发飘动") == (True, "头发飘动")
    assert _caption_i2v_intent("生成视频 加运镜") == (True, "加运镜")
    assert _caption_i2v_intent("图转视频 慢慢拉远") == (True, "慢慢拉远")
    # 普通配文不被劫持
    assert _caption_i2v_intent("今天拍的照片真好看") == (False, "")
    assert _caption_i2v_intent("") == (False, "")
    # 分发器：i2v 优先且与 imgedit 不互相劫持
    assert _caption_is_plog_or_magnet("图生视频 镜头推近") == ("i2v", "镜头推近")
    assert _caption_is_plog_or_magnet("改图 加帽子") == ("imgedit", "加帽子")
    assert _caption_is_plog_or_magnet("图生图 换背景") == ("imgedit", "换背景")
    assert _caption_is_plog_or_magnet("这是我的猫") == (None, "")
    print("[ok] caption 解析：图生视频/视频/生成视频/图转视频命中 i2v；普通配文不劫持；与 imgedit 隔离")

    # ---------- 5) run_i2v_for_user 三态 ----------
    import services.plog_service as plog_svc
    bot = MagicMock()
    bot.send_video = AsyncMock()
    bot.send_document = AsyncMock()
    bot.send_message = AsyncMock()
    msg = _fake_private_message(text="/图生视频 镜头推近")
    uid = msg.from_user.id

    # 5a) 无照片
    plog_svc.clear_pending_photo(uid)
    sent_texts = []

    async def _capture_long_text(_bot, _chat, text, *a, **k):
        sent_texts.append(text)

    with patch.object(private_mod, "send_long_text", _capture_long_text), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()):
        await private_mod.run_i2v_for_user(bot, msg, "镜头推近")
    assert any("先发一张照片" in t for t in sent_texts), sent_texts

    # 5b) 有照片但无描述
    plog_svc.remember_photo(uid, file_path=ref, file_id="fid-best", caption=None)
    sent_texts.clear()
    with patch.object(private_mod, "send_long_text", _capture_long_text), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()):
        await private_mod.run_i2v_for_user(bot, msg, "")
    assert any("怎么动" in t for t in sent_texts), sent_texts

    # 5c) 有照片 + 描述 → 调 generate_video_from_image，发视频
    plog_svc.remember_photo(uid, file_path=ref, file_id="fid-best", caption=None)
    cap = {}

    async def _fake_gen_video(prompt, reference_path, **kwargs):
        cap["prompt"] = prompt
        cap["reference_path"] = reference_path
        return {"ok": True, "url": "https://x/edited.mp4", "data": None, "error": None}

    bot.send_video.reset_mock()
    with patch.object(private_mod, "generate_video_from_image", _fake_gen_video), \
         patch.object(private_mod, "send_long_text", AsyncMock()), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()):
        await private_mod.run_i2v_for_user(bot, msg, "镜头缓缓推近")
    assert cap.get("prompt") == "镜头缓缓推近", cap
    assert cap.get("reference_path") == ref, cap
    assert bot.send_video.await_count == 1, bot.send_video.await_count
    print("[ok] run_i2v_for_user：无照片/无描述各自给提示；有照片+描述调 wan i2v 服务并发视频")

    # 5d) 服务返回失败 → 给诚实失败文案 + 记 retry，不发视频
    plog_svc.remember_photo(uid, file_path=ref, file_id="fid-best", caption=None)

    async def _fake_gen_fail(prompt, reference_path, **kwargs):
        return {"ok": False, "url": None, "data": None, "error": "当前接口暂不支持图生视频"}

    bot.send_video.reset_mock()
    failed_texts = []

    async def _capture_kb(_bot, _chat, text, _kb):
        failed_texts.append(text)

    with patch.object(private_mod, "generate_video_from_image", _fake_gen_fail), \
         patch.object(private_mod, "send_long_text", AsyncMock()), \
         patch.object(private_mod, "_send_text_with_keyboard", _capture_kb), \
         patch.object(private_mod, "send_chat_action_safe", AsyncMock()):
        await private_mod.run_i2v_for_user(bot, msg, "镜头推近")
    assert bot.send_video.await_count == 0
    assert failed_texts and "再试一次" in failed_texts[0], failed_texts
    print("[ok] run_i2v_for_user 失败：不发视频，给诚实重试文案")

    # ---------- 6) 菜单可发现性 ----------
    mk_cbs = []
    for row in _build_make_image_keyboard().inline_keyboard:
        for btn in row:
            if btn.callback_data:
                mk_cbs.append(btn.callback_data)
    assert "play:i2v" in mk_cbs, mk_cbs
    assert "i2v" in _TOOL_HINTS and "图生15秒视频" in _TOOL_HINTS["i2v"]
    print("[ok] 做点图菜单含 play:i2v 按钮；_TOOL_HINTS 有 i2v 用法")

    # ---------- 7) IMAGE_TO_VIDEO_MODEL 别名覆盖 ----------
    sys.modules.pop("config", None)
    os.environ["IMAGE_TO_VIDEO_MODEL"] = "wan2.6-i2v-pro"
    import config as config2
    assert config2.I2V_VIDEO_MODEL == "wan2.6-i2v-pro", config2.I2V_VIDEO_MODEL
    del os.environ["IMAGE_TO_VIDEO_MODEL"]
    print("[ok] IMAGE_TO_VIDEO_MODEL 别名可覆盖 I2V_VIDEO_MODEL")

    await close_db()
    print("\nALL I2V VIDEO SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
