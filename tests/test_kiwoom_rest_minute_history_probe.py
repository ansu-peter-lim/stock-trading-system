from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from src.kiwoom_rest.auth import DemoConfig, TokenInfo
from src.kiwoom_rest.market_data_pilot import ChartHttpResult
from src.kiwoom_rest.minute_history_probe import MINUTE_LIST_KEY, probe_minute_history


def row(stamp: str, price: str = "+70000", signal: str = "2"):
    return {
        "cntr_tm": stamp, "cur_prc": price, "open_pric": price,
        "high_pric": price, "low_pric": price, "trde_qty": "10",
        "pred_pre_sig": signal,
    }


class MinuteHistoryProbeMockTests(unittest.TestCase):
    config = DemoConfig("demo", "demo-app-secret", "demo-secret-secret")
    token = TokenInfo("token-secret", "bearer", "20260831120000", 200)

    def run_probe(self, transport, **kwargs):
        with patch("src.kiwoom_rest.minute_history_probe.load_demo_config", return_value=self.config), patch(
            "src.kiwoom_rest.minute_history_probe.issue_demo_token", return_value=self.token
        ):
            return probe_minute_history(transport=transport, page_delay=0, **kwargs)

    @staticmethod
    def response(rows, cont="N", key=""):
        body = json.dumps({MINUTE_LIST_KEY: rows, "return_code": 0}).encode()
        return ChartHttpResult(200, body, {"cont-yn": cont, "next-key": key})

    def test_stops_when_continuation_is_exhausted(self):
        report = self.run_probe(lambda *_: self.response([row("20260828153000")]))
        self.assertEqual("continuation_exhausted", report.stop_reason)
        self.assertTrue(report.actual_api_cutoff_reached)

    def test_stops_at_target_date(self):
        report = self.run_probe(
            lambda *_: self.response([row("20230901100000")], "Y", "more"),
            target_date="20230901",
        )
        self.assertEqual("target_date_reached", report.stop_reason)
        self.assertTrue(report.target_date_reached)

    def test_max_pages_and_duplicates_and_signs(self):
        responses = [
            self.response([row("20260828153000", "+10", "2"), row("20260828152500", "-9", "5")], "Y", "a"),
            self.response([row("20260828152500", "8", "3")], "Y", "b"),
        ]
        report = self.run_probe(lambda *_: responses.pop(0), max_pages=2)
        self.assertEqual("max_pages", report.stop_reason)
        self.assertEqual(1, report.page_boundary_duplicate_count)
        self.assertEqual(1, report.duplicate_timestamp_count)
        self.assertEqual(4, report.plus_price_string_count)
        self.assertEqual(4, report.minus_price_string_count)
        self.assertEqual(4, report.unsigned_price_string_count)

    def test_missing_next_key_stops(self):
        report = self.run_probe(lambda *_: self.response([], "Y", ""))
        self.assertEqual("next_key_missing", report.stop_reason)

    def test_no_secret_is_in_report(self):
        report = self.run_probe(lambda *_: self.response([]))
        serialized = json.dumps(report.__dict__, ensure_ascii=False)
        for secret in (self.config.app_key, self.config.secret_key, self.token.token):
            self.assertNotIn(secret, serialized)

if __name__ == "__main__":
    unittest.main()
