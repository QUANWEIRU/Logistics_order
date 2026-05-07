"""
根据 DHL 运单号（Waybill）解析 Express 子单号（Piece ID，网页「货物详情」中形如 JD 开头的件 ID）。

说明：
- 无 API 时：用 Playwright 打开官网追踪页，监听 /utapi JSON 或从页面文本中正则提取 JD 子单号（依赖真实浏览器环境，部分网络需关闭代理或改用 --headed）。
- 有 DHL_API_KEY 时：可调用 Unified Tracking API（更稳定），见 fetch_piece_ids_unified_api。
"""

from __future__ import annotations

import os
import re
import socket
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import httpx

from playwright_chrome_session import playwright_page_session

# DHL Express 子单号：网页示例为 JD + 18 位数字；保留略宽模式以兼容变化
PIECE_ID_RE_STRICT = re.compile(r"\bJD\d{18}\b")
PIECE_ID_RE_LOOSE = re.compile(r"\bJD[0-9A-Z]{10,32}\b")

UNIFIED_API_BASE = "https://api-eu.dhl.com/track/shipments"

# 中国区简体中文追踪页路径（与浏览器地址栏一致）
DEFAULT_TRACKING_LOCALE = "cn-zh"


def build_tracking_page_url(
    tracking_id: str,
    *,
    locale_path: str = DEFAULT_TRACKING_LOCALE,
    submit: bool = True,
) -> str:
    """组装官网追踪 URL（带 tracking-id，便于整页自动查询）。"""
    # 保留运单号中常见的连字符等，避免误编码
    tid = quote(tracking_id, safe="-_.")
    q = f"tracking-id={tid}"
    if submit:
        q += "&submit=1"
    return f"https://www.dhl.com/{locale_path}/home/tracking.html?{q}"


def parse_tracking_id(url_or_number: str) -> str:
    """从完整追踪 URL 或纯数字/字母运单号中取出 trackingNumber。"""
    raw = (url_or_number or "").strip()
    if not raw:
        raise ValueError("运单号为空")

    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlparse(raw)
        qs = parse_qs(parsed.query)
        for key in ("tracking-id", "trackingId", "tracking_id", "trackingnumber"):
            if key in qs and qs[key]:
                return qs[key][0].strip()
        # 部分链接仅在路径中带单号，保守取路径最后一段（若像运单号）
        seg = parsed.path.rstrip("/").split("/")[-1]
        if seg and re.fullmatch(r"[A-Za-z0-9-]{4,50}", seg):
            return seg
        raise ValueError("无法从 URL 中解析 tracking-id 参数")

    return raw


def _walk_collect_piece_ids(obj: Any, out: set[str], loose: bool) -> None:
    """深度遍历 JSON，用正则抓取 Piece ID 字符串。"""
    pat = PIECE_ID_RE_LOOSE if loose else PIECE_ID_RE_STRICT

    if isinstance(obj, str):
        for m in pat.finditer(obj):
            out.add(m.group(0))
    elif isinstance(obj, dict):
        # 常见字段名直接收集（若值为字符串或列表）
        for key in (
            "pieceIds",
            "pieceId",
            "piece_ids",
            "id",
        ):
            if key not in obj:
                continue
            v = obj[key]
            if isinstance(v, str):
                for m in pat.finditer(v):
                    out.add(m.group(0))
            elif isinstance(v, list):
                for item in v:
                    _walk_collect_piece_ids(item, out, loose)

        for v in obj.values():
            _walk_collect_piece_ids(v, out, loose)
    elif isinstance(obj, list):
        for item in obj:
            _walk_collect_piece_ids(item, out, loose)


def extract_piece_ids_from_tracking_json(data: Any, loose: bool = False) -> list[str]:
    """从 Unified Tracking API（或结构相近）的 JSON 中提取子单号列表。"""
    found: set[str] = set()
    _walk_collect_piece_ids(data, found, loose=loose)
    return sorted(found)


def fetch_piece_ids_unified_api(
    tracking_number: str,
    *,
    api_key: str | None = None,
    service: str = "express",
    requester_country_code: str = "CN",
    origin_country_code: str | None = None,
    timeout: float = 45.0,
    trust_env: bool = False,
) -> list[str]:
    """
    调用 DHL Shipment Tracking – Unified API 获取子单号。

    api_key 默认读取环境变量 DHL_API_KEY。
    trust_env：设为 False 可避免本机 HTTP(S)_PROXY 导致超时或异常链路。
    """
    key = api_key or os.environ.get("DHL_API_KEY")
    if not key:
        raise ValueError(
            "缺少 API 密钥：请传入 api_key=... 或设置环境变量 DHL_API_KEY（见 developer.dhl.com）"
        )

    tid = parse_tracking_id(tracking_number)
    params: dict[str, str] = {
        "trackingNumber": tid,
        "service": service,
        "requesterCountryCode": requester_country_code,
    }
    if origin_country_code:
        params["originCountryCode"] = origin_country_code

    headers = {
        "Accept": "application/json",
        "DHL-API-Key": key,
    }

    with httpx.Client(http2=False, timeout=timeout, trust_env=trust_env) as client:
        r = client.get(UNIFIED_API_BASE, params=params, headers=headers)

    if r.status_code == 401:
        raise RuntimeError("DHL API 返回 401：请检查 DHL_API_KEY 是否有效")
    if r.status_code != 200:
        raise RuntimeError(f"DHL API 错误 HTTP {r.status_code}: {r.text[:500]}")

    data = r.json()
    ids = extract_piece_ids_from_tracking_json(data, loose=False)
    if not ids:
        ids = extract_piece_ids_from_tracking_json(data, loose=True)
    return ids


def fetch_piece_ids_auto(url_or_number: str) -> list[str]:
    """
    若存在环境变量 DHL_API_KEY 则走官方 API，否则与 fetch_piece_ids_scrape 相同。
    """
    if os.environ.get("DHL_API_KEY"):
        return fetch_piece_ids_unified_api(url_or_number)
    return fetch_piece_ids_scrape(url_or_number)


# ─── CDP 探测：18800 可达时优先复用 OpenClaw 风格的 dhl_tracker ────


def _parse_endpoint_host_port(endpoint: str) -> tuple[str, int] | None:
    """从 ``http(s)://host:port[/...]`` 中提取 (host, port)，失败返回 None。"""
    try:
        u = urlparse(endpoint)
        host = u.hostname or ""
        port = u.port or (443 if u.scheme == "https" else 80)
        return (host, int(port)) if host else None
    except (TypeError, ValueError):
        return None


def _cdp_endpoint_reachable(endpoint: str | None, *, timeout: float = 0.4) -> bool:
    """快速 TCP 探测：默认 ~400ms 超时；不发 HTTP 请求避免被代理影响。"""
    s = (endpoint or "").strip()
    if not s:
        return False
    hp = _parse_endpoint_host_port(s)
    if not hp:
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex(hp) == 0
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _resolve_cdp_for_fast_path(explicit: str | None) -> str | None:
    """
    解析「快速通道」最终用的 CDP 端点：
    - 用户显式传值 → 直接采用（不做端口探测，由 dhl_tracker 内部报错处理）；
    - 环境变量 ``CHROME_CDP_ENDPOINT`` 有值 → 采用；
    - 否则若本机默认端口 18800 已开 → 采用 ``http://127.0.0.1:18800``；
    - 都不满足 → 返回 None（回退原 scrape 流程）。
    """
    s = (explicit or "").strip()
    if s:
        return s
    s = (os.environ.get("CHROME_CDP_ENDPOINT") or "").strip()
    if s:
        return s
    default = "http://127.0.0.1:18800"
    return default if _cdp_endpoint_reachable(default) else None


def _fetch_piece_ids_via_dhl_tracker(
    tracking: str,
    *,
    cdp_endpoint: str | None,
) -> list[str]:
    """调用 dhl_tracker.track 并取 ``piece_ids``；任何异常都向上抛出。"""
    from dhl_tracker import track

    s = track(tracking, cdp_endpoint=cdp_endpoint)
    return list(s.piece_ids)


def fetch_piece_ids_scrape(
    url_or_number: str,
    *,
    locale_path: str = DEFAULT_TRACKING_LOCALE,
    headless: bool = True,
    browser: str = "firefox",
    chrome_cdp_endpoint: str | None = None,
    chrome_user_data_dir: str | None = None,
    navigation_timeout_ms: float = 120_000.0,
    poll_interval_ms: float = 1500.0,
    max_poll_rounds: int = 40,
) -> list[str]:
    """
    无 API 密钥时：启动浏览器打开 DHL 追踪页，从 utapi 接口响应或 DOM 中提取 JD 子单号。

    优化路径：若 CDP 端点可达（用户显式传值 / 环境变量 / 本机默认 18800），
    优先调用 ``dhl_tracker``——直接复用已通过 Akamai 验证的本机 Chrome，速度更快、抗风控。
    失败时再回退到下方的 firefox 兜底流程。

    需安装: pip install playwright && playwright install firefox
    browser: chromium | firefox | webkit（若 Chromium 报 HTTP2 错误可换 firefox/webkit）
    """
    cdp_fast = _resolve_cdp_for_fast_path(chrome_cdp_endpoint)
    if cdp_fast and not chrome_user_data_dir:
        try:
            return _fetch_piece_ids_via_dhl_tracker(url_or_number, cdp_endpoint=cdp_fast)
        except Exception:
            # CDP 路径失败（端口未开 / 浏览器未通过验证 / 其它）：回退到原浏览器抓取流程
            pass

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise ImportError(
            "无 API 抓取需要 Playwright：pip install playwright && playwright install firefox"
        ) from e

    tid = parse_tracking_id(url_or_number)
    if url_or_number.strip().startswith("http"):
        start_url = url_or_number.strip()
    else:
        start_url = build_tracking_page_url(tid, locale_path=locale_path)

    api_json_hits: list[Any] = []

    def _on_response(resp: Any) -> None:
        try:
            u = resp.url
            if "/utapi" not in u:
                return
            if resp.status != 200:
                return
            ct = (resp.headers.get("content-type") or "").lower()
            if "json" not in ct:
                return
            api_json_hits.append(resp.json())
        except Exception:
            pass

    with sync_playwright() as p:
        with playwright_page_session(
            p,
            browser=browser,
            headless=headless,
            chrome_cdp_endpoint=chrome_cdp_endpoint,
            chrome_user_data_dir=chrome_user_data_dir,
        ) as page:
            page.on("response", _on_response)
            _goto_tracking_page(page, start_url, navigation_timeout_ms)
            found_set = _poll_piece_ids_on_page(
                page,
                api_json_hits,
                poll_interval_ms=poll_interval_ms,
                max_poll_rounds=max_poll_rounds,
            )

    return sorted(found_set)


def _goto_tracking_page(page: Any, start_url: str, navigation_timeout_ms: float) -> None:
    """打开追踪页并尝试切到「货物详情」标签。"""
    page.goto(
        start_url,
        wait_until="domcontentloaded",
        timeout=navigation_timeout_ms,
    )
    try:
        tab = page.get_by_role("tab", name=re.compile("货物详情"))
        if tab.count():
            tab.first.click(timeout=5000)
    except Exception:
        pass


def _poll_piece_ids_on_page(
    page: Any,
    api_json_hits: list[Any],
    *,
    poll_interval_ms: float,
    max_poll_rounds: int,
) -> set[str]:
    """在当前已加载的页面上轮询直至解析到子单号或超时。"""
    found: set[str] = set()
    for _ in range(max_poll_rounds):
        for payload in api_json_hits:
            found.update(extract_piece_ids_from_tracking_json(payload, loose=False))
            found.update(extract_piece_ids_from_tracking_json(payload, loose=True))
        html = page.content()
        body_txt = ""
        try:
            body_txt = page.inner_text("body")
        except Exception:
            pass
        for blob in (html, body_txt):
            found.update(PIECE_ID_RE_STRICT.findall(blob))
            if not found:
                found.update(PIECE_ID_RE_LOOSE.findall(blob))
        if found:
            break
        page.wait_for_timeout(int(poll_interval_ms))
    return found


def fetch_piece_ids_scrape_batch(
    tracking_numbers: list[str],
    *,
    locale_path: str = DEFAULT_TRACKING_LOCALE,
    headless: bool = True,
    browser: str = "firefox",
    chrome_cdp_endpoint: str | None = None,
    chrome_user_data_dir: str | None = None,
    navigation_timeout_ms: float = 120_000.0,
    poll_interval_ms: float = 1500.0,
    max_poll_rounds: int = 40,
    dedupe: bool = True,
    on_progress: Any | None = None,
    on_waybill_done: Callable[[str, float], None] | None = None,
) -> dict[str, list[str]]:
    """
    单次浏览器会话内依次查询多个转单号，返回 {运单号: [子单号...]}。

    优化路径：若 CDP 端点可达（用户显式传值 / 环境变量 / 本机默认 18800）且未指定独立用户数据目录，
    优先用 ``dhl_tracker.track_batch`` 复用已通过 Akamai 验证的本机 Chrome；CDP 路径任何项失败时
    自动按需回退到下方的 firefox 兜底流程，仅对未拿到结果的运单重新走原 scrape。

    on_progress: 可选回调 (current_index: int, total: int, waybill: str) -> None，用于 UI 进度条。
    on_waybill_done: 每个运单查询结束后 (标准化运单号, 耗时秒) -> None。
    """
    raw_list = [str(x).strip() for x in tracking_numbers if str(x).strip()]
    if dedupe:
        seen_in: set[str] = set()
        ordered_in: list[str] = []
        for t in raw_list:
            if t not in seen_in:
                seen_in.add(t)
                ordered_in.append(t)
        raw_list = ordered_in

    if not raw_list:
        return {}

    cdp_fast = _resolve_cdp_for_fast_path(chrome_cdp_endpoint)
    if cdp_fast and not chrome_user_data_dir:
        from dhl_tracker import track_batch as _tracker_batch

        try:
            tracker_results = _tracker_batch(
                raw_list,
                cdp_endpoint=cdp_fast,
                on_progress=on_progress,
                on_waybill_done=on_waybill_done,
            )
        except Exception:
            tracker_results = {}

        results: dict[str, list[str]] = {}
        missing: list[str] = []
        for t in raw_list:
            try:
                tid = parse_tracking_id(t)
            except ValueError:
                continue
            sm = tracker_results.get(tid)
            if sm is not None and sm.piece_ids:
                results[tid] = list(sm.piece_ids)
            else:
                missing.append(t)

        if not missing:
            return results

        # 仅对 CDP 通道未取到结果的运单走兜底流程
        fallback = _fetch_piece_ids_scrape_batch_via_playwright(
            missing,
            locale_path=locale_path,
            headless=headless,
            browser=browser,
            chrome_cdp_endpoint=chrome_cdp_endpoint,
            chrome_user_data_dir=chrome_user_data_dir,
            navigation_timeout_ms=navigation_timeout_ms,
            poll_interval_ms=poll_interval_ms,
            max_poll_rounds=max_poll_rounds,
            dedupe=False,
            on_progress=None,
            on_waybill_done=on_waybill_done,
        )
        results.update(fallback)
        return results

    return _fetch_piece_ids_scrape_batch_via_playwright(
        raw_list,
        locale_path=locale_path,
        headless=headless,
        browser=browser,
        chrome_cdp_endpoint=chrome_cdp_endpoint,
        chrome_user_data_dir=chrome_user_data_dir,
        navigation_timeout_ms=navigation_timeout_ms,
        poll_interval_ms=poll_interval_ms,
        max_poll_rounds=max_poll_rounds,
        dedupe=False,
        on_progress=on_progress,
        on_waybill_done=on_waybill_done,
    )


def _fetch_piece_ids_scrape_batch_via_playwright(
    tracking_numbers: list[str],
    *,
    locale_path: str = DEFAULT_TRACKING_LOCALE,
    headless: bool = True,
    browser: str = "firefox",
    chrome_cdp_endpoint: str | None = None,
    chrome_user_data_dir: str | None = None,
    navigation_timeout_ms: float = 120_000.0,
    poll_interval_ms: float = 1500.0,
    max_poll_rounds: int = 40,
    dedupe: bool = True,
    on_progress: Any | None = None,
    on_waybill_done: Callable[[str, float], None] | None = None,
) -> dict[str, list[str]]:
    """原 firefox 兜底流程：单浏览器会话内串行查询多单。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise ImportError(
            "无 API 抓取需要 Playwright：pip install playwright && playwright install firefox"
        ) from e

    raw_list = [str(x).strip() for x in tracking_numbers if str(x).strip()]
    if dedupe:
        seen: set[str] = set()
        ordered: list[str] = []
        for t in raw_list:
            if t not in seen:
                seen.add(t)
                ordered.append(t)
        raw_list = ordered

    if not raw_list:
        return {}

    results: dict[str, list[str]] = {}
    api_json_hits: list[Any] = []

    def _on_response(resp: Any) -> None:
        try:
            u = resp.url
            if "/utapi" not in u:
                return
            if resp.status != 200:
                return
            ct = (resp.headers.get("content-type") or "").lower()
            if "json" not in ct:
                return
            api_json_hits.append(resp.json())
        except Exception:
            pass

    with sync_playwright() as p:
        with playwright_page_session(
            p,
            browser=browser,
            headless=headless,
            chrome_cdp_endpoint=chrome_cdp_endpoint,
            chrome_user_data_dir=chrome_user_data_dir,
        ) as page:
            page.on("response", _on_response)

            total = len(raw_list)
            for i, tid_in in enumerate(raw_list):
                tid = parse_tracking_id(tid_in)
                if on_progress:
                    on_progress(i + 1, total, tid)
                start_url = (
                    tid_in.strip()
                    if tid_in.strip().startswith("http")
                    else build_tracking_page_url(tid, locale_path=locale_path)
                )
                api_json_hits.clear()
                t0 = time.perf_counter()
                try:
                    _goto_tracking_page(page, start_url, navigation_timeout_ms)
                    found_set = _poll_piece_ids_on_page(
                        page,
                        api_json_hits,
                        poll_interval_ms=poll_interval_ms,
                        max_poll_rounds=max_poll_rounds,
                    )
                    results[tid] = sorted(found_set)
                except Exception:
                    results[tid] = []
                if on_waybill_done:
                    on_waybill_done(tid, time.perf_counter() - t0)

    return results


def fetch_piece_ids_batch(
    tracking_numbers: list[str],
    *,
    api_key: str | None = None,
    force_scrape: bool = False,
    scrape_kwargs: dict[str, Any] | None = None,
    on_waybill_done: Callable[[str, float], None] | None = None,
) -> dict[str, list[str]]:
    """
    批量查询：若存在 API 密钥且未 force_scrape，则逐个调用 Unified API；否则使用 scrape_batch。

    on_waybill_done: 每个运单查询结束后回调 (标准化运单号, 耗时秒)。
    """
    raw_list = [str(x).strip() for x in tracking_numbers if str(x).strip()]
    seen: set[str] = set()
    ordered: list[str] = []
    for t in raw_list:
        if t not in seen:
            seen.add(t)
            ordered.append(t)

    key = api_key if api_key is not None else os.environ.get("DHL_API_KEY")
    scrape_kwargs = scrape_kwargs or {}

    if key and not force_scrape:
        out: dict[str, list[str]] = {}
        for t in ordered:
            t0 = time.perf_counter()
            try:
                tid = parse_tracking_id(t)
            except ValueError:
                continue
            try:
                out[tid] = fetch_piece_ids_unified_api(
                    t,
                    api_key=key,
                    service=str(scrape_kwargs.get("service", "express")),
                    requester_country_code=str(scrape_kwargs.get("requester_country", "CN")),
                )
            except Exception:
                out[tid] = []
            if on_waybill_done:
                on_waybill_done(tid, time.perf_counter() - t0)
        return out

    sk = dict(scrape_kwargs)
    if on_waybill_done is not None:
        sk["on_waybill_done"] = on_waybill_done
    return fetch_piece_ids_scrape_batch(ordered, **sk)


def main() -> None:
    import argparse
    import sys

    p = argparse.ArgumentParser(description="从 DHL 运单号解析 Express 子单号（Piece ID）")
    p.add_argument(
        "tracking",
        help="运单号或完整追踪页 URL（含 tracking-id=）",
    )
    p.add_argument(
        "--api-key",
        dest="api_key",
        default=os.environ.get("DHL_API_KEY"),
        help="DHL-API-Key；默认读环境变量 DHL_API_KEY",
    )
    p.add_argument(
        "--service",
        default="express",
        help="Unified API 的 service 参数，默认 express",
    )
    p.add_argument(
        "--requester-country",
        default="CN",
        dest="requester_country",
        help="requesterCountryCode，默认 CN",
    )
    p.add_argument(
        "--scrape",
        action="store_true",
        help="强制使用浏览器抓取（忽略 DHL_API_KEY）",
    )
    p.add_argument(
        "--headed",
        action="store_true",
        help="显示浏览器窗口（便于通过人机验证或调试）",
    )
    p.add_argument(
        "--browser",
        default="firefox",
        choices=("chromium", "firefox", "webkit"),
        help="Playwright 浏览器，默认 firefox（部分环境 Chromium 易触发 HTTP/2 错误）",
    )
    args = p.parse_args()

    use_api = bool(args.api_key) and not args.scrape

    try:
        if use_api:
            ids = fetch_piece_ids_unified_api(
                args.tracking,
                api_key=args.api_key,
                service=args.service,
                requester_country_code=args.requester_country,
            )
        else:
            ids = fetch_piece_ids_scrape(
                args.tracking,
                headless=not args.headed,
                browser=args.browser,
            )
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)
    except ImportError as e:
        print(str(e), file=sys.stderr)
        sys.exit(3)

    if not ids:
        msg = (
            "未找到 JD 子单号。"
            if not use_api
            else "未在 API 响应中找到 JD 子单号（请确认运单为 DHL Express 且响应中含件信息）。"
        )
        print(msg, file=sys.stderr)
        if not use_api:
            print(
                "提示：可尝试 --headed 人工通过验证，或换 --browser webkit/chromium。",
                file=sys.stderr,
            )
        sys.exit(1)

    for pid in ids:
        print(pid)


if __name__ == "__main__":
    main()
