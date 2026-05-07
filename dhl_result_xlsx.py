"""
查询结果导出为 xlsx：「汇总」表与「子单明细」展开表（一格多子单拆成多行）。

子单明细：按运单下子单条数均分重量、数量、价值（与订单行总量一致）；件数恒为 1（每件一行）。
"""

from __future__ import annotations

import io
import re
from typing import Any

# 与 dhl_piece_ids.PIECE_ID_RE_STRICT 保持一致（本模块避免顶层 import httpx 依赖链）
_DHL_JD_STRICT = re.compile(r"\bJD\d{18}\b")


def split_dhl_piece_ids_from_cell(cell: str) -> list[str]:
    """从「子单号(JD)」单元格文本中提取 JD 子单号列表（保序去重）。"""
    if not (cell or "").strip():
        return []
    return list(dict.fromkeys(m.group(0) for m in _DHL_JD_STRICT.finditer(cell)))


def split_fedex_twelve_digit_from_cell(cell: str) -> list[str]:
    """从 FedEx 关联单元格中提取 12 位运单号（保序去重）。"""
    if not (cell or "").strip():
        return []
    return list(dict.fromkeys(re.findall(r"\b\d{12}\b", cell)))


_UPS_1Z_STRICT = re.compile(r"\b1Z[0-9A-Z]{16}\b", re.IGNORECASE)


def split_ups_1z_from_cell(cell: str) -> list[str]:
    """从 UPS 关联单元格中提取 1Z… 包裹单号（保序去重，统一大写）。"""
    if not (cell or "").strip():
        return []
    return list(
        dict.fromkeys(m.group(0).upper() for m in _UPS_1Z_STRICT.finditer(cell))
    )


def _parse_optional_float(raw: object) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _format_split_number(x: float) -> str:
    """去掉多余尾随 0，如 23.0625、12、10.25。"""
    s = f"{x:.10f}".rstrip("0").rstrip(".")
    return s if s else "0"


def build_carrier_detail_rows(
    summary_rows: list[dict[str, Any]],
    *,
    related_col: str,
    is_dhl: bool,
    non_dhl_related_style: str = "fedex",
) -> list[dict[str, Any]]:
    """
    由汇总行生成明细行（页面与 Excel 共用）。

    DHL：列 转单号、子单号(JD)、件数、重量、产品描述、数量、价值（重量/数量/价值为按子单数均分）。
    FedEx：列 转单号、关联单号、件数、耗时(秒)、状态（无 Excel 件重价时不拆分数值）。
    UPS：列 转单号、包裹单号(1Z)、件数、耗时(秒)、状态（与 FedEx 同属非 DHL 分支）。
    non_dhl_related_style：非 DHL 时取值 fedex | ups，决定如何从关联列拆出多条单号。
    """
    out: list[dict[str, Any]] = []
    for r in summary_rows:
        waybill = str(r.get("转单号", ""))
        cell = str(r.get(related_col, ""))
        if is_dhl:
            pieces = split_dhl_piece_ids_from_cell(cell)
        elif non_dhl_related_style == "ups":
            pieces = split_ups_1z_from_cell(cell)
        else:
            pieces = split_fedex_twelve_digit_from_cell(cell)
        n = len(pieces)

        if is_dhl:
            desc = str(r.get("产品描述", ""))
            w_raw = r.get("重量", "")
            q_raw = r.get("数量", "")
            v_raw = r.get("价值", "")
            wf = _parse_optional_float(w_raw)
            qf = _parse_optional_float(q_raw)
            vf = _parse_optional_float(v_raw)

            if n == 0:
                out.append(
                    {
                        "转单号": waybill,
                        "子单号(JD)": "",
                        "件数": "",
                        "重量": str(w_raw),
                        "产品描述": desc,
                        "数量": str(q_raw),
                        "价值": str(v_raw),
                    }
                )
                continue

            w_s = _format_split_number(wf / n) if wf is not None else str(w_raw)
            q_s = _format_split_number(qf / n) if qf is not None else str(q_raw)
            v_s = _format_split_number(vf / n) if vf is not None else str(v_raw)
            for p in pieces:
                out.append(
                    {
                        "转单号": waybill,
                        "子单号(JD)": p,
                        "件数": "1",
                        "重量": w_s,
                        "产品描述": desc,
                        "数量": q_s,
                        "价值": v_s,
                    }
                )
        else:
            sec = str(r.get("耗时(秒)", ""))
            status = str(r.get("状态", ""))
            if n == 0:
                out.append(
                    {
                        "转单号": waybill,
                        related_col: "",
                        "件数": "",
                        "耗时(秒)": sec,
                        "状态": status,
                    }
                )
                continue
            for p in pieces:
                out.append(
                    {
                        "转单号": waybill,
                        related_col: p,
                        "件数": "1",
                        "耗时(秒)": sec,
                        "状态": status,
                    }
                )
    return out


def build_result_workbook_bytes(
    rows: list[dict[str, Any]],
    *,
    related_col: str,
    is_dhl: bool,
    non_dhl_related_style: str = "fedex",
) -> bytes:
    """
    生成 xlsx 字节流。

    - 「汇总」：与 summary 行字典键顺序一致。
    - 「子单明细」/「单号明细」：与 build_carrier_detail_rows 一致。
    """
    import openpyxl

    wb = openpyxl.Workbook()
    ws0 = wb.active
    ws0.title = "汇总"

    if not rows:
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    headers = list(rows[0].keys())
    ws0.append(headers)
    for r in rows:
        ws0.append([r.get(h, "") for h in headers])

    detail_name = "子单明细" if is_dhl else "单号明细"
    ws1 = wb.create_sheet(title=detail_name)
    detail_rows = build_carrier_detail_rows(
        rows,
        related_col=related_col,
        is_dhl=is_dhl,
        non_dhl_related_style=non_dhl_related_style,
    )
    if detail_rows:
        dh = list(detail_rows[0].keys())
        ws1.append(dh)
        for dr in detail_rows:
            ws1.append([dr.get(h, "") for h in dh])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
