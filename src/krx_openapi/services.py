"""Immutable definitions for the four KRX services supported by K1."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class KrxServiceDefinition:
    service_id: str
    endpoint: str
    market: str
    artifact_group: str
    expected_root: str
    required_request_fields: tuple[str, ...]
    required_response_fields: tuple[str, ...]
    decimal_fields: tuple[str, ...]
    integer_fields: tuple[str, ...]
    stock_code_field: str


_BASIC_FIELDS = (
    "ISU_CD",
    "ISU_SRT_CD",
    "ISU_NM",
    "ISU_ABBRV",
    "ISU_ENG_NM",
    "LIST_DD",
    "MKT_TP_NM",
    "SECUGRP_NM",
    "SECT_TP_NM",
    "KIND_STKCERT_TP_NM",
    "PARVAL",
    "LIST_SHRS",
)

_DAILY_FIELDS = (
    "BAS_DD",
    "ISU_CD",
    "ISU_NM",
    "MKT_NM",
    "SECT_TP_NM",
    "TDD_CLSPRC",
    "CMPPREVDD_PRC",
    "FLUC_RT",
    "TDD_OPNPRC",
    "TDD_HGPRC",
    "TDD_LWPRC",
    "ACC_TRDVOL",
    "ACC_TRDVAL",
    "MKTCAP",
    "LIST_SHRS",
)


def _service(
    service_id: str,
    market: str,
    artifact_group: str,
    fields: tuple[str, ...],
) -> KrxServiceDefinition:
    is_basic = artifact_group == "stock_basic"
    return KrxServiceDefinition(
        service_id=service_id,
        endpoint=f"https://data-dbg.krx.co.kr/svc/apis/sto/{service_id}",
        market=market,
        artifact_group=artifact_group,
        expected_root="OutBlock_1",
        required_request_fields=("basDd",),
        required_response_fields=fields,
        decimal_fields=("PARVAL",)
        if is_basic
        else (
            "TDD_CLSPRC",
            "CMPPREVDD_PRC",
            "FLUC_RT",
            "TDD_OPNPRC",
            "TDD_HGPRC",
            "TDD_LWPRC",
            "ACC_TRDVAL",
            "MKTCAP",
        ),
        integer_fields=("LIST_SHRS",) if is_basic else ("ACC_TRDVOL", "LIST_SHRS"),
        stock_code_field="ISU_SRT_CD" if is_basic else "ISU_CD",
    )


KRX_SERVICES = {
    item.service_id: item
    for item in (
        _service("stk_isu_base_info", "KOSPI", "stock_basic", _BASIC_FIELDS),
        _service("ksq_isu_base_info", "KOSDAQ", "stock_basic", _BASIC_FIELDS),
        _service("stk_bydd_trd", "KOSPI", "daily_trade", _DAILY_FIELDS),
        _service("ksq_bydd_trd", "KOSDAQ", "daily_trade", _DAILY_FIELDS),
    )
}
