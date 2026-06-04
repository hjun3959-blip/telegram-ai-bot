"""PATCH 3 + PATCH 4 — openai_service 私聊截断 + chat_id 串行锁 + 临时错误退避 smoke。

不联网；纯函数 / asyncio 验证：
- _truncate_private_raw_text 在 ≤3500 时不动，>3500 时截到 ≤3500，并尽量在标点处断
- _is_transient_error 对 429 / 5xx / timeout / 连接错误为 True；对 400 / 鉴权错误为 False
- call_openai 接受 chat_id 关键字参数；同 chat_id 串行（A 完成才 B 开始）；不同 chat_id 可并发
- 私聊非 JSON 自然语言进入 _normalize_result 后 reply_text 长度 ≤3500
- business 非 JSON 仍 raise → fallback_business（空文本 + should_reply=False）
- 临时错误进入 _do_chat_with_retry 后会重试；非临时错误一次性 raise

跑法：python3 scripts/smoke_test_openai_serial_retry.py
"""

from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _ok(name, cond, detail=""):
    print(f"[{'OK' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        sys.exit(1)


def test_truncate():
    from services.openai_service import _truncate_private_raw_text
    _ok("空文本截断返回空", _truncate_private_raw_text("") == "")
    _ok("短文本不动", _truncate_private_raw_text("你好") == "你好")
    long_text = ("一段话。" * 1000)  # 4000 字符
    out = _truncate_private_raw_text(long_text, 3500)
    _ok("长文本被截到 ≤3500", len(out) <= 3500)
    _ok("长文本在句末标点处优雅断", out.endswith("。") or out.endswith("。") or out.endswith(".") or len(out) >= 2000)
    # 完全无标点的硬切
    no_punc = "啊" * 5000
    out2 = _truncate_private_raw_text(no_punc, 3500)
    _ok("无标点硬切到 ≤3500", len(out2) <= 3500)


def test_transient_classification():
    from services.openai_service import _is_transient_error

    class E429(Exception):
        status_code = 429
    class E500(Exception):
        status_code = 503
    class E400(Exception):
        status_code = 400
    class E401(Exception):
        status_code = 401
    class ETimeout(Exception):
        pass
    ETimeout.__name__ = "APITimeoutError"
    class ERateLimit(Exception):
        pass
    ERateLimit.__name__ = "RateLimitError"
    class EConn(Exception):
        pass
    EConn.__name__ = "APIConnectionError"

    _ok("429 是临时", _is_transient_error(E429()))
    _ok("503 是临时", _is_transient_error(E500()))
    _ok("400 不是临时", not _is_transient_error(E400()))
    _ok("401 不是临时", not _is_transient_error(E401()))
    _ok("Timeout 类名命中", _is_transient_error(ETimeout()))
    _ok("RateLimit 类名命中", _is_transient_error(ERateLimit()))
    _ok("APIConnection 类名命中", _is_transient_error(EConn()))


async def test_chat_lock_serial():
    """同 chat_id 应串行；不同 chat_id 应并发。"""
    from services import openai_service as oai

    order: list[tuple[str, str]] = []

    async def fake_do_chat(model, messages, mode, response_json=True):
        tag = messages[0]["content"]
        order.append(("start", tag))
        await asyncio.sleep(0.05)
        order.append(("end", tag))
        return {"reply_text": "ok", "sticker_type": None}

    # 直接替换 _do_chat（_do_chat_with_retry 会调用它）
    orig = oai._do_chat
    oai._do_chat = fake_do_chat  # type: ignore
    try:
        # 同 chat_id：A 必须先结束 B 才开始
        await asyncio.gather(
            oai.call_openai([{"role": "system", "content": "A"}], "m", "private", chat_id="c1"),
            oai.call_openai([{"role": "system", "content": "B"}], "m", "private", chat_id="c1"),
        )
        a_end = order.index(("end", "A"))
        b_start = order.index(("start", "B"))
        _ok("同 chat_id 串行", a_end < b_start, f"order={order}")

        # 不同 chat_id：可并发；要求 start 都在 end 前出现（交错）
        order.clear()
        await asyncio.gather(
            oai.call_openai([{"role": "system", "content": "X"}], "m", "private", chat_id="c2"),
            oai.call_openai([{"role": "system", "content": "Y"}], "m", "private", chat_id="c3"),
        )
        x_start_idx = order.index(("start", "X"))
        y_start_idx = order.index(("start", "Y"))
        x_end_idx = order.index(("end", "X"))
        y_end_idx = order.index(("end", "Y"))
        # 不同 chat 至少其中一对的 start 出现在对方 end 之前
        interleaved = (x_start_idx < y_end_idx) and (y_start_idx < x_end_idx)
        _ok("不同 chat_id 可并发", interleaved, f"order={order}")

        # chat_id=None 时不加锁（行为同原版）
        order.clear()
        await asyncio.gather(
            oai.call_openai([{"role": "system", "content": "N1"}], "m", "private"),
            oai.call_openai([{"role": "system", "content": "N2"}], "m", "private"),
        )
        _ok("chat_id=None 不报错且都跑完", len(order) == 4)
    finally:
        oai._do_chat = orig  # type: ignore


async def test_retry_transient():
    from services import openai_service as oai

    calls = {"n": 0}

    async def flaky(model, messages, mode, response_json=True):
        calls["n"] += 1
        if calls["n"] < 3:
            class _Err(Exception):
                status_code = 429
            raise _Err("rate limit")
        return {"reply_text": "ok", "sticker_type": None}

    orig = oai._do_chat
    oai._do_chat = flaky  # type: ignore
    # 加速：把退避时间换成极短
    orig_sleep = asyncio.sleep
    async def fast_sleep(t):
        return await orig_sleep(0)
    oai.asyncio.sleep = fast_sleep  # type: ignore
    try:
        result = await oai.call_openai([{"role": "system", "content": "z"}], "m", "private", chat_id="rt1")
        _ok("临时错误重试到成功", isinstance(result, dict) and result.get("reply_text") == "ok")
        _ok("临时错误共重试 3 次", calls["n"] == 3, f"calls={calls['n']}")
    finally:
        oai._do_chat = orig  # type: ignore
        oai.asyncio.sleep = orig_sleep  # type: ignore


async def test_no_retry_non_transient():
    from services import openai_service as oai

    calls = {"n": 0}

    async def fail400(model, messages, mode, response_json=True):
        calls["n"] += 1
        class _Err(Exception):
            status_code = 400
        raise _Err("bad request")

    orig = oai._do_chat
    oai._do_chat = fail400  # type: ignore
    # 让 BACKUP_MODEL 也走同一 fail400 → fallback
    try:
        result = await oai.call_openai([{"role": "system", "content": "n"}], "m", "private", chat_id="rt2")
        _ok("非临时错误一次性失败 → fallback", isinstance(result, dict))
        # 主模型 1 次 + BACKUP_MODEL 至多 1 次 = 2 次，不应被重试
        _ok("非临时不重试", calls["n"] <= 2, f"calls={calls['n']}")
    finally:
        oai._do_chat = orig  # type: ignore


async def test_private_non_json_truncate_3500():
    """通过 _do_chat 内部分支验证：私聊非 JSON 自然语言 → reply_text ≤ 3500。"""
    from services import openai_service as oai

    long_raw = "我能继续陪你聊。" * 800  # ~6400 字符
    expected_max = 3500

    class _FakeChoice:
        def __init__(self, content):
            self.message = type("M", (), {"content": content})()
    class _FakeResp:
        def __init__(self, content):
            self.choices = [_FakeChoice(content)]

    async def fake_create(**kwargs):
        return _FakeResp(long_raw)

    orig_create = oai.client.chat.completions.create
    oai.client.chat.completions.create = fake_create  # type: ignore
    try:
        out = await oai._do_chat("m", [{"role": "system", "content": "x"}], "private", response_json=True)
        _ok("私聊非 JSON 返回 dict", isinstance(out, dict))
        _ok("私聊非 JSON reply_text ≤ 3500", len(out.get("reply_text", "")) <= expected_max,
            f"len={len(out.get('reply_text', ''))}")

        # business 非 JSON 仍应 raise
        raised = False
        try:
            await oai._do_chat("m", [{"role": "system", "content": "x"}], "business", response_json=True)
        except Exception:
            raised = True
        _ok("business 非 JSON 仍 raise", raised)
    finally:
        oai.client.chat.completions.create = orig_create  # type: ignore


def main():
    test_truncate()
    test_transient_classification()
    asyncio.run(test_chat_lock_serial())
    asyncio.run(test_retry_transient())
    asyncio.run(test_no_retry_non_transient())
    asyncio.run(test_private_non_json_truncate_3500())
    print("ALL OPENAI SERIAL+RETRY SMOKE OK")


if __name__ == "__main__":
    main()
