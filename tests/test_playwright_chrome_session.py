"""playwright_chrome_session 单元测试（不启动浏览器）。"""

import os
import unittest

from playwright_chrome_session import (
    resolve_chrome_cdp_endpoint,
    resolve_chrome_user_data_dir,
)


class TestResolve(unittest.TestCase):
    def setUp(self) -> None:
        self._old_cdp = os.environ.get("CHROME_CDP_ENDPOINT")

    def tearDown(self) -> None:
        if self._old_cdp is None:
            os.environ.pop("CHROME_CDP_ENDPOINT", None)
        else:
            os.environ["CHROME_CDP_ENDPOINT"] = self._old_cdp

    def test_cdp_explicit_over_env(self):
        os.environ["CHROME_CDP_ENDPOINT"] = "http://env:9222"
        self.assertEqual(
            resolve_chrome_cdp_endpoint("http://explicit:9222"),
            "http://explicit:9222",
        )

    def test_udd_expanduser(self):
        self.assertTrue(
            resolve_chrome_user_data_dir("~/tmp_fedex_profile").endswith("tmp_fedex_profile")
        )


if __name__ == "__main__":
    unittest.main()
