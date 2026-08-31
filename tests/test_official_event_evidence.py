from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

from src.stock_mapping.official_event_evidence import (
    EvidenceResolutionReason,
    EvidenceResolutionStatus,
    OfficialDocumentType,
    OfficialEffectiveDateEvidence,
    OfficialEventType,
    OfficialEvidenceValidationError,
    OfficialSourceSystem,
    ResolvedEvidenceDisposition,
    resolve_authoritative_evidence,
    validate_official_evidence,
)


class OfficialEventEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def evidence(
        self,
        document_id: str = "doc-1",
        *,
        body: bytes = b"synthetic KIND document",
        **changes: object,
    ) -> OfficialEffectiveDateEvidence:
        path = self.root / f"{document_id}.html"
        path.write_bytes(body)
        values: dict[str, object] = {
            "revision_set_id": "name-change-018500-20260722",
            "event_type": OfficialEventType.NAME_CHANGE,
            "canonical_stock_code": "018500",
            "raw_source_stock_code": "A018500",
            "source_code_namespace": "KIND_SHORT_CODE_V1",
            "identity_contract_version": "1",
            "official_effective_date": date(2026, 7, 22),
            "previous_full_name_raw": "동원금속보통주",
            "current_full_name_raw": "동원모빌리티보통주",
            "previous_abbreviation_raw": "동원금속",
            "current_abbreviation_raw": "동원모빌리티",
            "market": "KOSPI",
            "source_system": OfficialSourceSystem.KRX_KIND,
            "source_document_id": document_id,
            "source_reference": f"https://kind.example/{document_id}",
            "published_at": datetime(2026, 7, 20, 6, tzinfo=timezone.utc),
            "retrieved_at": datetime(2026, 8, 31, tzinfo=timezone.utc),
            "artifact_path": path.as_posix(),
            "artifact_sha256": hashlib.sha256(body).hexdigest(),
            "artifact_byte_size": len(body),
            "parser_version": "K3C1",
            "schema_version": "1",
            "revision_number": 1,
            "document_type": OfficialDocumentType.ORIGINAL,
            "supersedes_document_id": None,
        }
        values.update(changes)
        return OfficialEffectiveDateEvidence(**values)  # type: ignore[arg-type]

    def assert_validation_reason(
        self,
        evidence: OfficialEffectiveDateEvidence,
        reason: EvidenceResolutionReason,
    ) -> None:
        with self.assertRaises(OfficialEvidenceValidationError) as raised:
            validate_official_evidence(evidence)
        self.assertIn(reason, raised.exception.reasons)

    def test_valid_active_evidence_preserves_raw_fields_and_resolves(self) -> None:
        evidence = self.evidence()
        validate_official_evidence(evidence)
        result = resolve_authoritative_evidence(evidence.revision_set_id, [evidence])
        self.assertEqual(EvidenceResolutionStatus.RESOLVED, result.status)
        self.assertEqual((), result.reasons)
        self.assertIs(evidence, result.authoritative_evidence)
        self.assertEqual("A018500", evidence.raw_source_stock_code)
        self.assertEqual("동원금속보통주", evidence.previous_full_name_raw)
        self.assertEqual("동원모빌리티", evidence.current_abbreviation_raw)

    def test_evidence_id_is_deterministic_and_not_path_or_retrieval_dependent(
        self,
    ) -> None:
        first = self.evidence("doc-1")
        second = self.evidence("doc-1-copy")
        second = replace(
            second,
            source_document_id="doc-1",
            source_reference=first.source_reference,
            retrieved_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(first.evidence_id, second.evidence_id)
        self.assertEqual(64, len(first.evidence_id))
        self.assertNotEqual(
            first.evidence_id,
            replace(first, revision_number=2).evidence_id,
        )

    def test_artifact_provenance_failures_are_rejected(self) -> None:
        evidence = self.evidence()
        for invalid in (
            replace(evidence, artifact_sha256="0" * 64),
            replace(evidence, artifact_byte_size=evidence.artifact_byte_size + 1),
            replace(evidence, artifact_path=(self.root / "missing.html").as_posix()),
            replace(evidence, artifact_sha256="not-a-digest"),
        ):
            self.assert_validation_reason(
                invalid,
                EvidenceResolutionReason.INVALID_ARTIFACT_PROVENANCE,
            )

    def test_invalid_identity_names_and_secret_reference_are_rejected(self) -> None:
        evidence = self.evidence()
        self.assert_validation_reason(
            replace(evidence, canonical_stock_code="A18500"),
            EvidenceResolutionReason.INVALID_CANONICAL_STOCK_CODE,
        )
        self.assert_validation_reason(
            replace(evidence, current_full_name_raw=" 동원금속보통주 "),
            EvidenceResolutionReason.UNCHANGED_OFFICIAL_NAME,
        )
        self.assert_validation_reason(
            replace(evidence, source_reference="https://kind.example/x?auth_key=x"),
            EvidenceResolutionReason.INVALID_EVIDENCE_FIELD,
        )

    def test_valid_original_corrected_chain_selects_only_active_leaf(self) -> None:
        original = self.evidence("doc-1")
        corrected = self.evidence(
            "doc-2",
            body=b"corrected document",
            revision_number=2,
            document_type=OfficialDocumentType.CORRECTION,
            supersedes_document_id="doc-1",
        )
        original_id = original.evidence_id
        result = resolve_authoritative_evidence(
            original.revision_set_id, [corrected, original]
        )
        self.assertEqual(EvidenceResolutionStatus.RESOLVED, result.status)
        self.assertIs(corrected, result.authoritative_evidence)
        self.assertIsNot(original, result.authoritative_evidence)
        self.assertEqual(original_id, original.evidence_id)
        self.assertIn(
            ("doc-1", ResolvedEvidenceDisposition.SUPERSEDED),
            result.node_dispositions,
        )

    def test_resolution_is_input_order_independent(self) -> None:
        original = self.evidence("doc-1")
        corrected = self.evidence(
            "doc-2",
            revision_number=2,
            document_type=OfficialDocumentType.CORRECTION,
            supersedes_document_id="doc-1",
        )
        forward = resolve_authoritative_evidence(
            original.revision_set_id, [original, corrected]
        )
        reverse = resolve_authoritative_evidence(
            original.revision_set_id, [corrected, original]
        )
        self.assertEqual(forward, reverse)

    def test_three_document_chain_selects_c_without_rewriting_ancestors(self) -> None:
        original = self.evidence("doc-1")
        original_id = original.evidence_id
        correction = self.evidence(
            "doc-2",
            revision_number=2,
            document_type=OfficialDocumentType.CORRECTION,
            supersedes_document_id="doc-1",
        )
        latest = self.evidence(
            "doc-3",
            revision_number=3,
            document_type=OfficialDocumentType.CORRECTION,
            supersedes_document_id="doc-2",
        )
        result = resolve_authoritative_evidence(
            original.revision_set_id, [latest, original, correction]
        )
        self.assertEqual(EvidenceResolutionStatus.RESOLVED, result.status)
        self.assertIs(latest, result.authoritative_evidence)
        self.assertEqual(original_id, original.evidence_id)
        self.assertEqual(
            (
                ("doc-1", ResolvedEvidenceDisposition.SUPERSEDED),
                ("doc-2", ResolvedEvidenceDisposition.SUPERSEDED),
                ("doc-3", ResolvedEvidenceDisposition.ACTIVE),
            ),
            result.node_dispositions,
        )

    def test_explicit_revision_set_prevents_same_code_event_merging(self) -> None:
        first_event = self.evidence("doc-1")
        later_event = self.evidence(
            "doc-2",
            revision_set_id="name-change-018500-later-event",
            official_effective_date=date(2027, 1, 5),
            previous_full_name_raw="동원모빌리티보통주",
            current_full_name_raw="다른이름보통주",
            previous_abbreviation_raw="동원모빌리티",
            current_abbreviation_raw="다른이름",
        )
        result = resolve_authoritative_evidence(
            first_event.revision_set_id, [first_event, later_event]
        )
        self.assertEqual(EvidenceResolutionStatus.REVIEW_REQUIRED, result.status)
        self.assertIn(EvidenceResolutionReason.MIXED_REVISION_SETS, result.reasons)
        self.assertIsNone(result.authoritative_evidence)

    def test_missing_self_and_invalid_revision_are_typed(self) -> None:
        missing = self.evidence(
            "doc-2",
            revision_number=2,
            document_type=OfficialDocumentType.CORRECTION,
            supersedes_document_id="missing",
        )
        self.assertIn(
            EvidenceResolutionReason.MISSING_SUPERSEDED_DOCUMENT,
            resolve_authoritative_evidence(missing.revision_set_id, [missing]).reasons,
        )
        self_superseding = self.evidence(
            "doc-self",
            document_type=OfficialDocumentType.CORRECTION,
            supersedes_document_id="doc-self",
        )
        self.assertIn(
            EvidenceResolutionReason.SELF_SUPERSESSION,
            resolve_authoritative_evidence(
                self_superseding.revision_set_id, [self_superseding]
            ).reasons,
        )
        original = self.evidence("doc-1")
        invalid_order = self.evidence(
            "doc-2",
            revision_number=1,
            document_type=OfficialDocumentType.CORRECTION,
            supersedes_document_id="doc-1",
        )
        self.assertIn(
            EvidenceResolutionReason.INVALID_REVISION_ORDER,
            resolve_authoritative_evidence(
                original.revision_set_id, [original, invalid_order]
            ).reasons,
        )

    def test_cycle_is_rejected(self) -> None:
        first = self.evidence(
            "doc-1",
            revision_number=2,
            document_type=OfficialDocumentType.CORRECTION,
            supersedes_document_id="doc-2",
        )
        second = self.evidence(
            "doc-2",
            revision_number=3,
            document_type=OfficialDocumentType.CORRECTION,
            supersedes_document_id="doc-1",
        )
        result = resolve_authoritative_evidence(first.revision_set_id, [first, second])
        self.assertEqual(EvidenceResolutionStatus.CONFLICT, result.status)
        self.assertIn(EvidenceResolutionReason.REVISION_CYCLE, result.reasons)
        self.assertIsNone(result.authoritative_evidence)

    def test_competing_active_leaves_and_duplicate_document_are_rejected(self) -> None:
        first = self.evidence("doc-1")
        second = self.evidence("doc-2")
        competing = resolve_authoritative_evidence(
            first.revision_set_id, [second, first]
        )
        self.assertEqual(EvidenceResolutionStatus.CONFLICT, competing.status)
        self.assertIn(
            EvidenceResolutionReason.COMPETING_ACTIVE_LEAVES,
            competing.reasons,
        )
        duplicate = resolve_authoritative_evidence(
            first.revision_set_id, [first, replace(first, revision_number=2)]
        )
        self.assertEqual(EvidenceResolutionStatus.CONFLICT, duplicate.status)
        self.assertIn(EvidenceResolutionReason.DUPLICATE_DOCUMENT_ID, duplicate.reasons)
        self.assertIn(
            EvidenceResolutionReason.CONFLICTING_DOCUMENT_ID, duplicate.reasons
        )

    def test_cancelled_authoritative_leaf_is_not_usable(self) -> None:
        original = self.evidence("doc-1")
        cancelled = self.evidence(
            "doc-2",
            revision_number=2,
            document_type=OfficialDocumentType.CANCELLATION,
            supersedes_document_id="doc-1",
        )
        result = resolve_authoritative_evidence(
            original.revision_set_id, [cancelled, original]
        )
        self.assertEqual(EvidenceResolutionStatus.CANCELLED, result.status)
        self.assertEqual(
            (EvidenceResolutionReason.CANCELLED_AUTHORITATIVE_LEAF,), result.reasons
        )
        self.assertIsNone(result.authoritative_evidence)
        self.assertIn(
            ("doc-1", ResolvedEvidenceDisposition.SUPERSEDED),
            result.node_dispositions,
        )
        self.assertIn(
            ("doc-2", ResolvedEvidenceDisposition.CANCELLED),
            result.node_dispositions,
        )

    def test_reason_ordering_is_deterministic(self) -> None:
        invalid = self.evidence(
            canonical_stock_code="bad",
            artifact_sha256="bad",
            previous_full_name_raw="",
        )
        forward = resolve_authoritative_evidence(
            invalid.revision_set_id, [invalid, invalid]
        )
        reverse = resolve_authoritative_evidence(
            invalid.revision_set_id, [invalid, invalid][::-1]
        )
        self.assertEqual(forward.reasons, reverse.reasons)
        self.assertEqual(
            tuple(
                reason
                for reason in EvidenceResolutionReason
                if reason in set(forward.reasons)
            ),
            forward.reasons,
        )

    def test_model_has_no_historical_state_or_write_path(self) -> None:
        evidence = self.evidence()
        self.assertFalse(hasattr(evidence, "effective_date"))
        self.assertFalse(hasattr(evidence, "valid_from"))
        self.assertFalse(hasattr(evidence, "historical_stock"))


if __name__ == "__main__":
    unittest.main()
