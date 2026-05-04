"""
查询结果导出为 xlsx：「汇总」表与「子单明细」展开表（一格多子单拆成多行）。
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


def build_result_workbook_bytes(
    rows: list[dict[str, Any]],
    *,
    related_col: str,
    is_dhl: bool,
) -> bytes:
    """
    生成 xlsx 字节流。

    - 第一张表「汇总」：与界面结果行一致（列顺序与 rows[0] 键顺序相同）。
    - 第二张表「子单明细」或「单号明细」：将关联列拆成一行一件/一单号，其余列按行复制。
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
    if is_dhl:
        detail_headers = [
            "转单号",
            "子单号(JD)",
            "重量",
            "产品描述",
            "数量",
            "价值",
            "耗时(秒)",
            "状态",
        ]
    else:
        detail_headers = ["转单号", related_col, "耗时(秒)", "状态"]
    ws1.append(detail_headers)

    for r in rows:
        waybill = str(r.get("转单号", ""))
        cell = str(r.get(related_col, ""))
        pieces = split_dhl_piece_ids_from_cell(cell) if is_dhl else split_fedex_twelve_digit_from_cell(cell)
        for p in pieces or [""]:
            if is_dhl:
                ws1.append(
                    [
                        waybill,
                        p,
                        str(r.get("重量", "")),
                        str(r.get("产品描述", "")),
                        str(r.get("数量", "")),
                        str(r.get("价值", "")),
                        str(r.get("耗时(秒)", "")),
                        str(r.get("状态", "")),
                    ]
                )
            else:
                ws1.append(
                    [
                        waybill,
                        p,
                        str(r.get("耗时(秒)", "")),
                        str(r.get("状态", "")),
                    ]
                )

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
