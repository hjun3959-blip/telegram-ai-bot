"""Smoke test：贝贝情绪雷达 + 阿君风格 + 斗图弹药库。

只测试纯逻辑，不联网；DB 用临时文件，不污染真实库。
覆盖：
1) config 三个常量都存在且关键关键词命中
2) BUSINESS_SYSTEM_PROMPT / PRIVATE_SYSTEM_PROMPT 都带上了阿君风格指南
3) DB init 幂等；老库自动补 direction/source_owner/source_username 列
4) record_self_media(direction=outgoing) 与 record_incoming_media 都能落库
5) pick_media_asset 排除指定 file_unique_id 时确实拿到不同的素材
6) 同一轮里把 incoming uid 当 exclude 传入，结果不可能选中 incoming 本身
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _fake_message(
    file_id: str,
    file_unique_id: str,
    *,
    kind: str = "sticker",
    chat_id: int = 10001,
    user_id: int = 20001,
    username: str = "tester",
    emoji: str = "😂",
    set_name: str = "set_a",
    is_animated: bool = False,
    is_video: bool = False,
    business_connection_id: str | None = None,
):
    """构造一个最小可用的 Message 替身。只暴露 self_media_service 实际访问的属性。"""
    sticker = animation = None
    if kind == "sticker":
        sticker = SimpleNamespace(
            file_id=file_id,
            file_unique_id=file_unique_id,
            emoji=emoji,
            set_name=set_name,
            is_animated=is_animated,
            is_video=is_video,
            width=512,
            height=512,
            file_size=12345,
        )
    elif kind == "animation":
        animation = SimpleNamespace(
            file_id=file_id,
            file_unique_id=file_unique_id,
            duration=2,
            width=320,
            height=240,
            file_size=22222,
            file_name="x.gif",
        )
    chat = SimpleNamespace(id=chat_id, type="private")
    from_user = SimpleNamespace(id=user_id, username=username, is_bot=False)
    return SimpleNamespace(
        sticker=sticker,
        animation=animation,
        photo=None,
        voice=None,
        video=None,
        chat=chat,
        from_user=from_user,
        caption=None,
        business_connection_id=business_connection_id,
    )


async def main() -> None:
    # 1) 用临时 DB
    tmpdir = tempfile.mkdtemp(prefix="beibei_smoke_")
    db_path = os.path.join(tmpdir, "smoke.sqlite3")
    os.environ["BOT_DB_PATH"] = db_path
    # 清掉可能加载过的 config 缓存
    for mod in ("config", "db.core", "services.self_media_service"):
        sys.modules.pop(mod, None)

    import config  # noqa
    from db.core import init_db, close_db, fetchall, execute
    from services.self_media_service import (
        record_self_media,
        record_incoming_media,
        pick_media_asset,
        bump_media_use,
    )

    # --- 验证 config ---
    assert hasattr(config, "AJUN_STYLE_GUIDE"), "缺 AJUN_STYLE_GUIDE"
    assert hasattr(config, "BEIBEI_PROFILE_BLOCK"), "缺 BEIBEI_PROFILE_BLOCK"
    assert hasattr(config, "BEIBEI_EMOTION_RADAR_BLOCK"), "缺 BEIBEI_EMOTION_RADAR_BLOCK"
    assert "阿君" in config.AJUN_STYLE_GUIDE
    assert "2001-02-13" in config.BEIBEI_PROFILE_BLOCK
    assert "星座" in config.BEIBEI_PROFILE_BLOCK  # 内部参考提到星座但不外露
    for tok in ("emotion_state", "risk_level", "reply_strategy"):
        assert tok in config.BEIBEI_EMOTION_RADAR_BLOCK, f"雷达缺关键词 {tok}"
    # 阿君风格指南已经拼进两个 system prompt
    assert "客服腔" in config.BUSINESS_SYSTEM_PROMPT, "BUSINESS_SYSTEM_PROMPT 缺阿君风格"
    assert "客服腔" in config.PRIVATE_SYSTEM_PROMPT, "PRIVATE_SYSTEM_PROMPT 缺阿君风格"
    # business prompt 仍要求 JSON
    assert "should_reply" in config.BUSINESS_SYSTEM_PROMPT
    print("[ok] config 三块常量 + 阿君风格注入 + JSON 输出约束都在")

    # --- 验证 DB ---
    await init_db()
    # 应至少包含三个补丁列
    rows = await fetchall("PRAGMA table_info(self_media_assets)")
    cols = {r["name"] for r in rows}
    for must in ("direction", "source_owner", "source_username", "file_unique_id", "use_count", "last_used_at"):
        assert must in cols, f"self_media_assets 缺列 {must}"
    print("[ok] self_media_assets schema 完整：包含 direction/source_owner/source_username")

    # --- 采集 owner 自发 ---
    msg_owner = _fake_message("owner_file_id_1", "uid_owner_A", emoji="😂", set_name="ownerset")
    ok = await record_self_media(msg_owner, mode="business", direction="outgoing", source_owner=True)
    assert ok is True
    # --- 采集对方发来的 ---
    msg_in = _fake_message("incoming_file_id_X", "uid_incoming_X", emoji="😂", set_name="theirset", user_id=99999, username="them")
    ok = await record_incoming_media(msg_in, mode="business")
    assert ok is True

    rows = await fetchall("SELECT direction, source_owner, file_unique_id, file_id FROM self_media_assets ORDER BY id")
    dirs = [(r["direction"], r["source_owner"], r["file_unique_id"]) for r in rows]
    assert ("outgoing", 1, "uid_owner_A") in dirs
    assert ("incoming", 0, "uid_incoming_X") in dirs
    print("[ok] 两个方向都入库：owner outgoing + 对方 incoming")

    # --- pick_media_asset 排除 incoming 本身 ---
    picked = await pick_media_asset(media_type="sticker", exclude_file_unique_id="uid_incoming_X")
    assert picked is not None, "应该能挑到 owner 那张"
    assert picked["file_unique_id"] != "uid_incoming_X", "reuse_in_same_turn=false 被违反！"
    # owner 自发优先
    assert picked["source_owner"] == 1, "prefer_owner 应该优先 owner 自发"
    print(f"[ok] pick_media_asset 排除 incoming uid 成功 | 选中 owner 张：{picked['file_unique_id']}")

    # --- 极端情况：只剩 incoming 的素材，pick 应该返回 None 而不是把 incoming 选上 ---
    await execute("DELETE FROM self_media_assets WHERE source_owner = 1")
    picked2 = await pick_media_asset(media_type="sticker", exclude_file_unique_id="uid_incoming_X")
    assert picked2 is None, "排除掉唯一可选项后必须返回 None，绝不能违反 reuse_in_same_turn=false"
    print("[ok] 只剩 incoming 时 pick_media_asset 拒绝复用，返回 None")

    # --- bump_media_use 不应抛异常 ---
    await bump_media_use("uid_incoming_X", "sticker")
    rows = await fetchall("SELECT use_count, last_used_at FROM self_media_assets WHERE file_unique_id='uid_incoming_X'")
    assert rows[0]["use_count"] >= 1
    print("[ok] bump_media_use 累加 use_count 正常")

    # --- 验证 reply_service.send_reply 不会因 STICKER_MAP 空报错 ---
    # 不真发，直接做 sticker_file_id 解析校验
    from services.reply_service import clean_reply_text
    assert clean_reply_text("晚安渠") == "晚安，早点休息"
    print("[ok] reply_service 清洗逻辑健在")

    # --- 验证 xiaopang_service.build_system_prompt_with_xiaopang 拼接 ---
    # 走 fetchone/fetchall，需要 DB
    from services.xiaopang_service import build_system_prompt_with_xiaopang, remember_xiaopang_identity
    # 构造一个 fake xiaopang 用户：username=yj_syj 命中规则
    fake_xp = SimpleNamespace(
        chat=SimpleNamespace(id=88, type="private"),
        from_user=SimpleNamespace(id=42, username="yj_syj", is_bot=False),
        business_connection_id="bc-1",  # business 模式
    )
    prompt = await build_system_prompt_with_xiaopang(config.BUSINESS_SYSTEM_PROMPT, fake_xp)
    # 必须包含贝贝画像 + 情绪雷达
    assert "贝贝本人画像" in prompt, "贝贝画像未注入 system prompt"
    assert "贝贝情绪雷达" in prompt, "情绪雷达未注入 business system prompt"
    # 不能把 emotion_state 这个字段名暴露成回复模板
    # （它出现在 system prompt 内部约束里是合法的；只要最终 JSON 模板还是原四字段）
    assert "should_reply" in prompt
    # 私信模式（非 business）不应注入情绪雷达——只注入画像
    fake_xp_private = SimpleNamespace(
        chat=SimpleNamespace(id=42, type="private"),
        from_user=SimpleNamespace(id=42, username="yj_syj", is_bot=False),
        business_connection_id=None,
    )
    prompt_priv = await build_system_prompt_with_xiaopang(config.PRIVATE_SYSTEM_PROMPT, fake_xp_private)
    assert "贝贝本人画像" in prompt_priv
    assert "贝贝情绪雷达" not in prompt_priv, "情绪雷达不应进入贝贝自己的私信功能区"
    print("[ok] 贝贝画像 + 情绪雷达按 business 模式正确注入 system prompt")

    # --- 验证 /play 与 /help 文案不暴露隐藏功能 ---
    # 菜单二次收口：PLAY_MENU_TEXT / BEIBEI_PLAY_MENU_TEXT 是首页极简文案（4 大入口），
    # 不再直接铺命令；公开工具命令汇总到 _TOOL_HINTS 与 HOW_TO_USE_TEXT。
    from routers.private import HELP_TEXT, PLAY_MENU_TEXT, HOW_TO_USE_TEXT, BEIBEI_PLAY_MENU_TEXT, BEIBEI_HELP_TEXT, _TOOL_HINTS
    forbidden_tokens = ["小胖", "贝贝", "/新计划", "/计划列表", "/小胖", "/学习小胖聊天方式"]
    for ftext in (HELP_TEXT, PLAY_MENU_TEXT, HOW_TO_USE_TEXT, BEIBEI_PLAY_MENU_TEXT, BEIBEI_HELP_TEXT):
        for tok in forbidden_tokens:
            assert tok not in ftext, f"/play /help 暴露了隐藏关键词 {tok}: {ftext[:80]}"
    # 贝贝首页额外不能出现：授权/管理/控制台/情绪雷达
    for tok_extra in ["授权", "管理面板", "控制台", "情绪雷达"]:
        assert tok_extra not in BEIBEI_PLAY_MENU_TEXT, f"贝贝专用菜单联想了管理词：{tok_extra}"
    # 公开工具命令现在汇总到 _TOOL_HINTS（按钮 hint）与 HOW_TO_USE_TEXT，贝贝侧也能通过点按钮看到
    for must_cmd in ["img", "meme", "polish", "tldr", "eli5", "excel", "eat", "reply"]:
        assert must_cmd in _TOOL_HINTS and _TOOL_HINTS[must_cmd], f"_TOOL_HINTS 缺公开工具 {must_cmd}"
        assert f"/{must_cmd}" in _TOOL_HINTS[must_cmd]
    print("[ok] /play /help 文案不暴露隐藏入口；公开工具命令在 _TOOL_HINTS 中（贝贝也能用）")

    # --- owner 隐藏指令集合可用 ---
    from services.xiaopang_service import XIAOPANG_OWNER_COMMANDS
    assert "/小胖设置" in XIAOPANG_OWNER_COMMANDS
    assert "/学习小胖聊天方式" in XIAOPANG_OWNER_COMMANDS
    print("[ok] 小胖 owner 隐藏指令集合保留")

    # --- round 2：贝贝侧轻提示可用功能 ---
    assert hasattr(config, "BEIBEI_PRIVATE_GENTLE_BLOCK"), "缺 BEIBEI_PRIVATE_GENTLE_BLOCK"
    gentle = config.BEIBEI_PRIVATE_GENTLE_BLOCK
    # 必须包含新语义关键词：轻提示 / 公开工具 / 不广告腔 / 不引导授权 / 不泄露隐藏功能
    for tok in ("轻提示可用功能", "/play", "/help", "/img", "/meme", "不广告腔", "不引导授权", "小胖摘要"):
        assert tok in gentle, f"BEIBEI_PRIVATE_GENTLE_BLOCK 缺关键词 {tok}"
    # 不能再出现老的“绝不推功能”表述（不严格，只检查宽松梨子）
    # 贝贝侧私信（非 business）系统 prompt 必须拼上轻提示约束
    assert "轻提示可用功能" in prompt_priv or "可以用公开小工具" in prompt_priv, "贝贝私信 prompt 缺轻提示约束"
    # business prompt 不受影响（代聊仍是情绪雷达详细）
    print("[ok] 贝贝私信轻提示可用功能：推公开工具/不广告腔/不泄露隐藏功能、注入 system prompt")

    # --- round 2：handle_xiaopang_private_setting 对贝贝侧 admin 关键词 不再返回功能腔回御 ---
    from services.xiaopang_service import handle_xiaopang_private_setting
    # 模拟贝贝发送“设置称呼”或“开启摘要”等指令 — 都不应该有功能腔确认
    fake_bb_msg = SimpleNamespace(
        chat=SimpleNamespace(id=42, type="private"),
        from_user=SimpleNamespace(id=42, username="yj_syj", is_bot=False),
        business_connection_id=None,
    )
    for trial in ("设置称呼 宝贝", "开启摘要", "设置语气 温柔", "关闭摘要", "提醒关键词 生日"):
        r = await handle_xiaopang_private_setting(fake_bb_msg, trial)
        assert r is None, f"贝贝侧 admin 关键词 不应返回任何功能腔回复，但 {trial!r} 返回了 {r!r}"
    print("[ok] 贝贝侧 admin 关键词 一律静默（无功能腔回御）")

    # --- round 2：xiaopang_block_owner_command_for_private 软化后不再解释/推功能 ---
    from services.xiaopang_service import xiaopang_block_owner_command_for_private
    bb_msg_cmd = SimpleNamespace(
        chat=SimpleNamespace(id=42, type="private"),
        from_user=SimpleNamespace(id=42, username="yj_syj", is_bot=False),
        business_connection_id=None,
    )
    blocked = await xiaopang_block_owner_command_for_private(bb_msg_cmd, "/小胖设置")
    assert blocked is not None
    for bad in ("入口", "不给你用", "机器人", "功能", "工具"):
        assert bad not in blocked, f"贝贝侧 owner 命令拦截回复滩入功能腔词汇 {bad}: {blocked!r}"
    print(f"[ok] owner 命令被贝贝误发时轻接住：{blocked!r}")

    await close_db()
    try:
        os.remove(db_path)
    except Exception:
        pass
    print("\nALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
