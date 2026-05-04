"""fedex_tracking 单元测试（不访问网络）。"""

import unittest

from fedex_tracking import (
    extract_twelve_digit_trackings,
    normalize_fedex_tracking,
    order_related_numbers,
)


class TestNormalize(unittest.TestCase):
    def test_digits(self):
        self.assertEqual(normalize_fedex_tracking("871241251143"), "871241251143")

    def test_strip(self):
        self.assertEqual(normalize_fedex_tracking(" 8712 41251143 "), "871241251143")


class TestExtract(unittest.TestCase):
    def test_sample_html(self):
        html = "主货件 871241251143 另一 871241251154 end"
        self.assertEqual(
            extract_twelve_digit_trackings(html),
            ["871241251143", "871241251154"],
        )


class TestOrder(unittest.TestCase):
    def test_master_first(self):
        self.assertEqual(
            order_related_numbers(
                "871241251143",
                ["871241251154", "871241251143"],
            ),
            ["871241251143", "871241251154"],
        )


if __name__ == "__main__":
    unittest.main()
