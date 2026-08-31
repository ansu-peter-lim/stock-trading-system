"""Production-safe KRX Open API ingestion foundations."""

from .parser import KrxParsedResponse, KrxSchemaError, parse_krx_response
from .services import KRX_SERVICES, KrxServiceDefinition
from .store import (
    ArtifactDisposition,
    ImmutableRawStore,
    ManifestEvent,
    ManifestStatus,
)
from .transport import (
    EnvironmentAuthKeyProvider,
    KrxConfigurationError,
    KrxHttpError,
    KrxTransport,
    TransportResult,
)

__all__ = [
    "KRX_SERVICES",
    "ArtifactDisposition",
    "EnvironmentAuthKeyProvider",
    "ImmutableRawStore",
    "KrxConfigurationError",
    "KrxHttpError",
    "KrxParsedResponse",
    "KrxSchemaError",
    "KrxServiceDefinition",
    "KrxTransport",
    "ManifestEvent",
    "ManifestStatus",
    "TransportResult",
    "parse_krx_response",
]
