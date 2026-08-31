"""Secret-safe HTTP transport for KRX Open API requests."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .services import KrxServiceDefinition

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - exercised only without the dependency
    load_dotenv = None


class KrxConfigurationError(RuntimeError):
    """Safe configuration failure that never embeds credential material."""


class KrxHttpError(RuntimeError):
    """Safe HTTP/network failure suitable for a non-secret manifest."""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


class AuthKeyProvider(Protocol):
    def get_auth_key(self) -> str: ...


@dataclass(frozen=True, slots=True)
class EnvironmentAuthKeyProvider:
    """Read KRX_AUTH_KEY from process environment or an optional local .env."""

    env_path: Path | None = Path(".env")
    environ: Mapping[str, str] | None = field(default=None, repr=False)

    def get_auth_key(self) -> str:
        if self.env_path is not None:
            if load_dotenv is None:
                raise KrxConfigurationError(
                    "python-dotenv is required when an env_path is configured"
                )
            load_dotenv(self.env_path, override=False)
        source = os.environ if self.environ is None else self.environ
        key = source.get("KRX_AUTH_KEY", "").strip()
        if not key:
            raise KrxConfigurationError("KRX_AUTH_KEY is required for network access")
        return key


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    service_id: str
    market: str
    bas_dd: str


@dataclass(frozen=True, slots=True)
class TransportResult:
    identity: RequestIdentity
    http_status: int
    raw_bytes: bytes
    retrieved_at: str


def validate_bas_dd(value: str) -> str:
    try:
        parsed = date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:8]}")
    except (TypeError, ValueError) as exc:
        raise KrxConfigurationError("basDd must be a valid YYYYMMDD date") from exc
    if len(value) != 8 or not value.isascii() or not value.isdigit():
        raise KrxConfigurationError("basDd must be a valid YYYYMMDD date")
    return parsed.strftime("%Y%m%d")


class KrxTransport:
    def __init__(
        self,
        *,
        opener: Callable[..., object] = urlopen,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._opener = opener
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def fetch(
        self,
        service: KrxServiceDefinition,
        *,
        bas_dd: str,
        auth_key_provider: AuthKeyProvider,
        timeout: float = 30.0,
    ) -> TransportResult:
        canonical_date = validate_bas_dd(bas_dd)
        if timeout <= 0 or timeout > 120:
            raise KrxConfigurationError("timeout must be in (0, 120] seconds")
        auth_key = auth_key_provider.get_auth_key()
        url = f"{service.endpoint}?{urlencode({'basDd': canonical_date})}"
        request = Request(url, headers={"AUTH_KEY": auth_key})
        try:
            with self._opener(request, timeout=timeout) as response:
                status = int(response.status)
                body = response.read()
        except HTTPError as exc:
            raise KrxHttpError(
                f"KRX request failed with HTTP status {exc.code}",
                http_status=exc.code,
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise KrxHttpError(
                "KRX request failed before a response was received"
            ) from exc
        retrieved_at = (
            self._clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        return TransportResult(
            RequestIdentity(service.service_id, service.market, canonical_date),
            status,
            body,
            retrieved_at,
        )
