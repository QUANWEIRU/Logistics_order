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
from excel_tracking import merge_waybill_lists, parse_waybill_text, read_waybills_from_xlsx
from fedex_tracking import fetch_fedex_related_tracking_scrape_batch, normalize_fedex_tracking

st.set_page_config(page_title="物流转单号查询", layout="wide")


def _results_to_csv_rows(
    mapping: dict[str, list[str]],
    timings_sec: dict[str, float],
    *,
    display_waybills: dict[str, str] | None = None,
    related_column: str = "子单号",
) -> bytes:
    """display_waybills: tid -> 用户侧的转单号展示串（与表格「转单号」列一致）。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["转单号", related_column, "关联条数", "耗时(秒)"])
    for tid in sorted(mapping.keys()):
        pieces = mapping[tid]
        row_waybill = (display_waybills or {}).get(tid, tid)
        elapsed = timings_sec.get(tid)
        w.writerow(
            [
                row_waybill,
                " ".join(pieces) if pieces else "",
                len(pieces),  # 关联条数（DHL 为子单数；FedEx 为解析到的运单号个数）
                f"{elapsed:.2f}" if elapsed is not None else "",
            ]
        )
    return buf.getvalue().encode("utf-8-sig")


def main() -> None:
    st.title("物流转单号查询")
    st.caption(
        "**DHL**：解析 JD 子单号；无 `DHL_API_KEY` 时用 Playwright 打开 DHL 官网。"
        "**FedEx**：解析同一托运下的多件 **12 位** 关联运单号（如主单 871241251143 与 871241251154）；"
        "使用 Playwright 打开 [FedEx 追踪页](https://www.fedex.com/fedextrack/)（首页 [fedex.com/zh-cn](https://www.fedex.com/zh-cn/home.html) 无密钥直连易被 WAF 拦截）。"
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
    with tab_xlsx:
        up = st.file_uploader("选择 xlsx 文件", type=("xlsx",))
        if up is not None:
            try:
                file_wb = read_waybills_from_xlsx(up.read(), sheet_index=0, column_index=column_index)
                st.success(f"已从表格读取 **{len(file_wb)}** 个转单号（列索引 {column_index}）。")
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

    api_key = api_key_in.strip() or os.environ.get("DHL_API_KEY") or None

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
    for wb in merged:
        if carrier == "DHL（子单 JD）":
            tid = parse_tracking_id(wb)
        else:
            try:
                tid = normalize_fedex_tracking(wb)
            except ValueError:
                rows.append(
                    {
                        "转单号": wb,
                        related_col: "",
                        "关联条数": 0,
                        "耗时(秒)": "",
                        "状态": "运单号无效",
                    }
                )
                continue
        tid_to_display[tid] = wb
        pcs = result.get(tid, [])
        sec = timings_sec.get(tid)
        ok = bool(pcs)
        if carrier == "DHL（子单 JD）":
            status = "成功" if ok else "未解析到子单号"
        else:
            status = "成功" if ok else "未解析到关联运单号"
        rows.append(
            {
                "转单号": wb,
                related_col: " ".join(pcs) if pcs else "",
                "关联条数": len(pcs),
                "耗时(秒)": f"{sec:.2f}" if sec is not None else "",
                "状态": status,
            }
        )

    st.subheader("结果")
    st.dataframe(rows, use_container_width=True)

    csv_bytes = _results_to_csv_rows(
        result,
        timings_sec,
        display_waybills=tid_to_display,
        related_column=related_col,
    )
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = "dhl_piece" if carrier == "DHL（子单 JD）" else "fedex_related"
    st.download_button(
        "下载 CSV",
        data=csv_bytes,
        file_name=f"{prefix}_{ts}.csv",
        mime="text/csv",
    )


if __name__ == "__main__":
    main()
