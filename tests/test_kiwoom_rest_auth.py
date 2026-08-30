from __future__ import annotations

import io
import json
import logging
import unittest

from src.kiwoom_rest.auth import (
    ACCOUNT_PATH,
    DEMO_BASE_URL,
    TOKEN_PATH,
    ConfigurationError,
    DemoConfig,
    HttpResult,
    issue_demo_token,
    load_demo_config,
    mask_credential,
)


class KiwoomRestAuthTests(unittest.TestCase):
    def demo_env(self):
        return {
            "KIWOOM_ENV": "demo",
            "KIWOOM_DEMO_APP_KEY": "demo-app-key",
            "KIWOOM_DEMO_SECRET_KEY": "demo-secret-key",
        }

    def test_demo_environment_loads(self):
        config = load_demo_config(environ=self.demo_env())
        self.assertEqual("demo", config.environment)
        self.assertEqual(DEMO_BASE_URL, config.base_url)

    def test_environment_defaults_to_demo_not_real(self):
        values = self.demo_env()
        values.pop("KIWOOM_ENV")
        self.assertEqual("demo", load_demo_config(environ=values).environment)

    def test_missing_demo_value(self):
        values = self.demo_env()
        values.pop("KIWOOM_DEMO_SECRET_KEY")
        with self.assertRaisesRegex(ConfigurationError, "KIWOOM_DEMO_SECRET_KEY"):
            load_demo_config(environ=values)

    def test_invalid_environment(self):
        values = self.demo_env() | {"KIWOOM_ENV": "staging"}
        with self.assertRaisesRegex(ConfigurationError, "demo.*real"):
            load_demo_config(environ=values)

    def test_real_environment_is_blocked(self):
        values = self.demo_env() | {
            "KIWOOM_ENV": "real", "KIWOOM_REAL_APP_KEY": "real-app",
            "KIWOOM_REAL_SECRET_KEY": "real-secret",
        }
        with self.assertRaisesRegex(ConfigurationError, "disabled"):
            load_demo_config(environ=values)

    def test_demo_does_not_fall_back_to_real_credentials(self):
        values = {
            "KIWOOM_ENV": "demo", "KIWOOM_REAL_APP_KEY": "real-app",
            "KIWOOM_REAL_SECRET_KEY": "real-secret",
        }
        with self.assertRaises(ConfigurationError):
            load_demo_config(environ=values)

    def test_token_value_is_not_exposed_in_log(self):
        secret_token = "token-that-must-never-appear-in-full"

        def transport(url, headers, body):
            self.assertEqual(DEMO_BASE_URL + TOKEN_PATH, url)
            self.assertEqual("client_credentials", body["grant_type"])
            return HttpResult(200, json.dumps({
                "expires_dt": "20260831000000", "token_type": "bearer",
                "token": secret_token, "return_code": 0,
            }).encode())

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("src.kiwoom_rest.auth")
        old_level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            token = issue_demo_token(DemoConfig("demo", "app", "secret"), transport)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
        self.assertEqual(secret_token, token.token)
        self.assertNotIn(secret_token, stream.getvalue())
        self.assertIn(mask_credential(secret_token), stream.getvalue())

    def test_credential_masking(self):
        value = "abcdefghij"
        masked = mask_credential(value)
        self.assertEqual("abc****hij", masked)
        self.assertNotEqual(value, masked)
        self.assertEqual("***", mask_credential("abc"))

    def test_no_order_endpoint_constant_is_used(self):
        self.assertEqual("/oauth2/token", TOKEN_PATH)
        self.assertEqual("/api/dostk/acnt", ACCOUNT_PATH)


if __name__ == "__main__":
    unittest.main()
