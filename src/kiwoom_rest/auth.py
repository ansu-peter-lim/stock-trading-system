"""Demo-only Kiwoom REST authentication and read-only account pilot.

No token, credential, or account number is persisted or logged by this module.
The real environment is intentionally rejected before any HTTP request.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


LOGGER = logging.getLogger(__name__)
DEMO_BASE_URL = "https://mockapi.kiwoom.com"
TOKEN_PATH = "/oauth2/token"
ACCOUNT_PATH = "/api/dostk/acnt"
ACCOUNT_API_ID = "ka00001"


class ConfigurationError(RuntimeError):
    """Safe configuration error that never embeds a secret value."""


class KiwoomApiError(RuntimeError):
    """Safe API error that never embeds response bodies or credentials."""


@dataclass(frozen=True)
class DemoConfig:
    environment: str
    app_key: str
    secret_key: str
    base_url: str = DEMO_BASE_URL


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: bytes


@dataclass(frozen=True)
class TokenInfo:
    token: str
    token_type: str
    expires_at: str
    http_status: int


@dataclass(frozen=True)
class PilotResult:
    environment: str
    token_http_status: int
    token_succeeded: bool
    token_type: str
    expires_at: str
    account_http_status: int | None
    account_lookup_succeeded: bool


Transport = Callable[[str, Mapping[str, str], Mapping[str, str]], HttpResult]


def mask_credential(value: str, visible: int = 3) -> str:
    """Return a non-reversible display form; short values are fully masked."""
    if not value:
        return "<empty>"
    if len(value) <= visible * 2:
        return "*" * len(value)
    return f"{value[:visible]}{'*' * (len(value) - visible * 2)}{value[-visible:]}"


def load_demo_config(
    env_path: Path = Path(".env"), environ: Mapping[str, str] | None = None,
) -> DemoConfig:
    if environ is None:
        if load_dotenv is None:
            raise ConfigurationError("python-dotenv is required")
        load_dotenv(env_path, override=False)
        environ = os.environ

    environment = environ.get("KIWOOM_ENV", "demo").strip().lower()
    if environment not in {"demo", "real"}:
        raise ConfigurationError("KIWOOM_ENV must be 'demo' or 'real'")
    if environment == "real":
        raise ConfigurationError("Real-environment API calls are disabled in this pilot")

    app_key = environ.get("KIWOOM_DEMO_APP_KEY", "").strip()
    secret_key = environ.get("KIWOOM_DEMO_SECRET_KEY", "").strip()
    missing = [name for name, value in (
        ("KIWOOM_DEMO_APP_KEY", app_key),
        ("KIWOOM_DEMO_SECRET_KEY", secret_key),
    ) if not value]
    if missing:
        # Never substitute the configured real credentials.
        raise ConfigurationError("Missing required demo credential variable(s): " + ", ".join(missing))
    return DemoConfig(environment, app_key, secret_key)


def _urllib_transport(
    url: str, headers: Mapping[str, str], body: Mapping[str, str],
) -> HttpResult:
    request = Request(
        url,
        data=json.dumps(dict(body), separators=(",", ":")).encode("utf-8"),
        headers=dict(headers),
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            return HttpResult(response.status, response.read())
    except HTTPError as exc:
        raise KiwoomApiError(f"Kiwoom API returned HTTP status {exc.code}") from exc
    except URLError as exc:
        raise KiwoomApiError("Kiwoom API connection failed") from exc


def _json_object(result: HttpResult, operation: str) -> dict[str, object]:
    if not 200 <= result.status < 300:
        raise KiwoomApiError(f"{operation} returned HTTP status {result.status}")
    try:
        payload = json.loads(result.body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KiwoomApiError(f"{operation} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise KiwoomApiError(f"{operation} returned an unexpected JSON shape")
    return payload


def issue_demo_token(config: DemoConfig, transport: Transport = _urllib_transport) -> TokenInfo:
    if config.environment != "demo" or config.base_url != DEMO_BASE_URL:
        raise ConfigurationError("Only the fixed Kiwoom demo endpoint is allowed")
    result = transport(
        config.base_url + TOKEN_PATH,
        {"Content-Type": "application/json;charset=UTF-8"},
        {"grant_type": "client_credentials", "appkey": config.app_key,
         "secretkey": config.secret_key},
    )
    payload = _json_object(result, "Token request")
    if payload.get("return_code") not in (None, 0):
        raise KiwoomApiError("Token request was rejected by Kiwoom")
    token = payload.get("token")
    token_type = payload.get("token_type")
    expires_at = payload.get("expires_dt")
    if not all(isinstance(value, str) and value for value in (token, token_type, expires_at)):
        raise KiwoomApiError("Token response is missing required fields")
    LOGGER.info("Demo token issued: type=%s expires_at=%s token=%s",
                token_type, expires_at, mask_credential(token))
    return TokenInfo(token, token_type, expires_at, result.status)


def lookup_demo_accounts(
    config: DemoConfig, token: TokenInfo, transport: Transport = _urllib_transport,
) -> tuple[int, bool]:
    if config.environment != "demo" or config.base_url != DEMO_BASE_URL:
        raise ConfigurationError("Only the fixed Kiwoom demo endpoint is allowed")
    authorization = f"{token.token_type.title()} {token.token}"
    result = transport(
        config.base_url + ACCOUNT_PATH,
        {"Content-Type": "application/json;charset=UTF-8",
         "authorization": authorization, "api-id": ACCOUNT_API_ID},
        {},
    )
    payload = _json_object(result, "Account lookup")
    succeeded = payload.get("return_code") in (None, 0) and bool(payload.get("acctNo"))
    LOGGER.info("Demo read-only account lookup completed: http_status=%d success=%s",
                result.status, succeeded)
    return result.status, succeeded


def run_pilot(transport: Transport = _urllib_transport) -> PilotResult:
    config = load_demo_config()
    token = issue_demo_token(config, transport)
    account_status, account_succeeded = lookup_demo_accounts(config, token, transport)
    return PilotResult(
        environment=config.environment,
        token_http_status=token.http_status,
        token_succeeded=True,
        token_type=token.token_type,
        expires_at=token.expires_at,
        account_http_status=account_status,
        account_lookup_succeeded=account_succeeded,
    )


def main() -> None:
    argparse.ArgumentParser(description="Kiwoom demo authentication/read-only pilot").parse_args()
    result = run_pilot()
    # dataclass fields are deliberately safe and contain no token or account number.
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
