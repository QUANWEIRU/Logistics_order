"""ups_tracking 单元测试（不访问网络）。"""

import unittest

import ups_tracking as ups_mod
from ups_tracking import (
    extract_ups_1z_trackings,
    infer_multipiece_total_count,
    normalize_ups_tracking,
    order_related_numbers,
)


class TestNormalize(unittest.TestCase):
    def test_sample(self):
        self.assertEqual(
            normalize_ups_tracking("1ZB870570416816188"),
            "1ZB870570416816188",
        )

    def test_strip_spaces(self):
        self.assertEqual(
            normalize_ups_tracking(" 1zb870570416816188 "),
            "1ZB870570416816188",
        )


class TestExtract(unittest.TestCase):
    def test_two_ids(self):
        html = "包裹一 1ZB870570416816188 包裹二 1ZB870570416995593 end"
        got = extract_ups_1z_trackings(html)
        self.assertEqual(
            got,
            ["1ZB870570416816188", "1ZB870570416995593"],
        )


class TestOrder(unittest.TestCase):
    def test_master_first(self):
        self.assertEqual(
            order_related_numbers(
                "1ZB870570416816188",
                ["1ZB870570416995593", "1ZB870570416816188"],
            ),
            ["1ZB870570416816188", "1ZB870570416995593"],
        )


class TestInferMultipiece(unittest.TestCase):
    def test_cn_phrase(self):
        self.assertEqual(infer_multipiece_total_count("侧边 1 / 2 件货件 →"), 2)

    def test_fullwidth_slash(self):
        self.assertEqual(infer_multipiece_total_count("1／2 件货件"), 2)

    def test_heading_implies_multi(self):
        self.assertEqual(
            infer_multipiece_total_count("追踪详情\n该货件中的其他包裹"),
            2,
        )

    def test_none(self):
        self.assertIsNone(infer_multipiece_total_count("已派送"))


class TestRegexMultipiece(unittest.TestCase):
    def test_link_pattern(self):
        self.assertTrue(ups_mod.UPS_CN_MULTIPiece_LINK.search("1 / 2 件货件"))


if __name__ == "__main__":
    unittest.main()
