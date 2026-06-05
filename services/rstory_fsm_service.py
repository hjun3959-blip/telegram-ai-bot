"""R 级互动剧情状态机引擎（FSM）。

职责：消费 services/rstory_content.py 定义的剧情图，驱动用户在某角色剧情里的状态推进：
- 读取/初始化用户当前状态（在哪个 stage 的哪个 node）。
- 校验合法转移：用户给的 choice_key 必须是当前 node 的某条 transition。
- 推进到下一节点；到达阶段边界（transition 带 next_stage）时触发"解锁检查"：
  * 若目标阶段已解锁（rstory_store）→ 进入该阶段入口节点。
  * 若未解锁 → 不推进，返回 needs_unlock 信号，交给支付层去创建订单。

引擎只关心"状态结构与转移合法性"，不直接收钱、不渲染文案：
- 解锁记录的读写在 rstory_store。
- 创建订单 / 确认支付 / 解锁后推进，由 rstory_payment 编排（它解锁后调用本模块的
  enter_stage 把 FSM 推进到新阶段）。

返回值统一用 AdvanceResult，避免调用方靠异常区分"非法转移"与"需要解锁"等正常分支。
"""

from __future__ import annotations

from dataclasses import dataclass

from services import rstory_content as content
from services import rstory_store as store
from utils.logger import setup_logging

logger = setup_logging()


# AdvanceResult.status 取值：
STATUS_OK = "ok"  # 正常推进到 result.node
STATUS_INVALID = "invalid_transition"  # choice_key 非法
STATUS_NEEDS_UNLOCK = "needs_unlock"  # 想进下一阶段但未解锁，result.unlock_stage 给出阶段号
STATUS_END = "end"  # 已是剧情终点，无后续转移


@dataclass
class StateView:
    """用户当前状态的只读视图（给路由层渲染用）。"""

    character: str
    stage: int
    node_id: str
    node: content.Node


@dataclass
class AdvanceResult:
    status: str
    character: str
    # 推进成功时为新节点；needs_unlock / invalid 时为停留的当前节点。
    node: content.Node | None = None
    stage: int | None = None
    unlock_stage: int | None = None  # 仅 needs_unlock 时有意义
    message: str = ""


class RStoryFSMError(Exception):
    """引擎级错误（角色/节点不存在等数据问题，非正常业务分支）。"""


def _require_character(character_id: str) -> content.Character:
    ch = content.get_character(character_id)
    if ch is None:
        raise RStoryFSMError(f"unknown character: {character_id}")
    return ch


def _require_stage(ch: content.Character, stage: int) -> content.Stage:
    st = ch.stages.get(stage)
    if st is None:
        raise RStoryFSMError(f"unknown stage {stage} for character {ch.character_id}")
    return st


def _require_node(st: content.Stage, node_id: str) -> content.Node:
    node = st.nodes.get(node_id)
    if node is None:
        raise RStoryFSMError(f"unknown node {node_id} in stage {st.stage}")
    return node


async def start_story(user_id: int | str, character_id: str | None = None) -> StateView:
    """开始/恢复某角色剧情。

    - 已有进度：恢复到存储里的 stage/node。
    - 无进度：初始化到阶段 1 入口节点（阶段 1 视为"免费试看入口"，是否对阶段1收费由
      调用方决定；本引擎在进入阶段时统一走解锁检查，见 enter_stage / try_advance）。
    """
    character_id = character_id or content.DEFAULT_CHARACTER_ID
    ch = _require_character(character_id)

    prog = await store.get_progress(user_id, character_id)
    if prog is not None:
        st = _require_stage(ch, prog.stage)
        node = _require_node(st, prog.node)
        return StateView(character_id, prog.stage, node.node_id, node)

    # 初始化到阶段 1 入口
    stage1 = _require_stage(ch, 1)
    entry = _require_node(stage1, stage1.entry_node)
    await store.set_progress(user_id, character_id, 1, entry.node_id)
    return StateView(character_id, 1, entry.node_id, entry)


async def get_state(user_id: int | str, character_id: str) -> StateView | None:
    """读取当前状态视图；无进度返回 None。"""
    ch = _require_character(character_id)
    prog = await store.get_progress(user_id, character_id)
    if prog is None:
        return None
    st = _require_stage(ch, prog.stage)
    node = _require_node(st, prog.node)
    return StateView(character_id, prog.stage, node.node_id, node)


def _find_transition(node: content.Node, choice_key: str) -> content.Transition | None:
    for tr in node.transitions:
        if tr.choice_key == choice_key:
            return tr
    return None


async def try_advance(
    user_id: int | str, character_id: str, choice_key: str
) -> AdvanceResult:
    """根据用户选择推进一步。

    流程：
    1. 取当前节点；若该节点无任何转移 → STATUS_END。
    2. 校验 choice_key 是合法转移 → 否则 STATUS_INVALID。
    3. 普通转移（同阶段内）：写进度，返回 STATUS_OK + 新节点。
    4. 阶段边界转移（next_stage）：检查目标阶段是否已解锁：
       - 已解锁 → enter_stage 推进到该阶段入口，STATUS_OK。
       - 未解锁 → STATUS_NEEDS_UNLOCK（不改进度），unlock_stage 给出阶段号。
    """
    ch = _require_character(character_id)
    state = await get_state(user_id, character_id)
    if state is None:
        # 没开始过：先开始再让调用方重试
        state = await start_story(user_id, character_id)

    node = state.node
    if not node.transitions:
        return AdvanceResult(
            status=STATUS_END,
            character=character_id,
            node=node,
            stage=state.stage,
            message="剧情已到终点。",
        )

    tr = _find_transition(node, choice_key)
    if tr is None:
        return AdvanceResult(
            status=STATUS_INVALID,
            character=character_id,
            node=node,
            stage=state.stage,
            message=f"无效选择：{choice_key}",
        )

    # 阶段边界转移：触发解锁检查
    if tr.next_stage is not None:
        target_stage = tr.next_stage
        _require_stage(ch, target_stage)  # 数据校验：目标阶段必须存在
        unlocked = await store.is_stage_unlocked(user_id, character_id, target_stage)
        if not unlocked:
            return AdvanceResult(
                status=STATUS_NEEDS_UNLOCK,
                character=character_id,
                node=node,
                stage=state.stage,
                unlock_stage=target_stage,
                message=f"进入阶段{target_stage}需要先解锁。",
            )
        new_state = await enter_stage(user_id, character_id, target_stage)
        return AdvanceResult(
            status=STATUS_OK,
            character=character_id,
            node=new_state.node,
            stage=new_state.stage,
            message="已进入新阶段。",
        )

    # 普通同阶段转移
    if tr.target_node is None:
        raise RStoryFSMError(
            f"transition {choice_key} on node {node.node_id} has neither target_node nor next_stage"
        )
    st = _require_stage(ch, state.stage)
    next_node = _require_node(st, tr.target_node)
    await store.set_progress(user_id, character_id, state.stage, next_node.node_id)
    if not next_node.transitions:
        return AdvanceResult(
            status=STATUS_END,
            character=character_id,
            node=next_node,
            stage=state.stage,
            message="本段已到终点。",
        )
    return AdvanceResult(
        status=STATUS_OK,
        character=character_id,
        node=next_node,
        stage=state.stage,
        message="",
    )


async def enter_stage(user_id: int | str, character_id: str, stage: int) -> StateView:
    """把 FSM 推进到指定阶段的入口节点并写进度。

    解锁成功后由 rstory_payment 调用本函数完成"推进 FSM"。不在此处做解锁校验：
    解锁判定属于调用方（payment / try_advance）的职责，本函数只负责状态迁移。
    """
    ch = _require_character(character_id)
    st = _require_stage(ch, stage)
    entry = _require_node(st, st.entry_node)
    await store.set_progress(user_id, character_id, stage, entry.node_id)
    return StateView(character_id, stage, entry.node_id, entry)
