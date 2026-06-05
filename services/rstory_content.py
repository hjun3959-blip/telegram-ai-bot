"""R 级互动剧情内容图（数据层，与 FSM 引擎解耦）。

这里只定义"角色 → 阶段(stage) → 节点(node) → 转移(transition)"的纯数据结构。
引擎（services/rstory_fsm_service.py）只消费本模块的数据，不关心具体文案/媒体；
后续填充真实剧情时只改这里（或换一份数据源），引擎、存储、支付层都不用动。

数据结构约定：
- 一个 Character 有一条剧情线，由若干 Stage 组成；阶段号从 1 递增。
- 每个 Stage 有若干 Node；每个 Stage 指定一个入口节点 entry_node。
- Node 之间通过 Transition 连接：一条 transition = (choice_key, 目标 node_id)。
  * choice_key 是用户的选择/输入标识（按钮 callback 或文本），引擎据此校验合法转移。
  * 目标 node 可以在同一 stage 内，也可以是"阶段终点"（next_stage 字段表示要进入下一阶段）。
- 阶段边界：当某个 node 的某条 transition 带 next_stage 时，代表"想进入下一阶段"，
  引擎会在这里触发解锁检查（是否已付费解锁该阶段）。

阶段内容形态（与定价对应，仅占位）：
- 阶段 1：纯文字
- 阶段 2：图文（占位 media）
- 阶段 3：视频（占位 media）
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 内容形态标识，仅用于占位与展示，不影响引擎逻辑。
CONTENT_TEXT = "text"
CONTENT_IMAGE_TEXT = "image_text"
CONTENT_VIDEO = "video"


@dataclass(frozen=True)
class Transition:
    """一条从当前节点出发的合法转移。

    choice_key：用户选择/输入的标识（引擎据此匹配）。
    target_node：目标节点 id。
    next_stage：若不为 None，表示这条转移想进入该阶段号（触发解锁检查）。
    label：给用户看的选项文案（占位）。
    """

    choice_key: str
    target_node: str | None
    next_stage: int | None = None
    label: str = ""


@dataclass(frozen=True)
class Node:
    """剧情节点（一屏内容 + 若干可选转移）。"""

    node_id: str
    content_type: str
    text: str
    transitions: tuple[Transition, ...] = field(default_factory=tuple)
    media_placeholder: str | None = None  # 阶段2/3 的图/视频占位描述
    is_stage_end: bool = False  # 到达即代表本阶段内容走完


@dataclass(frozen=True)
class Stage:
    """一个阶段：入口节点 + 节点集合 + 内容形态。"""

    stage: int
    title: str
    content_type: str
    entry_node: str
    nodes: dict[str, Node]


@dataclass(frozen=True)
class Character:
    """一个角色的完整剧情线。"""

    character_id: str
    name: str
    intro: str
    stages: dict[int, Stage]

    @property
    def stage_numbers(self) -> list[int]:
        return sorted(self.stages.keys())


def _stage1() -> Stage:
    """阶段 1：纯文字（占位）。一个入口节点 + 一个分支 + 阶段终点。"""
    nodes = {
        "s1_intro": Node(
            node_id="s1_intro",
            content_type=CONTENT_TEXT,
            text="【阶段1·占位文字】夜色刚落，她在门口回头看了你一眼。要不要走过去？",
            transitions=(
                Transition("approach", "s1_talk", label="走过去搭话"),
                Transition("wait", "s1_wait", label="先在原地等等"),
            ),
        ),
        "s1_talk": Node(
            node_id="s1_talk",
            content_type=CONTENT_TEXT,
            text="【阶段1·占位文字】你走上前，她笑了笑，气氛慢慢热起来。",
            transitions=(
                Transition("end", "s1_end", label="继续"),
            ),
        ),
        "s1_wait": Node(
            node_id="s1_wait",
            content_type=CONTENT_TEXT,
            text="【阶段1·占位文字】你没动，她反倒主动走了过来。",
            transitions=(
                Transition("end", "s1_end", label="继续"),
            ),
        ),
        "s1_end": Node(
            node_id="s1_end",
            content_type=CONTENT_TEXT,
            text="【阶段1·占位文字】这一段先到这里。想更进一步吗？",
            transitions=(
                # 想进入阶段 2：带 next_stage=2，引擎在此触发解锁检查
                Transition("go_stage2", None, next_stage=2, label="解锁阶段2（图文）"),
            ),
            is_stage_end=True,
        ),
    }
    return Stage(
        stage=1,
        title="初遇（纯文字）",
        content_type=CONTENT_TEXT,
        entry_node="s1_intro",
        nodes=nodes,
    )


def _stage2() -> Stage:
    """阶段 2：图文（占位 media）。"""
    nodes = {
        "s2_intro": Node(
            node_id="s2_intro",
            content_type=CONTENT_IMAGE_TEXT,
            text="【阶段2·占位图文】灯光暧昧，她递给你一张照片。",
            media_placeholder="[占位图片：阶段2 场景图]",
            transitions=(
                Transition("look", "s2_end", label="仔细看看"),
            ),
        ),
        "s2_end": Node(
            node_id="s2_end",
            content_type=CONTENT_IMAGE_TEXT,
            text="【阶段2·占位图文】她凑近了一些。要看最后一幕吗？",
            media_placeholder="[占位图片：阶段2 收尾图]",
            transitions=(
                Transition("go_stage3", None, next_stage=3, label="解锁阶段3（视频）"),
            ),
            is_stage_end=True,
        ),
    }
    return Stage(
        stage=2,
        title="升温（图文）",
        content_type=CONTENT_IMAGE_TEXT,
        entry_node="s2_intro",
        nodes=nodes,
    )


def _stage3() -> Stage:
    """阶段 3：视频（占位 media）。最终阶段，无后续 next_stage。"""
    nodes = {
        "s3_intro": Node(
            node_id="s3_intro",
            content_type=CONTENT_VIDEO,
            text="【阶段3·占位视频】她按下了播放键。",
            media_placeholder="[占位视频：阶段3 主视频]",
            transitions=(
                Transition("finish", "s3_end", label="看完"),
            ),
        ),
        "s3_end": Node(
            node_id="s3_end",
            content_type=CONTENT_VIDEO,
            text="【阶段3·占位视频】剧情到此结束。感谢体验。",
            media_placeholder="[占位视频：阶段3 结局]",
            transitions=(),  # 终点，无转移
            is_stage_end=True,
        ),
    }
    return Stage(
        stage=3,
        title="高潮（视频）",
        content_type=CONTENT_VIDEO,
        entry_node="s3_intro",
        nodes=nodes,
    )


# 示例角色：1 个角色 + 3 个阶段占位剧情图，用于自测与演示。
_LINGLING = Character(
    character_id="lingling",
    name="玲玲",
    intro="示例角色（占位）。三阶段：纯文字 → 图文 → 视频。",
    stages={
        1: _stage1(),
        2: _stage2(),
        3: _stage3(),
    },
)


# 角色注册表：character_id -> Character。后续加角色只在这里登记。
CHARACTERS: dict[str, Character] = {
    _LINGLING.character_id: _LINGLING,
}

# 默认角色（/rstory 演示入口用）。
DEFAULT_CHARACTER_ID = _LINGLING.character_id


def get_character(character_id: str) -> Character | None:
    """按 id 取角色；不存在返回 None。"""
    return CHARACTERS.get(character_id)


def list_character_ids() -> list[str]:
    return list(CHARACTERS.keys())
