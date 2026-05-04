"""
物流转单号查询：DHL（JD 子单号）与 FedEx（多件关联 12 位运单号）。

运行: streamlit run piece_lookup_app.py
"""

from __future__ import annotations

import csv
import io
import os
import time
from datetime import datetime

import streamlit as st

from dhl_piece_ids import fetch_piece_ids_batch, parse_tracking_id
from dhl_result_xlsx import build_carrier_detail_rows, build_result_workbook_bytes
from excel_tracking import (
    merge_waybill_lists,
    parse_waybill_text,
    read_waybills_from_xlsx,
    read_waybills_with_dhl_extras_from_xlsx,
)
from fedex_tracking import fetch_fedex_related_tracking_scrape_batch, normalize_fedex_tracking

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


def main() -> None:
    st.title("物流转单号查询")
    st.caption(
        "**DHL**：解析 JD 子单号；无 `DHL_API_KEY` 时用 Playwright 打开 DHL 官网。"
        "**FedEx**：解析同一托运下的多件 **12 位** 关联运单号（如主单 871241251143 与 871241251154）；"
        "使用 Playwright 打开 [FedEx 追踪页](https://www.fedex.com/fedextrack/)（首页 [fedex.com/zh-cn](https://www.fedex.com/zh-cn/home.html) 无密钥直连易被 WAF 拦截）。"
    )
    if _is_streamlit_community_cloud():
        st.info(
            "当前为 **Streamlit 云端**：首次使用「网页抓取」时会自动下载对应浏览器内核（约数百 MB，可能需数分钟），"
            "请耐心等待；**DHL** 也可在 **Settings → Secrets** 配置 `DHL_API_KEY` 走官方 API，避免依赖浏览器。"
        )

    with st.sidebar:
        st.header("选项")
        carrier = st.radio(
            "承运商",
            ("DHL（子单 JD）", "FedEx（关联运单号）"),
            index=0,
        )
        api_key_in = st.text_input(
            "DHL API Key（可选）",
            value=os.environ.get("DHL_API_KEY") or "",
            type="password",
            help="仅 DHL 生效：填写或环境变量 DHL_API_KEY 可走官方 API；否则网页抓取",
            disabled=(carrier == "FedEx（关联运单号）"),
        )
        force_scrape = st.checkbox(
            "强制网页抓取",
            value=False,
            help="仅 DHL：即使有 API Key 也使用浏览器",
            disabled=(carrier == "FedEx（关联运单号）"),
        )
        browser = st.selectbox("浏览器内核", ("firefox", "chromium", "webkit"), index=0)
        headed = st.checkbox("显示浏览器窗口", value=False, help="遇到验证时可勾选")
        max_rows = st.number_input("最多查询单号数量", min_value=1, max_value=500, value=80, step=10)
        col_letter = st.selectbox("Excel 转单号列", ("C 列（默认）", "B 列", "D 列"), index=0)
        col_map = {"C 列（默认）": 2, "B 列": 1, "D 列": 3}
        column_index = col_map[col_letter]

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
                else:
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
        ph = (
            "3112411665\n1113656224"
            if carrier == "DHL（子单 JD）"
            else "871241251143\n871241251154"
        )
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
    if cloud and carrier == "FedEx（关联运单号）":
        st.warning(
            "云端首次 FedEx 网页抓取会下载 Playwright 浏览器，耗时较长；若失败可在本机执行 "
            "`pip install -r requirements.txt && python -m playwright install firefox` 后本地运行。"
        )
    if cloud and carrier == "DHL（子单 JD）" and not api_key and not force_scrape:
        st.warning(
            "未配置 **DHL_API_KEY** 时将走网页抓取（云端首次会下载浏览器）。"
            "更稳定的方式：在 **Settings → Secrets** 或侧边栏填写密钥（[DHL Developer](https://developer.dhl.com)）。"
        )
    if cloud and carrier == "DHL（子单 JD）" and force_scrape:
        st.warning(
            "已勾选「强制网页抓取」：云端将使用浏览器而非 API，首次可能需较长时间下载内核。"
        )

    progress = st.progress(0.0, text="准备中…")

    def on_progress(cur: int, total: int, waybill: str) -> None:
        progress.progress(cur / max(total, 1), text=f"[{cur}/{total}] {waybill}")

    scrape_kw = {
        "browser": browser,
        "headless": not headed,
        "on_progress": on_progress,
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
            else:
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
                    on_progress=on_progress,
                    on_waybill_done=on_waybill_done,
                )
    except ImportError as e:
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

    related_col = "子单号(JD)" if carrier == "DHL（子单 JD）" else "关联运单号(12位)"
    rows = []
    tid_to_display: dict[str, str] = {}
    # 仅 DHL 且本次会话读过带附加列的 Excel 时，在结果中展示重量/产品描述/数量/价值
    show_xlsx_cols = carrier == "DHL（子单 JD）" and bool(file_xlsx_extras)
    for wb in merged:
        if carrier == "DHL（子单 JD）":
            tid = parse_tracking_id(wb)
        else:
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
        tid_to_display[tid] = wb
        pcs = result.get(tid, [])
        sec = timings_sec.get(tid)
        ok = bool(pcs)
        if carrier == "DHL（子单 JD）":
            status = "成功" if ok else "未解析到子单号"
        else:
            status = "成功" if ok else "未解析到关联运单号"
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

    detail_rows = build_carrier_detail_rows(
        rows,
        related_col=related_col,
        is_dhl=(carrier == "DHL（子单 JD）"),
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
    prefix = "dhl_piece" if carrier == "DHL（子单 JD）" else "fedex_related"
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
            xlsx_bytes = build_result_workbook_bytes(
                rows,
                related_col=related_col,
                is_dhl=(carrier == "DHL（子单 JD）"),
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


if __name__ == "__main__":
    main()
