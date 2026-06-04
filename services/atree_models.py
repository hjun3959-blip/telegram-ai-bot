"""阿树系统 / 阿君·贝贝双核心同级高配模型配置（P0）。

设计目标：
- 贝贝核心和阿君核心**同级高配**，不降级到 LIGHT / mini。
- 普通用户 / 工具 / 娱乐窗口**不动**：继续用 config.CORE_MODEL / LIGHT_MODEL / VISION_MODEL。
- 模型名沿用 spec 给的「期望名称」（claude-sonnet-latest / claude-opus-latest /
  gpt-5.5-pro / gemini-pro-latest），但实际项目里 OpenAI 兼容接口未必能直接拨号到
  这些名字 → 由 `resolve_model()` 做兜底：env 别名 > 期望名 > config 的现有 CORE_MODEL。
- Fallback 链是数据，不强制重试；测试只验证链存在 + 不是 LIGHT/mini 兜底。
"""

from __future__ import annotations

import os

from config import CORE_MODEL, LIGHT_MODEL, VISION_MODEL

# ===================== 期望模型名 =====================

# 贝贝陪伴（companion）核心
ATREE_MODEL_COMPANION_DEFAULT = "claude-sonnet-latest"
ATREE_MODEL_COMPANION_DEEP = "claude-opus-4-7"
# 贝贝判断/分析脑（reasoning brain）
ATREE_MODEL_BRAIN = "gpt-5.5"
ATREE_MODEL_BRAIN_DEEP = "gpt-5.5-pro"
# 危机/红线场景：宁可慢一点也用最稳的
ATREE_MODEL_CRISIS = "gpt-5.5-pro"
# 核心窗口视觉（贝贝 + 阿君）
BEIBEI_VISION_MODEL = "gemini-3.1-pro-preview"

# 阿君核心
OWNER_DEFAULT_MODEL = "claude-sonnet-latest"
OWNER_DEEP_MODEL = "claude-opus-4-7"
OWNER_ANALYSIS_MODEL = "gpt-5.5"
OWNER_CRITICAL_MODEL = "gpt-5.5-pro"
OWNER_REWRITE_MODEL = "claude-sonnet-latest"
OWNER_REWRITE_DEEP = "claude-opus-4-7"
OWNER_FILTER_MODEL = "gpt-5.5"
OWNER_CODE_MODEL = "claude-sonnet-latest"
OWNER_VISION_MODEL = "gemini-3.1-pro-preview"

# 普通窗口视觉（维持原定）
GENERAL_VISION_MODEL = "gemini-3.1-flash-lite"


# ===================== 窗口策略阈值 =====================

WINDOW_SUMMARIZE_AT = 0.60   # >=60% 上下文 → 触发摘要
WINDOW_TRUNCATE_AT = 0.85    # >=85% → 截断早期消息
WINDOW_EMERGENCY_AT = 0.95   # >=95% → emergency 短回 + 压缩
CORE_RECENT_MESSAGES_NORMAL = 20
CORE_RECENT_MESSAGES_TIGHT = 10
CORE_SUMMARY_MAX_TOKENS = 600


# ===================== Fallback 链（数据） =====================
#
# 用途：当首选模型不可用 / 调用失败时，**按顺序**尝试下一个；最后兜底必须仍是核心级模型，
# 不允许落到 LIGHT/mini。Resolver/调用方决定是否真重试；测试只验证链不为空、不出现 mini。

ATREE_FALLBACK_COMPANION_DEFAULT: list[str] = [
    ATREE_MODEL_COMPANION_DEFAULT,
    ATREE_MODEL_COMPANION_DEEP,
    ATREE_MODEL_BRAIN,
    ATREE_MODEL_BRAIN_DEEP,
]
ATREE_FALLBACK_COMPANION_DEEP: list[str] = [
    ATREE_MODEL_COMPANION_DEEP,
    ATREE_MODEL_COMPANION_DEFAULT,
    ATREE_MODEL_BRAIN_DEEP,
    ATREE_MODEL_BRAIN,
]
ATREE_FALLBACK_BRAIN: list[str] = [
    ATREE_MODEL_BRAIN,
    ATREE_MODEL_BRAIN_DEEP,
    ATREE_MODEL_COMPANION_DEFAULT,
]
ATREE_FALLBACK_CRISIS: list[str] = [
    ATREE_MODEL_CRISIS,
    ATREE_MODEL_BRAIN,
    ATREE_MODEL_COMPANION_DEEP,
]
OWNER_FALLBACK_DEFAULT: list[str] = [
    OWNER_DEFAULT_MODEL,
    OWNER_DEEP_MODEL,
    OWNER_ANALYSIS_MODEL,
]
OWNER_FALLBACK_DEEP: list[str] = [
    OWNER_DEEP_MODEL,
    OWNER_DEFAULT_MODEL,
    OWNER_CRITICAL_MODEL,
    OWNER_ANALYSIS_MODEL,
]
FALLBACK_CHAIN_COMPANION: list[str] = [
    "claude-opus-4-7",
    "claude-sonnet-latest",
    "gpt-5.5",
    "fixed_safe_reply",
]
FALLBACK_CHAIN_OWNER: list[str] = [
    "claude-opus-4-7",
    "claude-sonnet-latest",
    "gpt-5.5",
    "gpt-5.4",
    "local_rule_owner_safe_mode",
]
OWNER_FALLBACK_ANALYSIS: list[str] = [
    OWNER_ANALYSIS_MODEL,
    OWNER_CRITICAL_MODEL,
    OWNER_DEFAULT_MODEL,
]
OWNER_FALLBACK_VISION: list[str] = [
    OWNER_VISION_MODEL,
    GENERAL_VISION_MODEL,
]
BEIBEI_FALLBACK_VISION: list[str] = [
    BEIBEI_VISION_MODEL,
    GENERAL_VISION_MODEL,
]
FALLBACK_CHAIN_CORE_VISION: list[str] = [
    "gemini-3.1-pro-preview",
    "gemini-3.1-flash-lite",
    "text-fallback",
]


# ===================== 模型名 resolver =====================
#
# 真实生产里某些「期望名」（claude-sonnet-latest / gpt-5.5-pro / gemini-pro-latest）
# 可能并不在当前 OpenAI 兼容端点的目录上。Resolver 做三层兜底：
#   1) env 别名：ATREE_MODEL_ALIAS__<UPPERCASED_NAME>=<actual>  覆盖单个名字
#   2) 期望名本身（若 env 未禁用 ATREE_ALLOW_EXPECTED_MODELS=0）
#   3) 角色级 fallback：core → CORE_MODEL；vision → VISION_MODEL；其它 → CORE_MODEL
#
# 这样：开发/测试默认行为是「输出期望名」；生产里只要在 env 配一个别名表，就能跑通真实接口。
# **不会** 主动落到 LIGHT_MODEL/mini。

_ROLE_CORE_FALLBACK = CORE_MODEL
_ROLE_VISION_FALLBACK = VISION_MODEL


def _env_alias(model_name: str) -> str:
    """env 里查 `ATREE_MODEL_ALIAS__<UPPER>=<actual>`，命中返回；否则返回空串。"""
    if not model_name:
        return ""
    key = "ATREE_MODEL_ALIAS__" + model_name.replace("-", "_").replace(".", "_").upper()
    return (os.environ.get(key) or "").strip()


def _allow_expected() -> bool:
    raw = (os.environ.get("ATREE_ALLOW_EXPECTED_MODELS", "1") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def resolve_model(name: str, *, role: str = "core") -> str:
    """把「期望模型名」翻译成「当前环境真正可用」的模型名。

    role: "core" / "vision"；只是兜底用，影响第 3 层落点。
    """
    if not name:
        return _ROLE_VISION_FALLBACK if role == "vision" else _ROLE_CORE_FALLBACK
    alias = _env_alias(name)
    if alias:
        return alias
    if _allow_expected():
        return name
    return _ROLE_VISION_FALLBACK if role == "vision" else _ROLE_CORE_FALLBACK


def resolve_chain(chain: list[str], *, role: str = "core") -> list[str]:
    """对一条 fallback 链整体走 resolver。保序、去重、剔除 LIGHT/mini。"""
    out: list[str] = []
    seen: set[str] = set()
    forbidden = {LIGHT_MODEL.lower(), "gpt-5.4-mini"}
    for n in chain:
        rn = resolve_model(n, role=role)
        if not rn or rn.lower() in forbidden:
            continue
        if rn in seen:
            continue
        seen.add(rn)
        out.append(rn)
    if not out:
        # 真极端情况：还是兜底到 CORE_MODEL（仍非 mini）
        out = [_ROLE_VISION_FALLBACK if role == "vision" else _ROLE_CORE_FALLBACK]
    return out


# ===================== 路由级便捷选择 =====================


def pick_beibei_companion_model(*, deep: bool = False) -> str:
    """贝贝陪伴回复主模型。长倾诉/重情绪/关系敏感 → deep。"""
    name = ATREE_MODEL_COMPANION_DEEP if deep else ATREE_MODEL_COMPANION_DEFAULT
    return resolve_model(name, role="core")


def pick_beibei_brain_model(*, deep: bool = False) -> str:
    """贝贝侧的 reasoning brain（判断 / 模式分类等）。"""
    name = ATREE_MODEL_BRAIN_DEEP if deep else ATREE_MODEL_BRAIN
    return resolve_model(name, role="core")


def pick_crisis_model() -> str:
    """危机/复合/钱/小肥撑不住等红线场景。"""
    return resolve_model(ATREE_MODEL_CRISIS, role="core")


def pick_owner_default_model(*, deep: bool = False) -> str:
    """阿君默认私信聊天模型。"""
    name = OWNER_DEEP_MODEL if deep else OWNER_DEFAULT_MODEL
    return resolve_model(name, role="core")


def pick_owner_analysis_model(*, critical: bool = False) -> str:
    """阿君分析/复盘/出站审查。critical 时升 critical 模型。"""
    name = OWNER_CRITICAL_MODEL if critical else OWNER_ANALYSIS_MODEL
    return resolve_model(name, role="core")


def pick_beibei_vision_model() -> str:
    return resolve_model(BEIBEI_VISION_MODEL, role="vision")


def pick_owner_vision_model() -> str:
    return resolve_model(OWNER_VISION_MODEL, role="vision")
