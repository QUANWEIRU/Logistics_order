"""
Playwright 与「日常使用的 Chrome」对齐的两种用法：

1) **CDP 附着**（推荐）：本机已用调试端口启动 Chrome 时，通过 connect_over_cdp 在同一浏览器里新开标签页抓取，
   共享 Cookie / 登录态 / 风控白名单，效果最接近你原来那个窗口。
2) **持久化 user-data-dir**：指定独立用户数据目录（勿与正在运行的默认 Chrome 配置目录并发占用），
   用 launch_persistent_context(channel=\"chrome\") 启动。

环境变量（可选，与函数参数同名时参数优先）：
- CHROME_CDP_ENDPOINT：例如 http://127.0.0.1:9222
- CHROME_USER_DATA_DIR：用户数据目录绝对路径
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from playwright_bootstrap import ensure_playwright_browser_installed


class CDPConnectionError(RuntimeError):
    """无法附着到本机 Chrome 远程调试端口（常见于未启动或未监听该端口）。"""


def _cdp_refused_help(endpoint: str) -> str:
    """生成 CDP 连接被拒绝时的中文说明（macOS 路径为主，附通用提示）。"""
    return (
        f"无法通过 CDP 连接到 Chrome（已填写地址：**{endpoint}**）。"
        "常见原因是：**本机尚未用远程调试端口启动 Chrome**，或对应该端口的进程未监听。\n\n"
        "**macOS 示例**（请先完全退出 Chrome，再在终端执行）：\n"
        '`"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" '
        '--remote-debugging-port=9222`\n\n'
        "Chrome 打开后，保持窗口不关，再回到本页点击「开始查询」。"
        "若命令里换了端口，请把侧边栏 **Chrome CDP 地址** 改成同一端口（如 `http://127.0.0.1:9333`）。\n\n"
        "**若不需要附着本机 Chrome**：清空侧边栏里的 **Chrome CDP 地址**，"
        "保存后将使用 Playwright 自带的浏览器内核（无需先开 Chrome）。"
    )


def _strip_or_none(v: str | None) -> str | None:
    s = (v or "").strip()
    return s or None


def resolve_chrome_cdp_endpoint(explicit: str | None = None) -> str | None:
    return _strip_or_none(explicit) or _strip_or_none(os.environ.get("CHROME_CDP_ENDPOINT"))


def resolve_chrome_user_data_dir(explicit: str | None = None) -> str | None:
    v = _strip_or_none(explicit) or _strip_or_none(os.environ.get("CHROME_USER_DATA_DIR"))
    if v and v.startswith("~"):
        v = os.path.expanduser(v)
    return v


@contextmanager
def playwright_page_session(
    p: Any,
    *,
    browser: str = "chrome",
    headless: bool = True,
    chrome_cdp_endpoint: str | None = None,
    chrome_user_data_dir: str | None = None,
) -> Iterator[Any]:
    """
    产出单个 Playwright Page，并在退出时做力所能及的安全清理。

    - CDP：关闭本逻辑新建的标签页并断开连接（不退出你本机的 Chrome）。
    - 持久化目录：关闭本逻辑新建的标签页并关闭 Playwright 启动的浏览器实例。
    - 普通 launch：关闭浏览器。
    """
    b = (browser or "chrome").strip().lower()
    cdp = resolve_chrome_cdp_endpoint(chrome_cdp_endpoint)
    udd = resolve_chrome_user_data_dir(chrome_user_data_dir)

    chrome_family = b in {"chrome", "chromium"}
    use_cdp = bool(cdp) and chrome_family
    use_udd = bool(udd) and chrome_family

    if not use_cdp and not use_udd:
        ensure_playwright_browser_installed(b)

    page: Any = None
    browser_inst: Any = None
    context_inst: Any = None
    cdp_browser: Any = None

    try:
        if use_cdp:
            try:
                cdp_browser = p.chromium.connect_over_cdp(cdp)
            except Exception as e:
                low = str(e).lower()
                if (
                    "econnrefused" in low
                    or "connection refused" in low
                    or "failed to connect" in low
                ):
                    raise CDPConnectionError(_cdp_refused_help(cdp)) from e
                raise
            ctx = cdp_browser.contexts[0] if cdp_browser.contexts else cdp_browser.new_context()
            page = ctx.new_page()
        elif use_udd:
            context_inst = p.chromium.launch_persistent_context(
                user_data_dir=udd,
                channel="chrome",
                headless=headless,
                args=[
                    "--disable-http2",
                    "--disable-blink-features=AutomationControlled",
                ],
                ignore_default_args=["--enable-automation"],
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = context_inst.new_page()
        else:
            launch_kwargs: dict[str, Any] = {"headless": headless}
            use_chromium_family = b in {"chrome", "chromium"}
            if use_chromium_family:
                launch_kwargs["args"] = [
                    "--disable-http2",
                    "--disable-blink-features=AutomationControlled",
                ]
                launch_kwargs["ignore_default_args"] = ["--enable-automation"]
                if b == "chrome":
                    launch_kwargs["channel"] = "chrome"

            if use_chromium_family:
                pw = p.chromium
            elif b == "webkit":
                pw = p.webkit
            else:
                pw = p.firefox

            browser_inst = pw.launch(**launch_kwargs)
            page = browser_inst.new_page(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            )

        yield page
    finally:
        try:
            if page is not None:
                page.close()
        except Exception:
            pass
        try:
            if context_inst is not None:
                context_inst.close()
        except Exception:
            pass
        try:
            if browser_inst is not None:
                browser_inst.close()
        except Exception:
            pass
        try:
            if cdp_browser is not None:
                cdp_browser.close()
        except Exception:
            pass
