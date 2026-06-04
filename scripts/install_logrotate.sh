#!/usr/bin/env bash
# install_logrotate.sh
#
# 为 /opt/project_phase1_1_test/logs/*.log 安装 logrotate 规则。
# 仅生成 / 安装 logrotate 配置，不重启 bot，不动 systemd。
#
# 默认配置：
#   - 路径：/opt/project_phase1_1_test/logs/*.log
#   - 频率：daily
#   - 保留：rotate 7
#   - 压缩：compress
#   - 容忍空文件：missingok
#   - 切割方式：copytruncate（不需要 bot 重新打开日志句柄）
#
# 配置文件落地路径：/etc/logrotate.d/tg-ai-bot-project-phase1
#
# 用法：
#   sudo bash scripts/install_logrotate.sh
#
# 退出码：
#   0 - 安装成功
#   1 - 权限不足或 logrotate 未安装

set -euo pipefail

LOG_DIR="${LOG_DIR:-/opt/project_phase1_1_test/logs}"
CONF_PATH="${CONF_PATH:-/etc/logrotate.d/tg-ai-bot-project-phase1}"

echo "[install_logrotate] 目标日志路径：${LOG_DIR}/*.log"
echo "[install_logrotate] 目标配置文件：${CONF_PATH}"

# 检查 root 权限
if [ "$(id -u)" -ne 0 ]; then
    echo "[ERROR] 需要 root 权限才能写入 /etc/logrotate.d/。请用 sudo 运行。" >&2
    exit 1
fi

# 检查 logrotate 是否可用
if ! command -v logrotate >/dev/null 2>&1; then
    echo "[WARN] 未检测到 logrotate 命令。仍然写入配置文件，但请安装 logrotate："
    echo "       apt-get install -y logrotate    # Debian/Ubuntu"
    echo "       yum install -y logrotate        # CentOS/RHEL"
fi

# 确保日志目录存在（不存在则提示，但仍然写配置）
if [ ! -d "${LOG_DIR}" ]; then
    echo "[WARN] 日志目录 ${LOG_DIR} 不存在。已写入配置，logrotate 会因 missingok 静默跳过，直到目录出现。"
fi

# 写入配置文件（采用临时文件 + mv 原子替换，避免半写状态）
TMP_PATH="$(mktemp)"
cat > "${TMP_PATH}" <<EOF
${LOG_DIR}/*.log {
    daily
    rotate 7
    compress
    missingok
    copytruncate
    notifempty
    dateext
}
EOF

chmod 0644 "${TMP_PATH}"
mv -f "${TMP_PATH}" "${CONF_PATH}"

echo "[OK] 已写入 ${CONF_PATH}"
echo "------ 内容 ------"
cat "${CONF_PATH}"
echo "------------------"

# 可选：跑一次 dry-run 校验
if command -v logrotate >/dev/null 2>&1; then
    echo "[install_logrotate] 跑一次 logrotate -d (dry-run) 校验配置……"
    if logrotate -d "${CONF_PATH}" >/tmp/logrotate_dryrun.log 2>&1; then
        echo "[OK] dry-run 通过。详情：/tmp/logrotate_dryrun.log"
    else
        echo "[WARN] dry-run 报告了问题，请人工检查 /tmp/logrotate_dryrun.log"
    fi
fi

echo "[install_logrotate] 完成。本脚本不会重启 bot；如需立即触发一次切割，可运行："
echo "    sudo logrotate -f ${CONF_PATH}"
