"""阿树系统 / 阿君·贝贝双核心同级高配 + 窗口防爆 smoke。

覆盖：
1) atree_models 常量存在；双核心**不是** mini / LIGHT_MODEL
2) 普通窗口常量（config.CORE_MODEL / LIGHT_MODEL / VISION_MODEL）未被覆盖
3) Resolver：
   - 默认（env 无 alias）输出期望模型名
   - env alias 命中时输出别名
   - 期望名禁用 ATREE_ALLOW_EXPECTED_MODELS=0 时回落 CORE_MODEL（仍非 mini）
   - fallback 链整体 resolve 后不出现 LIGHT/mini
4) pick_beibei_companion_model / pick_owner_default_model / pick_crisis_model
   首选都不是 LIGHT/mini
5) core_window_policy 阈值：<60% normal, 60-85% summarize, 85-95% truncate, >=95% emergency
6) emergency 贝贝短回 sanitized（不含后台 / 承诺词），阿君短回明示「窗口已满 / 模型异常」
7) routers/private.py owner 私信 + business 贝贝都已切到 resolver；不再硬绑 CORE_MODEL
8) routers/business.py 已移除非联系人硬静默；广告仍静默
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _eq(name, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    return ok


def _truthy(name, val):
    ok = bool(val)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={val!r}")
    return ok


def _is_mini(name: str) -> bool:
    """『mini』作为独立 token 出现才算（避免误伤 gemini-*）。"""
    n = (name or "").lower()
    return "-mini" in n or n.endswith("mini") or n.startswith("mini-")


def _falsy(name, val):
    ok = not val
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={val!r}")
    return ok


# ---------- 1) 常量存在 + 不是 mini ----------

def test_constants():
    print("\n[1] atree_models constants")
    from services import atree_models as am

    expected = {
        "ATREE_MODEL_COMPANION_DEFAULT": "claude-sonnet-latest",
        "ATREE_MODEL_COMPANION_DEEP": "claude-opus-4-7",
        "ATREE_MODEL_BRAIN": "gpt-5.5",
        "ATREE_MODEL_BRAIN_DEEP": "gpt-5.5-pro",
        "ATREE_MODEL_CRISIS": "gpt-5.5-pro",
        "BEIBEI_VISION_MODEL": "gemini-3.1-pro-preview",
        "OWNER_DEFAULT_MODEL": "claude-sonnet-latest",
        "OWNER_DEEP_MODEL": "claude-opus-4-7",
        "OWNER_ANALYSIS_MODEL": "gpt-5.5",
        "OWNER_CRITICAL_MODEL": "gpt-5.5-pro",
        "OWNER_REWRITE_MODEL": "claude-sonnet-latest",
        "OWNER_REWRITE_DEEP": "claude-opus-4-7",
        "OWNER_FILTER_MODEL": "gpt-5.5",
        "OWNER_CODE_MODEL": "claude-sonnet-latest",
        "OWNER_VISION_MODEL": "gemini-3.1-pro-preview",
        "GENERAL_VISION_MODEL": "gemini-3.1-flash-lite",
    }
    ok = True
    for k, want in expected.items():
        got = getattr(am, k, None)
        ok &= _eq(k, got, want)
    ok &= _eq(
        "FALLBACK_CHAIN_CORE_VISION",
        getattr(am, "FALLBACK_CHAIN_CORE_VISION", None),
        ["gemini-3.1-pro-preview", "gemini-3.1-flash-lite", "text-fallback"],
    )
    # 不是 mini
    for k in ("ATREE_MODEL_COMPANION_DEFAULT", "ATREE_MODEL_COMPANION_DEEP",
              "OWNER_DEFAULT_MODEL", "OWNER_DEEP_MODEL"):
        v = getattr(am, k, "")
        ok &= _truthy(f"{k} not mini", "mini" not in v.lower())

    # 阈值常量
    ok &= _eq("WINDOW_SUMMARIZE_AT", am.WINDOW_SUMMARIZE_AT, 0.60)
    ok &= _eq("WINDOW_TRUNCATE_AT", am.WINDOW_TRUNCATE_AT, 0.85)
    ok &= _eq("WINDOW_EMERGENCY_AT", am.WINDOW_EMERGENCY_AT, 0.95)
    ok &= _eq("CORE_RECENT_MESSAGES_NORMAL", am.CORE_RECENT_MESSAGES_NORMAL, 20)
    ok &= _eq("CORE_RECENT_MESSAGES_TIGHT", am.CORE_RECENT_MESSAGES_TIGHT, 10)
    ok &= _eq("CORE_SUMMARY_MAX_TOKENS", am.CORE_SUMMARY_MAX_TOKENS, 600)
    return ok


# ---------- 2) 普通窗口常量未被覆盖 ----------

def test_ordinary_window_unchanged():
    print("\n[2] config 普通窗口未被改")
    import config
    ok = True
    ok &= _eq("CORE_MODEL", config.CORE_MODEL, "gpt-5.5")
    ok &= _eq("LIGHT_MODEL", config.LIGHT_MODEL, "gpt-5.4-mini")
    # VISION_MODEL 由 env 控制，默认值即可
    ok &= _truthy("VISION_MODEL not empty", config.VISION_MODEL)
    return ok


# ---------- 3) Resolver 行为 ----------

def test_resolver():
    print("\n[3] resolve_model")
    # 先清旧 env，确保默认行为
    for k in list(os.environ.keys()):
        if k.startswith("ATREE_MODEL_ALIAS__"):
            del os.environ[k]
    os.environ.pop("ATREE_ALLOW_EXPECTED_MODELS", None)
    # 重 import 以读到新环境
    sys.modules.pop("services.atree_models", None)
    from services.atree_models import resolve_model, resolve_chain, ATREE_FALLBACK_COMPANION_DEFAULT

    ok = True
    # 默认输出期望名
    ok &= _eq("default → claude-sonnet-latest", resolve_model("claude-sonnet-latest"), "claude-sonnet-latest")

    # env alias
    os.environ["ATREE_MODEL_ALIAS__CLAUDE_SONNET_LATEST"] = "real-sonnet-id"
    sys.modules.pop("services.atree_models", None)
    from services.atree_models import resolve_model as r2
    ok &= _eq("alias → real-sonnet-id", r2("claude-sonnet-latest"), "real-sonnet-id")

    # 禁用期望名 → 回落 CORE_MODEL；同时再清 alias
    del os.environ["ATREE_MODEL_ALIAS__CLAUDE_SONNET_LATEST"]
    os.environ["ATREE_ALLOW_EXPECTED_MODELS"] = "0"
    sys.modules.pop("services.atree_models", None)
    from services.atree_models import resolve_model as r3
    import config
    ok &= _eq("禁用期望名 → CORE_MODEL", r3("claude-opus-4-7"), config.CORE_MODEL)
    # 仍非 mini
    ok &= _falsy("禁用回落不是 mini", "mini" in r3("claude-opus-4-7").lower())

    # 还原
    del os.environ["ATREE_ALLOW_EXPECTED_MODELS"]
    sys.modules.pop("services.atree_models", None)
    from services.atree_models import resolve_chain as rc, ATREE_FALLBACK_COMPANION_DEFAULT as chain1, OWNER_FALLBACK_DEEP as chain2
    out1 = rc(chain1)
    out2 = rc(chain2)
    ok &= _truthy("fallback chain non-empty", out1 and out2)
    for n in out1 + out2:
        ok &= _falsy(f"chain no mini: {n}", _is_mini(n))

    return ok


# ---------- 4) picker 不返回 mini ----------

def test_pickers():
    print("\n[4] pickers do not return mini")
    sys.modules.pop("services.atree_models", None)
    from services.atree_models import (
        pick_beibei_companion_model,
        pick_beibei_brain_model,
        pick_crisis_model,
        pick_owner_default_model,
        pick_owner_analysis_model,
        pick_beibei_vision_model,
        pick_owner_vision_model,
    )
    ok = True
    for fn, name, kwargs in [
        (pick_beibei_companion_model, "beibei_companion default", {}),
        (pick_beibei_companion_model, "beibei_companion deep", {"deep": True}),
        (pick_beibei_brain_model, "beibei_brain default", {}),
        (pick_crisis_model, "crisis", {}),
        (pick_owner_default_model, "owner_default", {}),
        (pick_owner_default_model, "owner_deep", {"deep": True}),
        (pick_owner_analysis_model, "owner_analysis", {}),
        (pick_owner_analysis_model, "owner_critical", {"critical": True}),
        (pick_beibei_vision_model, "beibei_vision", {}),
        (pick_owner_vision_model, "owner_vision", {}),
    ]:
        v = fn(**kwargs)
        ok &= _truthy(f"{name} non-empty", v)
        ok &= _falsy(f"{name} not mini ({v})", _is_mini(v))
    return ok


# ---------- 5) window policy 阈值 ----------

def test_window_policy_thresholds():
    print("\n[5] core_window_policy thresholds")
    sys.modules.pop("services.atree_models", None)
    sys.modules.pop("services.core_window_policy", None)
    from services.core_window_policy import decide

    ok = True
    # 构造长字符串到指定 ratio 的字符数
    def chars_for_ratio(r, budget=8000):
        # tokens = chars/1.6 ; ratio = tokens/budget
        return int(r * budget * 1.6) + 10

    # ratio ~ 0.40 → normal
    msg = [{"role": "user", "content": "x" * chars_for_ratio(0.40)}]
    d = decide(msg)
    ok &= _eq("0.40 → normal", d.level, "normal")
    ok &= _eq("normal keep_recent", d.keep_recent, 20)
    ok &= _falsy("normal need_summary", d.need_summary)

    # ratio ~ 0.70 → summarize
    msg = [{"role": "user", "content": "x" * chars_for_ratio(0.70)}]
    d = decide(msg)
    ok &= _eq("0.70 → summarize", d.level, "summarize")
    ok &= _eq("summarize keep_recent", d.keep_recent, 20)
    ok &= _truthy("summarize need_summary", d.need_summary)

    # ratio ~ 0.90 → truncate
    msg = [{"role": "user", "content": "x" * chars_for_ratio(0.90)}]
    d = decide(msg)
    ok &= _eq("0.90 → truncate", d.level, "truncate")
    ok &= _eq("truncate keep_recent", d.keep_recent, 10)

    # ratio ~ 0.96 → emergency
    msg = [{"role": "user", "content": "x" * chars_for_ratio(0.96)}]
    d = decide(msg)
    ok &= _eq("0.96 → emergency", d.level, "emergency")
    ok &= _truthy("emergency reply set (beibei)", d.emergency_reply)

    # 阿君端 emergency 文案应明示状态
    d2 = decide(msg, for_owner=True)
    ok &= _eq("0.96 for_owner → emergency", d2.level, "emergency")
    txt = d2.emergency_reply
    ok &= _truthy("owner emergency 包含『窗口』", "窗口" in txt)
    ok &= _truthy("owner emergency 包含『压缩』 / 『继续』", ("压缩" in txt) or ("继续" in txt))

    return ok


# ---------- 6) emergency 贝贝 sanitized；阿君不 sanitize ----------

def test_emergency_sanitize():
    print("\n[6] emergency replies safety")
    sys.modules.pop("services.atree_models", None)
    sys.modules.pop("services.core_window_policy", None)
    from services.atree_persona import is_safe_visible
    from services.core_window_policy import decide

    long = [{"role": "user", "content": "x" * 20000}]

    d_bb = decide(long, for_owner=False)
    ok = True
    ok &= _truthy("beibei emergency safe_visible", is_safe_visible(d_bb.emergency_reply))
    for forb in ("机器人", "系统", "状态通报"):
        ok &= _falsy(f"beibei emergency 不含 {forb}", forb in d_bb.emergency_reply)

    d_owner = decide(long, for_owner=True)
    # 阿君端反而**应该**包含「窗口」「模型」「压缩」之类系统词，不需要 sanitize
    ok &= _truthy("owner emergency 含『窗口』或『模型』", "窗口" in d_owner.emergency_reply or "模型" in d_owner.emergency_reply)
    return ok


# ---------- 7) 路由已切到 resolver ----------

def test_routers_wired():
    print("\n[7] routers 已切到 resolver")
    priv = open(os.path.join(ROOT, "routers", "private.py"), encoding="utf-8").read()
    biz = open(os.path.join(ROOT, "routers", "business.py"), encoding="utf-8").read()
    ok = True
    ok &= _truthy("private 使用 pick_owner_default_model", "pick_owner_default_model" in priv)
    ok &= _truthy("private 使用 pick_beibei_companion_model", "pick_beibei_companion_model" in priv)
    ok &= _truthy("business 使用 pick_beibei_companion_model", "pick_beibei_companion_model" in biz)
    # 不再硬绑 LIGHT_MODEL 在 owner private 默认路径
    return ok


# ---------- 8) business 仍移除非联系人硬静默；广告仍静默 ----------

def test_business_contact_behavior():
    print("\n[8] business 联系人 + 广告")
    biz = open(os.path.join(ROOT, "routers", "business.py"), encoding="utf-8").read()
    ok = True
    ok &= _falsy("business 无 [非联系人静默]", "非联系人静默" in biz)
    ok &= _truthy("business 仍有广告静默", "广告静默" in biz)
    return ok


def main():
    secs = [
        ("constants", test_constants),
        ("ordinary_window_unchanged", test_ordinary_window_unchanged),
        ("resolver", test_resolver),
        ("pickers", test_pickers),
        ("window_policy_thresholds", test_window_policy_thresholds),
        ("emergency_sanitize", test_emergency_sanitize),
        ("routers_wired", test_routers_wired),
        ("business_contact_behavior", test_business_contact_behavior),
    ]
    fails = []
    for name, fn in secs:
        try:
            ok = fn()
        except Exception as e:
            print(f"  [FAIL] {name} crashed: {e}")
            ok = False
        if not ok:
            fails.append(name)
    print("\n========================================")
    if fails:
        print(f"FAILED: {fails}")
        return 1
    print("ALL ATREE-MODELS SMOKE PASSED ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
