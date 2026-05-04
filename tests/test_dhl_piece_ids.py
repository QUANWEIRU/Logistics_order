"""dhl_piece_ids 模块单元测试（不调用真实 DHL API）。"""

import unittest

from dhl_piece_ids import (
    build_tracking_page_url,
    extract_piece_ids_from_tracking_json,
    parse_tracking_id,
)


class TestBuildTrackingPageUrl(unittest.TestCase):
    def test_cn_submit(self):
        u = build_tracking_page_url("3112411665")
        self.assertIn("tracking-id=3112411665", u)
        self.assertIn("submit=1", u)
        self.assertTrue(u.startswith("https://www.dhl.com/cn-zh/home/tracking.html"))


class TestParseTrackingId(unittest.TestCase):
    def test_plain_number(self):
        self.assertEqual(parse_tracking_id("3112411665"), "3112411665")

    def test_url_with_tracking_id(self):
        u = "https://www.dhl.com/cn-zh/home/tracking.html?tracking-id=3112411665&submit=1&inputsource=marketingstage"
        self.assertEqual(parse_tracking_id(u), "3112411665")

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            parse_tracking_id("")

    def test_url_without_param_raises(self):
        with self.assertRaises(ValueError):
            parse_tracking_id("https://www.dhl.com/cn-zh/home/tracking.html")


class TestExtractPieceIds(unittest.TestCase):
    def test_from_nested_json(self):
        data = {
            "shipments": [
                {
                    "events": [
                        {
                            "pieceIds": [
                                "JD014600012591699173",
                                "JD014600012591699174",
                            ]
                        }
                    ]
                }
            ]
        }
        self.assertEqual(
            extract_piece_ids_from_tracking_json(data),
            ["JD014600012591699173", "JD014600012591699174"],
        )

    def test_from_string_field(self):
        data = {"raw": "包裹 JD014600012591699173 与 JD014600012591699174"}
        self.assertEqual(
            len(extract_piece_ids_from_tracking_json(data)),
            2,
        )


if __name__ == "__main__":
    unittest.main()
