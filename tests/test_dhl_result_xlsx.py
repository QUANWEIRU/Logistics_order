"""dhl_result_xlsx 子单拆分逻辑测试。"""

import unittest

from dhl_result_xlsx import (
    build_carrier_detail_rows,
    split_dhl_piece_ids_from_cell,
    split_fedex_twelve_digit_from_cell,
    split_ups_1z_from_cell,
)


class TestSplitPieces(unittest.TestCase):
    def test_dhl_multiple(self):
        s = "JD014600012592400513 JD014600012592400514 JD014600012592400515"
        got = split_dhl_piece_ids_from_cell(s)
        self.assertEqual(
            got,
            [
                "JD014600012592400513",
                "JD014600012592400514",
                "JD014600012592400515",
            ],
        )

    def test_dhl_empty(self):
        self.assertEqual(split_dhl_piece_ids_from_cell(""), [])
        self.assertEqual(split_dhl_piece_ids_from_cell("   "), [])

    def test_fedex(self):
        s = "871241251143 871241251154"
        self.assertEqual(split_fedex_twelve_digit_from_cell(s), ["871241251143", "871241251154"])

    def test_ups_1z(self):
        s = "1ZB870570416816188 1ZB870570416995593"
        self.assertEqual(
            split_ups_1z_from_cell(s),
            ["1ZB870570416816188", "1ZB870570416995593"],
        )

    def test_detail_split_proportional(self):
        """汇总重量/数量/价值按子单条数均分到明细行。"""
        summary = [
            {
                "转单号": "4191468945",
                "子单号(JD)": (
                    "JD014600012592400513 JD014600012592400514 "
                    "JD014600012592400515 JD014600012592400516"
                ),
                "关联条数": 4,
                "耗时(秒)": "9.1",
                "重量": "48",
                "产品描述": "PVC卡",
                "数量": "41",
                "价值": "92.25",
                "状态": "成功",
            }
        ]
        detail = build_carrier_detail_rows(summary, related_col="子单号(JD)", is_dhl=True)
        self.assertEqual(len(detail), 4)
        self.assertEqual(detail[0]["件数"], "1")
        self.assertEqual(detail[0]["重量"], "12")
        self.assertEqual(detail[0]["数量"], "10.25")
        self.assertEqual(detail[0]["价值"], "23.0625")
        self.assertEqual(detail[0]["产品描述"], "PVC卡")

    def test_detail_ups_style(self):
        summary = [
            {
                "转单号": "1ZB870570416816188",
                "包裹单号(1Z)": "1ZB870570416816188 1ZB870570416995593",
                "关联条数": 2,
                "耗时(秒)": "12",
                "状态": "成功",
            }
        ]
        detail = build_carrier_detail_rows(
            summary,
            related_col="包裹单号(1Z)",
            is_dhl=False,
            non_dhl_related_style="ups",
        )
        self.assertEqual(len(detail), 2)
        self.assertEqual(detail[0]["包裹单号(1Z)"], "1ZB870570416816188")


if __name__ == "__main__":
    unittest.main()
