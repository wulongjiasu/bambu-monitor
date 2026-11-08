#!/usr/bin/env bash
# 拓竹打印机监控 · 一键安装脚本
#
# 用法:
#   sudo ./install.sh                      # 默认装到 /opt/bambu-monitor，端口 8088
#   sudo ./install.sh --port 9000          # 自定义端口
#   sudo ./install.sh --prefix /srv/bambu  # 自定义安装目录
#   sudo ./install.sh --no-start           # 只安装不启动
#
set -euo pipefail

APP_NAME="bambu-monitor"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="dev"
[ -f "$SRC_DIR/VERSION" ] && VERSION="$(tr -d '[:space:]' < "$SRC_DIR/VERSION")"

PREFIX="/opt/bambu-monitor"
SERVICE_NAME="bambu-monitor"
SERVICE_USER="bambu"
PORT="8088"
DO_START=1

info() { printf '\033[32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m警告:\033[0m %s\n' "$*"; }
die()  { printf '\033[31m错误:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
拓竹打印机监控 v$VERSION 安装脚本

用法: sudo ./install.sh [选项]

选项:
  --prefix DIR        安装目录（默认 /opt/bambu-monitor）
  --service-name NAME systemd 服务名（默认 bambu-monitor）
  --user USER         运行服务的用户（默认 bambu，不存在会自动创建）
  --port PORT         网页端口（默认 8088）
  --no-start          安装后不立即启动
  -h, --help          显示帮助

示例:
  sudo ./install.sh --port 9000
  sudo ./install.sh --prefix /srv/bambu --user pi
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix)       PREFIX="$2"; shift 2 ;;
        --service-name) SERVICE_NAME="$2"; shift 2 ;;
        --user)         SERVICE_USER="$2"; shift 2 ;;
        --port)         PORT="$2"; shift 2 ;;
        --no-start)     DO_START=0; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              die "未知参数: $1（用 --help 查看用法）" ;;
    esac
done

# ---------- 前置检查 ----------
[ "$(id -u)" = "0" ] || die "请用 root 运行：sudo ./install.sh"
command -v systemctl >/dev/null 2>&1 || die "当前系统没有 systemd，无法安装为服务"
case "$PORT" in
    ''|*[!0-9]*) die "端口必须是数字: $PORT" ;;
esac
case "$PREFIX" in
    /tmp/*|/var/tmp/*) die "安装目录不能位于 /tmp（systemd PrivateTmp 会隔离），请使用 /opt 或 /srv 下的目录" ;;
esac

PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || die "未找到 python3，请先安装 Python 3.10 或更高版本"
"$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "Python 版本过低（当前 $("$PYTHON" -V 2>&1)），需要 3.10 及以上"

info "安装 $APP_NAME v$VERSION 到 $PREFIX（端口 $PORT）"

# ---------- 创建运行用户 ----------
USER_CREATED=0
if [ "$SERVICE_USER" != "root" ] && ! id "$SERVICE_USER" >/dev/null 2>&1; then
    info "创建系统用户 $SERVICE_USER"
    useradd --system --home-dir "$PREFIX" --shell /usr/sbin/nologin "$SERVICE_USER" 2>/dev/null \
        || adduser --system --home "$PREFIX" --shell /usr/sbin/nologin "$SERVICE_USER" \
        || die "创建用户 $SERVICE_USER 失败"
    USER_CREATED=1
fi

# ---------- 复制程序文件 ----------
install -d -m 0755 "$PREFIX"
if [ "$SRC_DIR" != "$PREFIX" ]; then
    for item in server.py requirements.txt config.example.json static tools deploy README.md INSTALL.md VERSION install.sh uninstall.sh vendor; do
        [ -e "$SRC_DIR/$item" ] && cp -a "$SRC_DIR/$item" "$PREFIX/"
    done
fi

# ---------- 生成空白配置 ----------
if [ -f "$PREFIX/config.json" ]; then
    info "检测到已有配置，保留 $PREFIX/config.json"
else
    cat > "$PREFIX/config.json" <<JSON
{
  "title": "打印工坊",
  "server": {
    "host": "0.0.0.0",
    "port": $PORT,
    "allow_control": false,
    "allow_manage": true
  },
  "printers": []
}
JSON
    info "已生成空白配置（0 台打印机）"
fi
chmod 600 "$PREFIX/config.json"

# ---------- Python 虚拟环境 ----------
if [ ! -x "$PREFIX/.venv/bin/python" ]; then
    info "创建 Python 虚拟环境"
    if ! "$PYTHON" -m venv "$PREFIX/.venv" 2>/dev/null; then
        warn "创建 venv 失败，尝试安装 python3-venv"
        if command -v apt-get >/dev/null 2>&1; then
            apt-get update -qq && apt-get install -y python3-venv
        elif command -v dnf >/dev/null 2>&1; then
            dnf install -y python3-virtualenv || dnf install -y python3-devel
        elif command -v yum >/dev/null 2>&1; then
            yum install -y python3-virtualenv
        fi
        "$PYTHON" -m venv "$PREFIX/.venv" || die "无法创建虚拟环境，请手动安装 python3-venv"
    fi
fi

PIP="$PREFIX/.venv/bin/pip"
"$PIP" install -q --upgrade pip >/dev/null 2>&1 || true

info "安装依赖 paho-mqtt"
if [ -d "$PREFIX/vendor" ]; then
    # 优先离线安装，失败再联网
    "$PIP" install -q --no-index --find-links "$PREFIX/vendor" -r "$PREFIX/requirements.txt" 2>/dev/null \
        || "$PIP" install -q -r "$PREFIX/requirements.txt"
else
    "$PIP" install -q -r "$PREFIX/requirements.txt"
fi

# ---------- 权限 ----------
if [ "$SERVICE_USER" != "root" ]; then
    chown -R "$SERVICE_USER:$SERVICE_USER" "$PREFIX"
fi
chmod 600 "$PREFIX/config.json"

# ---------- 写入 systemd 服务 ----------
info "写入 systemd 服务 /etc/systemd/system/$SERVICE_NAME.service"
sed -e "s|@PREFIX@|$PREFIX|g" \
    -e "s|@USER@|$SERVICE_USER|g" \
    -e "s|@PORT@|$PORT|g" \
    "$PREFIX/deploy/bambu-monitor.service.in" > "/etc/systemd/system/$SERVICE_NAME.service"
chmod 644 "/etc/systemd/system/$SERVICE_NAME.service"

cat > "$PREFIX/.install-meta" <<META
PREFIX=$PREFIX
SERVICE_NAME=$SERVICE_NAME
SERVICE_USER=$SERVICE_USER
USER_CREATED=$USER_CREATED
PORT=$PORT
VERSION=$VERSION
META

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1

# ---------- 启动 ----------
if [ "$DO_START" = "1" ]; then
    info "启动服务"
    systemctl restart "$SERVICE_NAME"
    sleep 2
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        info "服务运行中"
    else
        warn "服务启动异常，请执行：journalctl -u $SERVICE_NAME -n 50"
    fi
fi

# ---------- 防火墙提示 ----------
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    warn "检测到 ufw 已启用，如需其他机器访问：ufw allow $PORT/tcp"
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "$IP" ] || IP="127.0.0.1"

cat <<EOF

========================================
 安装完成  v$VERSION
========================================
 安装目录 : $PREFIX
 配置文件 : $PREFIX/config.json
 服务名   : $SERVICE_NAME
 访问地址 : http://$IP:$PORT

 下一步:
   1. 浏览器打开上面的地址，点击「添加打印机」扫描局域网
      （访问码在打印机屏幕 → 网络设置 / 设备信息）
   2. 也可以直接编辑配置后重启
        sudo nano $PREFIX/config.json
        sudo systemctl restart $SERVICE_NAME
   3. 查看日志
        journalctl -u $SERVICE_NAME -f

 常用命令:
   systemctl status  $SERVICE_NAME
   systemctl restart $SERVICE_NAME
   systemctl stop    $SERVICE_NAME

 卸载:
   sudo ./uninstall.sh
========================================
EOF
