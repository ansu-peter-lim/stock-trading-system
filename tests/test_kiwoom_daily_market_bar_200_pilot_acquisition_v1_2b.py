import json
from datetime import date, datetime, timezone

import pytest

from src.kiwoom_minute.pipeline import (
    KiwoomMinuteStore,
    MinuteCollectionRequest,
    MinutePipelineIssue,
    MinutePriceBasis,
    MinuteValidationError,
    collect_minute_series,
)
from src.kiwoom_rest.auth import DemoConfig, KiwoomApiError, TokenInfo
from src.kiwoom_rest.market_data_pilot import ChartHttpResult


def _row(*, label: str = "20260828090000") -> dict[str, str]:
    return {
        "cur_prc": "+100",
        "trde_qty": "0",
        "cntr_tm": label,
        "open_pric": "-100",
        "high_pric": "+110",
        "low_pric": "100",
        "acc_trde_qty": "0",
        "pred_pre": "0",
        "pred_pre_sig": "0",
    }


def _response(rows: list[dict[str, str]]) -> ChartHttpResult:
    body = json.dumps(
        {
            "stk_cd": "005930",
            "return_code": 0,
            "return_msg": "ok",
            "stk_min_pole_chart_qry": rows,
        }
    ).encode()
    return ChartHttpResult(200, body, {"cont-yn": "N", "next-key": ""})


def _request() -> MinuteCollectionRequest:
    return MinuteCollectionRequest(
        "005930", date(2026, 8, 28), date(2026, 8, 28), MinutePriceBasis.RAW
    )


def _kwargs(store: KiwoomMinuteStore, transport):
    return {
        "config": DemoConfig("demo", "app", "secret"),
        "token": TokenInfo("token", "bearer", "20260902120000", 200),
        "store": store,
        "transport": transport,
        "page_delay": 0,
        "clock": lambda: datetime(2026, 9, 2, tzinfo=timezone.utc),
    }


def test_transport_success_records_complete_diagnostic(tmp_path):
    diagnostic: dict[str, object] = {}
    series = collect_minute_series(
        _request(),
        **_kwargs(KiwoomMinuteStore(tmp_path), lambda *_: _response([_row()])),
        diagnostic=diagnostic,
    )

    assert len(series.rows) == 1
    assert diagnostic == {
        "stock_code": "005930",
        "requested_date": "2026-08-28",
        "request_sequence": 1,
        "failure_stage": "COMPLETE",
        "minute_validation_issue_code": None,
        "transport_completed": True,
        "response_object_available": True,
        "response_bytes_available_to_pipeline": True,
        "raw_persistence_started": True,
        "raw_persistence_completed": True,
        "parser_started": True,
        "parser_completed": True,
        "pagination_started": True,
        "pagination_completed": True,
        "outcome": "SUCCESS",
    }


def test_typed_transport_failure_preserves_issue_and_never_stores_raw(tmp_path):
    diagnostic: dict[str, object] = {}

    def transport(*_):
        raise KiwoomApiError("Authorization Bearer credential-must-not-be-recorded")

    with pytest.raises(MinuteValidationError) as raised:
        collect_minute_series(
            _request(),
            **_kwargs(KiwoomMinuteStore(tmp_path), transport),
            diagnostic=diagnostic,
        )

    assert raised.value.issue is MinutePipelineIssue.HTTP_ERROR
    assert diagnostic["failure_stage"] == "TRANSPORT_CALL"
    assert diagnostic["minute_validation_issue_code"] == "HTTP_ERROR"
    assert diagnostic["transport_completed"] is False
    assert diagnostic["response_bytes_available_to_pipeline"] is False
    assert not list(tmp_path.rglob("*.json"))
    assert "credential" not in json.dumps(diagnostic).lower()


class _FailingStore(KiwoomMinuteStore):
    def store_page(self, request, sequence, raw_bytes):
        raise OSError("disk error")


def test_raw_persistence_failure_is_distinguished_from_parser_failure(tmp_path):
    diagnostic: dict[str, object] = {}
    with pytest.raises(OSError):
        collect_minute_series(
            _request(),
            **_kwargs(_FailingStore(tmp_path), lambda *_: _response([_row()])),
            diagnostic=diagnostic,
        )

    assert diagnostic["failure_stage"] == "RAW_PERSISTENCE"
    assert diagnostic["transport_completed"] is True
    assert diagnostic["raw_persistence_started"] is True
    assert diagnostic["raw_persistence_completed"] is False
    assert diagnostic["parser_started"] is False
    assert diagnostic["outcome"] == "FAILED"


def test_parser_failure_has_already_persisted_raw_and_typed_issue(tmp_path):
    diagnostic: dict[str, object] = {}
    malformed = _row()
    del malformed["high_pric"]
    with pytest.raises(MinuteValidationError) as raised:
        collect_minute_series(
            _request(),
            **_kwargs(KiwoomMinuteStore(tmp_path), lambda *_: _response([malformed])),
            diagnostic=diagnostic,
        )

    assert raised.value.issue is MinutePipelineIssue.MALFORMED_ROW
    assert diagnostic["failure_stage"] == "ROW_VALIDATION"
    assert diagnostic["raw_persistence_completed"] is True
    assert diagnostic["parser_started"] is True
    assert diagnostic["parser_completed"] is False
    assert list(tmp_path.rglob("*.json"))
