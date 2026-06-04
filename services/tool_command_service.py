"""文字类工具命令服务。

封装老私信窗口里的六个轻量工具：
- /polish 文本：高情商表达润色
- /tldr   长文本：3-5 条要点摘要
- /eli5   概念：通俗解释
- /excel  需求：Excel / Google Sheets 公式
- /eat    状态/偏好：只给一个吃什么建议
- /reply  对方的话：三种回复策略

设计要点：
- 复用 services.openai_service 里的 AsyncOpenAI client，密钥来自 config，不读取 .env
- 不要求模型返回 JSON（call_openai 强制 JSON 不合适，因此本模块直接走 chat.completions）
- 主模型失败时回落到 BACKUP_MODEL；都失败时返回简短中文错误
- 不会把任何密钥写入日志
"""

from __future__ import annotations

from config import BACKUP_MODEL, CORE_MODEL, LIGHT_MODEL
from services.openai_service import client
from utils.logger import setup_logging

logger = setup_logging()


# 每个工具的 system prompt：保持短、自然、像真人
POLISH_SYSTEM = (
    "你是中文表达润色助手。把用户给的文字改得更自然、得体、高情商。\n"
    "规则：\n"
    "- 不要把意思改掉，只改表达\n"
    "- 默认 1-3 句话，简洁\n"
    "- 跟随用户语言（中文/英文）\n"
    "- 不要解释你做了什么，只输出润色后的文本本身"
)

TLDR_SYSTEM = (
    "你是文本摘要助手。请用 3-5 条要点提炼用户给的文本。\n"
    "规则：\n"
    "- 每条以「- 」开头\n"
    "- 抓重点，不复述全文\n"
    "- 保留关键数字、人物、结论\n"
    "- 跟随原文主要语言"
)

ELI5_SYSTEM = (
    "你是“给小白讲解”助手。把用户提到的概念用最通俗的话说清楚。\n"
    "规则：\n"
    "- 用生活化比喻\n"
    "- 默认 3-6 句话\n"
    "- 不要堆术语，必要时用括号简单解释\n"
    "- 跟随用户语言"
)

EXCEL_SYSTEM = (
    "你是表格公式助手。用户描述需求，你给出可直接粘贴的公式。\n"
    "规则：\n"
    "- 优先给 Excel 公式；若 Google Sheets 写法不同，再附一行 Google Sheets 版本\n"
    "- 公式用 ` ` 反引号包裹\n"
    "- 公式之外用 1-2 句简短中文说明它干了什么\n"
    "- 不要长篇大论"
)

EAT_SYSTEM = (
    "你是“今天吃什么”决策助手。用户告诉你状态或偏好，你只给一个具体建议。\n"
    "规则：\n"
    "- 只给一个推荐，不要给一长串清单\n"
    "- 一句话点名菜品/餐厅类型；可以再加一句简短理由\n"
    "- 自然、像朋友，不要客服腔\n"
    "- 跟随用户语言"
)

REPLY_SYSTEM = (
    "你是社交回复策略助手。用户贴出对方说的话，你给三种回复风格。\n"
    "输出格式严格如下（每行一条，简洁）：\n"
    "1. 稳重得体：……\n"
    "2. 轻松幽默：……\n"
    "3. 暧昧拉近：……\n"
    "规则：\n"
    "- 三条各自独立，不要互相重复\n"
    "- 跟随对方语言（中文/英文）\n"
    "- 不要解释为什么，只给回复本身"
)


# 工具名到 (system_prompt, model) 的映射
_TOOL_REGISTRY = {
    "polish": (POLISH_SYSTEM, LIGHT_MODEL),
    "tldr": (TLDR_SYSTEM, LIGHT_MODEL),
    "eli5": (ELI5_SYSTEM, LIGHT_MODEL),
    "excel": (EXCEL_SYSTEM, CORE_MODEL),
    "eat": (EAT_SYSTEM, LIGHT_MODEL),
    "reply": (REPLY_SYSTEM, CORE_MODEL),
}


# 短文本阈值：tldr 短文本时直接提示无需摘要
TLDR_SHORT_THRESHOLD = 80


async def _chat_text(system_prompt: str, user_content: str, model: str) -> str:
    """走 chat.completions 拿纯文本，不强制 JSON。"""
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        temperature=0.7,
        max_tokens=600,
    )
    raw = response.choices[0].message.content or ""
    return raw.strip()


async def run_text_tool(tool: str, user_input: str) -> str:
    """统一入口：根据工具名调用对应 prompt。失败时返回简短错误文案。"""
    user_input = (user_input or "").strip()
    if not user_input:
        return "格式：/{tool} 你的内容".format(tool=tool)

    # tldr 的短文本快捷分支
    if tool == "tldr" and len(user_input) < TLDR_SHORT_THRESHOLD:
        return "这段文字不长，应该不用摘要啦～直接看原文就行。"

    if tool not in _TOOL_REGISTRY:
        return "未知工具命令"

    system_prompt, model = _TOOL_REGISTRY[tool]

    try:
        text = await _chat_text(system_prompt, user_input, model)
        if text:
            return text
    except Exception as e:
        logger.exception("Text tool failed | tool=%s | model=%s | err=%s", tool, model, e)

    # 主模型失败，尝试 BACKUP_MODEL
    if BACKUP_MODEL and BACKUP_MODEL != model:
        try:
            text = await _chat_text(system_prompt, user_input, BACKUP_MODEL)
            if text:
                return text
        except Exception as e:
            logger.exception("Text tool backup failed | tool=%s | backup=%s | err=%s", tool, BACKUP_MODEL, e)

    return "刚才有点卡，稍后再试一次。"


def build_meme_prompt(user_desc: str) -> str:
    """把用户描述包装成 meme 风格的 image prompt。"""
    desc = (user_desc or "").strip()
    if not desc:
        return ""
    return (
        "Internet meme style image. "
        "Bold, exaggerated, humorous, cartoon-like composition. "
        "Optional impact-style caption text if it fits. "
        f"Subject: {desc}"
    )
