"""
FedEx 官网追踪：根据主/输入运单号，解析同一托运下的多件关联单号（如「主货件」+ 其他 12 位运单号）。

多件（MPS）时首页往往只出现主单号，脚本会自动点击「Shipment is x of y pieces」链接，
展开后再从「N Piece Shipment」表格所在页面抽取全部 12 位运单号。

可与本机日常 Chrome 对齐：环境变量 ``CHROME_CDP_ENDPOINT``（如 ``http://127.0.0.1:9222``）或
``CHROME_USER_DATA_DIR``，详见 ``playwright_chrome_session`` 模块。

与 DHL 类似：无官方 API 凭证时使用 Playwright 打开追踪页，从页面文本中提取 12 位 FedEx 运单号。
官方集成见 FedEx Basic Integrated Visibility（需 developer.fedex.com 项目与 OAuth）：
https://developer.fedex.com/api/en-us/catalog/track.html
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from playwright_chrome_session import playwright_page_session

if TYPE_CHECKING:
    from playwright.sync_api import Page

# FedEx Express 常见为 12 位数字运单号（含多件 MPS 场景）
FEDEX_TRACKING_12 = re.compile(r"\b\d{12}\b")
# 英文站：「Shipment is 1 of 5 pieces」；展开后区块标题常见为「5 Piece Shipment」
FEDEX_MPS_LINK = re.compile(
    r"shipment\s+is\s+\d+\s+of\s+(\d+)\s+pieces",
    re.IGNORECASE,
)
FEDEX_PIECE_SECTION = re.compile(r"(\d+)\s+piece\s+shipment", re.IGNORECASE)

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


def parse_trackings_from_fedex_piece_shipment_paste(
    pasted_text: str,
    *,
    master_hint: str | None = None,
) -> list[str]:
    """
    从浏览器里复制的「N Piece Shipment」表格/明细纯文本中解析全部 12 位运单号。

    当无头自动化被 FedEx WAF 拦截时，可在页面上全选表格复制后调用本函数，效果等价于取到各件追踪号。
    master_hint 若给出，则输出顺序为：主单（与 hint 匹配者）优先，其余按数字升序。
    """
    nums = extract_twelve_digit_trackings(pasted_text or "")
    if not nums:
        return []
    if master_hint:
        return order_related_numbers(master_hint, nums)
    return sorted(nums)


def order_related_numbers(master_input: str, found: list[str]) -> list[str]:
    """将用户输入的主单放在首项，其余按数字排序。"""
    master = normalize_fedex_tracking(master_input)
    uniq = list(dict.fromkeys(found))
    rest = sorted(x for x in uniq if x != master)
    if master in uniq:
        return [master, *rest]
    return sorted(uniq)


def infer_multipiece_total_count(page_text: str) -> int | None:
    """
    从页面文案推断多件货总件数（如 Shipment is 1 of 5 pieces / 5 Piece Shipment）。
    无法识别时返回 None。
    """
    t = page_text or ""
    m = FEDEX_MPS_LINK.search(t)
    if m:
        try:
            n = int(m.group(1))
            return n if n >= 2 else None
        except ValueError:
            pass
    m2 = FEDEX_PIECE_SECTION.search(t)
    if m2:
        try:
            n = int(m2.group(1))
            return n if n >= 2 else None
        except ValueError:
            pass
    return None


def _dismiss_fedex_cookie_banner(page: Page) -> None:
    """关闭 FedEx Cookie 横幅，避免遮挡可点击区域。"""
    try:
        btn = page.get_by_role("button", name=re.compile(r"accept\s+all\s+cookies", re.I))
        if btn.count() > 0:
            btn.first.click(timeout=4000)
            page.wait_for_timeout(400)
    except Exception:
        pass


def _fedex_waf_or_block_blob(blob: str) -> bool:
    """判断是否为 FedEx WAF/拒绝页（无有效追踪正文）。"""
    t = (blob or "").lower()
    if not t.strip():
        return False
    needles = (
        "don't have permission",
        "do not have permission",
        "can't process your request",
        "cannot process your request",
        "incident number",
        "system down",
        "无权限",
        "无法处理您的请求",
    )
    return any(n in t for n in needles)


def _multipiece_shipment_locator(page: Page):
    """
    定位「多件货」入口（英文站链接文案为主；部分 UI 用宽松 a11y 名称）。
    返回首个匹配的 Locator，找不到则 None。
    """
    candidates = [
        page.get_by_role(
            "link",
            name=re.compile(r"shipment\s+is\s+\d+\s+of\s+\d+\s+pieces", re.I),
        ),
        page.get_by_role(
            "link",
            name=re.compile(r"\d+\s+of\s+\d+\s+pieces", re.I),
        ),
        page.locator("a").filter(
            has_text=re.compile(r"shipment\s+is\s+\d+\s+of\s+\d+\s+pieces", re.I)
        ),
        page.locator("a").filter(has_text=re.compile(r"\d+\s+of\s+\d+\s+pieces", re.I)),
        # 中文 FedEx 常见：「共 5 件货件」类（措辞因站点版本可能略有出入）
        page.locator("a, button").filter(has_text=re.compile(r"\d+\s*件\s*货件|共\s*\d+\s*件", re.I)),
    ]
    for loc in candidates:
        try:
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def _try_click_multipiece_shipment_link(page: Page) -> bool:
    """
    点击「Shipment is x of y pieces」类链接，展开多件货明细（页面上才会出现全部子单号）。
    返回是否发生了点击。
    """
    try:
        link = _multipiece_shipment_locator(page)
        if link is None or link.count() == 0:
            return False
        link.first.click(timeout=8000)
        page.wait_for_timeout(1500)
        return True
    except Exception:
        return False


def fetch_fedex_related_tracking_scrape(
    tracking_number: str,
    *,
    headless: bool = True,
    browser: str = "chrome",
    chrome_cdp_endpoint: str | None = None,
    chrome_user_data_dir: str | None = None,
    navigation_timeout_ms: float = 120_000.0,
    poll_interval_ms: float = 2000.0,
    max_poll_rounds: int = 35,
) -> list[str]:
    """打开 FedEx 追踪页，解析页面上的 12 位关联运单号列表。"""
    return fetch_fedex_related_tracking_scrape_batch(
        [tracking_number],
        headless=headless,
        browser=browser,
        chrome_cdp_endpoint=chrome_cdp_endpoint,
        chrome_user_data_dir=chrome_user_data_dir,
        navigation_timeout_ms=navigation_timeout_ms,
        poll_interval_ms=poll_interval_ms,
        max_poll_rounds=max_poll_rounds,
    ).get(normalize_fedex_tracking(tracking_number), [])


def fetch_fedex_related_tracking_scrape_batch(
    tracking_numbers: list[str],
    *,
    headless: bool = True,
    browser: str = "chrome",
    chrome_cdp_endpoint: str | None = None,
    chrome_user_data_dir: str | None = None,
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
            "FedEx 网页抓取需要 Playwright：pip install playwright && playwright install chromium"
        ) from e

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
        with playwright_page_session(
            p,
            browser=browser,
            headless=headless,
            chrome_cdp_endpoint=chrome_cdp_endpoint,
            chrome_user_data_dir=chrome_user_data_dir,
        ) as page:
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

                try:
                    page.wait_for_load_state(
                        "networkidle", timeout=min(45_000.0, navigation_timeout_ms)
                    )
                except Exception:
                    pass
                page.wait_for_timeout(800)
                _dismiss_fedex_cookie_banner(page)

                collected: set[str] = set()
                last_size = -1
                stable_rounds = 0
                multipiece_expanded = False
                multipiece_expand_tries = 0
                for _ in range(max_poll_rounds):
                    blob = ""
                    try:
                        blob = page.inner_text("body")
                    except Exception:
                        pass
                    if not blob or not blob.strip():
                        try:
                            raw_html = page.content()
                            blob = raw_html or ""
                        except Exception:
                            blob = ""

                    if _fedex_waf_or_block_blob(blob):
                        break

                    nums = extract_twelve_digit_trackings(blob)
                    collected.update(nums)
                    size = len(collected)
                    if size == last_size:
                        stable_rounds += 1
                    else:
                        stable_rounds = 0
                        last_size = size

                    expected_total = infer_multipiece_total_count(blob)
                    mps_loc = _multipiece_shipment_locator(page)
                    try:
                        mps_cnt = mps_loc.count() if mps_loc is not None else 0
                    except Exception:
                        mps_cnt = 0

                    # 多件货：SPA 可能先渲染「x of y」链接再写入主单正文，故不强制要求 tn 已在 collected
                    if (
                        not multipiece_expanded
                        and mps_cnt > 0
                        and multipiece_expand_tries < 3
                    ):
                        need = expected_total if expected_total is not None else 2
                        if len(collected) < need:
                            multipiece_expand_tries += 1
                            if _try_click_multipiece_shipment_link(page):
                                multipiece_expanded = True
                                stable_rounds = 0

                    if tn in collected:
                        if expected_total is not None:
                            if len(collected) >= expected_total:
                                break
                        elif len(collected) >= 2 or stable_rounds >= 2:
                            break
                    else:
                        # 正文迟迟无单号：避免空转耗尽 max_poll_rounds（Firefox 无头常见）
                        if stable_rounds >= 5 and not blob.strip():
                            break
                        if stable_rounds >= 3 and _fedex_waf_or_block_blob(blob):
                            break

                    page.wait_for_timeout(int(poll_interval_ms))

                results[tn] = order_related_numbers(tn, list(collected)) if collected else []
                if on_waybill_done:
                    on_waybill_done(tn, time.perf_counter() - t0)

    return results


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python fedex_tracking.py <FedEx12位单号>", file=sys.stderr)
        raise SystemExit(2)
    for line in fetch_fedex_related_tracking_scrape(sys.argv[1], headless=True):
        print(line)
