"""
FedEx 官网追踪：根据主/输入运单号，解析同一托运下的多件关联单号（如「主货件」+ 其他 12 位运单号）。

与 DHL 类似：无官方 API 凭证时使用 Playwright 打开追踪页，从页面文本中提取 12 位 FedEx 运单号。
官方集成见 FedEx Basic Integrated Visibility（需 developer.fedex.com 项目与 OAuth）：
https://developer.fedex.com/api/en-us/catalog/track.html
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from playwright_bootstrap import ensure_playwright_browser_installed

# FedEx Express 常见为 12 位数字运单号（含多件 MPS 场景）
FEDEX_TRACKING_12 = re.compile(r"\b\d{12}\b")

# 直接打开 fedextrack 页面，避免首页多步点击
FEDEX_TRACK_BASE = "https://www.fedex.com/fedextrack/?trknbr="


def normalize_fedex_tracking(raw: str) -> str:
    """去除空白；若用户粘贴带空格单号则只保留数字。"""
    s = (raw or "").strip()
    if not s:
        raise ValueError("FedEx 运单号为空")
    digits = re.sub(r"\D", "", s)
    if len(digits) < 10:
        raise ValueError(f"FedEx 运单号过短: {raw!r}")
    if len(digits) > 22:
        raise ValueError(f"FedEx 运单号过长: {raw!r}")
    return digits


def build_fedex_track_url(tracking_number: str) -> str:
    tn = normalize_fedex_tracking(tracking_number)
    return f"{FEDEX_TRACK_BASE}{tn}"


def extract_twelve_digit_trackings(text: str) -> list[str]:
    """从文本中提取不重复的 12 位运单号，保持出现顺序。"""
    seen: set[str] = set()
    out: list[str] = []
    for m in FEDEX_TRACKING_12.finditer(text or ""):
        v = m.group(0)
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def order_related_numbers(master_input: str, found: list[str]) -> list[str]:
    """将用户输入的主单放在首项，其余按数字排序。"""
    master = normalize_fedex_tracking(master_input)
    uniq = list(dict.fromkeys(found))
    rest = sorted(x for x in uniq if x != master)
    if master in uniq:
        return [master, *rest]
    return sorted(uniq)


def fetch_fedex_related_tracking_scrape(
    tracking_number: str,
    *,
    headless: bool = True,
    browser: str = "firefox",
    navigation_timeout_ms: float = 120_000.0,
    poll_interval_ms: float = 2000.0,
    max_poll_rounds: int = 35,
) -> list[str]:
    """打开 FedEx 追踪页，解析页面上的 12 位关联运单号列表。"""
    return fetch_fedex_related_tracking_scrape_batch(
        [tracking_number],
        headless=headless,
        browser=browser,
        navigation_timeout_ms=navigation_timeout_ms,
        poll_interval_ms=poll_interval_ms,
        max_poll_rounds=max_poll_rounds,
    ).get(normalize_fedex_tracking(tracking_number), [])


def fetch_fedex_related_tracking_scrape_batch(
    tracking_numbers: list[str],
    *,
    headless: bool = True,
    browser: str = "firefox",
    navigation_timeout_ms: float = 120_000.0,
    poll_interval_ms: float = 2000.0,
    max_poll_rounds: int = 35,
    dedupe: bool = True,
    on_progress: Callable[[int, int, str], None] | None = None,
    on_waybill_done: Callable[[str, float], None] | None = None,
) -> dict[str, list[str]]:
    """
    单次浏览器会话内依次查询多个 FedEx 运单号。

    返回: {标准化主单号: [主单?, 关联单1, ...]}，首项尽量为本次输入对应单号。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise ImportError(
            "FedEx 网页抓取需要 Playwright：pip install playwright && playwright install firefox"
        ) from e

    ensure_playwright_browser_installed(browser)

    raw_list: list[str] = []
    for x in tracking_numbers:
        try:
            raw_list.append(normalize_fedex_tracking(str(x).strip()))
        except ValueError:
            continue
    if dedupe:
        raw_list = list(dict.fromkeys(raw_list))
    if not raw_list:
        return {}

    results: dict[str, list[str]] = {}

    with sync_playwright() as p:
        launch_kwargs: dict[str, Any] = {"headless": headless}
        if browser == "chromium":
            launch_kwargs["args"] = ["--disable-http2"]

        pw = getattr(p, browser)
        browser_inst = pw.launch(**launch_kwargs)
        page = browser_inst.new_page(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        )
        total = len(raw_list)
        for i, tn in enumerate(raw_list):
            if on_progress:
                on_progress(i + 1, total, tn)
            url = build_fedex_track_url(tn)
            t0 = time.perf_counter()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=navigation_timeout_ms)
            except Exception:
                results[tn] = []
                if on_waybill_done:
                    on_waybill_done(tn, time.perf_counter() - t0)
                continue

            collected: set[str] = set()
            last_size = -1
            stable_rounds = 0
            for _ in range(max_poll_rounds):
                blob = ""
                try:
                    blob = page.inner_text("body")
                except Exception:
                    pass
                if not blob:
                    try:
                        blob = page.content()
                    except Exception:
                        pass
                nums = extract_twelve_digit_trackings(blob)
                collected.update(nums)
                size = len(collected)
                if size == last_size:
                    stable_rounds += 1
                else:
                    stable_rounds = 0
                    last_size = size
                # 已出现多件；或已包含输入单号且页面数字集合稳定（单件也可结束）
                if (tn in collected and len(collected) >= 2) or (
                    tn in collected and stable_rounds >= 2
                ):
                    break
                page.wait_for_timeout(int(poll_interval_ms))

            results[tn] = order_related_numbers(tn, list(collected)) if collected else []
            if on_waybill_done:
                on_waybill_done(tn, time.perf_counter() - t0)

        browser_inst.close()

    return results
