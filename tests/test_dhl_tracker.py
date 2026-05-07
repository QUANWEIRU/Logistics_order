"""dhl_tracker 单元测试（不启动浏览器、不连接 CDP）。"""

import os
import unittest

from dhl_tracker import (
    DEFAULT_CDP_ENDPOINT,
    Shipment,
    TrackingEvent,
    _parse_shipment,
    format_shipment,
    resolve_cdp_endpoint,
    shipment_to_dict,
)


# 模拟 DHL UTAPI 的真实响应骨架（精简自实拍数据）
_FAKE_RAW = {
    "shipments": [
        {
            "id": "4191468945",
            "service": "express",
            "origin": {
                "address": {
                    "addressLocality": "HONG KONG - HONG KONG, HONG KONG",
                    "countryCode": "HK",
                }
            },
            "destination": {
                "address": {
                    "addressLocality": "NEW YORK, NY - USA",
                    "countryCode": "US",
                }
            },
            "status": {
                "timestamp": "2026-05-06T14:31:00",
                "location": {
                    "address": {
                        "addressLocality": "NEW YORK, NY - USA",
                        "countryCode": "US",
                    }
                },
                "statusCode": "delivered",
                "status": "Delivered",
                "description": "已派送",
            },
            "details": {
                "product": {"productName": "EXPRESS WORLDWIDE"},
                "totalNumberOfPieces": 4,
                "pieceIds": [
                    "JD014600012592400513",
                    "JD014600012592400514",
                    "JD014600012592400515",
                    "JD014600012592400516",
                ],
                "proofOfDelivery": {
                    "documentUrl": "https://example/pod.pdf",
                    "signatureUrl": "https://example/sig.png",
                },
            },
            "events": [
                {
                    "timestamp": "2026-05-06T14:31:00",
                    "location": {
                        "address": {
                            "addressLocality": "NEW YORK, NY - USA",
                        }
                    },
                    "description": "Delivered",
                    "pieceIds": [
                        "JD014600012592400513",
                        "JD014600012592400514",
                    ],
                },
                {
                    "timestamp": "2026-05-05T08:12:00",
                    "location": {"address": {"addressLocality": "CINCINNATI HUB - USA"}},
                    "description": "Departure from facility",
                },
            ],
        }
    ]
}


class TestParseShipment(unittest.TestCase):
    def test_basic_fields(self):
        s = _parse_shipment(_FAKE_RAW)
        self.assertIsInstance(s, Shipment)
        self.assertEqual(s.tracking_number, "4191468945")
        self.assertEqual(s.status, "delivered")
        self.assertEqual(s.status_description, "已派送")
        self.assertEqual(s.product, "EXPRESS WORLDWIDE")
        self.assertEqual(s.total_pieces, 4)
        self.assertEqual(s.last_location, "NEW YORK, NY - USA")
        self.assertEqual(s.origin, "HONG KONG - HONG KONG, HONG KONG")
        self.assertEqual(s.destination, "NEW YORK, NY - USA")

    def test_piece_ids_from_details(self):
        s = _parse_shipment(_FAKE_RAW)
        self.assertEqual(
            s.piece_ids,
            [
                "JD014600012592400513",
                "JD014600012592400514",
                "JD014600012592400515",
                "JD014600012592400516",
            ],
        )

    def test_pod_urls(self):
        s = _parse_shipment(_FAKE_RAW)
        self.assertEqual(s.proof_of_delivery_url, "https://example/pod.pdf")
        self.assertEqual(s.signature_url, "https://example/sig.png")

    def test_events(self):
        s = _parse_shipment(_FAKE_RAW)
        self.assertEqual(len(s.events), 2)
        ev = s.events[0]
        self.assertIsInstance(ev, TrackingEvent)
        self.assertEqual(ev.description, "Delivered")
        self.assertEqual(ev.location, "NEW YORK, NY - USA")
        self.assertEqual(
            ev.piece_ids,
            ["JD014600012592400513", "JD014600012592400514"],
        )

    def test_piece_ids_fallback_from_events(self):
        """details.pieceIds 缺失时应能从 events 中正则回退提取。"""
        raw = {
            "shipments": [
                {
                    "id": "X",
                    "status": {},
                    "details": {"product": {"productName": "P"}},
                    "events": [
                        {"description": "JD014600012591699173 leaving facility"}
                    ],
                }
            ]
        }
        s = _parse_shipment(raw)
        self.assertIn("JD014600012591699173", s.piece_ids)

    def test_empty_shipments_raises(self):
        with self.assertRaises(ValueError):
            _parse_shipment({"shipments": []})


class TestFormatShipment(unittest.TestCase):
    def test_renders_known_sections(self):
        s = _parse_shipment(_FAKE_RAW)
        text = format_shipment(s)
        self.assertIn("4191468945", text)
        self.assertIn("已派送", text)
        self.assertIn("EXPRESS WORLDWIDE", text)
        self.assertIn("JD014600012592400513", text)
        self.assertIn("时间线", text)


class TestShipmentToDict(unittest.TestCase):
    def test_keys(self):
        s = _parse_shipment(_FAKE_RAW)
        d = shipment_to_dict(s)
        for k in ("tracking_number", "status", "piece_ids", "events", "raw"):
            self.assertIn(k, d)
        self.assertEqual(d["tracking_number"], "4191468945")
        self.assertEqual(d["events"][0]["description"], "Delivered")


class TestResolveCdpEndpoint(unittest.TestCase):
    def setUp(self) -> None:
        self._old = os.environ.get("CHROME_CDP_ENDPOINT")

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("CHROME_CDP_ENDPOINT", None)
        else:
            os.environ["CHROME_CDP_ENDPOINT"] = self._old

    def test_default_when_no_env_no_explicit(self):
        os.environ.pop("CHROME_CDP_ENDPOINT", None)
        self.assertEqual(resolve_cdp_endpoint(None), DEFAULT_CDP_ENDPOINT)

    def test_env_overrides_default(self):
        os.environ["CHROME_CDP_ENDPOINT"] = "http://env-host:9999"
        self.assertEqual(resolve_cdp_endpoint(None), "http://env-host:9999")

    def test_explicit_overrides_env(self):
        os.environ["CHROME_CDP_ENDPOINT"] = "http://env-host:9999"
        self.assertEqual(
            resolve_cdp_endpoint("http://explicit:1234"),
            "http://explicit:1234",
        )


if __name__ == "__main__":
    unittest.main()
