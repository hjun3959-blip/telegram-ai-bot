#!/usr/bin/env bash
# deploy_to_project_phase1_1_test.sh
# 部署 project_phase1_1 候选版到 /opt/project_phase1_1_test。
#
# 该脚本：
#   - 备份 /opt/project_phase1_1_test 的旧关键文件（app.py / config.py / routers / services / db / utils 等）
#   - 同步当前代码目录到 /opt/project_phase1_1_test（保留服务器 .env / logs / bot_data.sqlite3）
#   - 运行 python3 -m compileall 做语法自检
#   - 提醒检查 TELEGRAM_TOKEN / OPENAI_API_KEY / OPENAI_BASE_URL / OWNER_USERNAMES / OWNER_CHAT_IDS
#   - 默认不重启服务，只打印重启命令
#
# 用法（在服务器上）：
#   bash deploy_to_project_phase1_1_test.sh                 # 默认源是脚本所在仓库父目录
#   SRC_DIR=/root/project_phase1_1 bash deploy_to_project_phase1_1_test.sh
#
# 注意：此脚本不会自动 systemctl restart，请人工确认无误后再重启。

set -euo pipefail

DEST_DIR="${DEST_DIR:-/opt/project_phase1_1_test}"
# 默认源目录 = 脚本所在目录的上一级（即项目根）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="${SRC_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
TS="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="${DEST_DIR}_backup_${TS}"

echo "==> 部署 project_phase1_1 候选版"
echo "    源目录 SRC_DIR  = ${SRC_DIR}"
echo "    目标   DEST_DIR = ${DEST_DIR}"
echo "    备份   BACKUP   = ${BACKUP_DIR}"
echo

if [ ! -d "$SRC_DIR" ]; then
  echo "[ERROR] 源目录不存在：$SRC_DIR" >&2
  exit 1
fi
if [ ! -f "$SRC_DIR/app.py" ]; then
  echo "[ERROR] 源目录看起来不是项目根（缺 app.py）：$SRC_DIR" >&2
  exit 1
fi

# 1. 备份旧关键文件
if [ -d "$DEST_DIR" ]; then
  echo "==> 备份旧版关键文件到 $BACKUP_DIR"
  mkdir -p "$BACKUP_DIR"
  for item in app.py config.py requirements.txt .env.example routers services db utils scripts; do
    if [ -e "$DEST_DIR/$item" ]; then
      cp -a "$DEST_DIR/$item" "$BACKUP_DIR/" || true
      echo "    backed up: $item"
    fi
  done
else
  echo "==> 目标目录不存在，将创建：$DEST_DIR"
  mkdir -p "$DEST_DIR"
fi

# 2. 同步代码（不覆盖服务器 .env / logs / 数据库 / __pycache__）
echo
echo "==> 同步代码到 $DEST_DIR （保留服务器 .env / logs / 数据库）"
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete \
    --exclude='.env' \
    --exclude='.env.local' \
    --exclude='logs/' \
    --exclude='*.log' \
    --exclude='*.sqlite3' \
    --exclude='*.sqlite3-journal' \
    --exclude='*.sqlite3-wal' \
    --exclude='*.sqlite3-shm' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='.git/' \
    "$SRC_DIR"/ "$DEST_DIR"/
else
  echo "[WARN ] 未安装 rsync，回退到 cp -a（不会删除多余文件）"
  # 兜底：cp -a，保留服务器 .env / logs / sqlite
  ( cd "$SRC_DIR" && tar --exclude='./.env' \
      --exclude='./.env.local' \
      --exclude='./logs' \
      --exclude='*.sqlite3' \
      --exclude='*.sqlite3-journal' \
      --exclude='*.sqlite3-wal' \
      --exclude='*.sqlite3-shm' \
      --exclude='__pycache__' \
      --exclude='*.pyc' \
      --exclude='./.git' \
      -cf - . ) | ( cd "$DEST_DIR" && tar -xf - )
fi

# 3. 确保 logs 目录存在
mkdir -p "$DEST_DIR/logs"

# 4. 编译自检
echo
echo "==> python3 -m compileall 校验语法"
python3 -m compileall -q "$DEST_DIR" || {
  echo "[ERROR] compileall 失败，请回滚到 $BACKUP_DIR" >&2
  exit 2
}
echo "    compileall OK"

# 5. .env 提示
echo
if [ ! -f "$DEST_DIR/.env" ]; then
  echo "[WARN ] $DEST_DIR/.env 不存在。请先 cp .env.example .env 并填写以下变量："
else
  echo "==> 检测到 $DEST_DIR/.env，未覆盖，请确认以下变量是否齐全："
fi
cat <<EOF
    - TELEGRAM_TOKEN     （Telegram Bot Token，必填）
    - OPENAI_API_KEY     （yungpt / OpenAI 兼容 Key，必填）
    - OPENAI_BASE_URL    （例如 https://yungpt.com/v1）
    - OWNER_USERNAMES    （例：jinlid,pay9l）
    - OWNER_CHAT_IDS     （例：7256055877，告警必须）
EOF

# 6. 不重启，只打印命令
echo
echo "==> 部署完成。默认不重启服务，请人工确认无误后执行以下任一命令："
cat <<'EOF'
    # 候选 systemd 名称（按你服务器上实际名称选一个）
    sudo systemctl daemon-reload
    sudo systemctl restart project_phase1_1     # 候选 1
    sudo systemctl restart project-phase1-1     # 候选 2
    sudo systemctl restart tg-ai-bot            # 候选 3

    # 查看状态与日志
    sudo systemctl status project_phase1_1 --no-pager
    sudo journalctl -u project_phase1_1 -n 200 --no-pager
EOF

echo
echo "==> DONE. 备份目录：$BACKUP_DIR"
