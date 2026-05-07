"""
Playwright：pip 安装包后仍需单独下载浏览器二进制。

在 Streamlit Community Cloud 等环境中于首次启动内核前调用本模块，
自动执行 ``python -m playwright install <browser>``。
"""

from __future__ import annotations

import subprocess
import sys
from threading import Lock

_lock = Lock()
_installed: set[str] = set()


def ensure_playwright_browser_installed(browser: str) -> None:
    """
    确保当前进程已安装指定内核（chrome 会映射到 chromium 内核）。

    同一内核只安装一次；失败时抛出 RuntimeError（stderr 摘要）。
    """
    b = (browser or "firefox").strip().lower()
    # Playwright 的 channel=chrome 仍基于 chromium 内核能力
    if b == "chrome":
        b = "chromium"
    if b not in {"chromium", "firefox", "webkit"}:
        b = "firefox"

    with _lock:
        if b in _installed:
            return
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "playwright", "install", b],
                capture_output=True,
                text=True,
                timeout=600,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"下载 Playwright 浏览器「{b}」超时（600s），"
                "请稍后重试或在部署环境预执行：python -m playwright install " + b
            ) from e

        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()
            if len(tail) > 4000:
                tail = tail[:4000] + "\n…"
            raise RuntimeError(
                f"Playwright 安装浏览器「{b}」失败（退出码 {proc.returncode}）。\n{tail}"
            )
        _installed.add(b)
