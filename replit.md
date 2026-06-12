# Telegram AI Bot（“小林子”）

## 项目概述
这是一个基于 Telegram 的个人 AI 助理机器人，使用 Python + aiogram 构建。核心包含两种运行模式：
- **私聊功能区（Private）**：机主专用的控制台与工具集（出图、改图、图生视频、文案优化、八字命理、互动剧情等）。
- **商务代聊（Business）**：作为机主的 AI 分身，在 Telegram Business 会话里替机主自然接话，并带风控/风险雷达。

### 技术栈
- 语言：Python 3.11+
- Telegram 框架：aiogram 3.x（长轮询 polling）
- AI/LLM：OpenAI 兼容接口（通过 `OPENAI_BASE_URL` 配置中转），多模型分工（核心/轻量/视觉/转写/图像/视频）
- 数据库：SQLite（aiosqlite 异步）
- 命理排盘：lunar_python（节气/立春精确排盘）
- 配置：python-dotenv + 环境变量（见 `.env.example`）

### 运行方式
- 入口：`python app.py`（工作流名称：`Telegram Bot`，console 输出，无前端）
- 必需密钥：`TELEGRAM_TOKEN`、`OPENAI_API_KEY`（可选 `OPENAI_BASE_URL`）
- 注意：同一个 `TELEGRAM_TOKEN` 同时只能有一个实例拉取消息（Telegram 限制）。若别处已有实例在跑，本环境轮询会报 `Conflict`，需停掉其它实例或换测试 token。

### 项目结构
- `app.py`：启动入口（初始化 DB、注册路由、启动调度器、开始轮询）
- `config.py`：全局配置与系统提示词
- `routers/`：aiogram 路由（private / business / mingli / media / rstory / owner_menu / admin_agent）
- `services/`：业务逻辑层（openai / media / mingli / 各类 service）
- `db/`：数据库 schema 与初始化
- `scripts/`：诊断与离线 smoke 测试脚本
- `vendor/`：第三方/集成子项目（mingli_bench）

## 用户偏好（User preferences）
- **界面语言：一律简体中文。** 之后生成或修改任何用户可见的界面文字（按钮、菜单、标题、标签、提示信息、占位符、错误/成功提示、确认弹窗、Toast 等）必须使用自然、流畅、专业的简体中文，禁止出现英文。
  - 不要机翻腔，要符合中文母语者表达习惯。
  - 技术术语用标准中文翻译（Dashboard→仪表盘，Settings→设置，Login→登录，Submit→提交 等）。
  - 用户未特别说明语言时，一律默认简体中文。
- **界面风格：** 保持现代、简洁、美观，并支持深色模式。
- **沟通语言：** 与用户对话使用简体中文。
