"""阿树系统 P0 烟雾测试。

覆盖：
- atree_keyword_trigger: 各 intent 命中 + 否定前缀（不/没/别/无/未）正确忽略
- atree_persona: sanitize_visible_reply 兜底（后台词 / 承诺词 / 限句限长 / 空）
- atree_quote_library: pick_safe_reply 安全 + 池整体无 forbidden / commitment / 永远 等词
- atree_privacy_filter: should_forward_original / safe_excerpt
- atree_owner_alert: build_owner_notice 文本中性 + 危机带原话 + 普通不带
- atree_cooldown: severity TTL（critical=60s, high=5min, medium/low=30min）
- atree_outgoing_filter: 4 档（send / optimize / cooldown / block）
- atree_optimizer: 命中 OPTIMIZE 词的柔化建议
- atree_undo: record / get / TTL
- /宝宝 兼容当作 presence

不依赖 aiogram / bot；纯单元函数调用。
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _eq(name: str, got, want) -> bool:
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    return ok


def _truthy(name: str, val) -> bool:
    ok = bool(val)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={val!r}")
    return ok


def _falsy(name: str, val) -> bool:
    ok = not val
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={val!r}")
    return ok


# ----------------- 1) keyword trigger -----------------

def test_keyword_trigger() -> bool:
    print("\n[1] atree_keyword_trigger")
    from services.atree_keyword_trigger import detect_intent

    all_pass = True

    # /宝宝 → presence
    r = detect_intent("/宝宝")
    all_pass &= _eq("/宝宝 intent", r and r.intent, "presence")

    # 宝宝 → presence
    r = detect_intent("宝宝")
    all_pass &= _eq("宝宝 intent", r and r.intent, "presence")

    # 晚安 → sleep
    r = detect_intent("晚安")
    all_pass &= _eq("晚安 intent", r and r.intent, "sleep")

    # 早安 → morning
    r = detect_intent("早安")
    all_pass &= _eq("早安 intent", r and r.intent, "morning")

    # 小肥我撑不住了 → crisis_support critical forward_original
    r = detect_intent("小肥我撑不住了")
    all_pass &= _truthy("撑不住 hit", r)
    if r:
        all_pass &= _eq("撑不住 intent", r.intent, "crisis_support")
        all_pass &= _eq("撑不住 severity", r.severity, "critical")
        all_pass &= _eq("撑不住 forward_original", r.forward_original, True)

    # 想你了 → miss_xiaofei medium notify but NOT forward original
    r = detect_intent("想你了")
    all_pass &= _truthy("想你了 hit", r)
    if r:
        all_pass &= _eq("想你 intent", r.intent, "miss_xiaofei")
        all_pass &= _eq("想你 notify_owner", r.notify_owner, True)
        all_pass &= _eq("想你 forward_original", r.forward_original, False)

    # 小肥你在吗 → call_xiaofei forward
    r = detect_intent("小肥你在吗")
    all_pass &= _truthy("小肥 hit", r)
    if r:
        all_pass &= _eq("小肥 intent", r.intent, "call_xiaofei")
        all_pass &= _eq("小肥 forward_original", r.forward_original, True)

    # 我们复合吧 → reconciliation critical
    r = detect_intent("我们复合吧")
    all_pass &= _truthy("复合 hit", r)
    if r:
        all_pass &= _eq("复合 intent", r.intent, "reconciliation")
        all_pass &= _eq("复合 severity", r.severity, "critical")

    # 钱怎么办 → relationship_risk
    r = detect_intent("钱怎么办")
    all_pass &= _truthy("钱 hit", r)
    if r:
        all_pass &= _eq("钱 intent", r.intent, "relationship_risk")

    # 不想说 → space (low, no alert)
    r = detect_intent("不想说")
    all_pass &= _truthy("不想说 hit", r)
    if r:
        all_pass &= _eq("不想说 intent", r.intent, "space")
        all_pass &= _eq("不想说 notify_owner", r.notify_owner, False)

    # 否定前缀：不应触发普通情绪
    all_pass &= _eq("没那么烦了 not match", detect_intent("没那么烦了"), None)
    all_pass &= _eq("不烦 not match", detect_intent("不烦"), None)
    all_pass &= _eq("别烦 not match", detect_intent("别烦"), None)
    all_pass &= _eq("没那么想哭 not match", detect_intent("没那么想哭"), None)
    all_pass &= _eq("我没那么累 not match", detect_intent("我没那么累"), None)
    all_pass &= _eq("无所谓 not match", detect_intent("无所谓"), None)

    # 正常情绪仍能命中
    r = detect_intent("我烦死了")
    all_pass &= _eq("我烦死了 intent", r and r.intent, "annoyed")
    r = detect_intent("好想哭")
    all_pass &= _eq("好想哭 intent", r and r.intent, "sad")
    r = detect_intent("累死了")
    all_pass &= _eq("累死了 intent", r and r.intent, "tired")

    # 空文本
    all_pass &= _eq("空文本 not match", detect_intent(""), None)
    all_pass &= _eq("None not match", detect_intent(None), None)

    return all_pass


# ----------------- 2) persona sanitize -----------------

def test_persona_sanitize() -> bool:
    print("\n[2] atree_persona.sanitize_visible_reply")
    from services.atree_persona import (
        ATREE_COMMITMENT_FORBIDDEN_WORDS,
        ATREE_VISIBLE_FORBIDDEN_WORDS,
        sanitize_visible_reply,
        is_safe_visible,
    )

    all_pass = True

    # 后台/系统词 → 兜底
    all_pass &= _eq(
        "后台词兜底 系统",
        sanitize_visible_reply("根据系统检测，你现在状态不太好"),
        "嗯，我在。",
    )
    all_pass &= _eq(
        "后台词兜底 机器人",
        sanitize_visible_reply("我是机器人，我来分析你"),
        "嗯，我在。",
    )
    all_pass &= _eq(
        "后台词兜底 已通知",
        sanitize_visible_reply("已通知阿君"),
        "嗯，我在。",
    )

    # 承诺词 → 兜底
    for w in ("我永远在", "我保证你没事", "我们结婚吧", "复合吧"):
        all_pass &= _eq(f"承诺词兜底 {w}", sanitize_visible_reply(w), "嗯，我在。")

    # 空文本 → 兜底
    all_pass &= _eq("空 兜底", sanitize_visible_reply(""), "嗯，我在。")
    all_pass &= _eq("None 兜底", sanitize_visible_reply(None), "嗯，我在。")

    # 限句：max 2 句
    res = sanitize_visible_reply("第一句话。第二句话。第三句话。第四句话。")
    all_pass &= _truthy("限句 不含第三句", res and "第三" not in res)
    all_pass &= _truthy("限句 不含第四句", res and "第四" not in res)

    # 限字：默认 80
    long_text = "好" * 200
    res = sanitize_visible_reply(long_text)
    all_pass &= _truthy("限字 ≤ 80", res and len(res) <= 80)

    # is_safe_visible
    all_pass &= _eq("is_safe_visible 干净", is_safe_visible("嗯，我在。"), True)
    all_pass &= _eq("is_safe_visible 含 系统", is_safe_visible("系统已检测"), False)
    all_pass &= _eq("is_safe_visible 含 永远", is_safe_visible("永远在"), False)

    return all_pass


# ----------------- 3) quote library 安全性 + opening -----------------

def test_quote_library() -> bool:
    print("\n[3] atree_quote_library")
    from services.atree_persona import (
        ATREE_COMMITMENT_FORBIDDEN_WORDS,
        ATREE_VISIBLE_FORBIDDEN_WORDS,
        is_safe_visible,
    )
    from services.atree_quote_library import all_safe_pool_items, pick_safe_reply

    all_pass = True

    # 所有池条目都安全
    for period, intent, line in all_safe_pool_items():
        if not is_safe_visible(line):
            all_pass &= _eq(f"pool unsafe {period}/{intent}/{line}", False, True)
        for w in ATREE_VISIBLE_FORBIDDEN_WORDS:
            if w and w in line:
                all_pass &= _eq(f"pool 含禁词 {w} {period}/{intent}/{line}", False, True)
        for w in ATREE_COMMITMENT_FORBIDDEN_WORDS:
            if w and w in line:
                all_pass &= _eq(f"pool 含承诺 {w} {period}/{intent}/{line}", False, True)
    print(f"  [PASS] pool safety scanned {len(all_safe_pool_items())} items")

    # opening 池非空
    opening = pick_safe_reply("opening", night=False)
    all_pass &= _truthy("opening 日间 非空", opening)
    all_pass &= _truthy("opening 日间 safe", is_safe_visible(opening))
    opening_n = pick_safe_reply("opening", night=True)
    all_pass &= _truthy("opening 夜间 非空", opening_n)
    all_pass &= _truthy("opening 夜间 safe", is_safe_visible(opening_n))

    # reconciliation 池不能出现「复合吧」
    for _ in range(10):
        r = pick_safe_reply("reconciliation", night=False)
        all_pass &= _truthy(f"reconciliation 无复合吧 ({r})", "复合吧" not in r)

    # 未知 intent 走 fallback
    r = pick_safe_reply("not_an_intent", night=False)
    all_pass &= _truthy("未知 intent fallback", is_safe_visible(r))

    return all_pass


# ----------------- 4) privacy filter -----------------

def test_privacy_filter() -> bool:
    print("\n[4] atree_privacy_filter")
    from services.atree_keyword_trigger import detect_intent
    from services.atree_privacy_filter import safe_excerpt, should_forward_original

    all_pass = True

    # 危机 → 允许 forward
    r = detect_intent("小肥我撑不住了")
    all_pass &= _eq("crisis forward=True", should_forward_original(r), True)

    # 复合 → 允许
    r = detect_intent("我们复合吧")
    all_pass &= _eq("reconcile forward=True", should_forward_original(r), True)

    # 关系/钱 → 允许
    r = detect_intent("钱怎么办")
    all_pass &= _eq("money forward=True", should_forward_original(r), True)

    # 小肥 → 允许
    r = detect_intent("小肥你在吗")
    all_pass &= _eq("call_xiaofei forward=True", should_forward_original(r), True)

    # 想你 → 不允许
    r = detect_intent("想你了")
    all_pass &= _eq("miss forward=False", should_forward_original(r), False)

    # 烦 → 不允许
    r = detect_intent("我烦死了")
    all_pass &= _eq("annoyed forward=False", should_forward_original(r), False)

    # safe_excerpt 长度截断
    long = "好" * 200
    ex = safe_excerpt(long, max_chars=80)
    all_pass &= _truthy("safe_excerpt ≤ 80", len(ex) <= 80)
    all_pass &= _eq("safe_excerpt 空", safe_excerpt(""), "")

    return all_pass


# ----------------- 5) owner alert -----------------

def test_owner_alert() -> bool:
    print("\n[5] atree_owner_alert.build_owner_notice")
    from services.atree_keyword_trigger import detect_intent
    from services.atree_owner_alert import build_owner_notice

    all_pass = True

    BAD_NEUTRAL_WORDS = ("状态通报", "真人接管", "阿树已", "已通知阿君")

    # 危机：带原话
    r = detect_intent("小肥我撑不住了")
    text = build_owner_notice(r, original_text="小肥我撑不住了", sender_label="贝贝")
    all_pass &= _truthy("crisis notice has 撑不住", "撑不住" in text)
    for w in BAD_NEUTRAL_WORDS:
        all_pass &= _falsy(f"crisis notice 无 {w}", w in text)

    # 普通：不带原话
    r = detect_intent("我烦死了")
    text = build_owner_notice(r, original_text="我烦死了", sender_label="贝贝")
    all_pass &= _falsy("annoyed notice 不带原话", "我烦死了" in text)
    for w in BAD_NEUTRAL_WORDS:
        all_pass &= _falsy(f"annoyed notice 无 {w}", w in text)

    # 想你 medium：不带原话
    r = detect_intent("想你了")
    text = build_owner_notice(r, original_text="想你了", sender_label="贝贝")
    all_pass &= _falsy("miss notice 不带原话", "她刚说：想你了" in text)

    return all_pass


# ----------------- 6) cooldown -----------------

def test_cooldown() -> bool:
    print("\n[6] atree_cooldown")
    from services.atree_cooldown import reset, should_alert

    all_pass = True
    reset()

    uid = 12345

    # 第一次都允许
    all_pass &= _eq("critical 1st", should_alert(uid, "crisis_support", "critical"), True)
    all_pass &= _eq("high 1st", should_alert(uid, "reconciliation", "high"), True)
    all_pass &= _eq("medium 1st", should_alert(uid, "miss_xiaofei", "medium"), True)

    # 立刻再来一次同 intent → 被 cooldown 挡住
    all_pass &= _eq("critical 2nd", should_alert(uid, "crisis_support", "critical"), False)
    all_pass &= _eq("high 2nd", should_alert(uid, "reconciliation", "high"), False)
    all_pass &= _eq("medium 2nd", should_alert(uid, "miss_xiaofei", "medium"), False)

    # 不同 intent 不受影响
    all_pass &= _eq("relationship_risk 1st", should_alert(uid, "relationship_risk", "high"), True)

    reset()
    return all_pass


# ----------------- 7) outgoing filter -----------------

def test_outgoing_filter() -> bool:
    print("\n[7] atree_outgoing_filter.review_outgoing")
    from services.atree_outgoing_filter import (
        TIER_BLOCK,
        TIER_COOLDOWN,
        TIER_OPTIMIZE,
        TIER_SEND,
        review_outgoing,
    )

    all_pass = True

    # block
    for t in ("我永远爱你", "我保证我们一定会复合", "嫁给我吧", "我借钱给你"):
        r = review_outgoing(t)
        all_pass &= _eq(f"block: {t}", r.tier, TIER_BLOCK)

    # cooldown
    for t in ("烦死了你别烦我", "懒得说", "拉黑吧"):
        r = review_outgoing(t)
        all_pass &= _eq(f"cooldown: {t}", r.tier, TIER_COOLDOWN)

    # optimize
    for t in ("你怎么又这样", "你能不能听我说完", "我说了多少次", "你总是这样"):
        r = review_outgoing(t)
        all_pass &= _eq(f"optimize: {t}", r.tier, TIER_OPTIMIZE)
        all_pass &= _truthy(f"optimize suggested: {t}", r.suggested_text)

    # send
    for t in ("嗯，我听着。", "好，慢慢说"):
        r = review_outgoing(t)
        all_pass &= _eq(f"send: {t}", r.tier, TIER_SEND)

    # 空文本
    r = review_outgoing("")
    all_pass &= _eq("空文本 → send", r.tier, TIER_SEND)

    return all_pass


# ----------------- 8) optimizer -----------------

def test_optimizer() -> bool:
    print("\n[8] atree_optimizer.rewrite_to_softer")
    from services.atree_optimizer import rewrite_to_softer
    from services.atree_persona import is_safe_visible

    all_pass = True
    for term in ("早跟你说过", "我说了多少次", "你听不听", "你怎么又", "你能不能", "你总是", "你从来"):
        r = rewrite_to_softer(f"{term}……这样不对", matched_term=term)
        all_pass &= _truthy(f"rewrite 非空 {term}", r)
        all_pass &= _truthy(f"rewrite safe {term}", is_safe_visible(r))

    # 未知词也能给兜底
    r = rewrite_to_softer("不知道说啥", matched_term="不存在的词")
    all_pass &= _truthy("未知 term 也有兜底", r)
    return all_pass


# ----------------- 9) undo -----------------

def test_undo() -> bool:
    print("\n[9] atree_undo")
    from services.atree_undo import (
        clear_last_atree_reply,
        get_last_atree_reply,
        record_last_atree_reply,
        reset,
    )

    reset()
    all_pass = True

    chat_id = 9999
    record_last_atree_reply(chat_id, "嗯，我在。")
    got = get_last_atree_reply(chat_id)
    all_pass &= _truthy("record then get", got)
    if got:
        all_pass &= _eq("undo text", got["text"], "嗯，我在。")

    clear_last_atree_reply(chat_id)
    all_pass &= _eq("after clear", get_last_atree_reply(chat_id), None)
    reset()
    return all_pass


# ----------------- 10) /宝宝 兼容 -----------------

def test_legacy_baobao() -> bool:
    print("\n[10] /宝宝 → presence 兼容")
    from services.atree_keyword_trigger import detect_intent, is_legacy_baobao_slash

    all_pass = True
    all_pass &= _eq("is_legacy_baobao_slash 真", is_legacy_baobao_slash("/宝宝"), True)
    all_pass &= _eq("is_legacy_baobao_slash @bot", is_legacy_baobao_slash("/宝宝@yj_bot"), True)
    all_pass &= _eq("is_legacy_baobao_slash 假", is_legacy_baobao_slash("宝宝"), False)
    r = detect_intent("/宝宝")
    all_pass &= _eq("/宝宝 intent", r and r.intent, "presence")
    return all_pass


def main() -> int:
    sections = [
        ("keyword_trigger", test_keyword_trigger),
        ("persona_sanitize", test_persona_sanitize),
        ("quote_library", test_quote_library),
        ("privacy_filter", test_privacy_filter),
        ("owner_alert", test_owner_alert),
        ("cooldown", test_cooldown),
        ("outgoing_filter", test_outgoing_filter),
        ("optimizer", test_optimizer),
        ("undo", test_undo),
        ("legacy_baobao", test_legacy_baobao),
    ]
    failures = []
    for name, fn in sections:
        try:
            ok = fn()
        except Exception as e:
            print(f"  [FAIL] {name} crashed: {e}")
            ok = False
        if not ok:
            failures.append(name)

    print("\n========================================")
    if failures:
        print(f"FAILED sections: {failures}")
        return 1
    print("ALL ATREE SMOKE PASSED ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
