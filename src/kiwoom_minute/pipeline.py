"""Strict ka10080 parser, immutable store, and opaque source-bar adapter."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from datetime import time as clock_time
from decimal import Decimal
from enum import Enum
from pathlib import Path

from src.backtest_engine.events import stable_id
from src.backtest_engine.models import Ohlcv
from src.backtest_engine.validation import (
    KOREA_TZ,
    MarketDataValidationError,
    validate_ohlcv,
)
from src.kiwoom_daily.models import require_sha256, require_stock_code
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

API_ID = "ka10080"
LIST_KEY = "stk_min_pole_chart_qry"
ASSUMPTION_ID = "EXPERIMENTAL_KA10080_SEQUENCE_SEMANTICS"
PARSER_ID = "KIWOOM_KA10080_SOURCE_BAR_PARSER_V1"
SCHEMA_VERSION = "kiwoom-ka10080-source-bar-v1"
SIGNED_MAGNITUDE = re.compile(r"[+-]?[0-9]+", flags=re.ASCII)
UNSIGNED_INTEGER = re.compile(r"[0-9]+", flags=re.ASCII)
SOURCE_FIELDS = ("cntr_tm", "open_pric", "high_pric", "low_pric", "cur_prc", "trde_qty")


class MinutePriceBasis(str, Enum):
    RAW = "RAW"
    ADJUSTED = "ADJUSTED"

    @property
    def request_value(self) -> str:
        return "0" if self is MinutePriceBasis.RAW else "1"


class MinutePipelineIssue(str, Enum):
    INVALID_DATE_RANGE = "INVALID_DATE_RANGE"
    HTTP_ERROR = "HTTP_ERROR"
    API_ERROR = "API_ERROR"
    SCHEMA_ERROR = "SCHEMA_ERROR"
    STOCK_MISMATCH = "STOCK_MISMATCH"
    MALFORMED_ROW = "MALFORMED_ROW"
    MALFORMED_PRICE = "MALFORMED_PRICE"
    MALFORMED_VOLUME = "MALFORMED_VOLUME"
    INVALID_OHLCV = "INVALID_OHLCV"
    DUPLICATE_SOURCE_LABEL = "DUPLICATE_SOURCE_LABEL"
    PAGINATION_ERROR = "PAGINATION_ERROR"
    REQUIRED_START_NOT_REACHED = "REQUIRED_START_NOT_REACHED"
    PROVENANCE_ERROR = "PROVENANCE_ERROR"
    RAW_SIGNAL_ALIGNMENT_ERROR = "RAW_SIGNAL_ALIGNMENT_ERROR"


class MinuteFailureStage(str, Enum):
    """Credential-safe stages emitted by the collection attempt observer."""

    PRE_REQUEST = "PRE_REQUEST"
    TRANSPORT_CALL = "TRANSPORT_CALL"
    TRANSPORT_RETURNED = "TRANSPORT_RETURNED"
    RAW_PERSISTENCE = "RAW_PERSISTENCE"
    PAGINATION_VALIDATION = "PAGINATION_VALIDATION"
    JSON_PARSE = "JSON_PARSE"
    ROW_VALIDATION = "ROW_VALIDATION"
    SOURCE_QUALITY_VALIDATION = "SOURCE_QUALITY_VALIDATION"
    COMPLETE = "COMPLETE"


class MinuteValidationError(ValueError):
    def __init__(self, issue: MinutePipelineIssue, message: str) -> None:
        super().__init__(message)
        self.issue = issue


@dataclass(frozen=True, slots=True)
class MinuteCollectionRequest:
    stock_code: str
    start_date: date
    end_date: date
    price_basis: MinutePriceBasis

    def __post_init__(self) -> None:
        require_stock_code(self.stock_code)
        if type(self.start_date) is not date or type(self.end_date) is not date:
            raise MinuteValidationError(
                MinutePipelineIssue.INVALID_DATE_RANGE, "date values required"
            )
        if self.start_date > self.end_date:
            raise MinuteValidationError(
                MinutePipelineIssue.INVALID_DATE_RANGE,
                "start_date must not follow end_date",
            )
        if not isinstance(self.price_basis, MinutePriceBasis):
            raise TypeError("price_basis must be MinutePriceBasis")

    @property
    def base_date(self) -> str:
        return self.end_date.strftime("%Y%m%d")


@dataclass(frozen=True, slots=True)
class ParsedMinuteRow:
    stock_code: str
    source_label: str
    source_label_at: datetime
    trading_date: date
    raw: Ohlcv
    source_price_text: tuple[str, str, str, str]
    price_basis: MinutePriceBasis
    source_page: int
    source_row_index: int
    artifact_sha256: str


@dataclass(frozen=True, slots=True)
class MinutePageProvenance:
    stock_code: str
    price_basis: MinutePriceBasis
    base_date: str
    pagination_sequence: int
    retrieved_at: str
    raw_file_path: str
    raw_file_sha256: str
    row_count: int
    request_continuation_identity: str
    response_continuation_identity: str


@dataclass(frozen=True, slots=True)
class CollectedMinuteSeries:
    request: MinuteCollectionRequest
    rows: tuple[ParsedMinuteRow, ...]
    pages: tuple[MinutePageProvenance, ...]
    artifact_set_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        rows = tuple(sorted(self.rows, key=lambda row: row.source_label))
        labels = [row.source_label for row in rows]
        if len(labels) != len(set(labels)):
            raise MinuteValidationError(
                MinutePipelineIssue.DUPLICATE_SOURCE_LABEL,
                "duplicate source label in collected series",
            )
        if any(
            row.stock_code != self.request.stock_code
            or row.price_basis is not self.request.price_basis
            or not self.request.start_date <= row.trading_date <= self.request.end_date
            for row in rows
        ):
            raise MinuteValidationError(
                MinutePipelineIssue.STOCK_MISMATCH,
                "row identity or requested window mismatch",
            )
        pages = tuple(sorted(self.pages, key=lambda page: page.pagination_sequence))
        if tuple(page.pagination_sequence for page in pages) != tuple(
            range(1, len(pages) + 1)
        ):
            raise MinuteValidationError(
                MinutePipelineIssue.PAGINATION_ERROR,
                "page sequence must be contiguous and one-based",
            )
        object.__setattr__(self, "rows", rows)
        object.__setattr__(self, "pages", pages)
        semantic = {
            "assumption_id": ASSUMPTION_ID,
            "stock_code": self.request.stock_code,
            "price_basis": self.request.price_basis.value,
            "start_date": self.request.start_date.isoformat(),
            "end_date": self.request.end_date.isoformat(),
            "artifacts": [
                [page.pagination_sequence, page.raw_file_sha256] for page in pages
            ],
        }
        digest = hashlib.sha256(
            json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        object.__setattr__(self, "artifact_set_sha256", digest)


@dataclass(frozen=True, slots=True)
class MinuteSourceBar:
    stock_code: str
    source_label: str
    source_label_at: datetime
    trading_date: date
    source_bar_sequence: int
    source_bar_id: str
    raw: Ohlcv
    signal: Ohlcv
    assumption_id: str = ASSUMPTION_ID


@dataclass(frozen=True, slots=True)
class ParsedMinutePage:
    stock_code: str
    rows: tuple[ParsedMinuteRow, ...]


class KiwoomMinuteStore:
    def __init__(self, root: Path = Path("data/raw/kiwoom/minute")) -> None:
        self.root = root

    def store_page(
        self, request: MinuteCollectionRequest, sequence: int, raw_bytes: bytes
    ) -> tuple[Path, str]:
        digest = hashlib.sha256(raw_bytes).hexdigest()
        path = (
            self.root
            / request.stock_code
            / request.price_basis.value.lower()
            / request.base_date
            / f"page-{sequence:03d}-{digest}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != raw_bytes:
                raise MinuteValidationError(
                    MinutePipelineIssue.PROVENANCE_ERROR, "immutable artifact collision"
                )
        else:
            path.write_bytes(raw_bytes)
        return path, digest

    def append_manifest(self, record: Mapping[str, object]) -> None:
        path = self.root / "manifest" / "requests.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(
                json.dumps(dict(record), sort_keys=True, ensure_ascii=False) + "\n"
            )


def parse_minute_page(
    raw_bytes: bytes,
    request: MinuteCollectionRequest,
    *,
    source_page: int,
    artifact_sha256: str,
) -> ParsedMinutePage:
    require_sha256(artifact_sha256)
    try:
        payload = json.loads(raw_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MinuteValidationError(
            MinutePipelineIssue.SCHEMA_ERROR, "invalid UTF-8 JSON"
        ) from exc
    if not isinstance(payload, dict) or payload.get("return_code") != 0:
        raise MinuteValidationError(
            MinutePipelineIssue.API_ERROR, "ka10080 API failure"
        )
    if payload.get("stk_cd") != request.stock_code:
        raise MinuteValidationError(
            MinutePipelineIssue.STOCK_MISMATCH, "response stock mismatch"
        )
    source_rows = payload.get(LIST_KEY)
    if not isinstance(source_rows, list):
        raise MinuteValidationError(
            MinutePipelineIssue.SCHEMA_ERROR, "minute rows must be a list"
        )
    parsed: list[ParsedMinuteRow] = []
    labels: set[str] = set()
    for index, source in enumerate(source_rows):
        if not isinstance(source, dict) or any(
            field not in source for field in SOURCE_FIELDS
        ):
            raise MinuteValidationError(
                MinutePipelineIssue.MALFORMED_ROW, "missing minute field"
            )
        label, label_at = _parse_source_label(source["cntr_tm"])
        if label in labels:
            raise MinuteValidationError(
                MinutePipelineIssue.DUPLICATE_SOURCE_LABEL,
                "duplicate label within page",
            )
        source_values = tuple(
            source[field] for field in ("open_pric", "high_pric", "low_pric", "cur_prc")
        )
        values = tuple(_parse_source_price(value) for value in source_values)
        source_text = tuple(value for value in source_values if isinstance(value, str))
        if len(source_text) != 4:
            raise MinuteValidationError(
                MinutePipelineIssue.MALFORMED_PRICE,
                "price fields must be source strings",
            )
        volume = _parse_volume(source["trde_qty"])
        ohlcv = Ohlcv(values[0], values[1], values[2], values[3], volume)
        try:
            validate_ohlcv(ohlcv, request.price_basis.value.lower(), "ka10080 row")
        except MarketDataValidationError as exc:
            raise MinuteValidationError(
                MinutePipelineIssue.INVALID_OHLCV,
                "magnitude-normalized OHLCV is invalid",
            ) from exc
        parsed.append(
            ParsedMinuteRow(
                request.stock_code,
                label,
                label_at,
                label_at.date(),
                ohlcv,
                (source_text[0], source_text[1], source_text[2], source_text[3]),
                request.price_basis,
                source_page,
                index,
                artifact_sha256,
            )
        )
        labels.add(label)
    return ParsedMinutePage(request.stock_code, tuple(parsed))


def _parse_source_price(value: object) -> Decimal:
    if not isinstance(value, str) or SIGNED_MAGNITUDE.fullmatch(value) is None:
        raise MinuteValidationError(
            MinutePipelineIssue.MALFORMED_PRICE,
            "price must have one optional leading sign and ASCII digits",
        )
    magnitude = value[1:] if value[:1] in {"+", "-"} else value
    result = Decimal(magnitude)
    if result <= 0:
        raise MinuteValidationError(
            MinutePipelineIssue.INVALID_OHLCV, "price magnitude must be positive"
        )
    return result


def _parse_volume(value: object) -> int:
    if not isinstance(value, str) or UNSIGNED_INTEGER.fullmatch(value) is None:
        raise MinuteValidationError(
            MinutePipelineIssue.MALFORMED_VOLUME, "volume must be unsigned ASCII digits"
        )
    return int(value)


def _parse_source_label(value: object) -> tuple[str, datetime]:
    if (
        not isinstance(value, str)
        or len(value) != 14
        or not value.isascii()
        or not value.isdigit()
    ):
        raise MinuteValidationError(
            MinutePipelineIssue.MALFORMED_ROW, "cntr_tm must be YYYYMMDDHHMMSS digits"
        )
    try:
        parsed = datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=KOREA_TZ)
    except ValueError as exc:
        raise MinuteValidationError(
            MinutePipelineIssue.MALFORMED_ROW, "invalid cntr_tm"
        ) from exc
    return value, parsed


def align_source_bars(
    raw_series: CollectedMinuteSeries,
    adjusted_series: CollectedMinuteSeries,
    *,
    latest_label_time: clock_time,
) -> tuple[tuple[MinuteSourceBar, ...], int]:
    if raw_series.request.price_basis is not MinutePriceBasis.RAW:
        raise MinuteValidationError(
            MinutePipelineIssue.RAW_SIGNAL_ALIGNMENT_ERROR, "raw basis required"
        )
    if adjusted_series.request.price_basis is not MinutePriceBasis.ADJUSTED:
        raise MinuteValidationError(
            MinutePipelineIssue.RAW_SIGNAL_ALIGNMENT_ERROR, "adjusted basis required"
        )
    if (
        raw_series.request.stock_code != adjusted_series.request.stock_code
        or raw_series.request.start_date != adjusted_series.request.start_date
        or raw_series.request.end_date != adjusted_series.request.end_date
    ):
        raise MinuteValidationError(
            MinutePipelineIssue.RAW_SIGNAL_ALIGNMENT_ERROR, "request mismatch"
        )
    raw_by_label = {row.source_label: row for row in raw_series.rows}
    signal_by_label = {row.source_label: row for row in adjusted_series.rows}
    if set(raw_by_label) != set(signal_by_label):
        raise MinuteValidationError(
            MinutePipelineIssue.RAW_SIGNAL_ALIGNMENT_ERROR, "RAW/ADJUSTED labels differ"
        )
    included = [
        label
        for label in sorted(raw_by_label)
        if clock_time(9, 0)
        <= raw_by_label[label].source_label_at.time()
        <= latest_label_time
    ]
    excluded = len(raw_by_label) - len(included)
    bars = tuple(
        MinuteSourceBar(
            stock_code=raw_series.request.stock_code,
            source_label=label,
            source_label_at=raw_by_label[label].source_label_at,
            trading_date=raw_by_label[label].trading_date,
            source_bar_sequence=sequence,
            source_bar_id=stable_id(
                ASSUMPTION_ID, raw_series.request.stock_code, label
            ),
            raw=raw_by_label[label].raw,
            signal=signal_by_label[label].raw,
        )
        for sequence, label in enumerate(included)
    )
    return bars, excluded


Clock = Callable[[], datetime]


def collect_minute_series(
    request: MinuteCollectionRequest,
    *,
    config: DemoConfig,
    token: TokenInfo,
    store: KiwoomMinuteStore,
    transport: ChartTransport = _chart_transport,
    max_pages: int = 40,
    page_delay: float = 1.1,
    clock: Clock | None = None,
    diagnostic: MutableMapping[str, object] | None = None,
) -> CollectedMinuteSeries:
    if diagnostic is not None:
        diagnostic.clear()
        diagnostic.update(
            {
                "stock_code": request.stock_code,
                "requested_date": request.end_date.isoformat(),
                "request_sequence": 0,
                "failure_stage": MinuteFailureStage.PRE_REQUEST.value,
                "minute_validation_issue_code": None,
                "transport_completed": False,
                "response_object_available": False,
                "response_bytes_available_to_pipeline": False,
                "raw_persistence_started": False,
                "raw_persistence_completed": False,
                "parser_started": False,
                "parser_completed": False,
                "pagination_started": False,
                "pagination_completed": False,
                "outcome": "IN_PROGRESS",
            }
        )

    def stage(value: MinuteFailureStage) -> None:
        if diagnostic is not None:
            diagnostic["failure_stage"] = value.value

    def failed(exc: BaseException, value: MinuteFailureStage) -> None:
        if diagnostic is None:
            return
        stage(value)
        if isinstance(exc, MinuteValidationError):
            diagnostic["minute_validation_issue_code"] = exc.issue.value
        diagnostic["safe_exception_type"] = type(exc).__name__
        diagnostic["outcome"] = "FAILED"

    if config.environment != "demo" or config.base_url != DEMO_BASE_URL:
        failed(
            ConfigurationError("demo configuration required"),
            MinuteFailureStage.PRE_REQUEST,
        )
        raise ConfigurationError("Only the fixed Kiwoom demo endpoint is allowed")
    if not 1 <= max_pages <= 100:
        failed(ValueError("invalid max_pages"), MinuteFailureStage.PRE_REQUEST)
        raise ValueError("max_pages must be between 1 and 100")
    now = clock or (lambda: datetime.now(UTC))
    rows: list[ParsedMinuteRow] = []
    pages: list[MinutePageProvenance] = []
    seen: set[str] = set()
    next_key = ""
    reached_start = False
    for sequence in range(1, max_pages + 1):
        if diagnostic is not None:
            diagnostic["request_sequence"] = sequence
        stage(MinuteFailureStage.TRANSPORT_CALL)
        headers = _headers(token, sequence, next_key)
        body = {
            "stk_cd": request.stock_code,
            "tic_scope": "5",
            "upd_stkpc_tp": request.price_basis.request_value,
            "base_dt": request.base_date,
        }
        try:
            response = transport(config.base_url + CHART_PATH, headers, body)
        except KiwoomApiError as exc:
            failed(
                MinuteValidationError(MinutePipelineIssue.HTTP_ERROR, "request failed"),
                MinuteFailureStage.TRANSPORT_CALL,
            )
            raise MinuteValidationError(
                MinutePipelineIssue.HTTP_ERROR, "ka10080 request failed"
            ) from exc
        except Exception as exc:
            failed(exc, MinuteFailureStage.TRANSPORT_CALL)
            raise
        if diagnostic is not None:
            diagnostic["transport_completed"] = True
            diagnostic["response_object_available"] = True
            diagnostic["response_bytes_available_to_pipeline"] = True
        stage(MinuteFailureStage.TRANSPORT_RETURNED)
        if diagnostic is not None:
            diagnostic["raw_persistence_started"] = True
        try:
            path, digest = store.store_page(request, sequence, response.body)
        except Exception as exc:
            failed(exc, MinuteFailureStage.RAW_PERSISTENCE)
            raise
        if diagnostic is not None:
            diagnostic["raw_persistence_completed"] = True
        if diagnostic is not None:
            diagnostic["pagination_started"] = True
        try:
            cont_yn, response_key = parse_pagination_headers(response.headers)
        except Exception as exc:
            failed(exc, MinuteFailureStage.PAGINATION_VALIDATION)
            raise
        if diagnostic is not None:
            diagnostic["pagination_completed"] = True
        if not 200 <= response.status < 300:
            exc = MinuteValidationError(
                MinutePipelineIssue.HTTP_ERROR, "ka10080 HTTP failure"
            )
            failed(exc, MinuteFailureStage.PAGINATION_VALIDATION)
            raise exc
        if diagnostic is not None:
            diagnostic["parser_started"] = True
        try:
            page = parse_minute_page(
                response.body, request, source_page=sequence, artifact_sha256=digest
            )
        except MinuteValidationError as exc:
            stage_value = (
                MinuteFailureStage.JSON_PARSE
                if exc.issue is MinutePipelineIssue.SCHEMA_ERROR
                else MinuteFailureStage.ROW_VALIDATION
            )
            failed(exc, stage_value)
            raise
        except Exception as exc:
            failed(exc, MinuteFailureStage.ROW_VALIDATION)
            raise
        if diagnostic is not None:
            diagnostic["parser_completed"] = True
        retrieved = now().astimezone(UTC).isoformat().replace("+00:00", "Z")
        provenance = MinutePageProvenance(
            request.stock_code,
            request.price_basis,
            request.base_date,
            sequence,
            retrieved,
            path.as_posix(),
            digest,
            len(page.rows),
            _continuation_identity(next_key),
            _continuation_identity(response_key),
        )
        pages.append(provenance)
        store.append_manifest(
            {
                "provider": "KIWOOM",
                "api_id": API_ID,
                "assumption_id": ASSUMPTION_ID,
                **asdict(provenance),
                "price_basis": request.price_basis.value,
            }
        )
        for row in page.rows:
            if row.source_label in seen:
                raise MinuteValidationError(
                    MinutePipelineIssue.DUPLICATE_SOURCE_LABEL,
                    "duplicate label across pages",
                )
            seen.add(row.source_label)
            if request.start_date <= row.trading_date <= request.end_date:
                rows.append(row)
        if (
            page.rows
            and min(row.trading_date for row in page.rows) <= request.start_date
        ):
            reached_start = True
            break
        if cont_yn != "Y" or not response_key:
            break
        next_key = response_key
        if sequence < max_pages and page_delay:
            time.sleep(page_delay)
    if not reached_start:
        exc = MinuteValidationError(
            MinutePipelineIssue.REQUIRED_START_NOT_REACHED,
            "pagination did not reach start_date",
        )
        failed(exc, MinuteFailureStage.PAGINATION_VALIDATION)
        raise exc
    if diagnostic is not None:
        diagnostic["failure_stage"] = MinuteFailureStage.COMPLETE.value
        diagnostic["outcome"] = "SUCCESS"
    return CollectedMinuteSeries(request, tuple(rows), tuple(pages))


def _headers(token: TokenInfo, sequence: int, next_key: str) -> dict[str, str]:
    result = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"{token.token_type.title()} {token.token}",
        "api-id": API_ID,
    }
    if sequence > 1:
        result["cont-yn"] = "Y"
        result["next-key"] = next_key
    return result


def _continuation_identity(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest() if value else ""
