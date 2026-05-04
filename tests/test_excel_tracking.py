"""excel_tracking 解析逻辑测试。"""

import io
import unittest

from excel_tracking import merge_waybill_lists, parse_waybill_text, read_waybills_from_xlsx


class TestParseWaybillText(unittest.TestCase):
    def test_split(self):
        self.assertEqual(
            parse_waybill_text("3112411665, 1113656224\n9294612412"),
            ["3112411665", "1113656224", "9294612412"],
        )


class TestMerge(unittest.TestCase):
    def test_dedupe_order(self):
        self.assertEqual(
            merge_waybill_lists(["1", "2"], ["2", "3"]),
            ["1", "2", "3"],
        )


class TestReadXlsx(unittest.TestCase):
    def test_column_c(self):
        try:
            import openpyxl
        except ImportError:
            self.skipTest("openpyxl 未安装")

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["币种", "参考号", "转单号"])
        ws.append(["USD", "R1", "3112411665"])
        ws.append(["EUR", "R2", "1113656224"])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        ids = read_waybills_from_xlsx(buf.read(), column_index=2)
        self.assertEqual(ids, ["3112411665", "1113656224"])


if __name__ == "__main__":
    unittest.main()
