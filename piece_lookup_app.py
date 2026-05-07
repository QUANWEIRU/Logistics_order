"""
物流转单号查询：DHL（JD 子单号）、FedEx（多件关联 12 位运单号）、UPS（多件 1Z 包裹号）。

UI 顶部分两个 tab：
- **批量子单号查询**：原有功能（Excel 上传 / 文本粘贴 → 批量解析子单 / 关联单）
- **DHL 追踪详情**：调用 ``dhl_tracker``，单运单深度查询并展示完整时间线、POD、签收图

运行: streamlit run piece_lookup_app.py
"""

from __future__ import annotations

import csv
import importlib
import inspect
import io
import json
import os
import time
from datetime import datetime
from typing import Any

import streamlit as st

from dhl_piece_ids import fetch_piece_ids_batch, parse_tracking_id
import dhl_result_xlsx

# Streamlit 重跑会复用 sys.modules；编辑子模块后需 reload，否则会用到旧的函数签名
importlib.reload(dhl_result_xlsx)
from dhl_result_xlsx import build_carrier_detail_rows, build_result_workbook_bytes
from excel_tracking import (
    merge_waybill_lists,
    parse_waybill_text,
    read_waybills_from_xlsx,
    read_waybills_with_dhl_extras_from_xlsx,
)
from fedex_tracking import (
    fetch_fedex_related_tracking_scrape_batch,
    normalize_fedex_tracking,
    parse_trackings_from_fedex_piece_shipment_paste,
)
from playwright_chrome_session import CDPConnectionError
from ups_tracking import (
    fetch_ups_package_trackings_scrape_batch,
    normalize_ups_tracking,
)


def _call_build_carrier_detail_rows(
    rows: list[dict],
    *,
    related_col: str,
    is_dhl: bool,
    non_dhl_related_style: str,
):
    """兼容旧版 dhl_result_xlsx（无 non_dhl_related_style 形参时不再传入）。"""
    kw: dict = {"related_col": related_col, "is_dhl": is_dhl}
    if "non_dhl_related_style" in inspect.signature(build_carrier_detail_rows).parameters:
        kw["non_dhl_related_style"] = non_dhl_related_style
    return build_carrier_detail_rows(rows, **kw)


def _call_build_result_workbook_bytes(
    rows: list[dict],
    *,
    related_col: str,
    is_dhl: bool,
    non_dhl_related_style: str,
) -> bytes:
    kw: dict = {"related_col": related_col, "is_dhl": is_dhl}
    if "non_dhl_related_style" in inspect.signature(build_result_workbook_bytes).parameters:
        kw["non_dhl_related_style"] = non_dhl_related_style
    return build_result_workbook_bytes(rows, **kw)


st.set_page_config(page_title="物流转单号查询", layout="wide")


def _is_streamlit_community_cloud() -> bool:
    """判断是否在 Streamlit Community Cloud（用于提示首轮需下载 Playwright 浏览器）。"""
    here = os.path.abspath(__file__).replace("\\", "/")
    if "/mount/src/" in here:
        return True
    url = (
        os.environ.get("STREAMLIT_APP_URL", "")
        + os.environ.get("STREAMLIT_URL", "")
    ).lower()
    return "streamlit.app" in url


def _dhl_api_key_resolved(sidebar_input: str) -> str | None:
    """侧边栏、环境变量、Streamlit Secrets 中的 DHL API Key（任一非空即可）。"""
    for raw in ((sidebar_input or "").strip(), (os.environ.get("DHL_API_KEY") or "").strip()):
        if raw:
            return raw
    try:
        if "DHL_API_KEY" in st.secrets:
            s = str(st.secrets["DHL_API_KEY"]).strip()
            return s or None
    except (AttributeError, FileNotFoundError, KeyError, RuntimeError, TypeError):
        pass
    return None


def _results_to_csv_rows(
    mapping: dict[str, list[str]],
    timings_sec: dict[str, float],
    *,
    display_waybills: dict[str, str] | None = None,
    related_column: str = "子单号",
    xlsx_extras_by_waybill: dict[str, dict[str, str]] | None = None,
) -> bytes:
    """display_waybills: tid -> 用户侧的转单号展示串（与表格「转单号」列一致）。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    header = ["转单号", related_column, "关联条数", "耗时(秒)"]
    if xlsx_extras_by_waybill:
        header.extend(["重量", "产品描述", "数量", "价值"])
    w.writerow(header)
    for tid in sorted(mapping.keys()):
        pieces = mapping[tid]
        row_waybill = (display_waybills or {}).get(tid, tid)
        elapsed = timings_sec.get(tid)
        row_vals: list[str | int] = [
            row_waybill,
            " ".join(pieces) if pieces else "",
            len(pieces),  # 关联条数（DHL 为子单数；FedEx 为解析到的运单号个数）
            f"{elapsed:.2f}" if elapsed is not None else "",
        ]
        if xlsx_extras_by_waybill:
            ex = xlsx_extras_by_waybill.get(row_waybill, {})
            row_vals.extend(
                [
                    ex.get("重量", ""),
                    ex.get("产品描述", ""),
                    ex.get("数量", ""),
                    ex.get("价值", ""),
                ]
            )
        w.writerow(row_vals)
    return buf.getvalue().encode("utf-8-sig")


def _collect_sidebar_opts() -> dict[str, Any]:
    """
    渲染左侧 sidebar 并返回当前选项。所有控件保持原有键名/默认值。

    sidebar 内容跨 tab 共享：「批量子单号查询」依赖全部字段；
    「DHL 追踪详情」目前只用 ``chrome_cdp_in`` 一项。
    """
    with st.sidebar:
        st.header("选项")
        carrier = st.radio(
            "承运商",
            ("DHL（子单 JD）", "FedEx（关联运单号）", "UPS（包裹 1Z）"),
            index=0,
            help="仅在「批量子单号查询」tab 生效；DHL 追踪详情 tab 始终查询 DHL。",
        )
        api_key_in = st.text_input(
            "DHL API Key（可选）",
            value=os.environ.get("DHL_API_KEY") or "",
            type="password",
            help="仅 DHL 生效：填写或环境变量 DHL_API_KEY 可走官方 API；否则网页抓取",
            disabled=(carrier != "DHL（子单 JD）"),
        )
        force_scrape = st.checkbox(
            "强制网页抓取",
            value=False,
            help="仅 DHL：即使有 API Key 也使用浏览器",
            disabled=(carrier != "DHL（子单 JD）"),
        )
        browser = st.selectbox(
            "浏览器内核",
            ("chrome", "chromium", "firefox", "webkit"),
            index=0,
            help="FedEx / UPS 建议优先使用本机 Google Chrome（必要时配合 CDP）。",
        )
        headed = st.checkbox("显示浏览器窗口", value=False, help="遇到验证时可勾选")
        with st.expander("高级：复用本机 Chrome（CDP / 用户数据目录）"):
            chrome_cdp_in = st.text_input(
                "Chrome CDP 地址",
                value=os.environ.get("CHROME_CDP_ENDPOINT", "") or "",
                placeholder="http://127.0.0.1:18800",
                help="先在本机用远程调试端口启动 Chrome，再填写；脚本会在该浏览器中新建标签页查询，"
                "与日常窗口共享 Cookie。**留空时若本机 18800 端口已开（OpenClaw 默认），将自动启用 DHL 快速通道**。",
            )
            chrome_udd_in = st.text_input(
                "Chrome user-data-dir（可选）",
                value=os.environ.get("CHROME_USER_DATA_DIR", "") or "",
                placeholder="/绝对路径/到独立配置目录",
                help="使用独立用户数据目录启动 Chrome（需内核选 chrome/chromium）。"
                "勿与正在运行的主 Chrome 同时占用同一目录。",
            )
            st.caption(
                "CDP 示例（macOS）：先退出 Chrome，再执行：\n"
                "`/Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome "
                "--remote-debugging-port=18800`\n"
                "然后在浏览器里手动打开一次 DHL 追踪页通过 Akamai 验证，本工具即可自动复用该窗口。"
            )
        max_rows = st.number_input("最多查询单号数量", min_value=1, max_value=500, value=80, step=10)
        col_letter = st.selectbox("Excel 转单号列", ("C 列（默认）", "B 列", "D 列"), index=0)
        col_map = {"C 列（默认）": 2, "B 列": 1, "D 列": 3}
        column_index = col_map[col_letter]

    return {
        "carrier": carrier,
        "api_key_in": api_key_in,
        "force_scrape": force_scrape,
        "browser": browser,
        "headed": headed,
        "chrome_cdp_in": chrome_cdp_in,
        "chrome_udd_in": chrome_udd_in,
        "max_rows": max_rows,
        "column_index": column_index,
    }


def _render_dhl_detail_cloud_fallback() -> None:
    """
    云端版 detail tab 的引导卡片：解释为何不可用 + 给出三条可执行替代方案。
    保持与本地版一致的输入框布局，提交时仅给出友好提示，不再发起 CDP 连接。
    """
    st.warning(
        "**「DHL 追踪详情」tab 在 Streamlit Community Cloud 上不可用。**\n\n"
        "原因：本 tab 依赖一个**已通过 DHL Akamai 验证**的本机 Chrome（CDP 端口 18800），"
        "云端容器是 ephemeral 沙箱，无 Chrome、无 Xvfb、也没法人工通过验证，"
        "因此连接 18800 必然失败。",
        icon="☁️",
    )

    st.markdown("### 你可以怎么办")
    st.markdown(
        "**A. 本地直接运行（最快）**\n\n"
        "```bash\n"
        "git clone https://github.com/QUANWEIRU/Logistics_order.git\n"
        "cd Logistics_order && python3 -m venv .venv\n"
        ".venv/bin/pip install -r requirements.txt\n"
        ".venv/bin/streamlit run piece_lookup_app.py\n"
        "```\n"
        "需要本机有一个开了远程调试端口 18800 的 Chrome；启动方式见仓库 `deploy/README.md`。"
    )
    st.markdown(
        "**B. 自家 Linux VPS 部署（长期可用）**\n\n"
        "把 Chrome 用 Xvfb 跑成 systemd 常驻服务，首次通过 VNC 过一次 Akamai 验证后 24~72h 复用，"
        "Streamlit 与 Chrome 同主机部署。整套脚本与手册在仓库 `deploy/` 下，5 步走："
    )
    st.code(
        "sudo bash deploy/install-deps.sh\n"
        "sudo cp deploy/dhl-chrome.service /etc/systemd/system/\n"
        "sudo systemctl daemon-reload && sudo systemctl enable --now dhl-chrome\n"
        "# 用 VNC 过一次 Akamai\n"
        "python deploy/healthcheck.py   # 退出码 0 即就绪",
        language="bash",
    )
    st.markdown(
        "**C. 如果你只需要子单号 / 状态等基础信息**\n\n"
        "切换到左边的「**批量子单号查询**」tab。云端版需要在 **Settings → Secrets** "
        "里配置 `DHL_API_KEY`（[去 developer.dhl.com 申请](https://developer.dhl.com)），"
        "走 DHL 官方 Unified Tracking API，无需浏览器，免费额度 250 calls/day。"
    )

    with st.expander("仍想看一眼输入框（仅作 UI 演示，提交无效）", expanded=False):
        in_col, btn_col = st.columns([3, 1])
        with in_col:
            st.text_input(
                "DHL 运单号",
                placeholder="云端不可用",
                key="dhl_detail_tn_in_cloud",
                label_visibility="collapsed",
                disabled=True,
            )
        with btn_col:
            st.button(
                "查询详情",
                type="primary",
                use_container_width=True,
                disabled=True,
                key="dhl_detail_run_btn_cloud",
            )


def _render_dhl_detail_section(opts: dict[str, Any]) -> None:
    """
    DHL 单运单深度追踪：调用 ``dhl_tracker.track`` 展示完整时间线 / POD / 签收图。

    依赖本机 Chrome 远程调试端口（默认 18800，可在 sidebar「Chrome CDP 地址」覆盖）；
    需事先在浏览器内手动打开过一次 DHL 追踪页通过 Akamai 验证。

    在 **Streamlit Community Cloud** 这种 ephemeral 容器环境里：没有本机 Chrome、
    没有 Xvfb、也没有人能 VNC 进去通过 Akamai 验证，本 tab 必然不可用——直接显示
    引导卡片代替抛 ``ECONNREFUSED``，避免给访客看到红框报错。
    """
    if _is_streamlit_community_cloud():
        _render_dhl_detail_cloud_fallback()
        return

    st.markdown(
        "**单运单深度追踪**：复用本机已通过 Akamai 验证的 Chrome（默认 CDP 端口 18800），"
        "拦截 DHL 页面自身发起的 UTAPI 响应，渲染完整状态、子单号、时间线与签收凭证。"
    )

    in_col, btn_col = st.columns([3, 1])
    with in_col:
        tn_in = st.text_input(
            "DHL 运单号",
            placeholder="例如 4191468945；也可粘贴含 ?tracking-id= 的完整链接",
            key="dhl_detail_tn_in",
            label_visibility="collapsed",
        )
    with btn_col:
        run_btn = st.button(
            "查询详情",
            type="primary",
            use_container_width=True,
            key="dhl_detail_run_btn",
        )

    cdp_endpoint = ((opts.get("chrome_cdp_in") or "").strip()) or None
    if cdp_endpoint:
        st.caption(f"将使用 CDP 端点：`{cdp_endpoint}`")
    else:
        st.caption(
            "未指定 CDP 端点：若本机 `http://127.0.0.1:18800` 已开启远程调试，将自动复用；"
            "否则会因端口未通而失败。"
        )

    if not run_btn:
        return

    raw = (tn_in or "").strip()
    if not raw:
        st.warning("请填写 DHL 运单号。")
        return

    try:
        tid = parse_tracking_id(raw)
    except ValueError as e:
        st.error(f"运单号无效：{e}")
        return

    from dhl_tracker import format_shipment, shipment_to_dict, track

    with st.spinner(f"正在查询 {tid} … （约 6–10 秒）"):
        t0 = time.perf_counter()
        try:
            shipment = track(tid, cdp_endpoint=cdp_endpoint)
        except RuntimeError as e:
            st.error(str(e))
            return
        except Exception as e:
            st.exception(e)
            return
        elapsed = time.perf_counter() - t0

    badge_map = {
        "delivered": "✅ 已派送",
        "transit": "🚚 运输中",
        "failure": "⚠️ 异常",
        "unknown": "❓ 未知",
    }
    status_text = badge_map.get(shipment.status, shipment.status_description)

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric("状态", status_text)
    with m2:
        st.metric("件数", str(shipment.total_pieces))
    with m3:
        st.metric("子单数", str(len(shipment.piece_ids)))
    with m4:
        st.metric("耗时", f"{elapsed:.2f} 秒")

    st.subheader("基本信息")
    info_rows = [
        {"字段": "运单号", "值": shipment.tracking_number},
        {"字段": "产品", "值": shipment.product or "—"},
        {"字段": "发件", "值": shipment.origin or "—"},
        {"字段": "收件", "值": shipment.destination or "—"},
        {"字段": "最近更新", "值": shipment.last_update or "—"},
        {"字段": "最近位置", "值": shipment.last_location or "—"},
    ]
    st.dataframe(info_rows, use_container_width=True, hide_index=True)

    if shipment.piece_ids:
        st.subheader(f"JD 子单号（{len(shipment.piece_ids)}）")
        st.code("\n".join(shipment.piece_ids), language="text")

    pod_url = shipment.proof_of_delivery_url
    sig_url = shipment.signature_url
    if pod_url or sig_url:
        st.subheader("签收凭证")
        cols = st.columns(2)
        if pod_url:
            with cols[0]:
                st.link_button(
                    "📄 派送证明（POD）",
                    pod_url,
                    use_container_width=True,
                )
        if sig_url:
            with cols[1]:
                st.link_button(
                    "✍️ 签收图",
                    sig_url,
                    use_container_width=True,
                )

    if shipment.events:
        st.subheader(f"时间线（{len(shipment.events)} 条）")
        timeline_rows = [
            {
                "时间": ev.timestamp,
                "描述": ev.description,
                "位置": ev.location or "—",
                "子单号": ", ".join(ev.piece_ids) if ev.piece_ids else "",
            }
            for ev in shipment.events
        ]
        st.dataframe(timeline_rows, use_container_width=True, hide_index=True)

    with st.expander("查看 / 下载原始数据"):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_bytes = json.dumps(
            shipment_to_dict(shipment),
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        st.download_button(
            "下载结构化 JSON",
            data=json_bytes,
            file_name=f"dhl_shipment_{tid}_{ts}.json",
            mime="application/json",
        )
        text_blob = format_shipment(shipment)
        st.download_button(
            "下载文本视图",
            data=text_blob.encode("utf-8"),
            file_name=f"dhl_shipment_{tid}_{ts}.txt",
            mime="text/plain",
        )
        st.json(shipment.raw, expanded=False)


def _render_batch_lookup_section(opts: dict[str, Any]) -> None:
    """
    原有「批量子单号查询」主区：上传 Excel / 粘贴单号 → 调度对应承运商查询 → 汇总 / 明细 / 导出。
    """
    carrier = opts["carrier"]
    api_key_in = opts["api_key_in"]
    force_scrape = opts["force_scrape"]
    browser = opts["browser"]
    headed = opts["headed"]
    chrome_cdp_in = opts["chrome_cdp_in"]
    chrome_udd_in = opts["chrome_udd_in"]
    max_rows = opts["max_rows"]
    column_index = opts["column_index"]

    if _is_streamlit_community_cloud():
        has_api_key = bool(_dhl_api_key_resolved(api_key_in))
        st.info(
            "☁️ **云端能力矩阵**\n\n"
            "| 承运商 | 是否可用 | 说明 |\n"
            "|---|---|---|\n"
            f"| **DHL** | {'✅ 推荐' if has_api_key else '⚠️ 需 API Key'} | "
            "配 `DHL_API_KEY` 走官方 Unified API（最稳）；"
            "无 Key 走 headless Playwright，几乎一定被 Akamai 拦截 |\n"
            "| **FedEx** | ⚠️ 偶发被 WAF 拦 | 走 headless Playwright；失败时可在结果区粘贴页面文本兜底解析 |\n"
            "| **UPS** | ✅ 通常可用 | 走 headless Playwright；UPS 反爬较弱 |\n\n"
            "👉 想要完整「DHL 追踪详情」（时间线 / POD / 签收图）请在本地或自家 VPS 运行，"
            "见仓库 `deploy/README.md`。",
            icon="ℹ️",
        )

    tab_xlsx, tab_text = st.tabs(["上传 Excel", "粘贴单号"])

    file_wb: list[str] = []
    # 上传 Excel 时按 DHL 主表列位读取 F/G/H/K（重量、产品描述、数量、价值），键为单元格转单号原串
    file_xlsx_extras: dict[str, dict[str, str]] = {}
    with tab_xlsx:
        up = st.file_uploader("选择 xlsx 文件", type=("xlsx",))
        if up is not None:
            try:
                raw_xlsx = up.read()
                if carrier == "DHL（子单 JD）":
                    file_wb, file_xlsx_extras = read_waybills_with_dhl_extras_from_xlsx(
                        raw_xlsx,
                        sheet_index=0,
                        waybill_column_index=column_index,
                    )
                elif carrier in ("FedEx（关联运单号）", "UPS（包裹 1Z）"):
                    file_wb = read_waybills_from_xlsx(
                        raw_xlsx, sheet_index=0, column_index=column_index
                    )
                st.success(f"已从表格读取 **{len(file_wb)}** 个转单号（列索引 {column_index}）。")
                if carrier == "DHL（子单 JD）" and file_xlsx_extras:
                    st.caption(
                        "已关联表内列：**重量**(F)、**产品描述**(G)、**数量**(H)、**价值**(K)，将并入查询结果。"
                    )
            except Exception as e:
                st.error(f"读取 Excel 失败：{e}")

    text_wb: list[str] = []
    with tab_text:
        if carrier == "DHL（子单 JD）":
            ph = "3112411665\n1113656224"
        elif carrier == "UPS（包裹 1Z）":
            ph = "1ZB870570416816188\n1ZB870570416995593"
        else:
            ph = "871241251143\n871241251154"
        raw = st.text_area(
            "每行一个运单号，或用逗号、分号分隔",
            height=160,
            placeholder=ph,
        )
        text_wb = parse_waybill_text(raw)

    merged = merge_waybill_lists(file_wb, text_wb)
    if len(merged) > int(max_rows):
        st.warning(f"当前共 **{len(merged)}** 个单号，已超过上限 **{max_rows}**，将只处理前 {max_rows} 条。")
        merged = merged[: int(max_rows)]

    if merged:
        st.info(f"待查询 **{len(merged)}** 个不重复转单号。")
        preview = ", ".join(merged[:15])
        if len(merged) > 15:
            preview += " …"
        st.caption(preview)

    run = st.button("开始查询", type="primary", disabled=not merged)
    if not run or not merged:
        return

    api_key = _dhl_api_key_resolved(api_key_in)
    cloud = _is_streamlit_community_cloud()
    if cloud and carrier in ("FedEx（关联运单号）", "UPS（包裹 1Z）"):
        st.warning(
            "云端首次 FedEx / UPS 网页抓取会下载 Playwright 浏览器，耗时较长；若失败可在本机执行 "
            "`pip install -r requirements.txt && python -m playwright install chromium` 后本地运行。"
        )
    if cloud and carrier == "DHL（子单 JD）" and not api_key:
        st.error(
            "🚫 **云端查 DHL 几乎必然返回空结果**：未配置 `DHL_API_KEY` 时只能走 headless Playwright，"
            "DHL 站使用 Akamai 反爬，会识别 headless 浏览器并拒绝返回 UTAPI 响应。\n\n"
            "请在 **Settings → Secrets** 添加 `DHL_API_KEY`（[去 developer.dhl.com 申请](https://developer.dhl.com)），"
            "或本地 / 自家 VPS 运行（见仓库 `deploy/README.md`）；"
            "也可继续点「开始查询」体验失败流程。"
        )
    if cloud and carrier == "DHL（子单 JD）" and force_scrape:
        st.warning(
            "已勾选「强制网页抓取」：云端用 Chromium 跑 DHL 仍会被 Akamai 拦下。"
        )

    progress = st.progress(0.0, text="准备中…")

    def on_progress(cur: int, total: int, waybill: str) -> None:
        progress.progress(cur / max(total, 1), text=f"[{cur}/{total}] {waybill}")

    scrape_kw = {
        "browser": browser,
        "headless": not headed,
        "on_progress": on_progress,
        "chrome_cdp_endpoint": (chrome_cdp_in or "").strip() or None,
        "chrome_user_data_dir": (chrome_udd_in or "").strip() or None,
    }

    # 每个标准化运单号对应单次查询耗时（秒）
    timings_sec: dict[str, float] = {}

    def on_waybill_done(tid: str, seconds: float) -> None:
        timings_sec[tid] = seconds

    t_batch0 = time.perf_counter()
    result: dict[str, list[str]] = {}
    try:
        with st.spinner("查询中，请勿关闭页面…"):
            if carrier == "DHL（子单 JD）":
                result = fetch_piece_ids_batch(
                    merged,
                    api_key=api_key,
                    force_scrape=force_scrape,
                    scrape_kwargs=scrape_kw,
                    on_waybill_done=on_waybill_done,
                )
            elif carrier == "FedEx（关联运单号）":
                # FedEx：仅网页抓取；键为标准化 12 位运单号
                fed_merged: list[str] = []
                for w in merged:
                    try:
                        fed_merged.append(normalize_fedex_tracking(w))
                    except ValueError:
                        continue
                result = fetch_fedex_related_tracking_scrape_batch(
                    fed_merged,
                    headless=not headed,
                    browser=browser,
                    chrome_cdp_endpoint=scrape_kw.get("chrome_cdp_endpoint"),
                    chrome_user_data_dir=scrape_kw.get("chrome_user_data_dir"),
                    on_progress=on_progress,
                    on_waybill_done=on_waybill_done,
                )
            else:
                # UPS：仅网页抓取；键为标准化 1Z 单号
                ups_merged: list[str] = []
                for w in merged:
                    try:
                        ups_merged.append(normalize_ups_tracking(w))
                    except ValueError:
                        continue
                result = fetch_ups_package_trackings_scrape_batch(
                    ups_merged,
                    headless=not headed,
                    browser=browser,
                    chrome_cdp_endpoint=scrape_kw.get("chrome_cdp_endpoint"),
                    chrome_user_data_dir=scrape_kw.get("chrome_user_data_dir"),
                    on_progress=on_progress,
                    on_waybill_done=on_waybill_done,
                )
    except ImportError as e:
        st.error(str(e))
        return
    except CDPConnectionError as e:
        st.error(str(e))
        return
    except Exception as e:
        st.exception(e)
        return
    finally:
        progress.progress(1.0, text="完成")

    elapsed_total = time.perf_counter() - t_batch0
    n_done = len(merged)
    avg_sec = elapsed_total / n_done if n_done else 0.0

    col_a, col_b, col_c = st.columns(3)
    with col_a:
        st.metric("总耗时", f"{elapsed_total:.2f} 秒")
    with col_b:
        st.metric("平均每单", f"{avg_sec:.2f} 秒")
    with col_c:
        st.metric("查询单数", str(n_done))

    if carrier == "DHL（子单 JD）":
        related_col = "子单号(JD)"
    elif carrier == "FedEx（关联运单号）":
        related_col = "关联运单号(12位)"
    else:
        related_col = "包裹单号(1Z)"
    non_dhl_style = "fedex" if carrier == "FedEx（关联运单号）" else "ups"

    rows = []
    tid_to_display: dict[str, str] = {}
    # 仅 DHL 且本次会话读过带附加列的 Excel 时，在结果中展示重量/产品描述/数量/价值
    show_xlsx_cols = carrier == "DHL（子单 JD）" and bool(file_xlsx_extras)
    for wb in merged:
        if carrier == "DHL（子单 JD）":
            tid = parse_tracking_id(wb)
        elif carrier == "FedEx（关联运单号）":
            try:
                tid = normalize_fedex_tracking(wb)
            except ValueError:
                row_inv: dict[str, str | int] = {
                    "转单号": wb,
                    related_col: "",
                    "关联条数": 0,
                    "耗时(秒)": "",
                    "状态": "运单号无效",
                }
                rows.append(row_inv)
                continue
        else:
            try:
                tid = normalize_ups_tracking(wb)
            except ValueError:
                row_inv2: dict[str, str | int] = {
                    "转单号": wb,
                    related_col: "",
                    "关联条数": 0,
                    "耗时(秒)": "",
                    "状态": "运单号无效",
                }
                rows.append(row_inv2)
                continue
        tid_to_display[tid] = wb
        pcs = result.get(tid, [])
        sec = timings_sec.get(tid)
        ok = bool(pcs)
        if carrier == "DHL（子单 JD）":
            status = "成功" if ok else "未解析到子单号"
        elif carrier == "FedEx（关联运单号）":
            status = "成功" if ok else "未解析到关联运单号"
        else:
            status = "成功" if ok else "未解析到包裹单号(1Z)"
        row_out: dict[str, str | int] = {
            "转单号": wb,
            related_col: " ".join(pcs) if pcs else "",
            "关联条数": len(pcs),
            "耗时(秒)": f"{sec:.2f}" if sec is not None else "",
        }
        if show_xlsx_cols:
            ex = file_xlsx_extras.get(wb, {})
            row_out["重量"] = ex.get("重量", "")
            row_out["产品描述"] = ex.get("产品描述", "")
            row_out["数量"] = ex.get("数量", "")
            row_out["价值"] = ex.get("价值", "")
        row_out["状态"] = status
        rows.append(row_out)

    st.subheader("汇总")
    st.dataframe(rows, use_container_width=True)

    if carrier == "FedEx（关联运单号）" and rows:
        valid_rows = [r for r in rows if r.get("状态") != "运单号无效"]
        if valid_rows and all(int(r.get("关联条数", 0) or 0) == 0 for r in valid_rows):
            st.warning(
                "**FedEx 未解析到关联单号** 时，常见原因是 **无头浏览器被官网 WAF 拦截** 或 "
                "追踪页为 SPA、**Firefox 无头下正文长时间为空**。\n\n"
                "**建议**：侧边栏展开 **「复用本机 Chrome（CDP）」**，用调试端口附着到你日常使用的 Chrome；"
                "或勾选 **「显示浏览器窗口」**；内核选 **chrome**。\n\n"
                "若官网能打开 **「N Piece Shipment」** 表格：可复制该段文字到下方「粘贴兜底」解析 "
                "（与 `fedex_tracking.parse_trackings_from_fedex_piece_shipment_paste` 相同逻辑）。"
            )
            with st.expander("FedEx：粘贴「多件货」表格文本（可选兜底）"):
                paste_fb = st.text_area(
                    "从 FedEx 页复制的纯文本",
                    height=120,
                    key="fedex_piece_paste_fallback",
                    label_visibility="collapsed",
                    placeholder="粘贴含 12 位单号的「5 Piece Shipment」等区域…",
                )
                hint_tid = valid_rows[0].get("转单号", "")
                if st.button("从粘贴文本解析关联单号", key="fedex_parse_paste_btn"):
                    parsed = parse_trackings_from_fedex_piece_shipment_paste(
                        paste_fb,
                        master_hint=hint_tid if hint_tid else None,
                    )
                    if parsed:
                        st.success("解析到 " + " ".join(parsed))
                    else:
                        st.error("未能从粘贴内容中解析出 12 位运单号。")

    detail_rows = _call_build_carrier_detail_rows(
        rows,
        related_col=related_col,
        is_dhl=(carrier == "DHL（子单 JD）"),
        non_dhl_related_style=(
            non_dhl_style if carrier != "DHL（子单 JD）" else "fedex"
        ),
    )
    if carrier == "DHL（子单 JD）":
        st.subheader("子单明细")
    else:
        st.subheader("单号明细")
    st.dataframe(detail_rows, use_container_width=True)

    csv_bytes = _results_to_csv_rows(
        result,
        timings_sec,
        display_waybills=tid_to_display,
        related_column=related_col,
        xlsx_extras_by_waybill=file_xlsx_extras if show_xlsx_cols else None,
    )
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if carrier == "DHL（子单 JD）":
        prefix = "dhl_piece"
    elif carrier == "FedEx（关联运单号）":
        prefix = "fedex_related"
    else:
        prefix = "ups_packages"
    dl1, dl2 = st.columns(2)
    with dl1:
        st.download_button(
            "下载 CSV",
            data=csv_bytes,
            file_name=f"{prefix}_{ts}.csv",
            mime="text/csv",
        )
    with dl2:
        try:
            xlsx_bytes = _call_build_result_workbook_bytes(
                rows,
                related_col=related_col,
                is_dhl=(carrier == "DHL（子单 JD）"),
                non_dhl_related_style=(
                    non_dhl_style if carrier != "DHL（子单 JD）" else "fedex"
                ),
            )
            st.download_button(
                "下载 Excel（汇总+转换明细）",
                data=xlsx_bytes,
                file_name=f"{prefix}_{ts}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                help="与 CSV 列一致的「汇总」表，另附「子单明细」：一格多子单展开为每行一条 JD。",
            )
        except ImportError:
            st.caption("导出 Excel 需安装 openpyxl。")


def main() -> None:
    """页面入口：渲染顶部说明 + sidebar，并把内容分配到两个一级 tab。"""
    st.title("物流转单号查询")
    st.caption(
        "**DHL**：解析 JD 子单号；无 `DHL_API_KEY` 时用 Playwright 打开 DHL 官网。"
        "**FedEx**：解析同一托运下的多件 **12 位** 关联运单号；使用 Playwright 打开 [FedEx 追踪页](https://www.fedex.com/fedextrack/)。"
        "**UPS**：在 [UPS 中文追踪页](https://www.ups.com/track?loc=zh_CN&requester=ST/) 输入单号后，若显示「x / y 件货件」则自动展开并解析全部 **1Z** 包裹号。"
    )
    if _is_streamlit_community_cloud():
        st.info(
            "当前为 **Streamlit 云端**：首次使用「网页抓取」时会自动下载对应浏览器内核（约数百 MB，可能需数分钟），"
            "请耐心等待；**DHL** 也可在 **Settings → Secrets** 配置 `DHL_API_KEY` 走官方 API，避免依赖浏览器。"
        )

    opts = _collect_sidebar_opts()

    tab_lookup, tab_detail = st.tabs(
        ["批量子单号查询（DHL / FedEx / UPS）", "DHL 追踪详情（时间线 / POD）"]
    )
    with tab_lookup:
        _render_batch_lookup_section(opts)
    with tab_detail:
        _render_dhl_detail_section(opts)


if __name__ == "__main__":
    main()
