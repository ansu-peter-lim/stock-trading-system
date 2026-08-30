from __future__ import annotations

import os
import unittest

from src.kiwoom_rest.minute_history_probe import probe_minute_history


@unittest.skipUnless(
    os.environ.get("KIWOOM_RUN_NETWORK_TESTS") == "1",
    "set KIWOOM_RUN_NETWORK_TESTS=1 for an explicit demo-network test",
)
class MinuteHistoryProbeNetworkTests(unittest.TestCase):
    def test_one_demo_page(self):
        report = probe_minute_history(max_pages=1)
        self.assertEqual("demo", report.environment)
        self.assertEqual([200], report.page_http_statuses)


if __name__ == "__main__":
    unittest.main()
