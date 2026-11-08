#!/usr/bin/env bash
# 拓竹打印机监控 · 卸载脚本
set -euo pipefail

APP_NAME="bambu-monitor"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 在安装目录里直接运行时，自动识别安装前缀
if [ -f "$SCRIPT_DIR/.install-meta" ]; then
    DEFAULT_PREFIX="$SCRIPT_DIR"
else
    DEFAULT_PREFIX="/opt/bambu-monitor"
fi

PREFIX="$DEFAULT_PREFIX"
SERVICE_NAME="bambu-monitor"
CLI_PREFIX=""
CLI_SERVICE=""
PURGE=0

info() { printf '\033[32m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[31m错误:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
用法: sudo ./uninstall.sh [选项]

选项:
  --prefix DIR        安装目录（默认读安装记录，否则 /opt/bambu-monitor）
  --service-name NAME systemd 服务名（默认 bambu-monitor）
  --purge             同时删除程序目录和自动创建的用户
  -h, --help          显示帮助
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix)       CLI_PREFIX="$2"; shift 2 ;;
        --service-name) CLI_SERVICE="$2"; shift 2 ;;
        --purge)        PURGE=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              die "未知参数: $1" ;;
    esac
done

[ "$(id -u)" = "0" ] || die "请用 root 运行：sudo ./uninstall.sh"

# 读取安装记录（若存在）
META="$PREFIX/.install-meta"
if [ -f "$META" ]; then
    # shellcheck disable=SC1090
    . "$META"
fi

# 命令行参数优先级高于安装记录
[ -n "$CLI_PREFIX" ] && PREFIX="$CLI_PREFIX"
[ -n "$CLI_SERVICE" ] && SERVICE_NAME="$CLI_SERVICE"

info "停止并禁用服务 $SERVICE_NAME"
systemctl stop "$SERVICE_NAME" 2>/dev/null || true
systemctl disable "$SERVICE_NAME" 2>/dev/null || true
rm -f "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload

if [ "$PURGE" = "1" ]; then
    if [ -d "$PREFIX" ]; then
        info "删除目录 $PREFIX"
        rm -rf "$PREFIX"
    fi
    if [ "${USER_CREATED:-0}" = "1" ] && [ -n "${SERVICE_USER:-}" ] && [ "$SERVICE_USER" != "root" ]; then
        if id "$SERVICE_USER" >/dev/null 2>&1; then
            info "删除用户 $SERVICE_USER"
            userdel "$SERVICE_USER" 2>/dev/null || true
        fi
    fi
else
    info "已保留目录 $PREFIX（如需彻底删除，加 --purge）"
fi

info "卸载完成"
