"""CPU train-coverage fixtures, not validated uncertainty estimates."""
import unittest
from backend.applicability_domain import build_reference, query_domain
from backend.independent_validation_metrics import digest


class ApplicabilityDomainTests(unittest.TestCase):
    def fixture(self):
        smiles = ['CCO', 'CCC', 'c1ccccc1']
        binding = {'artifactVerified': True, 'weightsSha256': 'a'*64, 'trainingReferenceSha256': 'b'*64,
                   'trainingStructureOrderSha256': digest(smiles)}
        return smiles, binding

    def test_reference_train_binding_and_heuristic_not_confidence(self):
        smiles, binding = self.fixture()
        reference = build_reference(smiles, model_binding=binding, training_reference_sha256='b'*64)
        result = query_domain('CCO', reference)
        self.assertEqual(result['maximumTrainingTanimoto'], 1)
        self.assertFalse(result['validated'])
        self.assertIsNone(result['confidence'])
        self.assertTrue(result['scaffoldSeenInTraining'])
        self.assertEqual(result['status'], 'not-validated')
        self.assertEqual(result['descriptorRanges']['HeavyAtomCount']['queryValue'], 3)
        self.assertEqual(result['descriptorRanges']['HeavyAtomCount']['trainingMaximum'], 6)
        self.assertNotIn('smiles', str(result).lower())

    def test_wrong_train_membership_and_thresholds_rejected(self):
        smiles, binding = self.fixture()
        with self.assertRaises(ValueError):
            build_reference(smiles[:2], model_binding=binding, training_reference_sha256='b'*64)
        with self.assertRaises(ValueError):
            build_reference(smiles, model_binding=binding, training_reference_sha256='c'*64)
        with self.assertRaises(ValueError):
            build_reference(smiles, model_binding=binding, training_reference_sha256='b'*64, thresholds={'minimumTanimoto': 2, 'maximumDescriptorZ': 3})
        self.assertEqual(query_domain('CCO', None)['status'], 'insufficient-evidence')

    def test_unknown_inputs_and_outside_ranges_are_not_certified_uncertainty(self):
        smiles, binding = self.fixture()
        reference = build_reference(smiles, model_binding=binding, training_reference_sha256='b'*64)
        bad = query_domain('not a molecule', reference)
        self.assertEqual(bad['status'], 'insufficient-evidence')
        outside = query_domain('CCCCCCCCCCCCCCCCCCCC', reference)
        self.assertEqual(outside['status'], 'not-validated')
        self.assertTrue(outside['atomCountOutsideTrainingRange'])
        self.assertEqual(outside['coverageStatus'], 'outside-training-coverage-heuristic')
        self.assertIsNone(outside['confidence'])


if __name__ == '__main__':
    unittest.main()
