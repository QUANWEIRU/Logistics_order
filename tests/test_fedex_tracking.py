"""fedex_tracking 单元测试（不访问网络）。"""

import unittest

import fedex_tracking as fedex_tracking_mod
from fedex_tracking import (
    extract_twelve_digit_trackings,
    infer_multipiece_total_count,
    normalize_fedex_tracking,
    order_related_numbers,
    parse_trackings_from_fedex_piece_shipment_paste,
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


class TestInferMultipiece(unittest.TestCase):
    def test_shipment_is_phrase(self):
        self.assertEqual(
            infer_multipiece_total_count("Shipment is 1 of 5 pieces →"),
            5,
        )

    def test_piece_shipment_heading(self):
        self.assertEqual(
            infer_multipiece_total_count("5 Piece Shipment\n871204379497"),
            5,
        )

    def test_none(self):
        self.assertIsNone(infer_multipiece_total_count("On the way"))


class TestWafSnippet(unittest.TestCase):
    def test_detect_block_page(self):
        self.assertTrue(
            fedex_tracking_mod._fedex_waf_or_block_blob(
                "Incident Number: 18.xxx\n don't have permission "
            )
        )

    def test_normal_snippet(self):
        self.assertFalse(
            fedex_tracking_mod._fedex_waf_or_block_blob("Tracking ID 871204379497")
        )


class TestPastePieceShipment(unittest.TestCase):
    def test_five_piece_block(self):
        blob = """5 Piece Shipment
871204379497 (master) On the way
871204379501 On the way
871204379512
871204379523
871204379534
"""
        self.assertEqual(
            parse_trackings_from_fedex_piece_shipment_paste(blob, master_hint="871204379497"),
            [
                "871204379497",
                "871204379501",
                "871204379512",
                "871204379523",
                "871204379534",
            ],
        )


if __name__ == "__main__":
    unittest.main()
