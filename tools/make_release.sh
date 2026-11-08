#!/usr/bin/env bash
# 生成可分发的 Linux 一键安装包（输出到 dist/）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="$(tr -d '[:space:]' < VERSION)"
NAME="bambu-monitor-$VERSION"
DIST="$ROOT/dist"
OUT="$DIST/$NAME"

info() { printf '\033[32m==>\033[0m %s\n' "$*"; }

info "清理旧的构建产物"
rm -rf "$OUT" "$DIST/$NAME.tar.gz" "$DIST/SHA256SUMS.txt"
mkdir -p "$OUT"

info "复制运行所需文件"
FILES=(
    server.py
    requirements.txt
    config.example.json
    install.sh
    uninstall.sh
    README.md
    INSTALL.md
    VERSION
    Dockerfile
    docker-compose.yml
)
for f in "${FILES[@]}"; do
    [ -e "$f" ] && cp -a "$f" "$OUT/"
done

cp -a static tools deploy vendor "$OUT/"

# 只带界面截图，绝不打包真实配置和访问码
mkdir -p "$OUT/docs"
for img in docs/screenshot.png docs/mobile.png docs/add-printer.png; do
    [ -f "$img" ] && cp -a "$img" "$OUT/docs/"
done

chmod +x "$OUT/install.sh" "$OUT/uninstall.sh" "$OUT/tools/"*.py "$OUT/tools/"*.sh

info "安全检查：确认没有打包本地配置"
if find "$OUT" -name 'config.json' -o -name 'usage.json' -o -name '.install-meta' | grep -q .; then
    echo "错误：安装包中混入了本地配置" >&2
    exit 1
fi

info "打包"
tar -czf "$DIST/$NAME.tar.gz" -C "$DIST" "$NAME"
rm -rf "$OUT"
( cd "$DIST" && sha256sum "$NAME.tar.gz" > SHA256SUMS.txt )

SIZE="$(du -h "$DIST/$NAME.tar.gz" | cut -f1)"
info "完成：dist/$NAME.tar.gz（$SIZE）"

echo
echo "包含文件："
tar -tzf "$DIST/$NAME.tar.gz" | sed 's|^[^/]*/||' | grep -v '/$' | grep . | sort | sed 's/^/  /'
echo
echo "校验值："
cat "$DIST/SHA256SUMS.txt"
