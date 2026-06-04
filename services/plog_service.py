"""AI 大头贴 + 生活小报（/plog）服务层。

职责：
- 维护“每个用户最近一张私信照片”的内存缓存（带 TTL + 容量上限），用于 /plog 使用。
  仅缓存本地 file_path / file_id / 基本元数据；不长期存原图。每个用户只保留最近一张，
  生成后立刻清理。
- 风格 prompt 注册表：把用户传入的“风格关键词”翻译成小红书 plog 风格的中文 prompt 片段。
- 调用 image_generation_service.generate_image_from_reference 进行图片生成；
  如果底层没有 image edit 能力，会自动降级到“风格化排版 prompt + images.generate”，
  调用方拿到的接口形状不变。

设计要点：
- 用户隔离：缓存 key = user_id（int）。同一用户后来的照片会覆盖前一张（“只保留最近一张”）。
- 不缓存 business 模式的照片；只对 private 模式生效，避免污染真实代聊。
- TTL 默认 10 分钟；超时自动失效，调用方拿到 None 时按“没有待处理照片”处理。
- 失败时返回 ok=False + 中文温柔提示，调用方决定如何回退。
- 不读取任何密钥，复用 image_generation_service 的客户端。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

from utils.logger import setup_logging

logger = setup_logging()


# ---------- 缓存 ----------

# 单图缓存 TTL（秒）。10 分钟够了：用户发图后通常很快接 /plog。
_PHOTO_CACHE_TTL_SECONDS = 10 * 60
# 容量上限，避免被恶意触发导致内存泄漏；超出时按 LRU 淘汰最早的一条。
_PHOTO_CACHE_MAX_USERS = 256


@dataclass
class PendingPhoto:
    """一条等待 /plog 使用的私信照片。

    file_path 是机器人本地临时落盘路径（jpg/png）。也允许 None，表示只记下了 file_id
    （留作未来扩展，不在本期使用）。
    """
    user_id: int
    file_path: str | None
    file_id: str | None
    caption: str | None
    created_at: float


# user_id -> PendingPhoto
_pending_photos: dict[int, PendingPhoto] = {}


def _now() -> float:
    return time.time()


def _evict_expired() -> None:
    """删除超过 TTL 的条目。"""
    cutoff = _now() - _PHOTO_CACHE_TTL_SECONDS
    expired = [uid for uid, p in _pending_photos.items() if p.created_at < cutoff]
    for uid in expired:
        _safe_pop(uid)


def _safe_pop(user_id: int) -> PendingPhoto | None:
    """从缓存里弹出并尝试删本地文件。失败也吞掉。"""
    item = _pending_photos.pop(user_id, None)
    if item and item.file_path:
        try:
            if os.path.exists(item.file_path):
                os.remove(item.file_path)
        except Exception as e:
            logger.debug("plog cache file remove failed | path=%s | err=%s", item.file_path, e)
    return item


def remember_photo(
    user_id: int,
    *,
    file_path: str | None,
    file_id: str | None = None,
    caption: str | None = None,
) -> None:
    """记下用户最近一张私信照片。同一用户重复调用会覆盖前一张并清理其本地文件。"""
    if not user_id:
        return
    # 先淘汰过期
    _evict_expired()

    # 容量保护：超出时弹出最早的一条
    if len(_pending_photos) >= _PHOTO_CACHE_MAX_USERS and user_id not in _pending_photos:
        try:
            oldest_uid = min(_pending_photos, key=lambda u: _pending_photos[u].created_at)
            _safe_pop(oldest_uid)
        except Exception:
            pass

    # 覆盖前先清掉旧本地文件
    _safe_pop(user_id)

    _pending_photos[user_id] = PendingPhoto(
        user_id=user_id,
        file_path=file_path,
        file_id=file_id,
        caption=(caption or "").strip() or None,
        created_at=_now(),
    )


def get_pending_photo(user_id: int) -> PendingPhoto | None:
    """读取最近一张未过期的照片，不弹出。"""
    if not user_id:
        return None
    _evict_expired()
    return _pending_photos.get(user_id)


def consume_pending_photo(user_id: int) -> PendingPhoto | None:
    """读取并从缓存弹出（不删本地文件，留给调用方使用完再清理）。"""
    if not user_id:
        return None
    _evict_expired()
    return _pending_photos.pop(user_id, None)


def clear_pending_photo(user_id: int) -> None:
    """生成完毕后调用，清除缓存并删本地文件。"""
    _safe_pop(user_id)


def cache_size() -> int:
    """测试 / 调试用。"""
    _evict_expired()
    return len(_pending_photos)


# ---------- 风格 prompt ----------

# 默认风格：可爱手账风（贴近参考图）
DEFAULT_STYLE_KEY = "可爱手账风"

# 风格关键词 -> 中文 prompt 片段。命中 substring 或同义词即可；保留 key 原文用于日志。
# 文案要求：保留人物身份不变；只做风格化排版与小元素叠加；文字短且自然。
_STYLE_RULES: list[tuple[tuple[str, ...], str]] = [
    (
        ("可爱手账", "手账", "拼贴", "plog"),
        "粉白色调可爱手账风：照片作为主体，四周加白色与浅粉手写涂鸦、小箭头、爱心、星星、便签贴纸边框，"
        "拼贴小分身/大头贴效果。",
    ),
    (
        ("甜点", "甜品", "奶油", "cream"),
        "甜点小报风：奶油白与马卡龙色调，照片四周点缀小蛋糕、奶油涂鸦、爱心糖珠、可爱字体便签。",
    ),
    (
        ("y2k", "千禧", "少女拼贴"),
        "Y2K 少女拼贴风：金属粉、果冻紫、闪光星星、贴纸、CD 元素、贴图箭头与手写英文短句。",
    ),
    (
        ("韩系", "治愈", "ins"),
        "韩系治愈 ins 风：低饱和米白与浅绿，简洁手写花体英文、细线箭头、小花朵贴纸，留白多、不杂乱。",
    ),
    (
        ("贝贝", "贝贝风格", "温柔", "柔和"),
        "温柔粉白手账风：浅粉米白、柔光、手写圆体短句、爱心与小星星、轻轻的箭头和便签，整体安静治愈。",
    ),
]

# 可选的中文短句池：模型可挑 1-3 句使用；不要全用上。
_CAPTION_CANDIDATES = [
    "今天也要闪闪发光",
    "认真生活 慢慢来",
    "小确幸收集日常",
    "放松一下 享受生活",
    "把日子过成喜欢的样子",
    "今天份的好心情",
    "记录温柔的一天",
]

# Q 版分身 sub-mode 关键词（命中即切到 Q 版分身手账照模板）。
# 注意：这只是 /plog 的子模式，不是单独命令；与 /magnet 完全无关。
# 现已切到“Q版分身手账照”官方模板（保留真人 + 5-8 个 chibi 分身 + 手账涂鸦 + 手写短句）。
Q_VERSION_STYLE_KEY = "q版分身"
Q_VERSION_SYNONYMS: tuple[str, ...] = (
    "q版",
    "q 版",
    "qq版",
    "q版手账",
    "q版大头贴",
    "q版分身",
    "分身",
    "迷你分身",
    "大头贴",
    "sd公仔",
    "sd 公仔",
    "chibi",
    "手账照",
)


def _matches_q_version(text: str | None) -> bool:
    """判断用户输入是否命中 Q 版分身 sub-mode。命中即走 Q 版模板。"""
    if not text:
        return False
    low = text.strip().lower()
    if not low:
        return False
    for kw in Q_VERSION_SYNONYMS:
        if kw.lower() in low:
            return True
    return False


# 用户正式提供的 Q 版分身手账照 prompt 模板（仅 /plog 子模式使用，不用于 /magnet）。
# 这是当前 Q 版分支的正式模板：真人照增强 + 5-8 个 chibi 分身 + 手账涂鸦 + 手写短句。
PLOG_Q_VERSION_HANDBOOK_TEMPLATE = (
    "基于用户真人生活照，生成「Q版分身手账照」：\n"
    "【保留原图（硬红线）】\n"
    "• 真实人物，脸部、发型、发色、服装、姿势、背景、光线都不变\n"
    "• 不换脸、不插画化、不改身份、不美颜、不改身材比例\n"
    "【自动判断主题】\n"
    "• 根据场景（工作 / 自拍 / 饮食 / 运动 / 旅行 / 居家等）设计后续元素\n"
    "【Q版分身（5-8 个）】\n"
    "• 同一人物的迷你版，大头小身，统一 chibi 贴纸风格\n"
    "• 保留发型、发色、服装色系，必须看出是“同一个人”的 Q 版\n"
    "• 动作与情绪多样，围绕主题设计：\n"
    "  - 工作场景：打字、喝咖啡、赶路、抱文件、托腮发呆\n"
    "  - 自拍场景：比耶、跳跃、托腮、嘟嘴、举手机\n"
    "  - 饮食场景：举叉子、喝奶茶、捧碗、流口水、抱面包\n"
    "  - 运动场景：举铁、拉伸、擦汗、跑步、瑜伽\n"
    "  - 旅行场景：拖箱子、看地图、拍照、戴墨镜、举护照\n"
    "  - 居家场景：抱抱枕、伸懒腰、躺着、刷剧、撸猫\n"
    "【贴纸呈现】\n"
    "• 白色描边、柔和阴影、轻微浮起感，分布在人物四周空白处\n"
    "• 不遮挡主体（脸、身体不可被分身/涂鸦盖住）\n"
    "【手绘涂鸦】\n"
    "• 白色为主 + 少量粉色\n"
    "• 手绘星星、爱心、线条、标注、对话框等，增强手账感\n"
    "【手写短句（5-8 句）】\n"
    "• 手写风短句，贴近主题（例：“今日加油”“放松一下”“好好吃饭”）\n"
    "• 白色手写字，关键词可用粉色下划线\n"
    "【构图】\n"
    "• 中央真人 + 四周 Q 版分身 + 涂鸦与文字填充留白\n"
    "• 清爽有层次，不要拥挤、不要遮挡主体\n"
    "【整体风格】\n"
    "• 真实照片增强 + 可爱贴纸 + 手账日记感\n"
    "• 精致轻松，适合社交媒体\n"
    "【避免】\n"
    "• 文字乱码、画风不统一、人物不像、过度拥挤、遮挡主体\n"
    "• 与主题无关的动作或文案\n"
)

# 旧版（迷你分身互动合成）模板，作为可选回退保留；当前 Q 版默认走 Handbook 版本。
PLOG_Q_VERSION_TEMPLATE = PLOG_Q_VERSION_HANDBOOK_TEMPLATE


# 用户正式提供的 plog 手绘注解 prompt 模板（仅 /plog 使用，不用于 /magnet）。
# 作为公开常量保留，便于后续调。
PLOG_HAND_DRAWN_ANNOTATION_TEMPLATE = (
    "请观察照片中的元素、并为每个物件加上有意义的手绘风注解。请填写照片中的物品（例：披萨、汽水）\n"
    "【描写规则】\n"
    "• 使用像白色笔画的细线手绘线条\n"
    "• 一笔画风格、随性、略带不均匀感\n"
    "• 沿着物件外围加上描边轮廓\n"
    "• 用箭头或虚线做出视线引导\n"
    "【文字规则】\n"
    "• 手写风格字体（日系可爱感）\n"
    "• 句子简短、像自言自语的小碎念\n"
    "• 语气偏日记感、带一点情绪\n"
    "【注解生成规则】\n"
    "• 饮料 -> 味道、温度、心情（例：清爽、微甜、刚刚好）\n"
    "• 食物 -> 口感、好吃程度（例：松软、超好吃）\n"
    "• 空间 -> 氛围（例：很放松、喜欢这种感觉）\n"
    "• 整体 -> 一句总结（例：今天有点幸福~）\n"
    "【装饰】\n"
    "• 适度加入热气、闪光、爱心、星星、小表情等元素\n"
    "• 不要过度装饰，保留空白空间"
)


def resolve_style(raw: str | None) -> tuple[str, str]:
    """根据用户输入返回 (匹配到的风格名, 风格 prompt 片段)。

    匹配规则（按声明顺序）：
    - 命中任一同义词的 substring 即选中
    - 没命中时回退默认风格
    """
    text = (raw or "").strip().lower()
    if text:
        for synonyms, fragment in _STYLE_RULES:
            for kw in synonyms:
                if kw.lower() in text:
                    return synonyms[0], fragment
    # 默认
    for synonyms, fragment in _STYLE_RULES:
        if synonyms[0] == DEFAULT_STYLE_KEY:
            return synonyms[0], fragment
    # 兜底
    return DEFAULT_STYLE_KEY, _STYLE_RULES[0][1]


def _build_plog_prompt_q_version(*, beibei: bool, user_caption: str | None) -> str:
    """Q 版分身 sub-mode：人物照片 → 8 个 SD 公仔迷你分身环绕互动。"""
    caption_part = f"\n用户附加描述：{user_caption.strip()}" if (user_caption and user_caption.strip()) else ""
    tone_extra = (
        "\n整体请更柔和、可爱、生活感强；色调偏粉白米色，留白多一点。"
        if beibei else ""
    )
    return (
        f"AI 大头贴 / Q 版分身合成（{Q_VERSION_STYLE_KEY}）。\n\n"
        "【Q 版分身主指令（重要）】\n"
        f"{PLOG_Q_VERSION_TEMPLATE}"
        f"{tone_extra}"
        f"{caption_part}"
    )


def _build_plog_prompt_annotation(
    style_raw: str | None,
    *,
    beibei: bool,
    user_caption: str | None,
) -> str:
    """默认 sub-mode：手绘风注解 + 生活 plog 拼贴排版（适用于食物 / 饮料 / 空间 / 整体）。"""
    style_name, style_fragment = resolve_style(style_raw)
    captions_hint = "、".join(f"“{c}”" for c in _CAPTION_CANDIDATES)

    caption_part = f"\n用户附加描述：{user_caption.strip()}" if (user_caption and user_caption.strip()) else ""
    tone_extra = (
        "\n整体氛围请更柔和、可爱、生活感强，留白多一点，避免视觉拥挤。"
        if beibei else ""
    )

    return (
        f"小红书/生活 plog 风格的拼贴图（{style_name}）。\n"
        f"风格要点：{style_fragment}\n\n"
        "【手绘注解主指令（重要）】\n"
        f"{PLOG_HAND_DRAWN_ANNOTATION_TEMPLATE}\n\n"
        "【构图补充】\n"
        "- 以提供的原照片为画面主体，保持人物五官、身份、姿态不变，不要替换人脸\n"
        "- 可选叠加小分身大头贴/拼贴效果，但主照片仍清晰可辨\n"
        "- 除了上面的手绘注解，还可以点缀：手写涂鸦、贴纸边框、小爱心、星星\n"
        "- 可以参考的中文总结短句（1-3 条即可，不要全用）：\n"
        f"  {captions_hint}\n\n"
        "【硬红线】\n"
        "- 不要改变人物的身份或外貌；不要加水印；不要乱涂抹脸部\n"
        "- 文字不要乱码，不要密集文字，不要超长段落\n"
        f"{tone_extra}"
        f"{caption_part}"
    )


def build_plog_prompt(style_raw: str | None, *, beibei: bool = False, user_caption: str | None = None) -> str:
    """生成用于 image edit / image generation 的最终 prompt（中文）。

    /plog 内部有两个 sub-mode（完全分开的两套 prompt，不混用）：
    - Q 版分身（命中 Q_VERSION_SYNONYMS 时）：人物照专用，8 个 SD 公仔迷你分身互动
    - 手绘注解（默认）：食物 / 饮料 / 空间 / 整体的小红书 plog 注解排版

    style_raw / user_caption 任一命中 Q 版关键词都会切到 Q 版 sub-mode。
    """
    if _matches_q_version(style_raw) or _matches_q_version(user_caption):
        return _build_plog_prompt_q_version(beibei=beibei, user_caption=user_caption)
    return _build_plog_prompt_annotation(style_raw, beibei=beibei, user_caption=user_caption)


# ---------- 调用入口 ----------

async def generate_plog_image(
    *,
    style_raw: str | None,
    reference_path: str | None,
    beibei: bool = False,
    user_caption: str | None = None,
) -> dict[str, Any]:
    """生成 plog 图。返回结构与 image_generation_service.generate_image 一致：
    {"ok": bool, "url": str|None, "data": bytes|None, "error": str|None, "style": str}

    reference_path 为 None 时也允许（仅基于风格 prompt 生成）。
    内部失败时返回温柔提示，不抛异常。
    """
    # Q 版子模式时 style 名固定显示为 Q 版分身；否则按风格匹配。
    if _matches_q_version(style_raw) or _matches_q_version(user_caption):
        style_name = Q_VERSION_STYLE_KEY
    else:
        style_name, _ = resolve_style(style_raw)
    prompt = build_plog_prompt(style_raw, beibei=beibei, user_caption=user_caption)

    # 延迟导入避免循环
    from services.image_generation_service import generate_image_from_reference

    try:
        result = await generate_image_from_reference(
            prompt=prompt,
            reference_path=reference_path,
        )
    except Exception as e:
        logger.exception("plog generate_image_from_reference crashed | err=%s", e)
        return {
            "ok": False,
            "url": None,
            "data": None,
            "error": "图片生成开小差了，等下再发一次试试～",
            "style": style_name,
        }

    if not isinstance(result, dict):
        return {
            "ok": False,
            "url": None,
            "data": None,
            "error": "图片生成开小差了，等下再发一次试试～",
            "style": style_name,
        }

    # 温柔包装错误文案
    if not result.get("ok"):
        err = (result.get("error") or "").strip() or "图片生成开小差了，等下再发一次试试～"
        return {
            "ok": False,
            "url": None,
            "data": None,
            "error": err,
            "style": style_name,
        }

    return {
        "ok": True,
        "url": result.get("url"),
        "data": result.get("data"),
        "error": None,
        "style": style_name,
        "fallback_to_text2image": bool(result.get("fallback_to_text2image", False)),
    }
