"""Deterministic, injectable collector for one Kiwoom Daily series."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from src.kiwoom_rest.auth import (
    DEMO_BASE_URL,
    ConfigurationError,
    DemoConfig,
    KiwoomApiError,
    TokenInfo,
)
from src.kiwoom_rest.market_data_pilot import (
    CHART_PATH,
    ChartTransport,
    _chart_transport,
    parse_pagination_headers,
)

from .models import (
    API_ID,
    CollectedDailySeries,
    DailyCollectionRequest,
    DailyPipelineIssue,
    KiwoomDailyValidationError,
    PageProvenance,
    PriceBasis,
    VolumeBasis,
)
from .parser import parse_daily_page
from .store import (
    ImmutableKiwoomDailyStore,
    ManifestStatus,
    continuation_identity,
    manifest_event,
)

Clock = Callable[[], datetime]


def collect_daily_series(
    request: DailyCollectionRequest,
    *,
    config: DemoConfig,
    token: TokenInfo,
    store: ImmutableKiwoomDailyStore,
    transport: ChartTransport = _chart_transport,
    max_pages: int = 10,
    page_delay: float = 1.1,
    clock: Clock | None = None,
) -> CollectedDailySeries:
    """Collect until a source row reaches ``request.start_date``.

    The transport is injectable so normal tests never use the network.  The
    function never issues a token and never serializes request headers.
    """

    if not isinstance(request, DailyCollectionRequest):
        raise TypeError("request must be DailyCollectionRequest")
    if config.environment != "demo" or config.base_url != DEMO_BASE_URL:
        raise ConfigurationError("Only the fixed Kiwoom demo endpoint is allowed")
    if not 1 <= max_pages <= 100:
        raise ValueError("max_pages must be between 1 and 100")
    if page_delay < 0:
        raise ValueError("page_delay must not be negative")
    now = clock or (lambda: datetime.now(UTC))

    parsed_rows = []
    page_provenance = []
    seen_dates = set()
    request_continuation_key = ""
    reached_start = False

    for sequence in range(1, max_pages + 1):
        headers = _request_headers(token, sequence, request_continuation_key)
        body = {
            "stk_cd": request.stock_code,
            "base_dt": request.base_date,
            "upd_stkpc_tp": request.price_basis.request_value,
        }
        try:
            result = transport(config.base_url + CHART_PATH, headers, body)
        except KiwoomApiError as exc:
            raise KiwoomDailyValidationError(
                DailyPipelineIssue.HTTP_ERROR,
                "ka10081 request failed before a response was available",
            ) from exc
        retrieved_at = now().astimezone(UTC).isoformat().replace("+00:00", "Z")
        stored = store.store_page(
            request,
            pagination_sequence=sequence,
            raw_bytes=result.body,
        )
        response_continuation, response_next_key = parse_pagination_headers(
            result.headers
        )
        try:
            if not 200 <= result.status < 300:
                raise KiwoomDailyValidationError(
                    DailyPipelineIssue.HTTP_ERROR,
                    "ka10081 request returned a non-success HTTP status",
                )
            parsed_page = parse_daily_page(
                result.body,
                request,
                source_page=sequence,
                artifact_sha256=stored.raw_file_sha256,
            )
        except KiwoomDailyValidationError as exc:
            store.append_manifest(
                manifest_event(
                    request,
                    pagination_sequence=sequence,
                    request_continuation_key=request_continuation_key,
                    response_continuation_key=response_next_key,
                    retrieved_at=retrieved_at,
                    stored=stored,
                    row_count=None,
                    status=(
                        ManifestStatus.HTTP_ERROR
                        if exc.issue is DailyPipelineIssue.HTTP_ERROR
                        else ManifestStatus.VALIDATION_ERROR
                    ),
                    error_code=exc.issue.value,
                )
            )
            raise

        store.append_manifest(
            manifest_event(
                request,
                pagination_sequence=sequence,
                request_continuation_key=request_continuation_key,
                response_continuation_key=response_next_key,
                retrieved_at=retrieved_at,
                stored=stored,
                row_count=len(parsed_page.rows),
                status=ManifestStatus.SUCCESS,
            )
        )
        page_provenance.append(
            PageProvenance(
                provider="KIWOOM",
                api_id=API_ID,
                stock_code=request.stock_code,
                price_basis=request.price_basis,
                base_date=request.base_date,
                pagination_sequence=sequence,
                request_continuation_identity=continuation_identity(
                    request_continuation_key
                ),
                response_continuation_identity=continuation_identity(response_next_key),
                retrieved_at=retrieved_at,
                raw_file_path=stored.raw_file_path,
                raw_file_sha256=stored.raw_file_sha256,
                row_count=len(parsed_page.rows),
            )
        )

        for row in parsed_page.rows:
            if row.trade_date in seen_dates:
                raise KiwoomDailyValidationError(
                    DailyPipelineIssue.DUPLICATE_DAILY_DATE,
                    "duplicate trade date across ka10081 pages",
                )
            seen_dates.add(row.trade_date)
            if request.start_date <= row.trade_date <= request.end_date:
                parsed_rows.append(row)
        if (
            parsed_page.rows
            and min(row.trade_date for row in parsed_page.rows) <= request.start_date
        ):
            reached_start = True
            break
        if response_continuation != "Y" or not response_next_key:
            break
        request_continuation_key = response_next_key
        if sequence < max_pages and page_delay:
            time.sleep(page_delay)

    if not reached_start:
        raise KiwoomDailyValidationError(
            DailyPipelineIssue.REQUIRED_START_NOT_REACHED,
            "ka10081 pagination ended before required start_date was reached",
        )
    volume_basis = (
        VolumeBasis.RAW
        if request.price_basis is PriceBasis.RAW
        else VolumeBasis.PROVIDER_ADJUSTED_UNKNOWN_POLICY
    )
    return CollectedDailySeries(
        request=request,
        rows=tuple(parsed_rows),
        pages=tuple(page_provenance),
        volume_basis=volume_basis,
    )


def _request_headers(
    token: TokenInfo,
    sequence: int,
    continuation_key: str,
) -> Mapping[str, str]:
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"{token.token_type.title()} {token.token}",
        "api-id": API_ID,
    }
    if sequence > 1:
        headers["cont-yn"] = "Y"
        headers["next-key"] = continuation_key
    return headers
