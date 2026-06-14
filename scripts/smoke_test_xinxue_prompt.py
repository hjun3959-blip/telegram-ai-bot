"""阿树「心学主脑」prompt 预设 smoke。

不联网。验证：
- 预设文本含 philosophy / think-style / format-core 三块核心内容。
- 安全适配：强制不暴露思维链；不含 NSFW / jailbreak / show_thoughts 泄漏；
  不写死任何 token / 上下文上限。
- 开关 ATREE_XINXUE_PROMPT_ENABLED 默认关闭：admin brain prompt 不被改动。
- 开关打开时：预设被叠加到 owner-only 的 ADMIN_BRAIN_SYSTEM_PROMPT，
  且普通私聊 / Business prompt 完全不受影响。

跑法：python3 scripts/smoke_test_xinxue_prompt.py
"""

from __future__ import annotations

import importlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _ok(name, cond, detail=""):
    print(f"[{'OK' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        sys.exit(1)


def _fresh_config(enabled: str | None):
    """以指定开关重新加载 config，返回 module。"""
    if enabled is None:
        os.environ.pop("ATREE_XINXUE_PROMPT_ENABLED", None)
    else:
        os.environ["ATREE_XINXUE_PROMPT_ENABLED"] = enabled
    import config

    return importlib.reload(config)


def main() -> None:
    from services.atree_xinxue_prompt import (
        ATREE_XINXUE_PRESET_NAME,
        ATREE_XINXUE_PROMPT,
        build_xinxue_prompt,
    )

    preset = ATREE_XINXUE_PROMPT
    _ok("preset name is xinxue", ATREE_XINXUE_PRESET_NAME.startswith("xinxue"))
    _ok("build_xinxue_prompt is deterministic", build_xinxue_prompt() == preset)

    # philosophy / think-style / format-core 核心内容（对齐 V5.8 措辞）
    _ok("philosophy present", all(w in preset for w in ("心即理", "致良知", "知行合一", "事上格物")))
    _ok("think-style present", "意图追踪" in preset and "格物" in preset)
    _ok("format-core present", "输出要求" in preset and "我不知道" in preset)

    # 安全适配：明确禁止输出外部思维链（保留内部思维链用于诚意自省）
    _ok(
        "forbids chain-of-thought exposure",
        "不需要输出外部思维链" in preset
        and "直接给出你的最终回答" in preset
        and "不要包含" in preset
        and "思考精华" in preset,
    )
    _ok("think-style marked internal-only", "内部思维链" in preset)

    # 不含 NSFW / jailbreak / show_thoughts 泄漏
    low = preset.lower()
    _ok(
        "no nsfw/jailbreak/show_thoughts leak",
        not any(w in low for w in ("nsfw", "jailbreak", "show_thoughts")),
    )

    # 不写死任何 token / 上下文上限
    _ok(
        "no token/context overrides in preset",
        not any(w in low for w in ("max_tokens", "max_context", "openai_max", "2000000", "60000")),
    )

    # 默认关闭：admin brain prompt 不被改动
    cfg_off = _fresh_config(None)
    _ok("default disabled", cfg_off.ATREE_XINXUE_PROMPT_ENABLED is False)
    _ok("admin brain untouched when off", "事上格物" not in cfg_off.ADMIN_BRAIN_SYSTEM_PROMPT)

    cfg_off_false = _fresh_config("false")
    _ok("explicit false disabled", cfg_off_false.ATREE_XINXUE_PROMPT_ENABLED is False)

    # 打开：叠加到 owner-only admin brain；普通私聊 / Business 不受影响
    cfg_on = _fresh_config("true")
    _ok("enabled via true", cfg_on.ATREE_XINXUE_PROMPT_ENABLED is True)
    _ok("admin brain gets preset", "事上格物" in cfg_on.ADMIN_BRAIN_SYSTEM_PROMPT)
    _ok("private chat unaffected", "事上格物" not in cfg_on.PRIVATE_SYSTEM_PROMPT)
    _ok("business chat unaffected", "事上格物" not in cfg_on.BUSINESS_SYSTEM_PROMPT)

    # 还原环境，避免污染后续
    _fresh_config(None)
    print("\nALL OK")


if __name__ == "__main__":
    main()
