"""
DHL Express 追踪查询（结构化）— 复用本机已通过 Akamai 验证的 Chrome（CDP）。

核心思路（来自 OpenClaw 实测有效的方案）：
- 不主动调用 UTAPI（直连会被 Cloudflare/Akamai 428/403 拦下，headless 也被识别）；
- 用 ``page.on('response')`` 拦截 DHL 追踪页 JS 自身发起的 ``/utapi`` 成功响应；
- 该响应自带验证后的完整 cookie 与 header，得到结构化 JSON 后直接解析即可。

用法（CLI）::

    python dhl_tracker.py 4191468945              # 格式化输出
    python dhl_tracker.py 4191468945 --json       # 原始 JSON
    python dhl_tracker.py 4191468945 --piece-ids  # 仅打印 JD 子单号

用法（模块）::

    from dhl_tracker import track, track_batch
    s = track("4191468945")                       # → Shipment
    print(s.status_description, s.piece_ids)
    results = track_batch(["4191468945", "..."])  # → {tid: Shipment | None}

前置条件：本机已在端口 18800 启动 Chrome 远程调试，并已在浏览器内通过一次 DHL Akamai 验证。
若使用本项目其它 CDP 端口，可设环境变量 ``CHROME_CDP_ENDPOINT`` 覆盖默认值。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator

from dhl_piece_ids import (
    DEFAULT_TRACKING_LOCALE,
    build_tracking_page_url,
    extract_piece_ids_from_tracking_json,
    parse_tracking_id,
)

# OpenClaw 工作区固定使用 18800；其它环境可通过 CHROME_CDP_ENDPOINT 覆盖
DEFAULT_CDP_PORT = 18800
DEFAULT_CDP_ENDPOINT = f"http://127.0.0.1:{DEFAULT_CDP_PORT}"

# 单页超时与等待 UTAPI 响应的轮询参数
NAV_TIMEOUT_MS = 45_000
WAIT_AFTER_LOAD_MS = 6_000
EXTRA_WAIT_MS = 10_000


@dataclass
class TrackingEvent:
    """单条追踪事件（节点）。"""

    timestamp: str
    location: str
    description: str
    piece_ids: list[str] = field(default_factory=list)


@dataclass
class Shipment:
    """单个 DHL 运单的结构化追踪结果。"""

    tracking_number: str
    status: str  # statusCode：delivered / transit / unknown 等
    status_description: str  # 人类可读描述（zh/en 取决于 ``language``）
    last_update: str
    last_location: str
    origin: str
    destination: str
    product: str
    total_pieces: int
    piece_ids: list[str]
    events: list[TrackingEvent] = field(default_factory=list)
    proof_of_delivery_url: str | None = None
    signature_url: str | None = None
    raw: dict = field(default_factory=dict)


# ─── CDP 端点解析 ───────────────────────────────────────────────────


def resolve_cdp_endpoint(explicit: str | None = None) -> str:
    """
    解析最终使用的 CDP 端点：``explicit`` > ``CHROME_CDP_ENDPOINT`` > 默认 18800。

    本项目里其它模块（``playwright_chrome_session``）也读 ``CHROME_CDP_ENDPOINT``，
    保持一致以便在 Streamlit/CLI/脚本之间共享同一个浏览器会话。
    """
    s = (explicit or "").strip()
    if s:
        return s
    s = (os.environ.get("CHROME_CDP_ENDPOINT") or "").strip()
    return s or DEFAULT_CDP_ENDPOINT


# ─── 主入口 ─────────────────────────────────────────────────────────


def track(
    tracking_number: str,
    *,
    language: str = "zh",
    cdp_endpoint: str | None = None,
) -> Shipment:
    """
    查询单个 DHL 运单，返回结构化 ``Shipment``。

    若无法从 UTAPI 拦到数据（端口未开 / 浏览器未通过验证 / 单号查无结果），抛 ``RuntimeError``。
    """
    raw = _intercept_utapi_single(tracking_number, language=language, cdp_endpoint=cdp_endpoint)
    return _parse_shipment(raw)


def track_batch(
    tracking_numbers: Iterable[str],
    *,
    language: str = "zh",
    cdp_endpoint: str | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
    on_waybill_done: Callable[[str, float], None] | None = None,
) -> dict[str, Shipment | None]:
    """
    批量查询：同一个 CDP 浏览器会话内串行处理，省去重复连接成本。

    返回 ``{标准化运单号: Shipment | None}``；查询失败的项为 ``None``。
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in tracking_numbers:
        try:
            tid = parse_tracking_id(str(raw))
        except ValueError:
            continue
        if tid in seen:
            continue
        seen.add(tid)
        cleaned.append(tid)

    if not cleaned:
        return {}

    out: dict[str, Shipment | None] = {}
    total = len(cleaned)
    with _open_cdp_browser(cdp_endpoint) as browser:
        for i, tid in enumerate(cleaned):
            if on_progress is not None:
                on_progress(i + 1, total, tid)
            t0 = time.perf_counter()
            try:
                raw_data = _intercept_utapi_in_browser(browser, tid, language=language)
                out[tid] = _parse_shipment(raw_data)
            except Exception:
                out[tid] = None
            if on_waybill_done is not None:
                on_waybill_done(tid, time.perf_counter() - t0)
    return out


# ─── CDP 浏览器会话与拦截 ───────────────────────────────────────────


def _cdp_http_get_json(endpoint: str, path: str, timeout: float = 2.0) -> Any:
    """对 CDP HTTP 接口（如 ``/json/version``、``/json/list``）发 GET 并返回 JSON。"""
    url = endpoint.rstrip("/") + path
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return json.loads(r.read().decode("utf-8") or "null")


def _cdp_http_put(endpoint: str, path: str, timeout: float = 2.0) -> None:
    """对 CDP HTTP 接口发 PUT（用于 ``/json/new?<url>`` 创建新标签页）。"""
    url = endpoint.rstrip("/") + path
    req = urllib.request.Request(url, method="PUT")
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        r.read()


def _ensure_cdp_has_page_target(endpoint: str) -> None:
    """
    若 CDP Chrome 当前没有任何 ``page`` 目标，创建一个 ``about:blank`` 兜底。

    背景：Playwright 1.59+ 的 ``connect_over_cdp`` 在握手阶段会调用
    ``Browser.setDownloadBehavior``，但通过 ``--remote-debugging-port`` 启动的
    系统 Chrome 在「无任何 page target」状态下会返回
    ``Browser context management is not supported``，整个连接随即失败。

    只要存在 ≥1 个 page target，握手立即恢复正常。本函数静默兜底，
    对调用者透明，且不影响 OpenClaw 工作流（其浏览器内始终有 DHL 标签）。
    """
    try:
        targets = _cdp_http_get_json(endpoint, "/json/list", timeout=1.5) or []
    except (urllib.error.URLError, ValueError, OSError):
        # 端口不通 / 返回非 JSON：交给后续 connect_over_cdp 走正常报错路径
        return

    if any(isinstance(t, dict) and t.get("type") == "page" for t in targets):
        return

    try:
        _cdp_http_put(endpoint, "/json/new?about:blank", timeout=2.0)
        # 给 Chrome 一点时间把 target 注册到协议层
        time.sleep(0.2)
    except (urllib.error.URLError, OSError):
        # 创建失败也继续：让 connect_over_cdp 抛真实错误给上层
        pass


def _diagnose_cdp_endpoint(endpoint: str) -> str:
    """生成连接失败时的诊断说明。"""
    try:
        ver = _cdp_http_get_json(endpoint, "/json/version", timeout=1.0) or {}
        browser = ver.get("Browser", "<unknown>") if isinstance(ver, dict) else "<unknown>"
        targets_alive = True
    except (urllib.error.URLError, ValueError, OSError):
        return (
            f"  - HTTP 探测 {endpoint}/json/version 失败：端口未监听或无响应。\n"
            "  - 请用如下命令启动 Chrome（macOS 示例）：\n"
            '    `/Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome '
            "--remote-debugging-port=18800`"
        )

    try:
        targets = _cdp_http_get_json(endpoint, "/json/list", timeout=1.0) or []
    except (urllib.error.URLError, ValueError, OSError):
        targets = []
        targets_alive = False

    page_count = sum(1 for t in targets if isinstance(t, dict) and t.get("type") == "page")
    return (
        f"  - 端口探测 OK：{browser}\n"
        f"  - 当前 page 目标数：{page_count}（targets 列表 {'可读取' if targets_alive else '读取失败'}）\n"
        "  - 请在浏览器内手动打开一次 https://www.dhl.com/cn-zh/home/tracking.html "
        "通过 Akamai 验证后再试。"
    )


@contextmanager
def _open_cdp_browser(cdp_endpoint: str | None = None) -> Iterator[Any]:
    """
    连接到本机 Chrome（CDP），退出时只断开连接、不关闭真实浏览器。

    连接前会自动确保 Chrome 至少有一个 page target，规避 Playwright 1.59+
    在「空 Chrome 实例」上握手 ``Browser.setDownloadBehavior`` 失败的问题。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise ImportError(
            "需要 Playwright：pip install playwright && python -m playwright install chromium"
        ) from e

    endpoint = resolve_cdp_endpoint(cdp_endpoint)
    _ensure_cdp_has_page_target(endpoint)

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(endpoint, timeout=10_000)
        except Exception as e:
            diag = _diagnose_cdp_endpoint(endpoint)
            raise RuntimeError(
                f"无法通过 CDP 连接到 Chrome：{endpoint}\n"
                f"{diag}\n"
                f"  - 原始错误：{type(e).__name__}: {str(e)[:200]}"
            ) from e
        try:
            yield browser
        finally:
            try:
                browser.close()
            except Exception:
                pass


def _intercept_utapi_single(
    tracking_number: str,
    *,
    language: str = "zh",
    cdp_endpoint: str | None = None,
) -> dict:
    """单单查询入口：自管 CDP 连接。"""
    tid = parse_tracking_id(tracking_number)
    with _open_cdp_browser(cdp_endpoint) as browser:
        return _intercept_utapi_in_browser(browser, tid, language=language)


def _intercept_utapi_in_browser(browser: Any, tracking_number: str, *, language: str) -> dict:
    """在已连接的 CDP 浏览器中新开一个标签页拦截 UTAPI 响应。"""
    locale = DEFAULT_TRACKING_LOCALE if language.lower().startswith("zh") else "global-en"
    track_url = build_tracking_page_url(tracking_number, locale_path=locale)

    utapi_data: dict = {}
    contexts = browser.contexts
    ctx = contexts[0] if contexts else browser.new_context()
    page = ctx.new_page()

    def _capture(resp: Any) -> None:
        nonlocal utapi_data
        if utapi_data:
            return
        try:
            url = resp.url
            if "/utapi" not in url or resp.status != 200:
                return
            try:
                utapi_data = resp.json()
            except Exception:
                pass
        except Exception:
            pass

    page.on("response", _capture)

    try:
        page.goto(track_url, timeout=NAV_TIMEOUT_MS, wait_until="load")
        page.wait_for_timeout(WAIT_AFTER_LOAD_MS)

        if not utapi_data:
            try:
                page.wait_for_function(
                    "() => { const p = performance.getEntriesByType('resource'); "
                    "return p.some(r => r.name.includes('utapi')); }",
                    timeout=EXTRA_WAIT_MS,
                )
                page.wait_for_timeout(2_000)
            except Exception:
                pass
    finally:
        try:
            page.close()
        except Exception:
            pass

    if not utapi_data:
        raise RuntimeError(
            "无法获取 UTAPI 数据。请确认：\n"
            "  1. Chrome 已开启远程调试（默认端口 18800，可用 CHROME_CDP_ENDPOINT 覆盖）\n"
            "  2. 在浏览器内手动打开过一次 DHL 追踪页并通过 Akamai 验证"
        )

    if isinstance(utapi_data, dict) and "error" in utapi_data:
        raise RuntimeError(f"DHL UTAPI 错误：{utapi_data['error']}")
    return utapi_data


# ─── 解析 ───────────────────────────────────────────────────────────


def _addr_locality(node: Any) -> str:
    """从 ``location.address.addressLocality`` 中安全取值。"""
    if not isinstance(node, dict):
        return ""
    return str(
        node.get("location", {}).get("address", {}).get("addressLocality", "")
        if "location" in node
        else node.get("address", {}).get("addressLocality", "")
    )


def _parse_shipment(raw: dict) -> Shipment:
    shipments = raw.get("shipments", [])
    if not shipments:
        raise ValueError("DHL UTAPI 返回 shipments 为空（运单可能不存在）")

    s = shipments[0]
    st = s.get("status", {}) or {}
    det = s.get("details", {}) or {}

    events: list[TrackingEvent] = []
    for ev in s.get("events", []) or []:
        events.append(
            TrackingEvent(
                timestamp=str(ev.get("timestamp", "") or ""),
                location=_addr_locality(ev),
                description=str(ev.get("description", "") or ""),
                piece_ids=list(ev.get("pieceIds", []) or []),
            )
        )

    pod = det.get("proofOfDelivery", {}) or {}

    piece_ids = list(det.get("pieceIds", []) or [])
    if not piece_ids:
        piece_ids = extract_piece_ids_from_tracking_json(raw, loose=False)
        if not piece_ids:
            piece_ids = extract_piece_ids_from_tracking_json(raw, loose=True)

    return Shipment(
        tracking_number=str(s.get("id", "")),
        status=str(st.get("statusCode", "unknown") or "unknown"),
        status_description=str(st.get("description", "未知") or "未知"),
        last_update=str(st.get("timestamp", "") or ""),
        last_location=_addr_locality(st),
        origin=_addr_locality(s.get("origin", {})),
        destination=_addr_locality(s.get("destination", {})),
        product=str((det.get("product", {}) or {}).get("productName", "") or ""),
        total_pieces=int(det.get("totalNumberOfPieces", 0) or 0),
        piece_ids=piece_ids,
        events=events,
        proof_of_delivery_url=pod.get("documentUrl"),
        signature_url=pod.get("signatureUrl"),
        raw=raw,
    )


# ─── 输出 ───────────────────────────────────────────────────────────


_STATUS_BADGES = {
    "delivered": "✅ 已派送",
    "transit": "🚚 运输中",
    "failure": "⚠️ 异常",
    "unknown": "❓ 未知",
}


def format_shipment(s: Shipment) -> str:
    """将 ``Shipment`` 渲染为可在终端打印的多行字符串。"""
    badge = _STATUS_BADGES.get(s.status, s.status_description)
    sep = "═" * 60
    sub = "─" * 60
    lines = [
        sep,
        f"📦 DHL {s.tracking_number}",
        sep,
        f"  状态: {badge}",
        f"  更新: {s.last_update}  📍 {s.last_location}",
        f"  产品: {s.product}  |  {s.total_pieces} 件",
    ]
    if s.piece_ids:
        lines.append(f"  子单: {', '.join(s.piece_ids)}")
    if s.origin or s.destination:
        lines.append(f"  发件: {s.origin}")
        lines.append(f"  收件: {s.destination}")
    if s.proof_of_delivery_url:
        lines.append(f"  POD : {s.proof_of_delivery_url}")
    if s.signature_url:
        lines.append(f"  签收图: {s.signature_url}")
    if s.events:
        lines.append("")
        lines.append(sub + " 时间线 " + sub)
        for ev in s.events:
            tag = f" [{', '.join(ev.piece_ids)}]" if ev.piece_ids else ""
            lines.append(f"  {ev.timestamp}  {ev.description}{tag}")
            if ev.location:
                lines.append(f"             📍 {ev.location}")
    return "\n".join(lines)


def shipment_to_dict(s: Shipment) -> dict:
    """转换为可 JSON 序列化的 dict（保留 raw）。"""
    return asdict(s)


# ─── CLI ────────────────────────────────────────────────────────────


def _parse_argv(argv: list[str]) -> tuple[list[str], dict[str, Any]]:
    opts: dict[str, Any] = {
        "as_json": False,
        "piece_only": False,
        "language": "zh",
        "cdp_endpoint": None,
        "check": False,
    }
    positional: list[str] = []
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            opts["as_json"] = True
        elif a == "--piece-ids":
            opts["piece_only"] = True
        elif a == "--check":
            opts["check"] = True
        elif a == "--lang" and i + 1 < len(argv):
            opts["language"] = argv[i + 1]
            i += 1
        elif a == "--cdp" and i + 1 < len(argv):
            opts["cdp_endpoint"] = argv[i + 1]
            i += 1
        elif a in ("-h", "--help"):
            opts["help"] = True
        else:
            positional.append(a)
        i += 1
    return positional, opts


_USAGE = (
    "用法:\n"
    "  python dhl_tracker.py <运单号> [--json] [--piece-ids] [--lang zh|en] [--cdp http://...]\n"
    "  python dhl_tracker.py --check [--cdp http://...] [--json]\n"
    "      端口默认 http://127.0.0.1:18800（可用 CHROME_CDP_ENDPOINT 覆盖）。\n"
    "      --check 跑健康巡检：HTTP/targets/Playwright 握手/真实查询。"
)


def _run_check(opts: dict[str, Any], probe_tracking: str | None) -> int:
    """复用 deploy/healthcheck.py 的逻辑做端口/Playwright/Akamai 检查。"""
    try:
        from deploy.healthcheck import run_all, _format_report  # type: ignore
    except ImportError:
        # deploy/ 不在 sys.path 时（脚本式运行）兜底导入
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from deploy.healthcheck import run_all, _format_report  # type: ignore

    endpoint = resolve_cdp_endpoint(opts.get("cdp_endpoint"))
    tracking = probe_tracking or os.environ.get("DHL_PROBE_TRACKING") or "4191468945"
    report = run_all(endpoint, tracking, skip_real=False)
    if opts.get("as_json"):
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_format_report(report))
    return int(report.get("exit_code", 0))


def _main() -> None:
    args, opts = _parse_argv(sys.argv)
    if opts.get("help"):
        print(_USAGE)
        sys.exit(0)

    if opts.get("check"):
        sys.exit(_run_check(opts, args[0] if args else None))

    if not args:
        print(_USAGE)
        sys.exit(1)

    tracking = args[0]
    try:
        s = track(
            tracking,
            language=str(opts["language"]),
            cdp_endpoint=opts["cdp_endpoint"],
        )
    except Exception as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)

    if opts["piece_only"]:
        for pid in s.piece_ids:
            print(pid)
        return
    if opts["as_json"]:
        print(json.dumps(shipment_to_dict(s), ensure_ascii=False, indent=2))
        return
    print(format_shipment(s))


if __name__ == "__main__":
    _main()
