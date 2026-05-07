#!/usr/bin/env bash
# 在 Debian/Ubuntu 上一键安装 DHL 追踪服务依赖：
#   - google-chrome-stable（Akamai 对真实 Chrome 友好，比 chromium 稳）
#   - Xvfb（虚拟显示）
#   - x11vnc（首次/续期 Akamai 验证时通过 VNC 远程过验证；可选）
#   - 中文字体（DHL 中文站缺字体会触发 Akamai 指纹差异）
#   - Python venv 与 Playwright（Playwright 自带 Chromium 仍可保留作为兜底）
#
# 用法：
#   sudo bash deploy/install-deps.sh
#
# 设计要点：
# - 幂等：可重复执行
# - 不动现有 Python 环境（建议项目自己用 .venv）
# - 不静默装 google-chrome-stable，会写入 /etc/apt/sources.list.d/google-chrome.list 之前提示

set -Eeuo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "请用 root 或 sudo 执行：sudo bash $0" >&2
    exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
    echo "[FATAL] 仅支持 Debian/Ubuntu（找不到 apt-get）" >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive

log() { printf '\n\033[1;36m[install] %s\033[0m\n' "$*"; }

log "更新 apt 索引 …"
apt-get update -y

log "安装基础工具与中文字体 …"
apt-get install -y --no-install-recommends \
    ca-certificates curl wget gnupg lsb-release \
    xvfb x11vnc psmisc iproute2 \
    fonts-noto-cjk fonts-noto-color-emoji fonts-liberation \
    python3 python3-venv python3-pip

# Google Chrome stable 仓库
if [[ ! -f /etc/apt/sources.list.d/google-chrome.list ]]; then
    log "添加 Google Chrome 官方仓库 …"
    install -d -m 0755 /etc/apt/keyrings
    wget -q -O - https://dl.google.com/linux/linux_signing_key.pub \
        | gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg
    echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] \
http://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list
    apt-get update -y
fi

if ! dpkg -l | awk '{print $2}' | grep -q '^google-chrome-stable$'; then
    log "安装 google-chrome-stable …"
    apt-get install -y --no-install-recommends google-chrome-stable
else
    log "google-chrome-stable 已安装：$(google-chrome-stable --version 2>&1 || true)"
fi

# 创建系统用户 dhlchrome（无 shell 登录）
if ! id -u dhlchrome >/dev/null 2>&1; then
    log "创建系统用户 dhlchrome …"
    useradd -r -m -d /var/lib/dhl-chrome -s /usr/sbin/nologin dhlchrome
fi

mkdir -p /var/lib/dhl-chrome /var/log/dhl-chrome
chown -R dhlchrome:dhlchrome /var/lib/dhl-chrome /var/log/dhl-chrome

log "完成 ✅"
echo ""
echo "下一步："
echo "  1) 把项目放到 /opt/Logistics_order（或修改 deploy/dhl-chrome.service 中的 ExecStart 路径）"
echo "  2) sudo cp deploy/dhl-chrome.service /etc/systemd/system/"
echo "     sudo systemctl daemon-reload"
echo "     sudo systemctl enable --now dhl-chrome"
echo "  3) 首次需 VNC 进去手动通过 DHL 的 Akamai 验证："
echo "     - 编辑 /etc/systemd/system/dhl-chrome.service 取消 X11VNC_PORT/X11VNC_PASSWORD 注释"
echo "     - sudo systemctl daemon-reload && sudo systemctl restart dhl-chrome"
echo "     - 本地 SSH 隧道：ssh -L 5900:127.0.0.1:5900 user@server"
echo "     - 用 VNC 客户端连 127.0.0.1:5900 → 在浏览器里完成 Akamai 验证 → 关闭 VNC 即可"
echo "  4) python3 deploy/healthcheck.py    # 验证 CDP + Akamai 是否就绪"
