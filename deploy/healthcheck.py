#!/usr/bin/env python3
"""
DHL 追踪服务健康检查（VPS 部署侧）。

依次检测：
  1. CDP HTTP 端口（``/json/version``）是否健康
  2. Page targets 数量（=0 时 ``dhl_tracker._open_cdp_browser`` 会自动兜底创建）
  3. Playwright 能否成功 ``connect_over_cdp`` + ``new_page``
  4. 真单查询：调 ``dhl_tracker.track`` 拿一条数据；返回 ``shipments=[]`` / 抛 ``RuntimeError``
     视为 Akamai 验证已过期，需要 VNC 进去续期

用法::

    python3 deploy/healthcheck.py                    # 默认探 18800、探一个内置兜底单号
    python3 deploy/healthcheck.py --tracking 1234567 # 自定义探测单号
    python3 deploy/healthcheck.py --json             # JSON 输出（便于接入 Prometheus/告警）

退出码：
  0  全部通过
  1  CDP 端口不通
  2  Playwright 握手失败
  3  Akamai 验证未通过 / 需要人工续期
  4  其它异常
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# 允许从 deploy/ 子目录里直接运行：把项目根加入 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dhl_tracker import (  # noqa: E402
    _cdp_http_get_json,
    _ensure_cdp_has_page_target,
    resolve_cdp_endpoint,
    track,
)

DEFAULT_PROBE_TRACKING = "4191468945"


def _check_http(endpoint: str) -> dict[str, Any]:
    try:
        ver = _cdp_http_get_json(endpoint, "/json/version", timeout=2.0)
        return {"ok": True, "browser": (ver or {}).get("Browser", "<unknown>")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


def _check_targets(endpoint: str) -> dict[str, Any]:
    try:
        targets = _cdp_http_get_json(endpoint, "/json/list", timeout=2.0) or []
        page_count = sum(1 for t in targets if isinstance(t, dict) and t.get("type") == "page")
        return {"ok": True, "total": len(targets), "page": page_count}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


def _check_pw_handshake(endpoint: str) -> dict[str, Any]:
    """握手 + new_page 能力探测；耗时一般 0.5~1.5s。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        return {"ok": False, "error": f"Playwright not installed: {e}"}

    _ensure_cdp_has_page_target(endpoint)

    t0 = time.perf_counter()
    try:
        with sync_playwright() as p:
            b = p.chromium.connect_over_cdp(endpoint, timeout=10_000)
            ctx = b.contexts[0] if b.contexts else None
            if ctx is None:
                b.close()
                return {"ok": False, "error": "no browser context after auto-tab"}
            page = ctx.new_page()
            page.close()
            b.close()
        return {"ok": True, "elapsed_ms": int((time.perf_counter() - t0) * 1000)}
    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {str(e)[:200]}",
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        }


def _check_real_query(endpoint: str, tracking: str) -> dict[str, Any]:
    """真实跑一次 ``track``；判断 Akamai 是否仍生效。"""
    t0 = time.perf_counter()
    try:
        s = track(tracking, cdp_endpoint=endpoint)
    except RuntimeError as e:
        return {
            "ok": False,
            "akamai_likely": True,
            "error": str(e)[:300],
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {str(e)[:200]}",
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        }
    return {
        "ok": True,
        "tracking": s.tracking_number,
        "status": s.status,
        "pieces": len(s.piece_ids),
        "events": len(s.events),
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }


def run_all(endpoint: str, tracking: str, *, skip_real: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"endpoint": endpoint, "checks": {}}

    out["checks"]["http"] = _check_http(endpoint)
    if not out["checks"]["http"]["ok"]:
        out["status"] = "fail"
        out["exit_code"] = 1
        out["hint"] = "CDP 端口不通：检查 systemctl status dhl-chrome / journalctl -u dhl-chrome -n 100"
        return out

    out["checks"]["targets"] = _check_targets(endpoint)

    out["checks"]["playwright"] = _check_pw_handshake(endpoint)
    if not out["checks"]["playwright"]["ok"]:
        out["status"] = "fail"
        out["exit_code"] = 2
        out["hint"] = "Playwright 握手失败：可能版本不兼容；尝试 .venv/bin/pip install -U playwright"
        return out

    if skip_real:
        out["status"] = "ok"
        out["exit_code"] = 0
        return out

    out["checks"]["real_query"] = _check_real_query(endpoint, tracking)
    if not out["checks"]["real_query"]["ok"]:
        out["status"] = "fail"
        out["exit_code"] = 3 if out["checks"]["real_query"].get("akamai_likely") else 4
        out["hint"] = (
            "真实查询失败：Akamai 验证可能过期。\n"
            "  → 启用 X11VNC_PORT、用 VNC 客户端连服务器，\n"
            "    在浏览器里手动通过验证后再跑本检查。"
        )
        return out

    out["status"] = "ok"
    out["exit_code"] = 0
    return out


def _format_report(report: dict[str, Any]) -> str:
    lines = [f"endpoint = {report['endpoint']}"]
    for name, c in report["checks"].items():
        badge = "✅" if c.get("ok") else "❌"
        extra = ", ".join(f"{k}={v}" for k, v in c.items() if k != "ok")
        lines.append(f"  {badge} {name:12s} {extra}")
    if report.get("hint"):
        lines.append("")
        lines.append("hint:")
        for ln in str(report["hint"]).splitlines():
            lines.append(f"  {ln}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="DHL 追踪服务健康检查")
    parser.add_argument(
        "--cdp",
        default=os.environ.get("CHROME_CDP_ENDPOINT") or "http://127.0.0.1:18800",
        help="CDP 端点（默认 http://127.0.0.1:18800）",
    )
    parser.add_argument(
        "--tracking",
        default=os.environ.get("DHL_PROBE_TRACKING") or DEFAULT_PROBE_TRACKING,
        help="探测用的 DHL 运单号（默认 4191468945）",
    )
    parser.add_argument(
        "--skip-real",
        action="store_true",
        help="只跑端口/握手探测，不发起真实查询（快，但无法判断 Akamai）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    args = parser.parse_args()

    endpoint = resolve_cdp_endpoint(args.cdp)
    report = run_all(endpoint, args.tracking, skip_real=args.skip_real)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_format_report(report))

    sys.exit(int(report.get("exit_code", 0)))


if __name__ == "__main__":
    main()
