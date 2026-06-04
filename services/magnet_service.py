"""AI 冰箱贴海报（/magnet）服务层。

需求摘要（参考用户给的视觉重点）：
- 竖版 3:4 拼图
- 上半部分约 50%：从原图提取主色作为纯色背景；居中偏上放一个小而精致的“建筑/场景
  冰箱贴图标”，图标来自下半真实照片中最有识别度的主体（建筑立面/门窗/拱门/屋顶/
  阳台/正面等）；白色或浅色描边，轻微立体投影，像旅行纪念品冰箱贴；图标只占上半
  背景约 1/4，留大量留白；图标下方一行优雅英文：地点名, YYYY.MM
- 下半部分约 50%：保留原图，不做修改
- 风格：小红书城市打卡 / 建筑冰箱贴 / 极简拼贴 / 高级旅行摄影卡片 / 城市漫游感
- 禁忌：普通拼贴 / 复杂繁琐 / 多余装饰 / 乱码文字 / 过度卡通化 / 图标过大 / 文字压主体

设计要点：
- 与 plog_service 解耦：自己一套 prompt 常量，便于后续调
- 复用 plog_service 的待处理照片缓存（不重复造轮子）
- 复用 image_generation_service.generate_image_from_reference，做兼容降级
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from utils.logger import setup_logging

logger = setup_logging()


# ---------- 风格 prompt 常量（公开，便于调） ----------

MAGNET_FORMAT = "竖版 3:4 海报"

MAGNET_TOP_HALF_DIRECTIVE = (
    "【上半 50%】纯色背景区：\n"
    "- 背景颜色来自下半原图的主色调（柔和、明亮、干净，不要饱和度过高）\n"
    "- 居中偏上放置一个小而精致的“建筑/场景冰箱贴图标”，图标来自下半原图里最具识别度\n"
    "  的主体：建筑立面、门窗、拱门、屋顶、阳台、店面正面等；保留建筑的实际造型与比例，\n"
    "  不要随意杜撰\n"
    "- 图标处理：白色或浅色描边，轻微立体投影，像真实的旅行纪念品冰箱贴；可以略带凸起\n"
    "  立体感，但不要做成厚重卡通贴纸\n"
    "- 图标尺寸：只占上半背景约 1/4 面积，留大量留白；不要顶到边\n"
    "- 图标下方一行优雅英文：具体地点名, YYYY.MM（年月）——\n"
    "  字体细、轻、间距大、字号不大，不要花体也不要装饰字\n"
)

MAGNET_BOTTOM_HALF_DIRECTIVE = (
    "【下半 50%】真实照片区：\n"
    "- 完整保留原照片，不要二次修图、不要滤镜、不要加任何贴纸或文字\n"
    "- 与上半的纯色背景之间是清晰的水平分割，不要渐变过渡\n"
)

MAGNET_STYLE_DIRECTIVE = (
    "整体风格：小红书城市打卡 / 建筑冰箱贴 / 极简拼贴 / 高级旅行摄影卡片 / 城市漫游感。\n"
    "感觉要清爽、干净、明亮、有质感。\n"
)

MAGNET_HARD_NEGATIVES = (
    "硬禁忌（务必避免）：\n"
    "- 不要普通拼贴风（大头贴/手账/贴纸雨）\n"
    "- 不要复杂、繁琐、过度装饰\n"
    "- 不要任何乱码文字或胡乱英文；英文只允许“地点名, YYYY.MM”那一行\n"
    "- 不要过度卡通化，不要 emoji，不要可爱涂鸦\n"
    "- 不要把图标做大到压住背景或下半照片\n"
    "- 不要在原照片上盖文字、贴纸、水印\n"
    "- 不要改变下半原照片里人物身份与场景\n"
)


def _guess_month_label(now: datetime | None = None) -> str:
    """默认用当前的 YYYY.MM；调用方可在 user_caption 里显式覆盖。"""
    n = now or datetime.now()
    return f"{n.year}.{n.month:02d}"


def _parse_location_and_date(raw: str | None) -> tuple[str, str]:
    """从用户传入的风格/地点参数里抽出 (location, yyyymm_label)。

    支持格式：
    - 空：返回 ("", 当前 YYYY.MM)
    - "巴黎"：("巴黎", 当前 YYYY.MM)
    - "巴黎 2025.10"：("巴黎", "2025.10")
    - "巴黎, 2025.10"：("巴黎", "2025.10")
    """
    text = (raw or "").strip()
    if not text:
        return "", _guess_month_label()

    # 简单切：找最后一个 token 是否像日期
    parts = [p.strip(",， ") for p in text.replace("，", ",").split()]
    parts = [p for p in parts if p]
    if not parts:
        return "", _guess_month_label()

    last = parts[-1]
    # 像 2025.10 / 2025-10 / 2025/10 / 2025.10.01
    looks_date = (
        len(last) >= 6
        and last[:4].isdigit()
        and any(sep in last for sep in (".", "-", "/"))
    )
    if looks_date and len(parts) >= 2:
        # 规范化为 YYYY.MM
        norm = last.replace("-", ".").replace("/", ".")
        tokens = norm.split(".")
        if len(tokens) >= 2 and tokens[0].isdigit() and tokens[1].isdigit():
            month = int(tokens[1])
            yyyymm = f"{tokens[0]}.{month:02d}"
        else:
            yyyymm = norm
        location = " ".join(parts[:-1])
        return location, yyyymm

    return " ".join(parts), _guess_month_label()


def build_magnet_prompt(
    raw_arg: str | None,
    *,
    beibei: bool = False,
    user_caption: str | None = None,
) -> str:
    """生成用于 image edit / image generation 的最终 prompt（中文）。

    raw_arg 可以是“地点名”或“地点名 YYYY.MM”。
    """
    location, yyyymm = _parse_location_and_date(raw_arg)
    location_for_label = location.strip() or "Travel"

    extra_caption = (
        f"\n用户附加描述（仅作参考）：{user_caption.strip()}"
        if (user_caption and user_caption.strip())
        else ""
    )

    tone_extra = (
        "\n整体气质再温柔一点：背景色更轻一些，留白更多，避免任何视觉拥挤。\n"
        if beibei else ""
    )

    return (
        f"{MAGNET_FORMAT}：上下两部分各占 50%，水平分割清晰。\n\n"
        f"{MAGNET_TOP_HALF_DIRECTIVE}\n"
        f"  英文文字内容固定为：{location_for_label}, {yyyymm}\n\n"
        f"{MAGNET_BOTTOM_HALF_DIRECTIVE}\n"
        f"{MAGNET_STYLE_DIRECTIVE}\n"
        f"{MAGNET_HARD_NEGATIVES}"
        f"{tone_extra}"
        f"{extra_caption}"
    )


async def generate_magnet_image(
    *,
    raw_arg: str | None,
    reference_path: str | None,
    beibei: bool = False,
    user_caption: str | None = None,
) -> dict[str, Any]:
    """生成 magnet 海报图。返回结构与 image_generation_service.generate_image 一致。"""
    location, yyyymm = _parse_location_and_date(raw_arg)
    prompt = build_magnet_prompt(raw_arg, beibei=beibei, user_caption=user_caption)

    from services.image_generation_service import generate_image_from_reference

    try:
        result = await generate_image_from_reference(
            prompt=prompt,
            reference_path=reference_path,
            size="1024x1536",  # 接近 3:4，gpt-image 系常用尺寸
        )
    except Exception as e:
        logger.exception("magnet generate_image_from_reference crashed | err=%s", e)
        return {
            "ok": False,
            "url": None,
            "data": None,
            "error": "图片生成开小差了，等下再发一次试试～",
            "location": location,
            "yyyymm": yyyymm,
        }

    if not isinstance(result, dict) or not result.get("ok"):
        err = (
            (result or {}).get("error")
            if isinstance(result, dict)
            else "图片生成开小差了，等下再发一次试试～"
        )
        return {
            "ok": False,
            "url": None,
            "data": None,
            "error": (err or "").strip() or "图片生成开小差了，等下再发一次试试～",
            "location": location,
            "yyyymm": yyyymm,
        }

    return {
        "ok": True,
        "url": result.get("url"),
        "data": result.get("data"),
        "error": None,
        "location": location,
        "yyyymm": yyyymm,
        "fallback_to_text2image": bool(result.get("fallback_to_text2image", False)),
    }
