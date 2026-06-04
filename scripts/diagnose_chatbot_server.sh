#!/usr/bin/env bash
# diagnose_chatbot_server.sh
# 服务器侧诊断脚本：只读，不打印密钥，不修改任何文件。
#
# 检查项：
#   1. 候选 systemd service 名称：project_phase1_1 / project-phase1-1 / tg-ai-bot
#   2. /opt/project_phase1_1_test 目录是否存在
#   3. 最近 120 行日志（systemd 优先，回退到 logs/bot.log）
#   4. 关键字是否出现在代码里：
#        - business_message 路由
#        - gpt-5.5 主脑
#        - whisper-1 语音
#        - 小胖三账号 yj_syj / i_q772 / zp7987
#        - OWNER_CHAT_IDS
#   5. .env 是否存在（仅判断存在性，不读取内容）
#
# 用法（服务器上）：
#   bash diagnose_chatbot_server.sh

set -u

DEST_DIR="${DEST_DIR:-/opt/project_phase1_1_test}"
CANDIDATE_SERVICES=(project_phase1_1 project-phase1-1 tg-ai-bot)

echo "==================================================="
echo "  project_phase1_1 服务器诊断"
echo "  目标目录：$DEST_DIR"
echo "  时间：$(date '+%Y-%m-%d %H:%M:%S')"
echo "==================================================="

# ---------------- 1. service 名称 ----------------
echo
echo "==> 1. 候选 systemd service 状态"
FOUND_SERVICE=""
for svc in "${CANDIDATE_SERVICES[@]}"; do
  if command -v systemctl >/dev/null 2>&1; then
    if systemctl list-unit-files 2>/dev/null | grep -q "^${svc}\.service"; then
      state="$(systemctl is-active "$svc" 2>/dev/null || true)"
      enabled="$(systemctl is-enabled "$svc" 2>/dev/null || true)"
      echo "    [FOUND] $svc | active=$state | enabled=$enabled"
      if [ -z "$FOUND_SERVICE" ]; then
        FOUND_SERVICE="$svc"
      fi
    else
      echo "    [---  ] $svc 未注册"
    fi
  else
    echo "    [SKIP ] systemctl 不可用，跳过 $svc"
  fi
done
if [ -z "$FOUND_SERVICE" ]; then
  echo "    [WARN ] 没有找到任何候选 service，可能机器人是以脚本/nohup/tmux 方式运行"
fi

# ---------------- 2. 目录存在性 ----------------
echo
echo "==> 2. 目标目录与关键文件"
if [ -d "$DEST_DIR" ]; then
  echo "    [OK   ] $DEST_DIR 存在"
  for f in app.py config.py requirements.txt routers/business.py routers/private.py routers/media.py services/openai_service.py services/xiaopang_service.py; do
    if [ -f "$DEST_DIR/$f" ]; then
      echo "    [OK   ] $f"
    else
      echo "    [MISS ] $f"
    fi
  done
  if [ -f "$DEST_DIR/.env" ]; then
    echo "    [OK   ] .env 存在（内容不打印）"
  else
    echo "    [WARN ] .env 不存在"
  fi
else
  echo "    [ERROR] $DEST_DIR 不存在"
fi

# ---------------- 3. 最近日志 ----------------
echo
echo "==> 3. 最近 120 行日志"
LOG_PRINTED=0
if [ -n "$FOUND_SERVICE" ] && command -v journalctl >/dev/null 2>&1; then
  echo "    -- journalctl -u $FOUND_SERVICE -n 120 --no-pager --"
  journalctl -u "$FOUND_SERVICE" -n 120 --no-pager 2>/dev/null && LOG_PRINTED=1
fi
if [ "$LOG_PRINTED" = "0" ] && [ -f "$DEST_DIR/logs/bot.log" ]; then
  echo "    -- tail -n 120 $DEST_DIR/logs/bot.log --"
  tail -n 120 "$DEST_DIR/logs/bot.log"
  LOG_PRINTED=1
fi
if [ "$LOG_PRINTED" = "0" ]; then
  echo "    [WARN ] 没有可用日志（既没有 journalctl 也没有 logs/bot.log）"
fi

# ---------------- 4. 关键字检查 ----------------
echo
echo "==> 4. 代码关键字检查"
check_keyword() {
  local label="$1"; shift
  local pattern="$1"; shift
  if [ ! -d "$DEST_DIR" ]; then
    echo "    [SKIP ] $label：$DEST_DIR 不存在"
    return
  fi
  local hits
  hits="$(grep -RE --include='*.py' -l -- "$pattern" "$DEST_DIR" 2>/dev/null | wc -l | tr -d ' ')"
  if [ "${hits:-0}" -gt 0 ]; then
    echo "    [OK   ] $label 命中文件数=$hits"
  else
    echo "    [ERROR] $label 未在代码中找到（pattern=$pattern）"
  fi
}

check_keyword "business_message 路由" "business_message"
check_keyword "主脑 gpt-5.5"          "gpt-5\.5"
check_keyword "语音 whisper-1"        "whisper-1"
check_keyword "小胖账号 yj_syj"        "yj_syj"
check_keyword "小胖账号 i_q772"        "i_q772"
check_keyword "小胖账号 zp7987"        "zp7987"
check_keyword "OWNER_CHAT_IDS"        "OWNER_CHAT_IDS"

echo
echo "==================================================="
echo "  诊断完成。本脚本不打印任何密钥/Token。"
echo "==================================================="
