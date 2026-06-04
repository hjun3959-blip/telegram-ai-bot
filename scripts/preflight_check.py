"""运行前自检脚本。

用法：
    python3 scripts/preflight_check.py

检查项：
1. 关键文件是否存在
2. 关键依赖是否可 import
3. ffmpeg 是否在 PATH
4. 环境变量是否齐备（仅检查存在性与基本合法性，不打印密钥本身）
5. config 中模型分工是否符合阿君的设定
6. 配置中常见风险点（OWNER_CHAT_IDS 为空、贴纸全为空等）

退出码：
    0 表示通过；
    1 表示有 ERROR；
    任何 WARN 都会打印但不会让退出码非零。

注意：永远不会打印 TELEGRAM_TOKEN / OPENAI_API_KEY 等敏感值，
只会打印它们的“是否存在”和长度。
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 让 import config 等模块能直接 work
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------- 输出工具 ----------------

class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.infos: list[str] = []

    def err(self, msg: str) -> None:
        self.errors.append(msg)
        print(f"[ERROR] {msg}")

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        print(f"[WARN ] {msg}")

    def ok(self, msg: str) -> None:
        self.infos.append(msg)
        print(f"[OK   ] {msg}")

    def info(self, msg: str) -> None:
        print(f"[INFO ] {msg}")


def mask(value: str) -> str:
    """对敏感值打码：仅返回长度，不返回内容。"""
    if not value:
        return "<empty>"
    return f"<set, len={len(value)}>"


# ---------------- 各项检查 ----------------

REQUIRED_FILES = [
    "app.py",
    "config.py",
    "routers/__init__.py",
    "routers/private.py",
    "routers/business.py",
    "routers/media.py",
    "services/__init__.py",
    "services/openai_service.py",
    "services/context_service.py",
    "services/message_service.py",
    "services/reply_service.py",
    "services/media_service.py",
    "services/alert_service.py",
    "services/plan_service.py",
    "services/xiaopang_service.py",
    "services/filter_service.py",
    "db/core.py",
    "utils/logger.py",
    ".env.example",
    "requirements.txt",
]

REQUIRED_PACKAGES = [
    ("aiogram", "aiogram"),
    ("openai", "openai"),
    ("aiosqlite", "aiosqlite"),
    ("aiofiles", "aiofiles"),
    ("PIL", "Pillow"),
    ("dotenv", "python-dotenv"),
]


def check_files(report: Report) -> None:
    report.info("== 检查关键文件 ==")
    for rel in REQUIRED_FILES:
        path = PROJECT_ROOT / rel
        if not path.exists():
            report.err(f"缺少关键文件：{rel}")
        else:
            report.ok(f"文件存在：{rel}")


def check_packages(report: Report) -> None:
    report.info("== 检查 Python 依赖 ==")
    for mod, pkg_name in REQUIRED_PACKAGES:
        try:
            importlib.import_module(mod)
            report.ok(f"依赖可用：{pkg_name} ({mod})")
        except Exception as e:
            report.err(f"依赖缺失或导入失败：{pkg_name} ({mod}) | err={e}")


def check_ffmpeg(report: Report) -> None:
    report.info("== 检查 ffmpeg ==")
    path = shutil.which("ffmpeg")
    if path:
        report.ok(f"ffmpeg 可用：{path}")
    else:
        report.warn("ffmpeg 不在 PATH，语音/视频处理会失败。请安装：apt-get install ffmpeg")


def check_env_and_config(report: Report) -> None:
    report.info("== 检查环境变量与 config ==")
    # 直接 reimport config，使用其解析逻辑
    try:
        config = importlib.import_module("config")
    except Exception as e:
        report.err(f"无法导入 config.py：{e}")
        return

    telegram_token = getattr(config, "TELEGRAM_TOKEN", "")
    openai_api_key = getattr(config, "OPENAI_API_KEY", "")
    openai_base_url = getattr(config, "OPENAI_BASE_URL", None)
    db_path = getattr(config, "DB_PATH", "")
    owner_usernames = getattr(config, "OWNER_USERNAMES", set())
    owner_chat_ids = getattr(config, "OWNER_CHAT_IDS", [])
    ad_keywords = getattr(config, "AD_KEYWORDS", [])
    self_ignore = getattr(config, "SELF_MESSAGE_IGNORE_SECONDS", 0)
    sticker_map = getattr(config, "STICKER_MAP", {})

    core_model = getattr(config, "CORE_MODEL", "")
    light_model = getattr(config, "LIGHT_MODEL", "")
    vision_model = getattr(config, "VISION_MODEL", "")
    backup_model = getattr(config, "BACKUP_MODEL", "")
    transcribe_model = getattr(config, "TRANSCRIBE_MODEL", "")

    # 密钥仅打码后展示
    report.info(f"TELEGRAM_TOKEN: {mask(telegram_token)}")
    report.info(f"OPENAI_API_KEY: {mask(openai_api_key)}")
    report.info(f"OPENAI_BASE_URL: {openai_base_url or '<default>'}")
    report.info(f"BOT_DB_PATH: {db_path}")

    if not telegram_token:
        report.err("TELEGRAM_TOKEN 为空，机器人无法启动。")
    if not openai_api_key:
        report.err("OPENAI_API_KEY 为空，模型调用将全部失败。")

    if not owner_usernames:
        report.warn("OWNER_USERNAMES 为空，is_owner / 自发消息识别会失效。")
    else:
        report.ok(f"OWNER_USERNAMES 数量={len(owner_usernames)}")

    if not owner_chat_ids:
        report.warn("OWNER_CHAT_IDS 为空，告警/风险提醒无法推送给 owner。")
    else:
        report.ok(f"OWNER_CHAT_IDS 数量={len(owner_chat_ids)}")

    if not ad_keywords:
        report.warn("AD_KEYWORDS 为空，business 广告过滤会失效。")
    else:
        report.ok(f"AD_KEYWORDS 数量={len(ad_keywords)}")

    if not isinstance(self_ignore, int) or self_ignore < 0:
        report.err(f"SELF_MESSAGE_IGNORE_SECONDS 非法：{self_ignore}")
    else:
        report.ok(f"SELF_MESSAGE_IGNORE_SECONDS={self_ignore}s")
        if self_ignore == 0:
            report.warn("SELF_MESSAGE_IGNORE_SECONDS=0：owner 自己发完消息后没有静默窗口，可能被机器人回应。")

    # 模型分工核对
    expected = {
        "CORE_MODEL": ("gpt-5.5", core_model),
        "LIGHT_MODEL": ("gpt-5.4-mini", light_model),
        "VISION_MODEL": ("gemini-3.1-flash-lite", vision_model),
        "BACKUP_MODEL": ("deepseek-v4-flash", backup_model),
        "TRANSCRIBE_MODEL": ("whisper-1", transcribe_model),
    }
    for name, (want, got) in expected.items():
        if not got:
            report.err(f"{name} 未配置")
        elif got != want:
            report.warn(f"{name}={got}，与阿君约定 {want} 不一致")
        else:
            report.ok(f"{name}={got}")

    # 贴纸配置
    non_empty = [k for k, v in sticker_map.items() if v]
    if not non_empty:
        report.warn("STICKER_MAP 全部为空：模型即使返回 sticker_type 也不会发出贴纸（reply_service 已做兼容，不会报错）。")
    else:
        report.ok(f"STICKER_MAP 已配置：{','.join(non_empty)}")


def check_xiaopang_constants(report: Report) -> None:
    report.info("== 检查小胖逻辑常量 ==")
    try:
        xp = importlib.import_module("services.xiaopang_service")
    except Exception as e:
        report.err(f"无法导入 services.xiaopang_service：{e}")
        return
    expected = {"yj_syj", "i_q772", "zp7987"}
    canonical = set(getattr(xp, "XIAOPANG_CANONICAL_USERNAMES", set()))
    if expected.issubset(canonical):
        report.ok(f"小胖三账号已纳入：{sorted(canonical)}")
    else:
        report.err(f"小胖账号缺失：want={expected}，got={canonical}")


def check_import_routers(report: Report) -> None:
    report.info("== 编译/加载关键模块 ==")
    targets = [
        "config",
        "db.core",
        "services.openai_service",
        "services.reply_service",
        "services.context_service",
        "services.filter_service",
        "services.media_service",
        "services.message_service",
        "services.alert_service",
        "services.plan_service",
        "services.xiaopang_service",
        "routers.private",
        "routers.business",
        "routers.media",
        "app",
    ]
    for mod in targets:
        try:
            importlib.import_module(mod)
            report.ok(f"import 成功：{mod}")
        except Exception as e:
            report.err(f"import 失败：{mod} | err={e}")


def main() -> int:
    report = Report()
    report.info(f"项目根目录：{PROJECT_ROOT}")
    check_files(report)
    check_packages(report)
    check_ffmpeg(report)
    check_env_and_config(report)
    check_xiaopang_constants(report)
    check_import_routers(report)

    print()
    print("=" * 60)
    print(f"汇总：OK={len(report.infos)} | WARN={len(report.warnings)} | ERROR={len(report.errors)}")
    if report.errors:
        print("有 ERROR，必须先修复才能上线。")
        return 1
    if report.warnings:
        print("有 WARN，不阻断启动，但建议在上线前确认。")
    else:
        print("一切就绪。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
