"""
命理推算服务 — 八字四柱

数据来源：MingLi-Bench 启发（DestinyLinker/MingLi-Bench）
核心逻辑：接收出生信息 → 计算八字命盘 → 注入 AI 解读
"""

from __future__ import annotations

from datetime import date

from lunar_python import Solar

from config import CORE_MODEL
from services.openai_service import call_openai
from utils.logger import setup_logging

logger = setup_logging()

# ── 天干地支基础表 ──────────────────────────────────────────────────────────────
# 五行归属：四柱八个字（四天干 + 四地支）逐字统计。
_WUXING = {
    "甲": "木", "乙": "木", "丙": "火", "丁": "火", "戊": "土",
    "己": "土", "庚": "金", "辛": "金", "壬": "水", "癸": "水",
    "子": "水", "丑": "土", "寅": "木", "卯": "木", "辰": "土",
    "巳": "火", "午": "火", "未": "土", "申": "金", "酉": "金",
    "戌": "土", "亥": "水",
}
# 年柱地支 → 生肖，保证生肖始终与排盘的年支一致（以立春为界，非元旦/春节）。
_ZHI_SHENGXIAO = {
    "子": "鼠", "丑": "牛", "寅": "虎", "卯": "兔", "辰": "龙", "巳": "蛇",
    "午": "马", "未": "羊", "申": "猴", "酉": "鸡", "戌": "狗", "亥": "猪",
}


def compute_bazi(year: int, month: int, day: int, hour: int, minute: int = 0) -> dict:
    """四柱八字推算（公历输入）。

    使用 lunar_python（寿星天文历同源算法）排盘，正确处理：
    - 年柱以「立春」为界换年（非元旦、非农历正月初一）；
    - 月柱以二十四节气的「节」为界换月（非公历月份切换）；
    - 日柱按真实儒略日连续推算；
    - 时柱含子时跨日规则。

    入参为公历（阳历）年月日时；hour 为 0–23 的 24 小时制。
    返回结构向后兼容旧版（pillars / wuxing / day_master / day_master_element），
    另附 shengxiao（生肖）与 lunar_date（农历）便于展示。

    非法公历日期（如 2 月 30 日、平年 2 月 29 日）会抛 ValueError；
    lunar_python 本身不会对这类输入报错，会静默产出错盘，故在此显式校验。
    """
    date(year, month, day)  # 非法日期直接抛 ValueError，避免静默错盘

    solar = Solar.fromYmdHms(year, month, day, hour, minute, 0)
    lunar = solar.getLunar()
    eight_char = lunar.getEightChar()
    # 晚子时（23:00–24:00）日柱按「当天」计：约定明确，且不依赖库默认值跨版本漂移。
    eight_char.setSect(2)

    pillars = {
        "年柱": eight_char.getYear(),
        "月柱": eight_char.getMonth(),
        "日柱": eight_char.getDay(),
        "时柱": eight_char.getTime(),
    }

    wuxing_count: dict[str, int] = {"木": 0, "火": 0, "土": 0, "金": 0, "水": 0}
    for pillar in pillars.values():
        for char in pillar:
            wx = _WUXING.get(char)
            if wx:
                wuxing_count[wx] += 1

    day_gan = eight_char.getDayGan()
    year_zhi = eight_char.getYearZhi()

    return {
        "pillars": pillars,
        "wuxing": wuxing_count,
        "day_master": day_gan,
        "day_master_element": _WUXING.get(day_gan, ""),
        "shengxiao": _ZHI_SHENGXIAO.get(year_zhi, ""),
        "lunar_date": f"农历{lunar.getMonthInChinese()}月{lunar.getDayInChinese()}",
    }


def format_bazi_card(year: int, month: int, day: int, hour: int, gender: str) -> str:
    """八字命盘文字卡片（无 AI，纯计算）"""
    bazi = compute_bazi(year, month, day, hour)
    pillars = bazi["pillars"]
    wuxing = bazi["wuxing"]
    wx_emojis = {"木": "🌿", "火": "🔥", "土": "🏔️", "金": "⚔️", "水": "💧"}

    shengxiao = bazi.get("shengxiao", "")
    lunar_date = bazi.get("lunar_date", "")
    meta_bits = [f"公历 {year}年{month}月{day}日 {hour}时"]
    if lunar_date:
        meta_bits.append(lunar_date)
    if shengxiao:
        meta_bits.append(f"属{shengxiao}")
    meta_bits.append(f"{gender}")

    lines = [
        "🎴 *八字命盘*",
        "📅 " + " | ".join(meta_bits),
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

    shengxiao = bazi.get("shengxiao", "")
    lunar_date = bazi.get("lunar_date", "")
    birth_line = f"出生信息：公历 {year}年{month}月{day}日 {hour}时"
    if lunar_date:
        birth_line += f"（{lunar_date}）"
    if shengxiao:
        birth_line += f"，生肖属{shengxiao}"
    birth_line += f"，性别：{gender}"

    user_prompt = (
        f"{birth_line}\n\n"
        f"八字命盘（已按节气/立春精确排盘）：\n{pillar_str}\n\n"
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
