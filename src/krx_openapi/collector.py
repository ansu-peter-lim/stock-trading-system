"""One-request KRX collection orchestration with auditable outcomes."""

from __future__ import annotations

from .parser import KrxSchemaError, parse_krx_response
from .services import KrxServiceDefinition
from .store import ImmutableRawStore, ManifestEvent, ManifestStatus
from .transport import AuthKeyProvider, KrxHttpError, KrxTransport


def collect_one(
    service: KrxServiceDefinition,
    *,
    bas_dd: str,
    auth_key_provider: AuthKeyProvider,
    transport: KrxTransport,
    store: ImmutableRawStore,
    timeout: float = 30.0,
) -> ManifestEvent:
    try:
        result = transport.fetch(
            service,
            bas_dd=bas_dd,
            auth_key_provider=auth_key_provider,
            timeout=timeout,
        )
    except KrxHttpError as exc:
        event = ManifestEvent(
            service.service_id,
            service.market,
            bas_dd,
            ManifestStatus.HTTP_ERROR,
            "",
            exc.http_status,
            None,
            "",
            "",
            None,
            error_code="HTTP_ERROR",
        )
        store.append_manifest(event)
        return event

    artifact = store.store(service, result)
    try:
        parsed = parse_krx_response(result.raw_bytes, service)
        status = (
            ManifestStatus.EMPTY_RESPONSE
            if parsed.row_count == 0
            else ManifestStatus.SUCCESS
        )
        row_count: int | None = parsed.row_count
        error_code = ""
    except KrxSchemaError:
        status = ManifestStatus.SCHEMA_ERROR
        row_count = None
        error_code = "SCHEMA_ERROR"
    if artifact.disposition.value == "CONFLICT":
        status = ManifestStatus.CONFLICT
        error_code = "RAW_IDENTITY_CONFLICT"
    event = ManifestEvent(
        service.service_id,
        service.market,
        result.identity.bas_dd,
        status,
        result.retrieved_at,
        result.http_status,
        row_count,
        artifact.raw_file_path,
        artifact.sha256,
        artifact.byte_size,
        error_code=error_code,
    )
    store.append_manifest(event)
    return event
