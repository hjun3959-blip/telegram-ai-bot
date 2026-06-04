"""Y2K 拼贴海报（/y2k）服务层。

独立功能：使用用户提供的正式 Y2K 美学拼贴 prompt 作为核心，不与 /poster（明星拼贴）混用。

需求摘要：
- Y2K 美学 / 剪贴簿风格 / 拼贴海报
- 韩国少女 / 韩系脸赞 Ulzzang / K-pop 偶像美学 / 撒娇感 Aegyo
- 半扎半放发型 + 泡泡糖粉发尾 + 柔色花朵发夹
- 穿搭：短款超大号卫衣、柔色短裙、白色皮带、彩色条纹白袜、白色运动鞋
- 姿势元素：比心、拍立得、嘟嘴吹泡泡糖、抱猫、坐姿比耶、手持雏菊
- 背景：全息纹理、闪粉、涂鸦、霓虹轮廓、拍立得边框、贴纸艺术、文字 SO CUTE / 199X / GIRL VIBES
- 输出 3:4 比例

设计要点：
- 与 plog / magnet / poster 全部解耦：自己一套 prompt 常量
- 复用 plog_service 的“最近一张照片”缓存
- 复用 image_generation_service.generate_image_from_reference，支持降级
- 诚实说明：text-to-image 降级时不能保证保留原脸/姿势
"""

from __future__ import annotations

from typing import Any

from utils.logger import setup_logging

logger = setup_logging()


# 用户提供的正式 Y2K 拼贴海报 prompt 模板（核心，不要随意改写）。
Y2K_COLLAGE_TEMPLATE = (
    "(杰作, 最佳画质, 高分辨率, 8k:1.2), (照片级真实:1.3), "
    "(Y2K美学, 剪贴簿风格, 拼贴海报:1.4), 无边框排版, 杂志剪纸图形, 彩色的笔触和线条。\n"
    "(1个女孩, 单人焦点:1.1), (多视角合成:1.3), (不同姿势的组合), 主体位于中心。\n"
    "(韩国少女:1.4), (甜美面容:1.2), (K-pop偶像美学), (韩系脸赞风格/Ulzzang), "
    "(撒娇感/Aegyo), 白皙皮肤, 韩式平眉, (渐变咬唇妆), 柔和韩系妆容。\n"
    "(半扎半放的发型:1.2), 深棕色微卷发, (发尾染成泡泡糖粉色), 柔色花朵发夹, "
    "前额细碎刘海, 俏皮自信的表情, 微微嘟嘴, 直视镜头。\n"
    "[穿搭]: (带有刺绣补丁的短款超大号卫衣), 柔色短裙, 白色皮带, "
    "带有彩色条纹的白色短袜, 白色运动鞋, (厚重的彩色戒指, 塑料手镯, 闪亮腰链)。\n"
    "[姿势元素]: (手指比心的特写), (蹲姿手持白色拍立得相机), "
    "(手摸脸颊吹着粉色泡泡糖), (优雅微笑并抱着一只猫), (坐姿眨眼比耶手势), (手持雏菊)。\n"
    "[背景与细节]: 全息纹理, 柔色渐变, 闪粉点缀, 趣味涂鸦, 霓虹轮廓线, "
    "拍立得边框, 贴纸艺术, 文字\"SO CUTE!\", 文字\"199X!\", 文字\"GIRL VIBES\", "
    "(电影级布光, 柔和闪光灯, 光滑光泽肌肤:1.2), 梦幻复古光晕, 混乱但平衡的构图。 --ar 3:4"
)


Y2K_REFERENCE_HINT = (
    "【参考图说明】\n"
    "- 以提供的照片作为人物/风格参考；尽量延续原人物的脸型、发型、肤色与气质\n"
    "- 不要替换成另一个完全无关的人；如做不到严格保脸，请保留可识别的相似度\n"
)

Y2K_HARD_NEGATIVES = (
    "硬禁忌：\n"
    "- 不要乱码文字（除模板里指定的 SO CUTE / 199X / GIRL VIBES）\n"
    "- 不要扭曲变形的手指与五官\n"
    "- 不要灰暗低饱和色调（Y2K 要明亮、糖果色）\n"
    "- 不要写实严肃风（要拼贴海报、剪贴簿、杂志感）\n"
)


def build_y2k_prompt(
    raw_arg: str | None = None,
    *,
    beibei: bool = False,
    user_caption: str | None = None,
) -> str:
    """生成 /y2k 用的最终 prompt。raw_arg / user_caption 仅作为补充描述附在末尾。"""
    extras: list[str] = []
    if raw_arg and raw_arg.strip():
        extras.append(f"用户附加风格描述：{raw_arg.strip()}")
    if user_caption and user_caption.strip():
        extras.append(f"用户附加描述：{user_caption.strip()}")
    tone_extra = (
        "\n整体气质再温柔一点：色调更柔、闪粉更轻、文字更小，避免视觉拥挤。"
        if beibei
        else ""
    )
    extras_block = ("\n" + "\n".join(extras)) if extras else ""
    return (
        f"{Y2K_COLLAGE_TEMPLATE}\n\n"
        f"{Y2K_REFERENCE_HINT}\n"
        f"{Y2K_HARD_NEGATIVES}"
        f"{tone_extra}"
        f"{extras_block}"
    )


async def generate_y2k_image(
    *,
    raw_arg: str | None,
    reference_path: str | None,
    beibei: bool = False,
    user_caption: str | None = None,
) -> dict[str, Any]:
    """生成 /y2k 图片。返回结构与 image_generation_service.generate_image 一致；额外带 fallback 标记。"""
    prompt = build_y2k_prompt(raw_arg, beibei=beibei, user_caption=user_caption)

    from services.image_generation_service import generate_image_from_reference

    try:
        result = await generate_image_from_reference(
            prompt=prompt,
            reference_path=reference_path,
            size="1024x1536",  # 接近 3:4
        )
    except Exception as e:
        logger.exception("y2k generate_image_from_reference crashed | err=%s", e)
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
