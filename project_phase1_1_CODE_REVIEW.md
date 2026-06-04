# project_phase1_1 代码评审记录

本文件记录最近一次「恢复私信工具菜单 + 隐藏小胖功能」改动的关键点，方便回顾。

## 改动背景

旧机器人私信窗口里有一套老的小工具菜单（/play + /img /meme /polish /tldr /eli5 /excel /eat /reply），迁移到新项目后被遗漏。同时小胖相关命令（/小胖摘要、/小胖提醒、/小胖设置、/小胖聊天记录、/小胖档案、/学习小胖聊天方式）需要继续保留给 owner 使用，但**不能在任何对外文案（/play、/help、按钮、callback 提示）里出现**，避免暴露管理入口。

## 改动文件

| 文件 | 说明 |
| --- | --- |
| `config.py` | 新增 `IMAGE_MODEL`（默认 `gpt-image-2`，可由 env 覆盖） |
| `.env.example` | 同步新增 `IMAGE_MODEL` 注释与默认值 |
| `services/image_generation_service.py` | **新增**：封装 `images/generations` 调用，兼容 url / b64_json 两种返回 |
| `services/tool_command_service.py` | **新增**：封装 6 个文字工具（polish/tldr/eli5/excel/eat/reply）的 system prompt 与调用；不要求 JSON 输出 |
| `routers/private.py` | 重写处理顺序：`/play` 菜单、`callback_query`、工具命令分发、owner 计划/小胖隐藏命令、小胖隐私分支、普通聊天 |
| `project_phase1_1_CODE_REVIEW.md` | 本文件 |

## 实现要点

### 工具菜单

- `/play` 走 `Command("play")` filter，发送菜单文案 + InlineKeyboardMarkup
- 9 个按钮：AI 图片创作师、Meme 表情包、文本润色、长文摘要、概念解释、表格公式、吃什么、回复建议、使用说明
- callback_data 命名空间统一前缀 `play:`；按钮点击只回提示文案，不直接执行模型，避免误触发
- callback handler 只挂在 `private` router（`router.callback_query(F.data.startswith("play:"))`），不会影响 business / media 路由

### 文字工具

- 不复用 `call_openai`（它强制 `response_format=json_object`，与“返回纯文本”需求不符）
- 直接调 `services.openai_service.client.chat.completions.create`
- 每个工具独立 system prompt，模型由工具分配（轻工具用 `LIGHT_MODEL`，回复策略与公式用 `CORE_MODEL`）
- 主模型失败时回落 `BACKUP_MODEL`，再失败给统一短中文错误
- `/tldr` 短文本（< 80 字符）直接提示无需摘要，不消耗模型

### 图片工具

- `/img` 直接把用户描述当 prompt
- `/meme` 在 prompt 外层包一段 meme 风格指令（`tool_command_service.build_meme_prompt`）
- 调用前发 `UPLOAD_PHOTO` chat action
- 返回 `url` 时用 `URLInputFile` 发送；返回 `b64_json` 时 base64 解码后用 `BufferedInputFile` 发送
- 任何失败给一句简短中文，不抛异常

### 小胖隐藏

- `XIAOPANG_OWNER_COMMANDS` 在 `services/xiaopang_service.py` 里保持原值
- owner 在私信里输入这些命令仍然走 `owner_xiaopang_command_reply`，与之前一致
- 工具命令分发**只对非小胖用户生效**（`is_xiaopang(message)` 为 False 才匹配 `/img /meme /polish ...`），避免小胖意外触发管理面板或工具
- 小胖本人输入隐藏 owner 命令时，仍由 `xiaopang_block_owner_command_for_private` 拒绝，不暴露管理入口
- `PLAY_MENU_TEXT` / `HELP_TEXT` / `HOW_TO_USE_TEXT` 中完全没有任何小胖相关字样

## 安全性

- 不读取 `.env` 文件原文，所有配置通过 `config.py` 经由 `python-dotenv` 注入
- 不向日志写入 `TELEGRAM_TOKEN` / `OPENAI_API_KEY`
- `image_generation_service.generate_image` 与 `tool_command_service.run_text_tool` 仅在 logger 记录 model 名与 err 摘要

## Business 兼容

- `routers/private.py` 第一行就 `get_chat_mode(message) != "private"` 短路，business / 群聊不会进入工具分发
- business 路由代码未改动，仍是原始的 `routers/business.py`

## 验证

```
python3 -m compileall -q /home/user/workspace/project_phase1_1
python3 /home/user/workspace/project_phase1_1/scripts/preflight_check.py
```

本地无密钥导致的 preflight ERROR 可接受。
