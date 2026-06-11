"""
命理推算服务 — 八字四柱

数据来源：MingLi-Bench 启发（DestinyLinker/MingLi-Bench）
核心逻辑：接收出生信息 → 计算八字命盘 → 注入 AI 解读
"""

from __future__ import annotations

from config import CORE_MODEL
from services.openai_service import call_openai
from utils.logger import setup_logging

logger = setup_logging()

# ── 天干地支基础表 ──────────────────────────────────────────────────────────────
_TIANGAN = ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛", "壬", "癸"]
_DIZHI   = ["子", "丑", "寅", "卯", "辰", "巳", "午", "未", "申", "酉", "戌", "亥"]
_SHICHEN = {
    "子": (23, 1), "丑": (1, 3),  "寅": (3, 5),  "卯": (5, 7),
    "辰": (7, 9),  "巳": (9, 11), "午": (11, 13), "未": (13, 15),
    "申": (15, 17), "酉": (17, 19), "戌": (19, 21), "亥": (21, 23),
}
_WUXING = {
    "甲": "木", "乙": "木", "丙": "火", "丁": "火", "戊": "土",
    "己": "土", "庚": "金", "辛": "金", "壬": "水", "癸": "水",
    "子": "水", "丑": "土", "寅": "木", "卯": "木", "辰": "土",
    "巳": "火", "午": "火", "未": "土", "申": "金", "酉": "金",
    "戌": "土", "亥": "水",
}
_MONTH_ZHI = ["丑", "寅", "卯", "辰", "巳", "午", "未", "申", "酉", "戌", "亥", "子"]


def _gan(n: int) -> str:
    return _TIANGAN[n % 10]


def _zhi(n: int) -> str:
    return _DIZHI[n % 12]


def _hour_to_zhi(hour: int) -> str:
    if hour == 23 or hour == 0:
        return "子"
    for zhi, (start, end) in _SHICHEN.items():
        if start <= hour < end:
            return zhi
    return "子"


def compute_bazi(year: int, month: int, day: int, hour: int) -> dict:
    """简化四柱八字推算（天干地支 + 五行统计 + 日主）"""
    year_base = year - 1864
    year_gan = _gan(year_base)
    year_zhi = _zhi(year_base)

    year_gan_idx = _TIANGAN.index(year_gan)
    month_zhi = _MONTH_ZHI[month - 1]
    month_zhi_idx = _DIZHI.index(month_zhi)
    month_gan_start = [2, 4, 6, 8, 0][year_gan_idx % 5]
    offset = (month_zhi_idx - 2) % 12
    month_gan = _gan(month_gan_start + offset)

    a = (14 - month) // 12
    y = year - a
    m = month + 12 * a - 2
    jd = day + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045
    day_gan = _gan(jd)
    day_zhi = _zhi(jd + 2)

    hour_zhi = _hour_to_zhi(hour)
    hour_zhi_idx = _DIZHI.index(hour_zhi)
    day_gan_idx = _TIANGAN.index(day_gan)
    hour_gan_start = [0, 2, 4, 6, 8][day_gan_idx % 5]
    hour_gan = _gan(hour_gan_start + hour_zhi_idx)

    pillars = {
        "年柱": f"{year_gan}{year_zhi}",
        "月柱": f"{month_gan}{month_zhi}",
        "日柱": f"{day_gan}{day_zhi}",
        "时柱": f"{hour_gan}{hour_zhi}",
    }

    wuxing_count: dict[str, int] = {"木": 0, "火": 0, "土": 0, "金": 0, "水": 0}
    for pillar in pillars.values():
        for char in pillar:
            wx = _WUXING.get(char)
            if wx:
                wuxing_count[wx] += 1

    return {
        "pillars": pillars,
        "wuxing": wuxing_count,
        "day_master": day_gan,
        "day_master_element": _WUXING.get(day_gan, ""),
    }


def format_bazi_card(year: int, month: int, day: int, hour: int, gender: str) -> str:
    """八字命盘文字卡片（无 AI，纯计算）"""
    bazi = compute_bazi(year, month, day, hour)
    pillars = bazi["pillars"]
    wuxing = bazi["wuxing"]
    wx_emojis = {"木": "🌿", "火": "🔥", "土": "🏔️", "金": "⚔️", "水": "💧"}

    lines = [
        "🎴 *八字命盘*",
        f"📅 {year}年{month}月{day}日 {hour}时 | 性别：{gender}",
        "",
        f"年柱：`{pillars['年柱']}`   月柱：`{pillars['月柱']}`",
        f"日柱：`{pillars['日柱']}`   时柱：`{pillars['时柱']}`",
        "",
        f"日主：*{bazi['day_master']}*（{bazi['day_master_element']}）",
        "",
        "五行分布：",
    ]
    for wx, count in wuxing.items():
        bar = "█" * count + "░" * max(0, 8 - count)
        lines.append(f"{wx_emojis[wx]} {wx}  {bar}  {count}")

    return "\n".join(lines)


# ── AI 解读 ───────────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """你是一位精通中国传统命理的易学大师，擅长八字四柱推算与解读。
请根据用户提供的出生信息和已计算好的八字命盘，给出专业但通俗易懂的命理解读。

解读须包含以下维度（每项 2-3 句）：
1. 日主分析（强弱、用神方向）
2. 五行平衡（喜用神/忌神）
3. 性格特征
4. 事业财运走向
5. 感情婚姻
6. 健康注意事项
7. 近期运势提示

语气亲切自然，避免过于晦涩，适当加入实际建议。
输出用中文，总长度控制在 600–900 字。"""


async def interpret_bazi(
    year: int,
    month: int,
    day: int,
    hour: int,
    gender: str,
    chat_id: int | str | None = None,
    model: str | None = None,
) -> str:
    """八字 AI 解读，返回纯文本。"""
    bazi = compute_bazi(year, month, day, hour)
    pillars = bazi["pillars"]
    wuxing = bazi["wuxing"]
    wuxing_str = "、".join(f"{k}{v}个" for k, v in wuxing.items() if v > 0)
    pillar_str = " ".join(f"{k}[{v}]" for k, v in pillars.items())

    user_prompt = (
        f"出生信息：{year}年{month}月{day}日 {hour}时，性别：{gender}\n\n"
        f"八字命盘（已计算）：\n{pillar_str}\n\n"
        f"日主：{bazi['day_master']}（{bazi['day_master_element']}）\n"
        f"五行分布：{wuxing_str}\n\n"
        "请按照系统要求的维度给出完整命理解读。"
    )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user",   "content": user_prompt},
    ]

    result = await call_openai(
        messages=messages,
        model=model or CORE_MODEL,
        mode="private",
        response_json=False,
        chat_id=chat_id,
    )

    if isinstance(result, dict):
        return result.get("reply_text", "命理解读生成失败，请重试。")
    return str(result) if result else "命理解读生成失败，请重试。"
