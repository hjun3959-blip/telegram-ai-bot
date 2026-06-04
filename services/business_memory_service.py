"""Business 共享会话历史。

把 business window 的 user_histories 抽出来放在独立模块，避免
routers/business.py 与 routers/media.py 互相 import（循环引用）。

设计：
- 模块级 dict，按 user_id 存储最近若干轮 {"role": "user"/"assistant", "content": str}
- get_history(user_id): 返回该用户的最近历史副本
- save_history(user_id, user_content, assistant_reply): 追加一轮并 trim
- save_pair(user_id, role_a, content_a, role_b, content_b): 通用追加双轮（媒体场景可能 user 用 "[图片]" 这种占位）
- trim(user_id): 强制按 HISTORY_MAX_* trim 一次（一般 save_history 已自动做了）
- clear(user_id): 清掉单用户历史（测试用）

注意：
- 媒体场景需要让下一轮文本看到“刚刚发了图/贴纸”，所以媒体进来时也 save_history 一次
- save_history 接受空字符串：当 assistant 静默时，传 "" 即可，trim_history 不会因此抛错
- 媒体场景的 user_content 一律是已脱敏的文字描述（“[图片]说了 xxx”/“[贴纸:emoji=...]”），
  绝不传 file_id，避免向模型泄漏素材 ID
"""

from __future__ import annotations

from services.history_service import trim_history as _trim_history

# user_id -> list[ {role, content} ]
_user_histories: dict[int, list[dict]] = {}


def get_history(user_id: int) -> list[dict]:
    """返回最近历史的浅拷贝，调用方不应直接修改。"""
    return list(_user_histories.get(user_id, []))


def save_history(user_id: int, user_content: str, assistant_reply: str) -> None:
    """追加一轮对话并 trim。

    - user_content 为空时仍会写入一条占位（让模型看到“对方发过东西”）；
      但完全无内容（user 与 assistant 都为空）时直接跳过，避免污染。
    - assistant_reply 为空时也会写一条空字符串，保留“对方发完之后我没回话”的事实。
    """
    user_text = "" if user_content is None else str(user_content)
    asst_text = "" if assistant_reply is None else str(assistant_reply)
    if not user_text and not asst_text:
        return
    history = _user_histories.get(user_id, [])
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": asst_text})
    _user_histories[user_id] = _trim_history(history)


def trim(user_id: int) -> None:
    """对指定用户的历史强制 trim 一次。"""
    history = _user_histories.get(user_id)
    if not history:
        return
    _user_histories[user_id] = _trim_history(history)


def clear(user_id: int | None = None) -> None:
    """清空：传 user_id 清单个；不传清全部。仅供测试/调试使用。"""
    if user_id is None:
        _user_histories.clear()
    else:
        _user_histories.pop(user_id, None)


# 兼容旧引用：routers/business.py 之前直接 import user_histories
# 这里暴露同名变量，但其实是同一个内存 dict
user_histories = _user_histories
