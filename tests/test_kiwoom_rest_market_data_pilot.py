from __future__ import annotations

import io
import json
import logging
import unittest

from src.kiwoom_rest.auth import DemoConfig, TokenInfo
from src.kiwoom_rest.market_data_pilot import (
    DAILY_API_ID,
    DAILY_LIST_KEY,
    ChartHttpResult,
    fetch_chart_pages,
    parse_chart_rows,
    parse_pagination_headers,
)


class KiwoomMarketDataPilotTests(unittest.TestCase):
    config = DemoConfig("demo", "app-key-secret", "secret-key-secret")
    token = TokenInfo("access-token-secret", "bearer", "20260831120000", 200)

    def result(self, rows, headers=None):
        return ChartHttpResult(200, json.dumps({
            "stk_cd": "005930", DAILY_LIST_KEY: rows,
            "return_code": 0, "return_msg": "정상",
        }).encode(), headers or {})

    def test_normal_response_parsing(self):
        rows = [{"dt": "20260828", "open_pric": "70000", "high_pric": "71000",
                 "low_pric": "69000", "cur_prc": "70500", "trde_qty": "100"}]
        diagnostics = fetch_chart_pages(
            self.config, self.token, DAILY_API_ID, {}, DAILY_LIST_KEY, "dt", "YYYYMMDD",
            transport=lambda *_: self.result(rows), max_pages=1, page_delay=0,
        )
        self.assertTrue(diagnostics.api_succeeded)
        self.assertEqual([200], diagnostics.page_http_statuses)
        self.assertEqual(1, diagnostics.total_rows)
        self.assertEqual(0, diagnostics.numeric_conversion_failures)
        self.assertEqual("20260828", diagnostics.earliest_timestamp)

    def test_empty_response(self):
        rows, malformed = parse_chart_rows({}, DAILY_LIST_KEY)
        self.assertEqual([], rows)
        self.assertEqual(0, malformed)

    def test_malformed_row_is_counted(self):
        rows, malformed = parse_chart_rows({DAILY_LIST_KEY: [{"dt": "20260828"}, "bad"]}, DAILY_LIST_KEY)
        self.assertEqual([{"dt": "20260828"}], rows)
        self.assertEqual(1, malformed)

    def test_pagination_headers_are_case_insensitive_and_forwarded(self):
        calls = []

        def transport(url, headers, body):
            calls.append(dict(headers))
            if len(calls) == 1:
                return self.result([{"dt": "20260828"}], {"Cont-Yn": "Y", "Next-Key": "opaque"})
            return self.result([{"dt": "20260827"}], {"cont-yn": "N"})

        diagnostics = fetch_chart_pages(
            self.config, self.token, DAILY_API_ID, {}, DAILY_LIST_KEY, "dt", "YYYYMMDD",
            transport=transport, max_pages=2, page_delay=0,
        )
        self.assertEqual(("Y", "opaque"), parse_pagination_headers({"Cont-Yn": "Y", "Next-Key": "opaque"}))
        self.assertEqual("Y", calls[1]["cont-yn"])
        self.assertEqual("opaque", calls[1]["next-key"])
        self.assertEqual([1, 1], diagnostics.page_row_counts)

    def test_token_and_credentials_are_not_logged(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("src.kiwoom_rest.market_data_pilot")
        old_level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            fetch_chart_pages(
                self.config, self.token, DAILY_API_ID, {}, DAILY_LIST_KEY, "dt", "YYYYMMDD",
                transport=lambda *_: self.result([]), max_pages=1, page_delay=0,
            )
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
        output = stream.getvalue()
        for secret in (self.config.app_key, self.config.secret_key, self.token.token):
            self.assertNotIn(secret, output)


if __name__ == "__main__":
    unittest.main()
