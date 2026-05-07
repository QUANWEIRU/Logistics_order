# Linux VPS 部署：DHL 追踪服务（Streamlit + 常驻 Chrome）

> **适用对象**：你有一台 **自家 Linux 服务器**（阿里云 ECS / 腾讯云 CVM / AWS EC2 / Hetzner / VPS …），想长期运行 `piece_lookup_app.py`，并让「DHL 追踪详情」也能在线工作。
>
> ❗ **不适用于 Streamlit Community Cloud**：那里是无状态 ephemeral 容器，不能常驻 Chrome、不能跨 request 保留 Akamai cookie。云端只能走 `DHL_API_KEY`（见项目根 README 的 _方案 A_）。

---

## 架构

```
┌──────────────────────────────┐         ┌───────────────────────────┐
│  Xvfb 虚拟显示 :99           │  CDP    │   Streamlit 进程           │
│  └─ google-chrome-stable     │◄────────┤   piece_lookup_app.py     │
│      --remote-debugging      │  18800  │   (dhl_tracker.track)     │
│      --user-data-dir         │         └───────────────────────────┘
│      🔒 Akamai cookie 持久化  │
└──────────────────────────────┘
        ▲                                 公网 / 内网用户
        │ 首次/续期 Akamai 验证
        └──── x11vnc :5900 ◄───── SSH 隧道 ◄──── 本地 VNC 客户端
```

- Chrome 由 systemd 管理，崩溃自动重启
- Akamai cookie 写在 `--user-data-dir`，**有效期 24~72h**，过期时通过 VNC 进去手动通过一次即可
- Streamlit 与 Chrome 同主机部署，CDP 端口 `127.0.0.1:18800` 不暴露公网

---

## 一、首次安装（root）

```bash
# 1. 把项目放到 /opt（或自己习惯的位置；调整 dhl-chrome.service 中的路径即可）
sudo git clone <你的 repo> /opt/Logistics_order
cd /opt/Logistics_order

# 2. 一键装依赖：google-chrome-stable / Xvfb / x11vnc / 中文字体 / Python venv
sudo bash deploy/install-deps.sh

# 3. 装 Python 依赖到项目 venv
sudo -u dhlchrome python3 -m venv /opt/Logistics_order/.venv
sudo -u dhlchrome /opt/Logistics_order/.venv/bin/pip install -r requirements.txt
sudo -u dhlchrome /opt/Logistics_order/.venv/bin/python -m playwright install chromium

# 4. 部署 systemd 单元
sudo cp deploy/dhl-chrome.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dhl-chrome

# 5. 看日志确认 Chrome 起来了
sudo journalctl -u dhl-chrome -n 50 -f
```

---

## 二、首次过 Akamai 验证（VNC）

Chrome 第一次启动时 `--user-data-dir` 是空的，DHL 追踪页会跳 Akamai 人机验证。**只需要做一次**，cookie 会持久化进 `--user-data-dir`。

### 1. 启用 x11vnc

编辑 `/etc/systemd/system/dhl-chrome.service`，去掉这两行的注释并设强密码：

```ini
Environment=X11VNC_PORT=5900
Environment=X11VNC_PASSWORD=请改成强密码
```

```bash
sudo systemctl daemon-reload && sudo systemctl restart dhl-chrome
```

### 2. 本地建立 SSH 隧道

```bash
# 在你的 Mac/Win 本机执行：把服务器上 5900 转到本地 5900
ssh -N -L 5900:127.0.0.1:5900 user@your-server
```

### 3. 用 VNC 客户端连接

- macOS：Finder → ⌘K → `vnc://127.0.0.1:5900` → 输入上面的密码
- Win：[TightVNC Viewer](https://www.tightvnc.com/) / [RealVNC](https://www.realvnc.com/)
- 任意 Linux：`vncviewer 127.0.0.1:5900`

进入桌面后会看到 Chrome 已停在 DHL 追踪页：
1. 点击 Akamai 的「我不是机器人」复选框
2. 等到追踪页正常显示
3. 关闭 VNC（不要关 Chrome / 不要 stop service）

### 4. 验证 cookie 已生效

```bash
sudo -u dhlchrome /opt/Logistics_order/.venv/bin/python \
    /opt/Logistics_order/deploy/healthcheck.py
```

期望输出：

```
endpoint = http://127.0.0.1:18800
  ✅ http         browser=Chrome/147.x
  ✅ targets      total=1, page=1
  ✅ playwright   elapsed_ms=400
  ✅ real_query   tracking=4191468945, status=delivered, pieces=4, events=33, elapsed_ms=7600
```

### 5. （强烈建议）验证完后关闭 VNC 端口

```bash
# 注释回 X11VNC_PORT/X11VNC_PASSWORD
sudo $EDITOR /etc/systemd/system/dhl-chrome.service
sudo systemctl daemon-reload && sudo systemctl restart dhl-chrome
```

VNC 不应长期暴露——即使 `-localhost` 限制了监听，下一次过验证时再开即可。

---

## 三、跑 Streamlit

跟本地完全一样：

```bash
cd /opt/Logistics_order
.venv/bin/streamlit run piece_lookup_app.py \
    --server.address=0.0.0.0 \
    --server.port=8501 \
    --server.headless=true
```

「DHL 追踪详情」tab 会自动连 `127.0.0.1:18800`，无需额外配置。

如果想后台跑，再加一份 `streamlit-piece-lookup.service`（仿 `dhl-chrome.service` 写法即可）。

---

## 四、日常运维

### 查 Chrome 状态

```bash
sudo systemctl status dhl-chrome
sudo journalctl -u dhl-chrome -n 100 --no-pager
ss -ltn | grep 18800
```

### 健康巡检（建议加 cron 每 10 分钟）

```cron
*/10 * * * * dhlchrome /opt/Logistics_order/.venv/bin/python \
    /opt/Logistics_order/deploy/healthcheck.py --json \
    >> /var/log/dhl-chrome/healthcheck.log 2>&1
```

退出码：
- `0` 一切正常
- `1` CDP 端口不通 → `systemctl restart dhl-chrome`
- `2` Playwright 握手失败 → 检查版本兼容
- `3` Akamai cookie 已失效 → 重新走「VNC 过验证」流程
- `4` 其它异常 → 看日志

### Akamai cookie 失效时怎么办？

1. `healthcheck.py` 退出码 3 意味着 Akamai 已回到验证页
2. 按上面「二、首次过 Akamai 验证」操作
3. **不要**清空 `--user-data-dir`，否则连历史 cookie 也丢了

### 清空 profile / 强制重置

```bash
sudo systemctl stop dhl-chrome
sudo rm -rf /var/lib/dhl-chrome/profile
sudo systemctl start dhl-chrome
# 然后重新走 VNC 过验证流程
```

---

## 五、常见坑

| 现象 | 原因 | 处理 |
|---|---|---|
| `connect_over_cdp` 报 `Browser context management is not supported` | Chrome 实例没有任何 page target | `dhl_tracker._open_cdp_browser` 已自动兜底创建 about:blank；若仍报错确认 Chrome 健康 |
| Chrome 拒启 / 端口已占用 | 上次 SIGKILL 残留 SingletonLock | `start-dhl-chrome.sh` 已自动清理；手动可 `find /var/lib/dhl-chrome/profile -name 'Singleton*' -delete` |
| Akamai 频繁让验证 | 服务器 IP 段被识别为 IDC | 换国内出口的 VPS / 走家用宽带的 ZeroTier；或加 cron 每天预热一次 |
| 中文站显示□□□ | 中文字体没装 | `install-deps.sh` 已装 `fonts-noto-cjk`；自定 image 的话注意带 |
| 容器 / 受限内核报 sandbox 错 | 内核 namespace 限制 | `start-dhl-chrome.sh` 末尾加 `--no-sandbox`（仅在容器/可信环境用） |
| Streamlit 在云端 detail tab 失败 | 云端无 CDP | 见根目录方案 A：用 `DHL_API_KEY` 走官方 Unified API |

---

## 六、安全清单

- [ ] CDP 端口 18800 只监听 `127.0.0.1`（已在 `start-dhl-chrome.sh` 设置 `--remote-debugging-address=127.0.0.1`）
- [ ] x11vnc 仅在过验证时启用，且 `-localhost` 限制
- [ ] Streamlit 公网暴露时务必加反向代理 + 鉴权（nginx + basic auth / Cloudflare Access）
- [ ] `dhlchrome` 用户使用 `nologin` shell，不持有 sudo 权限
- [ ] systemd unit 已开 `PrivateTmp` + `ProtectSystem=strict`
- [ ] Chrome 的 `--user-data-dir` 模式 `0700`，仅 dhlchrome 可读

---

## 文件清单

```
deploy/
├── README.md              ← 当前文档
├── install-deps.sh        ← apt 装依赖 + 建用户
├── start-dhl-chrome.sh    ← Chrome + Xvfb 启动脚本（被 systemd 调）
├── dhl-chrome.service     ← systemd 单元
└── healthcheck.py         ← 4 步健康检查（含真实查询）
```
