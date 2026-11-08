# 安装教程 · 拓竹打印机监控 v1.1.0

本文面向 **Linux 服务器 / 小主机 / NAS**，从零完成安装、配置、升级和卸载。
如果你只想快速跑起来，执行「[三、一键安装](#三一键安装)」即可。

---

## 一、系统要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Linux，使用 **systemd**（Ubuntu / Debian / CentOS / Rocky / Fedora / 群晖等） |
| Python | **3.10 及以上**（自带 `venv` 模块） |
| 权限 | 安装和卸载需要 `root`（`sudo`） |
| 内存 | 128 MB 以上空闲内存即可 |
| 磁盘 | 约 30 MB |
| 网络 | 服务器与打印机在**同一局域网**；能访问打印机的 **8883/TCP** |
| 浏览器 | Chrome / Edge / Firefox / Safari 近期版本 |

> 没有 systemd 的环境（部分 LXC、Unraid、飞牛、群晖套件环境）请使用
> [Docker 部署](#九docker-部署)。

### 支持的打印机

X1 / X1 Carbon / X1E、P1P / P1S、A1 / A1 mini、P2S、H2D / H2C / H2S、X2D 等
拓竹 LAN Mode 机型。打印机固件需开启 **LAN Only / 局域网模式**。

---

## 二、安装前检查

在服务器上执行：

```bash
# 1. 确认有 systemd
systemctl --version | head -1

# 2. 确认 Python 版本 >= 3.10
python3 -V

# 3. 确认 venv 可用（无输出即正常）
python3 -c "import venv; print('venv ok')"

# 4. 确认 8088 端口没被占用（可换成你准备使用的端口）
ss -ltnp | grep ':8088' || echo "8088 可用"
```

常见缺失依赖的安装方法：

```bash
# Debian / Ubuntu
sudo apt update
sudo apt install -y python3 python3-venv tar curl

# CentOS / Rocky / Fedora
sudo dnf install -y python3 python3-pip tar curl
```

---

## 三、一键安装

### 1. 下载安装包

从 GitHub Releases 下载 `bambu-monitor-1.1.0.tar.gz` 和 `SHA256SUMS.txt`：

```bash
# 以 GitHub Releases 地址为例，替换成你的实际地址
wget https://github.com/wulongjiasu/bambu-monitor/releases/download/v1.1.0/bambu-monitor-1.1.0.tar.gz
wget https://github.com/wulongjiasu/bambu-monitor/releases/download/v1.1.0/SHA256SUMS.txt

# 校验完整性
sha256sum -c SHA256SUMS.txt
```

### 2. 解压并安装

```bash
tar -xzf bambu-monitor-1.1.0.tar.gz
cd bambu-monitor-1.1.0
sudo ./install.sh
```

安装脚本会自动完成：

1. 创建系统用户 `bambu`（无登录权限）
2. 复制程序到 `/opt/bambu-monitor`
3. 创建 Python 虚拟环境，优先用包内离线 wheel 安装依赖
4. 生成空白 `config.json`
5. 写入 systemd 服务并设置开机自启
6. 启动服务并打印访问地址

### 3. 安装参数（可选）

```bash
sudo ./install.sh --port 9000                        # 自定义网页端口
sudo ./install.sh --prefix /srv/bambu                # 自定义安装目录
sudo ./install.sh --user pi                          # 指定运行用户
sudo ./install.sh --service-name bambu-monitor-2     # 自定义服务名
sudo ./install.sh --no-start                         # 只安装，不启动
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--port PORT` | `8088` | 网页端口 |
| `--prefix DIR` | `/opt/bambu-monitor` | 安装目录 |
| `--service-name NAME` | `bambu-monitor` | systemd 服务名 |
| `--user USER` | `bambu`（不存在则创建） | 运行服务的用户 |
| `--no-start` | — | 安装后不立即启动 |

> 安装目录请使用 `/opt`、`/srv` 等常规路径，**不要使用 `/tmp`**：
> systemd 服务启用了 `PrivateTmp`，`/tmp` 会被隔离，导致服务找不到程序文件。
> 安装脚本会直接拒绝 `/tmp` 路径。

> 同一台服务器可以安装多个实例（不同端口 + 不同 `--prefix` + 不同
> `--service-name`），互不影响。

### 4. 打开面板

安装完成会输出类似：

```
访问地址 : http://192.168.1.20:8088
```

浏览器打开即可。首次安装是空面板，按下一节添加打印机。

---

## 四、添加打印机（推荐：网页扫描）

### 1. 扫描局域网

1. 点击页面右上角的 **＋ 添加打印机**
2. 点击 **扫描局域网**，等待约 10 秒
3. 页面会列出同网段打印机的 **型号、IP、序列号、信号强度**
4. 点击任意结果，型号 / 序列号 / IP / 名称会自动填入

### 2. 填写访问码

**访问码无法自动获取**，需要从打印机屏幕读取：

| 机型 | 位置 |
| --- | --- |
| X1 / X2 / H2 / P2 系列 | 设置 → 网络 → 局域网模式（LAN Only） |
| P1 系列 | 设置 → WLAN → 局域网模式 |
| A1 系列 | 设置 → 网络 → 局域网模式 |

访问码为 **8 位、区分大小写**。序列号为 **15 或 18 位**字母数字。

填好后点击 **添加**，设备会立即出现在面板并开始连接，**无需重启服务**。

### 3. 手动添加

扫描不到时也可以完全手动填写：名称、型号、序列号、IP、访问码，
起始工时可选（填打印机屏幕上已有的累计时长）。

### 4. 移除打印机

展开任意卡片 → 底部点击 **从监控中移除**。
会同时从面板和 `config.json` 中删除该设备（不影响打印机本身）。

> 想关闭网页添加 / 移除功能，把 `server.allow_manage` 改为 `false` 并重启服务。

---

## 五、配置文件说明

配置文件路径：`/opt/bambu-monitor/config.json`（权限 `600`，含访问码）。

```json
{
  "title": "打印工坊",
  "server": {
    "host": "0.0.0.0",
    "port": 8088,
    "allow_control": false,
    "allow_manage": true
  },
  "printers": [
    {
      "name": "X1C-01",
      "model": "X1 Carbon",
      "serial": "01S00A000000001",
      "ip": "192.168.1.101",
      "access_code": "12345678",
      "baseline_hours": 120.5
    }
  ]
}
```

### server 字段

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `host` | `0.0.0.0` | 监听地址，只允许本机访问可改 `127.0.0.1` |
| `port` | `8088` | 网页端口；安装时 `--port` 会写入这里 |
| `allow_control` | `false` | 是否显示暂停 / 继续 / 停止按钮 |
| `allow_manage` | `true` | 是否允许网页扫描、添加、移除打印机 |

### printers 字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 否 | 卡片名称，默认取序列号后 6 位 |
| `model` | 否 | 显示在卡片右上角，如 `X1 Carbon` / `P1S` / `A1` |
| `serial` | 是 | 序列号，用于订阅 MQTT 主题 |
| `ip` | 是 | 打印机局域网 IP，建议在路由器固定 DHCP |
| `access_code` | 是 | 打印机访问码，8 位、区分大小写 |
| `baseline_hours` | 否 | 起始累计工时（小时），可填打印机屏幕上的历史值 |

修改后重启服务生效：

```bash
sudo systemctl restart bambu-monitor
```

> 网页添加 / 移除会自动改写此文件，不需要手动编辑，也不需要重启。
> 累计工时另存于同目录 `usage.json`，删除它只会清零本地统计。

---

## 六、服务管理

```bash
# 查看状态
systemctl status bambu-monitor

# 启动 / 停止 / 重启
sudo systemctl start bambu-monitor
sudo systemctl stop bambu-monitor
sudo systemctl restart bambu-monitor

# 开机自启 / 关闭自启
sudo systemctl enable bambu-monitor
sudo systemctl disable bambu-monitor

# 实时日志（看到「MQTT 已连接」即成功）
journalctl -u bambu-monitor -f

# 最近 100 行日志
journalctl -u bambu-monitor -n 100 --no-pager
```

服务文件：`/etc/systemd/system/bambu-monitor.service`
程序目录：`/opt/bambu-monitor`
数据文件：`config.json`、`usage.json`

---

## 七、升级

1. 备份配置（访问码都在里面）：

   ```bash
   sudo cp /opt/bambu-monitor/config.json ~/bambu-config-$(date +%F).json
   sudo cp /opt/bambu-monitor/usage.json  ~/bambu-usage-$(date +%F).json
   ```

2. 下载并解压新版本，使用**与首次安装相同**的参数执行：

   ```bash
   tar -xzf bambu-monitor-<新版本>.tar.gz
   cd bambu-monitor-<新版本>
   sudo ./install.sh --port 8088
   ```

   安装脚本检测到已有 `config.json` 会**保留配置**，只更新程序并重启服务。
   如果首次安装用了 `--prefix` / `--service-name`，升级时也要带上。

3. 验证：

   ```bash
   systemctl status bambu-monitor
   curl -s http://127.0.0.1:8088/healthz
   ```

---

## 八、卸载

```bash
# 停止服务、删除 systemd 单元，保留配置目录（方便重装）
sudo /opt/bambu-monitor/uninstall.sh

# 彻底删除：程序目录 + 自动创建的 bambu 用户
sudo /opt/bambu-monitor/uninstall.sh --purge
```

> `--purge` 会删除 `/opt/bambu-monitor/config.json` 和累计工时，操作前请备份。
> 如果安装时自定义了服务名或目录，卸载时同样要指定：
> `sudo /opt/bambu-monitor/uninstall.sh --prefix /srv/bambu --service-name bambu-monitor-2 --purge`

---

## 九、Docker 部署

适合没有 systemd 或不想污染宿主 Python 的环境。

```bash
tar -xzf bambu-monitor-1.1.0.tar.gz
cd bambu-monitor-1.1.0

cp config.example.json config.json
mkdir -p data
sudo nano config.json        # 可选：先填好打印机，也可以装好后在网页添加

docker compose up -d --build
```

访问 `http://服务器IP:8088`。

说明：

- `docker-compose.yml` 默认使用 `network_mode: host`，因为局域网扫描依赖 UDP 组播；
- `config.json` 以**读写**方式挂载（网页添加/移除需要写配置）；
- 累计工时保存到宿主机 `./data/usage.json`；
- 若改用 bridge 网络，请打开 compose 中的 `ports` 映射，此时扫描可能不可用，
  可在网页手动添加打印机。

常用命令：

```bash
docker compose logs -f
docker compose restart
docker compose down
```

---

## 十、防火墙与网络

```bash
# Ubuntu ufw：放行网页端口
sudo ufw allow 8088/tcp

# firewalld
sudo firewall-cmd --permanent --add-port=8088/tcp
sudo firewall-cmd --reload
```

- 服务器 **出站** 需要能访问打印机的 `8883/TCP`（MQTT over TLS）
- 网页端口 `8088/TCP` 只需对可信内网开放
- 扫描功能需要 UDP 组播 `239.255.255.250:1990`、`:2021`；
  跨 VLAN 时需要在路由器/交换机放行组播，否则请手动添加打印机

---

## 十一、常见问题

### 1. 提示 `ensurepip is not available` / venv 创建失败

```bash
sudo apt install -y python3-venv     # Debian / Ubuntu
sudo dnf install -y python3-devel    # Fedora / Rocky
```

### 2. 端口被占用

```bash
ss -ltnp | grep ':8088'
```

换一个端口重装：

```bash
sudo ./install.sh --port 9000
```

或修改 `config.json` 的 `server.port` 后重启服务。

### 3. `systemctl` 不存在 / 容器里装不了

使用 [Docker 部署](#九docker-部署)，或手动运行：

```bash
cd /opt/bambu-monitor
.venv/bin/python server.py --config config.json --port 8088
```

### 4. 扫描不到打印机

依次检查：

1. 服务器和打印机是否在同一网段（`ip addr` 对比打印机 IP）
2. 是否用了 Docker bridge 网络 → 改 `network_mode: host`
3. 交换机 / 路由器是否拦截了组播（跨 VLAN 常见）
4. 打印机是否开启了 LAN Only 模式
5. 直接手动添加 IP + 序列号 + 访问码，功能完全相同

### 5. 日志显示「MQTT 连接被拒绝」

- 访问码错误（**区分大小写**，注意 `0/O`、`1/I`）
- 打印机没有开启局域网模式
- 序列号与 IP 不匹配

验证单台打印机：

```bash
/opt/bambu-monitor/.venv/bin/python \
  /opt/bambu-monitor/tools/check_printer.py <IP> <序列号> <访问码>
```

### 6. 全部显示「离线」

```bash
# 服务器能否访问打印机 8883 端口
nc -vz <打印机IP> 8883

# 看服务日志
journalctl -u bambu-monitor -n 50 --no-pager
```

常见原因：IP 变了、打印机休眠断网、防火墙拦截、访问码错误。

### 7. 网页添加时提示「写入配置文件失败」

程序以 `bambu` 用户运行，需要能写 `config.json`：

```bash
sudo chown bambu:bambu /opt/bambu-monitor/config.json
sudo chmod 600 /opt/bambu-monitor/config.json
```

Docker 部署时确认 `config.json` 是读写挂载（不要加 `:ro`）。

### 8. 服务启动失败

```bash
systemctl status bambu-monitor --no-pager
journalctl -u bambu-monitor -n 80 --no-pager
```

### 9. 页面一直「连接中断，重试中」

- 反向代理需关闭 SSE 缓冲：`proxy_buffering off;`
- 检查浏览器控制台和服务器 8088 端口连通性

### 10. 能和 Bambu Studio / Handy 同时连接吗？

可以。部分机型对并发 MQTT 连接数有限制，若 Studio 频繁掉线，
可减少同时运行的监控实例数量。

---

## 十二、安全建议

- 面板本身**没有登录鉴权**，请只在可信内网使用
- 不要直接把端口映射到公网；需要远程访问请用 VPN 或带认证的反向代理
- `config.json` 含全部访问码，权限已设为 `600`，不要提交到 Git 或公开分享
- 不需要网页管理时，把 `server.allow_manage` 设为 `false`
- 不需要远程控制时，保持 `server.allow_control` 为 `false`

---

## 十三、安装后的文件清单

```text
/opt/bambu-monitor/
├── server.py                 # 主程序（HTTP + SSE + MQTT）
├── static/                   # 前端页面
├── tools/                    # 打印机诊断、批量配置、打包脚本
├── vendor/                   # 离线依赖 wheel
├── deploy/                   # systemd 模板
├── .venv/                    # Python 虚拟环境
├── config.json               # 打印机配置（600，自动生成）
├── usage.json                # 累计工时（600，自动生成）
├── README.md
├── INSTALL.md
└── VERSION

/etc/systemd/system/bambu-monitor.service
```

---

## 十四、从源码运行（开发 / 预览）

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# 模拟 20 台打印机，不需要真实设备
python3 server.py --mock

# 读取真实配置
python3 server.py --config config.json --port 8088
```

制作发布包：

```bash
bash tools/make_release.sh
# 产物：dist/bambu-monitor-<版本>.tar.gz 和 dist/SHA256SUMS.txt
```

---

## 十五、版本说明

### v1.1.0

- 新增网页扫描局域网打印机（SSDP 多播 + TLS 证书 CN 识别序列号）
- 新增网页添加 / 移除打印机，配置热加载，无需重启
- 新增 `server.allow_manage` 开关（默认开启）
- 添加接口仅接受 `application/json`，降低跨站写入风险
- 优化 `uninstall.sh` 命令行参数优先级
- 更新 Docker 部署说明与离线依赖
