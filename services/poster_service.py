"""明星拼贴海报（/poster /starposter）服务层。

独立功能：粉色系柔和甜美 + 拼贴艺术 + 潮流明星/网红/时尚模特海报。
与 /plog（手账小报 / Q 版分身）、/magnet（旅行冰箱贴）、/y2k（Y2K 剪贴簿）
完全解耦：自己一套 prompt 常量、独立的生成入口，不复用对方的核心模板。

需求摘要（用户原始提示词要点）：
- 色彩搭配丰富：粉色系为主（浅粉、桃粉、玫瑰粉），营造温柔甜美氛围；
  搭配白色、金色、银色增加高级感；可加入黄色/蓝色点缀，整体和谐
- 人物形象多样：潮流感强（时尚模特/明星/网红感），穿搭、发型、妆容代表潮流趋势；
  可甜美、酷帅、复古，通过拼贴组合形成独特视觉
- 元素拼贴：时尚杂志人物照片、潮流图片、艺术元素、潮流物品、图案、文字组合，丰富层次与叙事性
- 排版独特：可简洁明了，也可夸张斜体、大字号、错位排列；字体大小/颜色/粗细形成层级
- 多元融合：Y2K 复古未来感、金属光泽、渐变字体、霓虹光影；颗粒质感、丝网印刷、
  90 年代杂志印刷氛围；也可加赛博朋克、镭射、荧光、光栅线条
- 视觉冲击强：色彩与形状对比，青春活力、现代感、创意张力

设计要点：
- 复用 plog_service 的“最近一张照片”缓存（共享缓存池）
- 复用 image_generation_service.generate_image_from_reference，做兼容降级
- 不能严格图生图时，由 image_generation_service 标记 fallback_to_text2image=True，
  路由层根据该标记诚实告诉用户“当前接口无法严格保留原图”
"""

from __future__ import annotations

from typing import Any

from utils.logger import setup_logging

logger = setup_logging()


# ---------- 风格 prompt 常量（公开，便于调） ----------

POSTER_FORMAT = "竖版 3:4 明星拼贴海报"

# 主视觉指令：粉色系 + 时尚明星拼贴 + 杂志感（核心，不要随意改写）
POSTER_CORE_DIRECTIVE = (
    "主题：粉色系柔和甜美 + 拼贴艺术 + 潮流明星/网红/时尚模特海报。\n"
    "【色彩搭配】\n"
    "- 主色：粉色系（浅粉、桃粉、玫瑰粉），整体氛围温柔甜美\n"
    "- 辅色：白色、金色、银色，增加高级感与质感\n"
    "- 点缀：少量黄色/蓝色作为对比色，整体保持和谐\n"
    "【人物表达】\n"
    "- 潮流感强：时尚模特/明星/网红气质\n"
    "- 穿搭、发型、妆容呈现当下潮流趋势\n"
    "- 气质可甜美、可酷帅、可复古，通过拼贴组合形成独特视觉\n"
    "【拼贴元素】\n"
    "- 时尚杂志风人物照片切片、潮流图片、艺术插画\n"
    "- 潮流物品（墨镜、唇膏、相机、星星、爱心、闪光、玫瑰、丝带、缎面）\n"
    "- 抽象图案与几何线条；丰富的层次与叙事性\n"
    "【排版】\n"
    "- 字体大小、颜色、粗细形成标题/正文/辅助信息三级层级\n"
    "- 允许夸张斜体、大字号、错位排列；也允许简洁明了的网格\n"
    "- 关键英文短词如 “STAR / VOGUE / GIRL / ICON / DREAM / GLOSS” 仅作装饰，不要乱码\n"
    "【风格融合】\n"
    "- Y2K 复古未来感、金属光泽、渐变字体、霓虹光影\n"
    "- 颗粒质感、丝网印刷、90 年代杂志印刷氛围\n"
    "- 可点缀赛博朋克、镭射、荧光、光栅线条\n"
    "【视觉冲击】\n"
    "- 色彩与形状对比；青春活力、现代感、创意张力\n"
)

POSTER_REFERENCE_HINT = (
    "【参考图说明】\n"
    "- 以提供的照片作为人物主体；尽量延续原人物的脸型、发型、肤色与气质\n"
    "- 拼贴海报里可出现多个角度/姿态的“同一个人”\n"
    "- 不要替换成完全无关的人；如做不到严格保脸，请保留可识别的相似度\n"
)

POSTER_HARD_NEGATIVES = (
    "硬禁忌：\n"
    "- 不要乱码文字（只允许装饰性英文短词）\n"
    "- 不要扭曲变形的手指与五官\n"
    "- 不要灰暗低饱和色调（要明亮粉色甜系）\n"
    "- 不要写实严肃风（要拼贴海报、杂志感、时尚海报）\n"
    "- 不要做成婚纱照、证件照、毕业照风格\n"
    "- 不要加水印、不要 emoji 表情包元素\n"
)


def build_poster_prompt(
    raw_arg: str | None = None,
    *,
    beibei: bool = False,
    user_caption: str | None = None,
) -> str:
    """生成 /poster /starposter 用的最终 prompt。

    raw_arg / user_caption 仅作为补充“风格倾向”附在末尾，例如：
    - /poster 甜酷复古 → 在主指令基础上偏甜酷复古
    - /poster 赛博朋克霓虹 → 在主指令基础上偏赛博朋克霓虹
    """
    extras: list[str] = []
    if raw_arg and raw_arg.strip():
        extras.append(f"用户附加风格描述：{raw_arg.strip()}")
    if user_caption and user_caption.strip():
        extras.append(f"用户附加描述：{user_caption.strip()}")
    tone_extra = (
        "\n整体气质再温柔一点：粉色更轻、装饰更稀疏、文字更小，避免视觉过度拥挤。"
        if beibei
        else ""
    )
    extras_block = ("\n" + "\n".join(extras)) if extras else ""
    return (
        f"{POSTER_FORMAT}。\n\n"
        f"{POSTER_CORE_DIRECTIVE}\n"
        f"{POSTER_REFERENCE_HINT}\n"
        f"{POSTER_HARD_NEGATIVES}"
        f"{tone_extra}"
        f"{extras_block}"
    )


async def generate_poster_image(
    *,
    raw_arg: str | None,
    reference_path: str | None,
    beibei: bool = False,
    user_caption: str | None = None,
) -> dict[str, Any]:
    """生成 /poster /starposter 图片。

    返回结构与 image_generation_service.generate_image 一致；额外带 fallback_to_text2image。
    - 任何异常都吞掉、转化为 ok=False，避免炸主流程
    - 路由层根据 fallback_to_text2image 决定是否给“无法严格保留原图”的诚实提示
    """
    prompt = build_poster_prompt(raw_arg, beibei=beibei, user_caption=user_caption)

    from services.image_generation_service import generate_image_from_reference

    try:
        result = await generate_image_from_reference(
            prompt=prompt,
            reference_path=reference_path,
            size="1024x1536",  # 接近 3:4
        )
    except Exception as e:
        logger.exception("poster generate_image_from_reference crashed | err=%s", e)
        return {
            "ok": False,
            "url": None,
            "data": None,
            "error": "图片生成开小差了，等下再发一次试试～",
            "fallback_to_text2image": False,
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
            "fallback_to_text2image": False,
        }

    return {
        "ok": True,
        "url": result.get("url"),
        "data": result.get("data"),
        "error": None,
        "fallback_to_text2image": bool(result.get("fallback_to_text2image", False)),
    }
