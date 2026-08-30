"""Small, reproducible pilot for KRX Open API historical master snapshots.

The authentication key is read only from ``KRX_AUTH_KEY`` in the environment
or a local ``.env`` file.  It is sent as an HTTP header and is never persisted.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.stock_mapping.normalization import normalize_stock_name

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - a clear error is raised by load_auth_key
    load_dotenv = None


ENDPOINTS = {
    "KOSPI": "https://data-dbg.krx.co.kr/svc/apis/sto/stk_isu_base_info",
    "KOSDAQ": "https://data-dbg.krx.co.kr/svc/apis/sto/ksq_isu_base_info",
}

# Eight dates only: period anchors plus known delisting/name-change boundaries.
PILOT_DATES = (
    "2023-09-01",  # analysis-period start
    "2023-11-06",  # ISU 181340 (Izmedia), last trading day before delisting
    "2023-11-07",  # ISU 181340, delisting date / first post-trading snapshot
    "2024-04-18",  # ISU 141080, before LigaChem name-change listing
    "2024-04-19",  # ISU 141080, name-change listing date
    "2025-01-24",  # middle period and Hyungji I&C NFKC search case
    "2026-08-27",  # recent period
    "2026-08-28",  # analysis-period end
)

MANIFEST_FIELDS = (
    "source_name", "service_name", "market", "requested_base_date",
    "retrieved_at", "request_parameters", "raw_file_path",
    "raw_file_sha256", "row_count", "http_status",
)


@dataclass(frozen=True)
class FetchResult:
    body: bytes
    http_status: int
    retrieved_at: str


def load_auth_key(env_path: Path = Path(".env")) -> str:
    """Load the secret without printing or returning it in diagnostics."""
    if load_dotenv is None:
        raise RuntimeError("python-dotenv is required: python -m pip install python-dotenv")
    load_dotenv(env_path, override=False)
    key = os.environ.get("KRX_AUTH_KEY", "").strip()
    if not key:
        raise RuntimeError("KRX_AUTH_KEY is not configured in the environment or .env")
    return key


def fetch_snapshot(endpoint: str, base_date: str, auth_key: str) -> FetchResult:
    bas_dd = base_date.replace("-", "")
    request = Request(f"{endpoint}?basDd={bas_dd}", headers={"AUTH_KEY": auth_key})
    try:
        with urlopen(request, timeout=30) as response:
            return FetchResult(
                response.read(), response.status,
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
    except HTTPError as exc:
        # Do not write error bodies as successful raw snapshots.
        raise RuntimeError(f"KRX request failed with HTTP status {exc.code}") from exc


def response_rows(body: bytes) -> list[dict[str, object]]:
    payload = json.loads(body.decode("utf-8-sig"))
    rows = payload.get("OutBlock_1")
    if not isinstance(rows, list):
        raise ValueError("KRX response does not contain a list-valued OutBlock_1")
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError("KRX OutBlock_1 contains a non-object row")
    return rows


def normalized_search_name(value: str) -> str:
    """Normalize only a search key; raw API and TOP30 names stay untouched."""
    return normalize_stock_name(value)


def collect_pilot(
    root: Path = Path("data/raw/krx/master/openapi"),
    manifest_path: Path | None = None,
    dates: tuple[str, ...] = PILOT_DATES,
    fetcher: Callable[[str, str, str], FetchResult] = fetch_snapshot,
) -> list[dict[str, object]]:
    """Collect only explicitly supplied pilot dates and write byte-exact JSON."""
    auth_key = load_auth_key()
    manifest_path = manifest_path or root / "manifest.csv"
    records: list[dict[str, object]] = []
    for base_date in dates:
        datetime.strptime(base_date, "%Y-%m-%d")
        for market, endpoint in ENDPOINTS.items():
            result = fetcher(endpoint, base_date, auth_key)
            rows = response_rows(result.body)  # validate before persisting
            relative = Path(market.lower()) / f"{base_date}.json"
            raw_path = root / relative
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(result.body)
            records.append({
                "source_name": "KRX Open API",
                "service_name": f"{market} issue basic information",
                "market": market,
                "requested_base_date": base_date,
                "retrieved_at": result.retrieved_at,
                "request_parameters": json.dumps({"basDd": base_date.replace('-', '')},
                                                  ensure_ascii=False, sort_keys=True),
                "raw_file_path": raw_path.as_posix(),
                "raw_file_sha256": hashlib.sha256(result.body).hexdigest(),
                "row_count": len(rows),
                "http_status": result.http_status,
            })
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        writer.writerows(records)
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect the fixed KRX historical-master pilot")
    parser.add_argument("--root", type=Path, default=Path("data/raw/krx/master/openapi"))
    args = parser.parse_args()
    records = collect_pilot(root=args.root)
    print(f"Collected {len(records)} KRX pilot snapshots.")


if __name__ == "__main__":
    main()
