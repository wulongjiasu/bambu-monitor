# 发布到 GitHub 指南

本目录已经是可直接推送的源码仓库，安装包在 `dist/`（已被 `.gitignore` 排除）。

## 一、创建仓库并推送源码

```bash
cd /root/bambu-monitor

git init
git add .
git commit -m "bambu-monitor v1.1.0: 网页扫描、添加与移除打印机"

# 在 GitHub 新建空仓库后，替换下面的地址
git branch -M main
git remote add origin https://github.com/wulongjiasu/bambu-monitor.git
git push -u origin main
```

`config.json`、`usage.json`、`.venv/`、`dist/` 已被 `.gitignore` 忽略，
不会被误传。推送前可以再确认一次：

```bash
git status --short
git ls-files | grep -E 'config\.json|usage\.json|\.venv' && echo "有敏感文件，请检查！" || echo "干净"
```

## 二、生成安装包

```bash
bash tools/make_release.sh
```

产物：

```text
dist/bambu-monitor-1.1.0.tar.gz
dist/SHA256SUMS.txt
```

脚本会自动检查包内没有 `config.json` / `usage.json` / `.install-meta`。

## 三、发布 Release

### 方式 A：网页上传（简单）

1. 打开仓库 → **Releases** → **Draft a new release**
2. Tag 填 `v1.1.0`，标题填 `v1.1.0`
3. 上传 `dist/bambu-monitor-1.1.0.tar.gz` 和 `dist/SHA256SUMS.txt`
4. 发布

### 方式 B：GitHub CLI

```bash
gh release create v1.1.0 \
  dist/bambu-monitor-1.1.0.tar.gz \
  dist/SHA256SUMS.txt \
  --title "v1.1.0" \
  --notes "网页扫描局域网、添加/移除打印机；详细安装见 INSTALL.md"
```

用户下载后按 `INSTALL.md` 执行：

```bash
sha256sum -c SHA256SUMS.txt
tar -xzf bambu-monitor-1.1.0.tar.gz
cd bambu-monitor-1.1.0
sudo ./install.sh
```

## 四、README 里的下载链接

把 `INSTALL.md` 和 README 中的 `wulongjiasu/bambu-monitor` 替换成真实地址，
或直接使用 Releases 固定链接：

```text
https://github.com/wulongjiasu/bambu-monitor/releases/latest/download/bambu-monitor-1.1.0.tar.gz
https://github.com/wulongjiasu/bambu-monitor/releases/latest/download/SHA256SUMS.txt
```

## 五、可选

- **许可证**：如需开源协议，可添加 `LICENSE`（MIT / Apache-2.0 等）并在 README 注明
- **Docker 镜像**：可推送到 GHCR / Docker Hub，`docker-compose.yml` 已支持本地 `build`
- **自动发布**：可加 GitHub Actions，在打 tag 时自动执行 `tools/make_release.sh`
  并把产物上传到 Release
- **截图**：`docs/` 中是模拟模式截图，不含真实打印机信息，可放心公开
