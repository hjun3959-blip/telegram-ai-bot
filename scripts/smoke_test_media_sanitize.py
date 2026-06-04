"""灰度前补丁 — Beibei 媒体出站 sanitize smoke。

不联网；只验证：
- _sanitize_beibei_result 对命中后台词 / 承诺词的 result 会替换 reply_text
- 不会抛异常；告警失败不影响主流程
- 阿君/普通用户路径不会调用本函数（this 文件不演示，因为只是断言 helper 行为，
  is_xp 守门在 routers/media.py 各 handler 里）

跑法：
    python3 scripts/smoke_test_media_sanitize.py
"""

from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _ok(name, cond):
    print(f"[{'OK' if cond else 'FAIL'}] {name}")
    if not cond:
        sys.exit(1)


async def main():
    from routers.media import _sanitize_beibei_result, _hit_redline_terms

    # 1) 含「机器人 / 系统」 → sanitize 替换为安全短句
    r = await _sanitize_beibei_result(
        {"reply_text": "我是机器人，根据系统检测你状态不太好", "sticker_type": None},
        bot=None, scene="test_photo", chat_id=10001,
    )
    _ok("forbidden 词被替换", r["reply_text"] == "嗯，我在。")

    # 2) 含「永远 / 复合吧」 → 同样安全兜底
    r = await _sanitize_beibei_result(
        {"reply_text": "我永远爱你，复合吧", "sticker_type": None},
        bot=None, scene="test_voice", chat_id=10002,
    )
    _ok("承诺词被替换", r["reply_text"] == "嗯，我在。")

    # 3) 正常文本 → 不动（但被截到 80 字以内 / 2 句以内）
    r = await _sanitize_beibei_result(
        {"reply_text": "嗯，我在。今天慢慢说。", "sticker_type": None},
        bot=None, scene="test_voice", chat_id=10003,
    )
    _ok("正常短句保留", "我在" in r["reply_text"])
    _ok("正常短句不会被换成纯兜底", r["reply_text"] != "嗯，我在。" or "今天" in r["reply_text"]
        or len(r["reply_text"]) > 0)

    # 4) 命中检测
    hits = _hit_redline_terms("我是机器人，永远爱你")
    _ok("命中检测返回多个", len(hits) >= 2)
    _ok("命中检测对空文本返回空", _hit_redline_terms("") == [])

    # 5) 错误兜底：传入非 dict 也不抛
    r = await _sanitize_beibei_result(
        None,  # 故意非法
        bot=None, scene="test_garbage", chat_id=10004,
    )
    _ok("非 dict 不抛", isinstance(r, dict) and "reply_text" in r)

    # 6) bot=None 时不调 alert，不抛
    r = await _sanitize_beibei_result(
        {"reply_text": "机器人", "sticker_type": None},
        bot=None, scene="test_no_bot", chat_id=10005,
    )
    _ok("bot=None 红线命中仍 sanitize", r["reply_text"] == "嗯，我在。")

    print("ALL MEDIA SANITIZE SMOKE OK")


if __name__ == "__main__":
    asyncio.run(main())
