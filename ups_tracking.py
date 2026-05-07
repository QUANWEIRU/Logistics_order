"""
UPS 官网追踪（简体中文）：根据输入运单号解析同一货件下的多件包裹单号（每件一条 1Z…）。

流程与 FedEx 多件类似：打开追踪页 → 输入单号 → 点击「追踪」。
多件时页面会出现折叠块 **「该货件中的其他包裹」**（或「1 / 2 件货件」），需先点击展开，再在该区域及页面全文中提取全部 1Z 子单号。

可与本机 Chrome 对齐：环境变量 ``CHROME_CDP_ENDPOINT``、``CHROME_USER_DATA_DIR``，
详见 ``playwright_chrome_session`` 模块。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from playwright_chrome_session import playwright_page_session

if TYPE_CHECKING:
    from playwright.sync_api import Page

# UPS 常见格式：1Z + 16 位字母数字（合计 18 位）
UPS_TRACKING_1Z = re.compile(r"\b1Z[0-9A-Z]{16}\b", re.IGNORECASE)

# 中文站多件提示（含半角/全角斜杠）
UPS_CN_MULTIPiece_LINK = re.compile(r"\d+\s*[/／]\s*\d+\s*件\s*货件")

UPS_TRACK_HOME = "https://www.ups.com/track?loc=zh_CN&requester=ST/"

# 多件货：子单号常出现在此折叠标题下方（与手工操作一致，优先点击）
OTHER_PACKAGES_HEADING = "该货件中的其他包裹"

# Akamai / 空壳页特征（无有效正文时不应长期空转）
_AKAMAI_BLOCK_HINTS = (
    "akamai",
    "powered and protected",
    "access denied",
    "访问被拒绝",
)


def normalize_ups_tracking(raw: str) -> str:
    """去除空白，统一为大写（UPS 单号不区分大小写）。"""
    s = (raw or "").strip().upper()
    if not s:
        raise ValueError("UPS 运单号为空")
    s = re.sub(r"[\s\-]+", "", s)
    if len(s) < 8:
        raise ValueError(f"UPS 运单号过短: {raw!r}")
    if len(s) > 32:
        raise ValueError(f"UPS 运单号过长: {raw!r}")
    return s


def build_ups_track_home_url() -> str:
    """追踪首页（与浏览器手工流程一致，便于输入后点击追踪）。"""
    return UPS_TRACK_HOME


def build_ups_track_direct_url(tracking_number: str) -> str:
    """
    带单号的追踪 URL（减少首页表单步骤；参数名与 UPS 公开链接惯例一致）。
    若直连打不开详情，可再回退到首页填单。
    """
    tn = normalize_ups_tracking(tracking_number)
    q = quote(tn, safe="")
    return f"{UPS_TRACK_HOME}&tracknum={q}"


def extract_ups_1z_trackings(text: str) -> list[str]:
    """从页面文本中提取不重复的 1Z 单号（大写），保持出现顺序。"""
    seen: set[str] = set()
    out: list[str] = []
    for m in UPS_TRACKING_1Z.finditer(text or ""):
        v = m.group(0).upper()
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def order_related_numbers(master_input: str, found: list[str]) -> list[str]:
    """将用户输入的单号置于首位，其余按字典序排列。"""
    master = normalize_ups_tracking(master_input)
    uniq = list(dict.fromkeys(found))
    rest = sorted(x for x in uniq if x != master)
    if master in uniq:
        return [master, *rest]
    return sorted(uniq)


def infer_multipiece_total_count(page_text: str) -> int | None:
    """
    从页面文案推断多件货总件数（如「1 / 2 件货件」→ 2）。
    若仅有折叠标题「该货件中的其他包裹」而无明确 x/y，仍视为多件（至少 2），以便持续展开/点击。
    """
    t = page_text or ""
    m = re.search(r"\d+\s*[/／]\s*(\d+)\s*件\s*货件", t)
    if m:
        try:
            n = int(m.group(1))
            return n if n >= 2 else None
        except ValueError:
            pass
    if OTHER_PACKAGES_HEADING in t:
        return 2
    return None


def _dismiss_ups_cookie_banner(page: Page) -> None:
    """尝试关闭 Cookie 横幅，避免遮挡点击。"""
    for pattern in (
        r"接受",
        r"同意",
        r"Accept",
        r"同意所有",
    ):
        try:
            btn = page.get_by_role("button", name=re.compile(pattern))
            if btn.count() > 0:
                btn.first.click(timeout=3000)
                page.wait_for_timeout(400)
                return
        except Exception:
            continue


def _multipiece_locator(page: Page):
    """定位「x / y 件货件」链接或按钮（accessible name 可能与正文略有差异，放宽匹配）。"""
    loose = re.compile(r"\d+\s*[/／]\s*\d+.*件\s*货件")
    candidates = [
        page.get_by_role("link", name=UPS_CN_MULTIPiece_LINK),
        page.get_by_role("button", name=UPS_CN_MULTIPiece_LINK),
        page.locator("a, button").filter(has_text=UPS_CN_MULTIPiece_LINK),
        page.locator("a").filter(has_text=loose),
        page.locator("button").filter(has_text=loose),
        page.get_by_text(loose),
    ]
    for loc in candidates:
        try:
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def _other_packages_section_locator(page: Page):
    """定位「该货件中的其他包裹」可点击区域（旧版选择器，作兜底）。"""
    patterns = [
        re.compile(r"^该货件中的其他包裹"),
        re.compile(r"该货件中的其他包裹"),
    ]
    for pat in patterns:
        try:
            loc = page.locator(
                "button, [role='button'], a, div[role='button'], span[role='button']"
            ).filter(has_text=pat)
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def _expand_other_packages_panel(page: Page) -> bool:
    """
    点击「该货件中的其他包裹」以展开子单列表（一件以上时 UPS 会显示此标题）。
    使用多种定位方式，避免 accessible name 与纯文本不一致导致点不到。
    """
    strategies: list = [
        # 完全匹配标题的按钮/链接
        lambda: page.get_by_role("button", name=re.compile(r"该货件中的其他包裹")),
        lambda: page.get_by_role("link", name=re.compile(r"该货件中的其他包裹")),
        lambda: page.get_by_text(OTHER_PACKAGES_HEADING, exact=True),
        lambda: page.get_by_text(re.compile(r"该货件中的\s*其他包裹")),
        lambda: page.locator(f"text={OTHER_PACKAGES_HEADING}"),
        lambda: page.locator("*").filter(
            has_text=re.compile(r"该货件中的其他包裹")
        ),
        lambda: _other_packages_section_locator(page),
    ]
    for factory in strategies:
        try:
            loc = factory()
            if loc is None:
                continue
            if hasattr(loc, "count") and loc.count() == 0:
                continue
            loc.first.scroll_into_view_if_needed(timeout=5000)
            loc.first.click(timeout=8000)
            page.wait_for_timeout(1500)
            return True
        except Exception:
            continue
    return False


def _collect_text_after_keyword(page: Page, keyword: str) -> str:
    """从各 frame 正文中，自关键词起截取一段，保证「其他包裹」块内 1Z 被纳入提取范围。"""
    chunks: list[str] = []
    targets: list = [page]
    try:
        targets.extend(list(page.frames))
    except Exception:
        pass
    for fr in targets:
        try:
            s = fr.evaluate(
                """(kw) => {
                  const w = document.body ? document.body.innerText : '';
                  const i = w.indexOf(kw);
                  if (i < 0) return '';
                  return w.slice(i, i + 10000);
                }""",
                keyword,
            )
            if s and str(s).strip():
                chunks.append(str(s))
        except Exception:
            pass
    return "\n".join(chunks)


def _try_click_multipiece_and_expand(page: Page) -> bool:
    """多件货：优先展开「该货件中的其他包裹」，再点「x / y 件货件」链接。"""
    clicked = False
    # 1）用户确认：子单在「该货件中的其他包裹」内，优先展开
    try:
        if _expand_other_packages_panel(page):
            clicked = True
    except Exception:
        pass
    try:
        m = _multipiece_locator(page)
        if m is not None and m.count() > 0:
            m.first.scroll_into_view_if_needed(timeout=5000)
            m.first.click(timeout=8000)
            page.wait_for_timeout(1200)
            clicked = True
    except Exception:
        pass
    # 2）再次尝试仅标题匹配（SPA 重绘后链接偶现）
    if not clicked:
        try:
            sec = _other_packages_section_locator(page)
            if sec is not None and sec.count() > 0:
                sec.first.scroll_into_view_if_needed(timeout=5000)
                sec.first.click(timeout=6000)
                page.wait_for_timeout(1200)
                clicked = True
        except Exception:
            pass
    return clicked


def _is_mostly_empty_or_blocked(blob: str) -> bool:
    """判断是否几乎无正文或为 Akamai/拦截占位页。"""
    t = (blob or "").strip().lower()
    if len(t) < 100:
        return True
    return any(h in t for h in _AKAMAI_BLOCK_HINTS)


def _gather_page_text_and_html(page: Page) -> str:
    """合并主帧、子帧 innerText 与 HTML，便于 SPA/ iframe 内取单号。"""
    parts: list[str] = []
    try:
        parts.append(page.inner_text("body"))
    except Exception:
        pass
    try:
        for fr in page.frames:
            try:
                parts.append(
                    fr.evaluate(
                        "() => document.body ? document.body.innerText : ''"
                    )
                    or ""
                )
            except Exception:
                pass
    except Exception:
        pass
    try:
        parts.append(page.content() or "")
    except Exception:
        pass
    # 多件：标题「该货件中的其他包裹」以下正文常被折叠，单独截取增强 1Z 命中
    try:
        tail = _collect_text_after_keyword(page, OTHER_PACKAGES_HEADING)
        if tail.strip():
            parts.append(tail)
    except Exception:
        pass
    return "\n".join(p for p in parts if p)


def _walk_json_for_1z(obj: Any, out: list[str]) -> None:
    """递归遍历 JSON，收集字符串中的 1Z 单号。"""
    if isinstance(obj, str):
        for m in UPS_TRACKING_1Z.finditer(obj):
            out.append(m.group(0).upper())
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk_json_for_1z(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_json_for_1z(v, out)


def _install_ups_response_sniffer(page: Page, bucket: list[str]) -> None:
    """监听 UPS 域 JSON 响应，补充页面上未渲染的单号（每个 Page 只注册一次）。"""

    def _on_response(resp: Any) -> None:
        try:
            if resp.status != 200:
                return
            u = (resp.url or "").lower()
            if "ups.com" not in u:
                return
            ct = (resp.headers.get("content-type") or "").lower()
            if "json" not in ct:
                return
            try:
                txt = resp.text()
            except Exception:
                return
            if not txt or "1z" not in txt.lower():
                return
            for m in UPS_TRACKING_1Z.finditer(txt):
                bucket.append(m.group(0).upper())
            try:
                _walk_json_for_1z(json.loads(txt), bucket)
            except Exception:
                pass
        except Exception:
            pass

    page.on("response", _on_response)


def _fill_home_form_and_click_track(page: Page, tn: str) -> None:
    """在追踪首页输入单号并点击「追踪」（不使用 networkidle，避免长时间卡在分析脚本）。"""
    filled = False
    for locator_factory in (
        lambda: page.get_by_placeholder(re.compile(r"追踪编号|Tracking", re.I)),
        lambda: page.get_by_label(re.compile(r"追踪编号|Tracking", re.I)),
        lambda: page.locator(
            'textarea[name*="track"], input[name*="track"], '
            '#trackNums, input[id*="track"], textarea[id*="track"]'
        ),
    ):
        try:
            box = locator_factory()
            if hasattr(box, "count") and box.count() > 0:
                box.first.click(timeout=5000)
                box.first.fill("", timeout=3000)
                box.first.fill(tn, timeout=5000)
                filled = True
                break
        except Exception:
            continue
    if not filled:
        raise RuntimeError("未找到 UPS 追踪编号输入框（页面结构可能已变更）")
    try:
        page.get_by_role("button", name=re.compile(r"追踪")).first.click(timeout=8000)
    except Exception:
        page.get_by_role("link", name=re.compile(r"追踪")).first.click(timeout=8000)
    page.wait_for_timeout(2500)
    _dismiss_ups_cookie_banner(page)


def _fill_tracking_and_submit(page: Page, tracking_number: str, navigation_timeout_ms: float) -> None:
    """
    优先使用带 tracknum 的直连 URL（省步骤、避免误等 networkidle）。
    若正文过短或疑似拦截页，再回退到首页填表。
    """
    tn = normalize_ups_tracking(tracking_number)
    primary = build_ups_track_direct_url(tn)
    alt = (
        f"https://www.ups.com/track?tracknum={quote(tn, safe='')}"
        f"&loc=zh_CN&requester=ST/"
    )

    for url in (primary, alt):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=navigation_timeout_ms)
        except Exception:
            continue
        page.wait_for_timeout(2000)
        _dismiss_ups_cookie_banner(page)
        blob = _gather_page_text_and_html(page)
        if not _is_mostly_empty_or_blocked(blob):
            try:
                page.wait_for_load_state("load", timeout=12_000)
            except Exception:
                pass
            page.wait_for_timeout(800)
            return
    # 直连均失败：打开首页填单
    page.goto(
        build_ups_track_home_url(),
        wait_until="domcontentloaded",
        timeout=navigation_timeout_ms,
    )
    page.wait_for_timeout(1200)
    _dismiss_ups_cookie_banner(page)
    _fill_home_form_and_click_track(page, tn)


def fetch_ups_package_trackings_scrape(
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
    """打开 UPS 中文追踪页，解析同一货件下的全部 1Z 包裹单号。"""
    return fetch_ups_package_trackings_scrape_batch(
        [tracking_number],
        headless=headless,
        browser=browser,
        chrome_cdp_endpoint=chrome_cdp_endpoint,
        chrome_user_data_dir=chrome_user_data_dir,
        navigation_timeout_ms=navigation_timeout_ms,
        poll_interval_ms=poll_interval_ms,
        max_poll_rounds=max_poll_rounds,
    ).get(normalize_ups_tracking(tracking_number), [])


def fetch_ups_package_trackings_scrape_batch(
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
    单次浏览器会话内依次查询多个 UPS 运单号。

    返回: {标准化单号: [用户单号优先, …]}。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise ImportError(
            "UPS 网页抓取需要 Playwright：pip install playwright && playwright install chromium"
        ) from e

    raw_list: list[str] = []
    for x in tracking_numbers:
        try:
            raw_list.append(normalize_ups_tracking(str(x).strip()))
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
            api_bucket: list[str] = []
            _install_ups_response_sniffer(page, api_bucket)

            total = len(raw_list)
            for i, tn in enumerate(raw_list):
                if on_progress:
                    on_progress(i + 1, total, tn)
                t0 = time.perf_counter()
                collected: set[str] = set()
                api_bucket.clear()
                try:
                    _fill_tracking_and_submit(page, tn, navigation_timeout_ms)
                except Exception:
                    results[tn] = []
                    if on_waybill_done:
                        on_waybill_done(tn, time.perf_counter() - t0)
                    continue

                last_size = -1
                stable_rounds = 0
                multipiece_tries = 0

                for round_i in range(max_poll_rounds):
                    blob = _gather_page_text_and_html(page)

                    if _is_mostly_empty_or_blocked(blob) and round_i >= 2:
                        break

                    nums = extract_ups_1z_trackings(blob)
                    collected.update(nums)
                    collected.update(api_bucket)

                    size = len(collected)
                    if size == last_size:
                        stable_rounds += 1
                    else:
                        stable_rounds = 0
                        last_size = size

                    expected_total = infer_multipiece_total_count(blob)
                    mps_loc = _multipiece_locator(page)
                    try:
                        mps_cnt = mps_loc.count() if mps_loc is not None else 0
                    except Exception:
                        mps_cnt = 0

                    need = expected_total if expected_total is not None else 2
                    if (mps_cnt > 0 or expected_total is not None) and multipiece_tries < 10:
                        if len(collected) < need:
                            multipiece_tries += 1
                            _try_click_multipiece_and_expand(page)
                            stable_rounds = 0

                    # 已拿到主单号且件数满足或页面稳定，结束
                    if tn in collected:
                        if expected_total is not None:
                            if len(collected) >= expected_total and stable_rounds >= 1:
                                break
                        elif len(collected) >= 2 and stable_rounds >= 2:
                            break
                        elif stable_rounds >= 4:
                            # 单件货：仅一条 1Z，轮询稳定后退出
                            break
                    elif stable_rounds >= 10 and len(blob.strip()) < 300:
                        break

                    page.wait_for_timeout(int(poll_interval_ms))

                results[tn] = order_related_numbers(tn, list(collected)) if collected else []
                if on_waybill_done:
                    on_waybill_done(tn, time.perf_counter() - t0)

    return results


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python ups_tracking.py <UPS1Z单号>", file=sys.stderr)
        raise SystemExit(2)
    for line in fetch_ups_package_trackings_scrape(sys.argv[1], headless=True):
        print(line)
