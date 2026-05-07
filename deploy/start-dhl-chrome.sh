#!/usr/bin/env bash
# 在 Linux VPS 上以 Xvfb 虚拟显示常驻一个 DHL 专用 Chrome（CDP 端口 18800）。
#
# 设计要点：
# - 使用 google-chrome-stable（Akamai 对 chromium 浏览器内核更敏感）
# - 持久化 user-data-dir，保留 Akamai cookie（可复用 24~72h）
# - 启动前清理 SingletonLock / SingletonCookie / SingletonSocket，
#   避免上一次 SIGKILL 残留导致 Chrome 拒启
# - 日志同时输出到 stderr 与 ${LOG_FILE}
# - 收到 SIGTERM 时优雅关闭 Chrome 与 Xvfb
#
# 环境变量（可选）：
#   CDP_PORT          默认 18800
#   USER_DATA_DIR     默认 /var/lib/dhl-chrome
#   LOG_FILE          默认 /var/log/dhl-chrome.log
#   DISPLAY_NUM       默认 :99
#   START_URL         首屏导航 URL，默认 DHL 中文追踪页
#   X11VNC_PORT       若设置（例如 5900），同时拉起 x11vnc 便于 VNC 远程过 Akamai
#   X11VNC_PASSWORD   x11vnc 密码（若启用 X11VNC_PORT 必须提供）

set -Eeuo pipefail

CDP_PORT="${CDP_PORT:-18800}"
USER_DATA_DIR="${USER_DATA_DIR:-/var/lib/dhl-chrome}"
LOG_FILE="${LOG_FILE:-/var/log/dhl-chrome.log}"
DISPLAY_NUM="${DISPLAY_NUM:-:99}"
START_URL="${START_URL:-https://www.dhl.com/cn-zh/home/tracking.html}"
X11VNC_PORT="${X11VNC_PORT:-}"
X11VNC_PASSWORD="${X11VNC_PASSWORD:-}"

CHROME_BIN="$(command -v google-chrome-stable || command -v google-chrome || true)"
if [[ -z "${CHROME_BIN}" ]]; then
    echo "[FATAL] 未找到 google-chrome / google-chrome-stable，请先执行 deploy/install-deps.sh" >&2
    exit 1
fi
if ! command -v xvfb-run >/dev/null 2>&1; then
    echo "[FATAL] 未找到 xvfb-run，请先执行 deploy/install-deps.sh" >&2
    exit 1
fi

mkdir -p "${USER_DATA_DIR}"
mkdir -p "$(dirname "${LOG_FILE}")"
touch "${LOG_FILE}"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "${LOG_FILE}" >&2; }

# 清理上次残留：上次 Chrome 被强杀时这些 Singleton 文件会让本次拒启
log "清理 ${USER_DATA_DIR} 内 Singleton 残留 …"
find "${USER_DATA_DIR}" -maxdepth 2 -type l \
    \( -name 'SingletonLock' -o -name 'SingletonCookie' -o -name 'SingletonSocket' \) \
    -delete 2>/dev/null || true

# 若端口已被占用：尝试先清理（同一脚本被重复 systemctl start 时会发生）
if ss -ltn "( sport = :${CDP_PORT} )" | grep -q LISTEN; then
    log "[WARN] 端口 ${CDP_PORT} 已在监听，尝试杀掉占用进程 …"
    fuser -k "${CDP_PORT}/tcp" 2>/dev/null || true
    sleep 1
fi

CHROME_FLAGS=(
    "--remote-debugging-port=${CDP_PORT}"
    # 必须绑 0.0.0.0 才能从同主机的 Streamlit 进程或其它容器访问；
    # 强烈建议同时用防火墙限制 18800 仅本机/内网可访问
    "--remote-debugging-address=127.0.0.1"
    "--user-data-dir=${USER_DATA_DIR}"
    "--no-first-run"
    "--no-default-browser-check"
    "--disable-features=Translate,InfinitePrefetch"
    "--disable-popup-blocking"
    "--password-store=basic"
    "--use-mock-keychain"
    "--start-maximized"
    "--window-size=1440,900"
    # 必要时可加 "--no-sandbox"（容器/受限内核），但有安全代价
    "${START_URL}"
)

# 若启用 x11vnc：先把 Chrome 跑到独立 Xvfb 上，再单独拉 x11vnc
if [[ -n "${X11VNC_PORT}" ]]; then
    if [[ -z "${X11VNC_PASSWORD}" ]]; then
        log "[FATAL] 启用了 X11VNC_PORT=${X11VNC_PORT} 但未设置 X11VNC_PASSWORD"
        exit 1
    fi
    if ! command -v Xvfb >/dev/null 2>&1 || ! command -v x11vnc >/dev/null 2>&1; then
        log "[FATAL] 缺少 Xvfb 或 x11vnc，请重跑 install-deps.sh"
        exit 1
    fi

    log "启动 Xvfb on ${DISPLAY_NUM} …"
    Xvfb "${DISPLAY_NUM}" -screen 0 1440x900x24 -ac +extension RANDR &
    XVFB_PID=$!
    sleep 1

    log "启动 x11vnc on :${X11VNC_PORT}（仅监听 127.0.0.1）…"
    VNC_PASSFILE="$(mktemp)"
    chmod 600 "${VNC_PASSFILE}"
    printf '%s' "${X11VNC_PASSWORD}" >"${VNC_PASSFILE}"
    x11vnc -display "${DISPLAY_NUM}" -rfbport "${X11VNC_PORT}" -localhost \
        -passwdfile "${VNC_PASSFILE}" -forever -shared -bg \
        -o "${LOG_FILE}.vnc" >/dev/null
    rm -f "${VNC_PASSFILE}"

    log "启动 Chrome（DISPLAY=${DISPLAY_NUM}, CDP=${CDP_PORT}）…"
    DISPLAY="${DISPLAY_NUM}" "${CHROME_BIN}" "${CHROME_FLAGS[@]}" \
        >>"${LOG_FILE}" 2>&1 &
    CHROME_PID=$!

    cleanup() {
        log "收到退出信号，清理 …"
        kill "${CHROME_PID}" 2>/dev/null || true
        kill "${XVFB_PID}" 2>/dev/null || true
        pkill -f "x11vnc.*-rfbport ${X11VNC_PORT}" 2>/dev/null || true
        wait 2>/dev/null || true
    }
    trap cleanup EXIT INT TERM
    wait "${CHROME_PID}"
else
    # 未启用 VNC：用 xvfb-run 自动管理 Xvfb 生命周期（更简）
    log "启动 Chrome via xvfb-run（CDP=${CDP_PORT}）…"
    cleanup() {
        log "收到退出信号，杀掉子进程 …"
        pkill -P $$ 2>/dev/null || true
    }
    trap cleanup EXIT INT TERM
    exec xvfb-run -a --server-args="-screen 0 1440x900x24" \
        "${CHROME_BIN}" "${CHROME_FLAGS[@]}" >>"${LOG_FILE}" 2>&1
fi
