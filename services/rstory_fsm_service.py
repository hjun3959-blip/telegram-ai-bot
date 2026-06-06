"""R 级互动剧情 —— 数据驱动状态机引擎（DB-driven FSM）。

重构（用户最终决定）：废弃旧的硬编码 dataclass 剧情图引擎，改为**从 DB 读规则**的引擎。
剧本/场景/转移规则全部存在 services/rstory_store.py 管理的表里：

- 当前状态：user_game_state.current_fsm_state（= 某个 scene_id）。
- 转移规则：fsm_transitions（from_state → to_state，按 priority 降序匹配第一条满足条件的）。
- 转移触发类型 trigger_type：
  * choice      —— 用户选项 value 匹配 trigger_value。
  * auto        —— 满足 condition 即自动跃迁（落地后引擎自动连跳）。
  * payment     —— 解锁支付完成后触发（trigger_value 形如 r_rated_paid / nsfw_char_luna_paid）。
  * age_verify  —— 年龄验证通过后触发（trigger_value=verified）。
- 条件 condition_json：{"AND":[ 叶子条件, ... ]}，叶子支持：
  * desire_gte / affection_gte / trust_gte / dominance_gte —— 数值阈值（>=）。
  * flag_set: "x"            —— user_char_relation.flags 里 x 为真。
  * content_level_unlocked: n —— 该内容分级 n 已解锁。
- 效果 effect_json：
  * set_flag: "x"           —— 置 flag。
  * affection_delta / trust_delta / desire_delta / dominance_delta —— 数值增减（写 stat_history）。
  * relationship: "intimate" —— 改关系阶段。

引擎只负责"状态结构 + 转移合法性 + 效果应用"。
- 解锁/支付：unlock_products / user_unlocks + rstory_charges（OxaPay），编排在 rstory_payment。
- 渲染/按钮：路由层（routers/rstory.py）。

返回值统一用 AdvanceResult，调用方据 status 分支（不靠异常区分正常业务分支）。
"""

from __future__ import annotations

from dataclasses import dataclass

import config
from services import rstory_store as store
from utils.logger import setup_logging

logger = setup_logging()


# 默认剧本（/rstory 入口用）。后续多剧本时由调用方传 script_id。
DEFAULT_SCRIPT_ID = "demo_mansion"

# AdvanceResult.status 取值：
STATUS_OK = "ok"  # 正常推进到 result.scene
STATUS_INVALID = "invalid_transition"  # 该 trigger 在当前状态无合法转移
STATUS_NEEDS_UNLOCK = "needs_unlock"  # payment_gate 未解锁，result.unlock_id 给出要解锁的产品
STATUS_NEEDS_AGE = "needs_age_verify"  # age_gate 未通过年龄验证
STATUS_END = "end"  # 当前 scene 为终局（state_type=end）


class RStoryFSMError(Exception):
    """引擎级错误（剧本/场景缺失等数据问题，非正常业务分支）。"""


@dataclass
class StateView:
    """用户当前状态的只读视图（路由层渲染用）。"""

    script_id: str
    scene: store.Scene
    char_id: str | None
    relation: store.Relation
    age_verified: bool


@dataclass
class AdvanceResult:
    status: str
    script_id: str
    scene: store.Scene | None = None
    char_id: str | None = None
    unlock_id: str | None = None  # 仅 NEEDS_UNLOCK 时有意义
    content_level: int = 0  # 仅 NEEDS_UNLOCK / NEEDS_AGE 时有意义
    message: str = ""


# ---------------- condition_json 求值器 ----------------

# 叶子数值阈值条件：key -> relation 字段名。
_NUMERIC_CONDITIONS = {
    "desire_gte": "desire",
    "affection_gte": "affection",
    "trust_gte": "trust",
    "dominance_gte": "dominance",
}


async def _eval_leaf(leaf: dict, user_id: int | str, relation: store.Relation) -> bool:
    """求值单个叶子条件。未知条件类型保守返回 False。"""
    for cond_key, stat in _NUMERIC_CONDITIONS.items():
        if cond_key in leaf:
            return getattr(relation, stat) >= int(leaf[cond_key])
    if "flag_set" in leaf:
        return bool(relation.flags.get(leaf["flag_set"]))
    if "content_level_unlocked" in leaf:
        return await store.is_level_unlocked(user_id, int(leaf["content_level_unlocked"]))
    logger.warning("rstory unknown condition leaf | leaf=%s", leaf)
    return False


async def evaluate_condition(
    condition: dict | None, user_id: int | str, relation: store.Relation
) -> bool:
    """求值 condition_json。None/空 → True。当前支持 AND 数组组合。"""
    if not condition:
        return True
    if "AND" in condition:
        for leaf in condition["AND"]:
            if not await _eval_leaf(leaf, user_id, relation):
                return False
        return True
    # 兼容：顶层直接是单叶子条件（无 AND 包裹）。
    return await _eval_leaf(condition, user_id, relation)


# ---------------- effect_json 应用器 ----------------

# 叶子 *_delta 条件：effect key -> stat 名。
_DELTA_EFFECTS = {
    "affection_delta": "affection",
    "trust_delta": "trust",
    "desire_delta": "desire",
    "dominance_delta": "dominance",
}


async def apply_effect(
    effect: dict | None, user_id: int | str, char_id: str | None, scene_id: str | None
) -> None:
    """应用 effect_json：set_flag / *_delta（写 stat_history）/ relationship。"""
    if not effect or not char_id:
        return
    deltas: dict[str, int] = {}
    set_flags: list[str] = []
    relationship: str | None = None
    for key, val in effect.items():
        if key in _DELTA_EFFECTS:
            deltas[_DELTA_EFFECTS[key]] = int(val)
        elif key == "set_flag":
            set_flags.append(str(val))
        elif key == "relationship":
            relationship = str(val)
        else:
            logger.warning("rstory unknown effect key | key=%s", key)
    await store.apply_relation_changes(
        user_id,
        char_id,
        deltas=deltas or None,
        set_flags=set_flags or None,
        relationship=relationship,
        scene_id=scene_id,
        reason=f"transition@{scene_id}",
    )


# ---------------- 状态解析 / 初始化 ----------------

async def _require_script(script_id: str) -> store.Script:
    script = await store.get_script(script_id)
    if script is None:
        raise RStoryFSMError(f"unknown script: {script_id}")
    return script


async def _require_scene(scene_id: str) -> store.Scene:
    scene = await store.get_scene(scene_id)
    if scene is None:
        raise RStoryFSMError(f"unknown scene: {scene_id}")
    return scene


async def _state_view(user_id: int | str, script_id: str, gs: store.GameState) -> StateView:
    scene = await _require_scene(gs.current_fsm_state)
    char_id = gs.current_char_id or scene.char_id
    relation = await store.get_or_create_relation(user_id, char_id) if char_id else _EMPTY_RELATION
    age_verified = await store.is_age_verified(user_id)
    return StateView(
        script_id=script_id,
        scene=scene,
        char_id=char_id,
        relation=relation,
        age_verified=age_verified,
    )


# 无角色场景的占位关系（不写库）。
_EMPTY_RELATION = store.Relation(
    user_id=0, char_id="", affection=0, trust=0, desire=0, dominance=0,
    relationship="stranger", current_mood="neutral", flags={}, total_messages=0,
)


async def start_story(
    user_id: int | str, script_id: str | None = None, username: str | None = None
) -> StateView:
    """开始/恢复某剧本。无进度则初始化到 script.entry_state 入口场景。"""
    script_id = script_id or DEFAULT_SCRIPT_ID
    script = await _require_script(script_id)
    await store.ensure_user(user_id, username)

    gs = await store.get_game_state(user_id, script_id)
    if gs is None:
        entry_scene = await _require_scene(script.entry_state)
        gs = store.GameState(
            user_id=int(user_id),
            script_id=script_id,
            current_fsm_state=script.entry_state,
            current_char_id=entry_scene.char_id,
            history=[script.entry_state],
        )
        await store.set_game_state(
            user_id, script_id, gs.current_fsm_state, gs.current_char_id, gs.history
        )
    # 落地后做一次 auto 连跳（入口可能直接满足某 auto 条件）。
    gs = await _auto_advance(user_id, script_id, gs)
    return await _state_view(user_id, script_id, gs)


async def enter_story(
    user_id: int | str,
    script_id: str,
    char_id: str,
    *,
    username: str | None = None,
) -> StateView:
    """进入某 (角色, 剧情线)。先选角色入口（Step 3）用。

    - 无该剧本进度：在该角色的入口场景初始化 user_game_state(current_char_id=char_id)。
    - 已有该剧本进度：直接恢复（不回退、不改 current_char_id），让用户接着上次继续。

    双线隔离：user_game_state 以 (user_id, script_id) 为主键，两条线各自一行，互不覆盖。
    """
    script = await _require_script(script_id)
    await store.ensure_user(user_id, username)

    gs = await store.get_game_state(user_id, script_id)
    if gs is None:
        entry_scene = await store.get_char_entry_scene(script_id, char_id)
        if entry_scene is None:
            # 该角色在此线暂无入口场景：回落剧本默认入口。
            entry_scene = await _require_scene(script.entry_state)
        gs = store.GameState(
            user_id=int(user_id),
            script_id=script_id,
            current_fsm_state=entry_scene.scene_id,
            current_char_id=char_id,
            history=[entry_scene.scene_id],
        )
        await store.set_game_state(
            user_id, script_id, gs.current_fsm_state, gs.current_char_id, gs.history
        )
    gs = await _auto_advance(user_id, script_id, gs)
    return await _state_view(user_id, script_id, gs)


async def get_state(user_id: int | str, script_id: str | None = None) -> StateView | None:
    script_id = script_id or DEFAULT_SCRIPT_ID
    gs = await store.get_game_state(user_id, script_id)
    if gs is None:
        return None
    return await _state_view(user_id, script_id, gs)


# ---------------- 转移落地 ----------------

async def _land_on(
    user_id: int | str, script_id: str, tr: store.Transition, gs: store.GameState
) -> store.GameState:
    """执行一条转移：应用 effect → 写新状态 + history → 记内容访问审计。"""
    char_id = gs.current_char_id
    await apply_effect(tr.effect, user_id, char_id, tr.to_state)
    target_scene = await _require_scene(tr.to_state)
    new_char = target_scene.char_id or char_id
    history = gs.history + [tr.to_state]
    await store.set_game_state(user_id, script_id, tr.to_state, new_char, history)

    age_verified = await store.is_age_verified(user_id)
    await store.log_content_access(user_id, target_scene.content_level, tr.to_state, age_verified)

    return store.GameState(
        user_id=int(user_id),
        script_id=script_id,
        current_fsm_state=tr.to_state,
        current_char_id=new_char,
        history=history,
    )


async def _auto_advance(
    user_id: int | str, script_id: str, gs: store.GameState
) -> store.GameState:
    """落地某状态后，连续跟随满足条件的 auto 转移（带环路保护）。"""
    seen: set[str] = set()
    while True:
        if gs.current_fsm_state in seen:
            break
        seen.add(gs.current_fsm_state)
        char_id = gs.current_char_id
        relation = (
            await store.get_or_create_relation(user_id, char_id) if char_id else _EMPTY_RELATION
        )
        transitions = await store.list_transitions(script_id, gs.current_fsm_state)
        moved = False
        for tr in transitions:
            if tr.trigger_type != "auto":
                continue
            if await evaluate_condition(tr.condition, user_id, relation):
                gs = await _land_on(user_id, script_id, tr, gs)
                moved = True
                break
        if not moved:
            break
    return gs


def _result_for_scene(script_id: str, scene: store.Scene) -> AdvanceResult:
    status = STATUS_END if scene.state_type == "end" else STATUS_OK
    return AdvanceResult(
        status=status,
        script_id=script_id,
        scene=scene,
        char_id=scene.char_id,
    )


async def try_choice(
    user_id: int | str, script_id: str, choice_value: str
) -> AdvanceResult:
    """处理用户选项推进。

    在当前状态的 choice 转移里，按 priority 降序找第一条 trigger_value 匹配且 condition
    满足的转移；命中则应用 effect 落地，再做 auto 连跳。无匹配 → INVALID。
    """
    gs = await store.get_game_state(user_id, script_id)
    if gs is None:
        await start_story(user_id, script_id)
        gs = await store.get_game_state(user_id, script_id)

    char_id = gs.current_char_id
    relation = (
        await store.get_or_create_relation(user_id, char_id) if char_id else _EMPTY_RELATION
    )
    transitions = await store.list_transitions(script_id, gs.current_fsm_state)
    for tr in transitions:
        if tr.trigger_type != "choice" or tr.trigger_value != choice_value:
            continue
        if not await evaluate_condition(tr.condition, user_id, relation):
            continue
        gs = await _land_on(user_id, script_id, tr, gs)
        gs = await _auto_advance(user_id, script_id, gs)
        scene = await _require_scene(gs.current_fsm_state)
        return await _gate_or_scene(user_id, script_id, scene)

    return AdvanceResult(
        status=STATUS_INVALID,
        script_id=script_id,
        scene=await _require_scene(gs.current_fsm_state),
        char_id=char_id,
        message=f"无效选择：{choice_value}",
    )


async def _gate_or_scene(
    user_id: int | str, script_id: str, scene: store.Scene
) -> AdvanceResult:
    """落到某 scene 后，若是 gate 则返回 needs_* 信号，否则返回普通 OK/END。

    - payment_gate：检查对应 content_level 的 unlock_product 是否已解锁；未解锁 → NEEDS_UNLOCK。
    - age_gate：检查 users.age_verified；未验证 → NEEDS_AGE。
    """
    if scene.state_type == "payment_gate":
        product = await store.get_product_for_level(scene.content_level, scene.char_id)
        if product is None:
            raise RStoryFSMError(
                f"payment_gate scene {scene.scene_id} has no unlock product for level {scene.content_level}"
            )
        if not await store.is_unlocked(user_id, product.unlock_id):
            # 内测放行（全局 RSTORY_TEST_MODE 或 user_id 命中 RSTORY_TEST_WHITELIST）：
            # 跳过 create_charge / 支付流程，直接视同已解锁。
            # 只放行收款动作——写一条 source=test_mode 的解锁记录后，照常走 payment 转移跃迁。
            # 其余（FSM 推进、数值、stat_history、relationship）不受影响。
            bypass, reason = config.rstory_test_bypass(user_id)
            if bypass:
                await store.record_unlock(
                    user_id, product.unlock_id, source=store.UNLOCK_SOURCE_TEST_MODE
                )
                logger.info(
                    "rstory 内测放行 payment_gate | via=%s user=%s unlock=%s scene=%s level=%s "
                    "(跳过收款，source=test_mode)",
                    reason,
                    user_id,
                    product.unlock_id,
                    scene.scene_id,
                    scene.content_level,
                )
                return await consume_payment(user_id, script_id, f"{product.unlock_id}_paid")
            return AdvanceResult(
                status=STATUS_NEEDS_UNLOCK,
                script_id=script_id,
                scene=scene,
                char_id=scene.char_id,
                unlock_id=product.unlock_id,
                content_level=scene.content_level,
                message=f"需要先解锁：{product.title}",
            )
        # 已解锁：尝试消费 payment 转移直接跃迁。
        return await consume_payment(user_id, script_id, f"{product.unlock_id}_paid")

    if scene.state_type == "age_gate":
        if not await store.is_age_verified(user_id):
            # 内测放行：白名单用户（或全局开关）视同已验证年龄，跳过 age_gate。
            # 仍写一条 content_access_log（age_verified=True）保留审计痕迹，标测试来源。
            bypass, reason = config.rstory_test_bypass(user_id)
            if bypass:
                await store.set_age_verified(user_id)
                await store.log_content_access(
                    user_id, scene.content_level, scene.scene_id, True
                )
                logger.info(
                    "rstory 内测放行 age_gate | via=%s user=%s scene=%s level=%s "
                    "(视同已验证年龄，写 content_access_log 审计)",
                    reason,
                    user_id,
                    scene.scene_id,
                    scene.content_level,
                )
                return await consume_age_verify(user_id, script_id)
            return AdvanceResult(
                status=STATUS_NEEDS_AGE,
                script_id=script_id,
                scene=scene,
                char_id=scene.char_id,
                content_level=scene.content_level,
                message="需要先完成年龄验证。",
            )
        return await consume_age_verify(user_id, script_id)

    return _result_for_scene(script_id, scene)


async def consume_payment(
    user_id: int | str, script_id: str, trigger_value: str
) -> AdvanceResult:
    """解锁支付完成后，消费当前 payment_gate 的 payment 转移跃迁。

    在当前状态找 trigger_type=payment 且 trigger_value 匹配、condition 满足的转移。
    condition 通常含 content_level_unlocked，此时解锁记录已写 → 满足。
    """
    gs = await store.get_game_state(user_id, script_id)
    if gs is None:
        return AdvanceResult(status=STATUS_INVALID, script_id=script_id, message="无进度。")
    char_id = gs.current_char_id
    relation = (
        await store.get_or_create_relation(user_id, char_id) if char_id else _EMPTY_RELATION
    )
    transitions = await store.list_transitions(script_id, gs.current_fsm_state)
    for tr in transitions:
        if tr.trigger_type != "payment" or tr.trigger_value != trigger_value:
            continue
        if not await evaluate_condition(tr.condition, user_id, relation):
            continue
        gs = await _land_on(user_id, script_id, tr, gs)
        gs = await _auto_advance(user_id, script_id, gs)
        scene = await _require_scene(gs.current_fsm_state)
        return await _gate_or_scene(user_id, script_id, scene)
    # 没有可消费的 payment 转移（条件未满足等）：停留原地。
    return AdvanceResult(
        status=STATUS_INVALID,
        script_id=script_id,
        scene=await _require_scene(gs.current_fsm_state),
        char_id=char_id,
        message="支付转移条件未满足或无对应规则。",
    )


async def consume_age_verify(user_id: int | str, script_id: str) -> AdvanceResult:
    """年龄验证通过后，消费当前 age_gate 的 age_verify 转移（trigger_value=verified）。"""
    gs = await store.get_game_state(user_id, script_id)
    if gs is None:
        return AdvanceResult(status=STATUS_INVALID, script_id=script_id, message="无进度。")
    char_id = gs.current_char_id
    relation = (
        await store.get_or_create_relation(user_id, char_id) if char_id else _EMPTY_RELATION
    )
    transitions = await store.list_transitions(script_id, gs.current_fsm_state)
    for tr in transitions:
        if tr.trigger_type != "age_verify" or tr.trigger_value != "verified":
            continue
        if not await evaluate_condition(tr.condition, user_id, relation):
            continue
        gs = await _land_on(user_id, script_id, tr, gs)
        gs = await _auto_advance(user_id, script_id, gs)
        scene = await _require_scene(gs.current_fsm_state)
        return await _gate_or_scene(user_id, script_id, scene)
    return AdvanceResult(
        status=STATUS_INVALID,
        script_id=script_id,
        scene=await _require_scene(gs.current_fsm_state),
        char_id=char_id,
        message="年龄验证转移条件未满足或无对应规则。",
    )
