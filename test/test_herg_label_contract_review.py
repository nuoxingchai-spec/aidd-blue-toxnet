"""Software contract tests only; no experimental activity values or labels."""
import json
import tempfile
import unittest
from pathlib import Path

from backend.herg_label_contract_review import (
    prereview_policy, prereview_policy_sha256, original_contract_review,
    require_custodian_mapping,
)


class HergContractReviewTests(unittest.TestCase):
    def test_threshold_and_mapping_remain_unresolved(self):
        policy = prereview_policy()
        for key in ('threshold', 'thresholdUnit', 'comparisonOperator', 'boundaryValueRule'):
            self.assertIsNone(policy[key])
        self.assertEqual(policy['status'], 'MANUAL_REVIEW_REQUIRED')
        for key in ('labelMappingExecutable', 'unitConversionExecutable',
                    'evaluationAuthorized', 'modelTrainingAuthorized', 'directComparisonApproved'):
            self.assertFalse(policy[key])
        self.assertEqual(policy['candidateOutcomeFieldsPermitted'], [])

    def test_policy_pin_is_deterministic_and_not_mutable_by_callers(self):
        before = prereview_policy_sha256()
        policy = prereview_policy()
        policy['labelMappingExecutable'] = True
        policy['requiredEvidenceBeforeAnyNewPolicy'].clear()
        self.assertEqual(before, prereview_policy_sha256())
        self.assertFalse(prereview_policy()['labelMappingExecutable'])
        self.assertTrue(prereview_policy()['requiredEvidenceBeforeAnyNewPolicy'])

    def test_karim_and_central_thresholds_never_substitute(self):
        policy = prereview_policy()
        self.assertEqual(policy['originalTask'], 'TDC hERG / Wang 2016 / Dataverse 4259588')
        self.assertIsNone(policy['threshold'])
        self.assertIn('Ki', policy['nonInterchangeableMeasurements'])
        self.assertIn('AC50', policy['nonInterchangeableMeasurements'])
        self.assertIn('hitcall', policy['nonInterchangeableMeasurements'])

    def test_mapping_guard_has_no_override_or_outcome_arguments(self):
        with self.assertRaisesRegex(PermissionError, 'MANUAL_REVIEW_REQUIRED'):
            require_custodian_mapping()
        with self.assertRaises(TypeError):
            require_custodian_mapping(approved=True)

    def test_evidence_is_only_tracked_metadata_and_source_code(self):
        result = original_contract_review()
        self.assertEqual(result['historicalDeclaration']['fileId'], 4259588)
        self.assertFalse(result['labelsRead'])
        self.assertFalse(result['canEnterStage7C'])
        self.assertFalse(result['evaluationPerformed'])
        self.assertEqual(result, original_contract_review())
        self.assertFalse(any(name.endswith('.csv') for name in result['sourceFileSha256']))
        self.assertNotIn('positiveRows', json.dumps(result))

    def test_missing_evidence_is_not_silently_approved(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'Missing regular historical contract'):
                original_contract_review(Path(directory))

    def test_changed_evidence_bytes_change_receipt(self):
        result = original_contract_review()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in result['sourceFileSha256']:
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b'Synthetic source evidence only')
            first = original_contract_review(root)
            (root / 'backend/training_data.py').write_bytes(b'Changed synthetic source evidence only')
            second = original_contract_review(root)
            self.assertNotEqual(first['evidenceSha256'], second['evidenceSha256'])
            self.assertNotEqual(first['sourceFileSha256']['backend/training_data.py'],
                                second['sourceFileSha256']['backend/training_data.py'])


if __name__ == '__main__':
    unittest.main()
