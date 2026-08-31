"""Explicitly gated four-request KRX K1 smoke-test command."""

from __future__ import annotations

import argparse
from pathlib import Path

from .collector import collect_one
from .services import KRX_SERVICES
from .store import ImmutableRawStore
from .transport import EnvironmentAuthKeyProvider, KrxConfigurationError, KrxTransport


def require_network_access(network: bool, provider: EnvironmentAuthKeyProvider) -> None:
    if not network:
        raise KrxConfigurationError(
            "network access requires an explicit --network flag"
        )
    provider.get_auth_key()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the four-request KRX K1 smoke test"
    )
    parser.add_argument("--network", action="store_true")
    parser.add_argument(
        "--bas-dd", required=True, help="confirmed trading day YYYYMMDD"
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--root", type=Path, default=Path("data/raw/krx"))
    args = parser.parse_args()
    provider = EnvironmentAuthKeyProvider()
    require_network_access(args.network, provider)
    transport = KrxTransport()
    store = ImmutableRawStore(args.root)
    for service in KRX_SERVICES.values():
        event = collect_one(
            service,
            bas_dd=args.bas_dd,
            auth_key_provider=provider,
            transport=transport,
            store=store,
            timeout=args.timeout,
        )
        print(f"{event.service_id} {event.market} {event.bas_dd}: {event.status.value}")


if __name__ == "__main__":
    main()
