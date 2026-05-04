"""dhl_result_xlsx 子单拆分逻辑测试。"""

import unittest

from dhl_result_xlsx import split_dhl_piece_ids_from_cell, split_fedex_twelve_digit_from_cell


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


if __name__ == "__main__":
    unittest.main()
