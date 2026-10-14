"""Synthetic CPU calibration fixtures; no published scientific calibration."""
import copy
import unittest

from backend.calibration_research import apply_calibrators, audit_role_isolation, calibration_diagnostics, fit_calibrators
from backend.chemistry import canonicalize_smiles
from backend.independent_validation_metrics import digest


def fixture():
    def rows(values, source):
        return [{'canonicalSmiles': canonicalize_smiles(v), 'sourceId': source, 'sourceRowId': str(i)} for i, v in enumerate(values)]
    roles = {'train': rows(['c1ccccc1'], 'fixture-train'),
             'calibration': rows(['c1ccncc1', 'Cc1ccncc1', 'Oc1ccncc1', 'Nc1ccncc1'], 'fixture-calibration'),
             'evaluation': rows(['C1CCCCC1'], 'fixture-evaluation')}
    binding = {'artifactVerified': True, 'weightsSha256': 'a'*64, 'manifestSha256': 'b'*64,
               'trainingReferenceSha256': 'f'*64, 'taskContractHashes': {'herg': 'c'*64},
               'trainingStructureOrderSha256': digest([roles['train'][0]['canonicalSmiles']])}
    role = {'role': 'calibration', 'predeclaredMethod': 'platt', 'evaluationUsedForSelection': False,
            'labelReadAuthorized': True, 'protocolSha256': 'd'*64, 'datasetManifestSha256': 'e'*64,
            'minimumPerClass': 2, 'taskContractHashes': binding['taskContractHashes'],
            'calibrationMembershipSha256': audit_role_isolation(roles)['membershipSha256']['calibration'],
            'calibrationSourceMembershipSha256': audit_role_isolation(roles)['sourceMembershipSha256']['calibration']}
    return roles, binding, role


class CalibrationResearchTests(unittest.TestCase):
    def test_predeclared_methods_fit_only_calibration_and_keep_raw_scores(self):
        roles, binding, role = fixture()
        for method in ('platt', 'temperature'):
            role['predeclaredMethod'] = method
            artifact = fit_calibrators([[.1], [.3], [.7], [.9]], [[0], [0], [1], [1]], [[1]]*4,
                tasks=['herg'], method=method, model_binding=binding, role_binding=role, identity_roles=roles, minimum_per_class=2)
            self.assertEqual(artifact['status'], 'fitted-research-only')
            result = apply_calibrators([[.1], [.9]], artifact, binding, tasks=['herg'])
            self.assertEqual(result['rawScores'], [[.1], [.9]])
            self.assertFalse(result['certified'])
            self.assertTrue(all(0 <= r[0] <= 1 for r in result['calibratedLabelProbabilities']))

    def test_isotonic_insufficient_class_support_never_fits_parameters(self):
        roles, binding, role = fixture()
        role['predeclaredMethod'] = 'isotonic'
        artifact = fit_calibrators([[.1], [.3], [.7], [.9]], [[0], [0], [1], [1]], [[1]]*4,
            tasks=['herg'], method='isotonic', model_binding=binding, role_binding=role, identity_roles=roles, minimum_per_class=2)
        self.assertEqual(artifact['status'], 'not-fitted')
        self.assertIsNone(artifact['endpoints']['herg']['parameters'])
        self.assertEqual(artifact['endpoints']['herg']['effectiveMinimumPerClass'], 100)

    def test_isotonic_supported_synthetic_fixture_is_monotonic_and_bound(self):
        # Entirely synthetic unit fixtures, never registered as experiments.
        roles, binding, role = fixture()
        structures = [canonicalize_smiles('C'*i+'c1ccnc('+'C'*j+')c1') for i in range(1, 11) for j in range(1, 21)]
        self.assertEqual(len(set(structures)), 200)
        roles['calibration'] = [{'canonicalSmiles': s, 'sourceId': 'synthetic-fixture-only', 'sourceRowId': str(i)}
                                for i, s in enumerate(structures)]
        audit = audit_role_isolation(roles)
        role.update(predeclaredMethod='isotonic', calibrationMembershipSha256=audit['membershipSha256']['calibration'],
                    calibrationSourceMembershipSha256=audit['sourceMembershipSha256']['calibration'])
        scores = [[(i+1)/201] for i in range(200)]
        artifact = fit_calibrators(scores, [[int(i >= 100)] for i in range(200)], [[1]]*200,
            tasks=['herg'], method='isotonic', model_binding=binding, role_binding=role, identity_roles=roles, minimum_per_class=2)
        self.assertEqual(artifact['status'], 'fitted-research-only')
        result = apply_calibrators([[.1], [.5], [.9]], artifact, binding, tasks=['herg'])
        self.assertEqual(result['calibratedLabelProbabilities'], sorted(result['calibratedLabelProbabilities']))

    def test_single_class_and_masked_labels_do_not_fit(self):
        roles, binding, role = fixture()
        artifact = fit_calibrators([[.1]]*4, [[1]]*4, [[1], [1], [0], [0]], tasks=['herg'], method='platt',
            model_binding=binding, role_binding=role, identity_roles=roles, minimum_per_class=2)
        self.assertEqual(artifact['status'], 'not-fitted')
        self.assertEqual(artifact['endpoints']['herg']['count'], 2)
        self.assertEqual(apply_calibrators([[.2]], artifact, binding, tasks=['herg'])['calibratedLabelProbabilities'], [[None]])

    def test_role_identity_source_overlap_and_model_tampering_rejected(self):
        roles, binding, role = fixture()
        bad = copy.deepcopy(roles)
        bad['evaluation'][0]['canonicalSmiles'] = roles['train'][0]['canonicalSmiles']
        with self.assertRaises(ValueError):
            audit_role_isolation(bad)
        bad = copy.deepcopy(roles)
        bad['evaluation'][0].update(sourceId='fixture-train', sourceRowId='0')
        with self.assertRaises(ValueError):
            audit_role_isolation(bad)
        role['evaluationUsedForSelection'] = True
        with self.assertRaises(ValueError):
            fit_calibrators([[.1]]*4, [[0]]*4, [[1]]*4, tasks=['herg'], method='platt', model_binding=binding, role_binding=role, identity_roles=roles)

    def test_calibrator_hash_or_bound_weights_change_rejected(self):
        roles, binding, role = fixture()
        artifact = fit_calibrators([[.1], [.3], [.7], [.9]], [[0], [0], [1], [1]], [[1]]*4, tasks=['herg'], method='platt',
            model_binding=binding, role_binding=role, identity_roles=roles, minimum_per_class=2)
        changed = dict(binding, weightsSha256='f'*64)
        with self.assertRaises(ValueError):
            apply_calibrators([[.5]], artifact, changed, tasks=['herg'])
        artifact['endpoints']['herg']['parameters']['coefficient'] += 1
        with self.assertRaises(ValueError):
            apply_calibrators([[.5]], artifact, binding, tasks=['herg'])

    def test_diagnostics_bound_to_fit_inputs_do_not_claim_heldout_improvement(self):
        roles, binding, role = fixture()
        scores, labels, masks = [[.1], [.3], [.7], [.9]], [[0], [0], [1], [1]], [[1]]*4
        artifact = fit_calibrators(scores, labels, masks, tasks=['herg'], method='platt', model_binding=binding,
            role_binding=role, identity_roles=roles, minimum_per_class=2)
        result = calibration_diagnostics(scores, labels, masks, artifact, binding, tasks=['herg'], role_binding=role)
        self.assertFalse(result['independentTest'])
        self.assertEqual(result['role'], 'calibration-resubstitution')
        self.assertEqual(sum(r['count'] for r in result['endpoints']['herg']['reliabilityBefore']), 4)
        self.assertTrue(result['endpoints']['herg']['smallSampleWarning'])
        changed = copy.deepcopy(scores)
        changed[0][0] = .4
        with self.assertRaises(ValueError):
            calibration_diagnostics(changed, labels, masks, artifact, binding, tasks=['herg'], role_binding=role)

    def test_source_lineage_task_semantics_and_member_binding_rejected(self):
        roles, binding, role = fixture()
        bad = copy.deepcopy(roles)
        bad['train'][0]['publicationIds'] = ['doi:fixture-only']
        bad['calibration'][0]['publicationIds'] = ['DOI:FIXTURE-ONLY']
        with self.assertRaises(ValueError):
            audit_role_isolation(bad)
        for key, value in (('taskContractHashes', {'herg': '0'*64}), ('calibrationMembershipSha256', '0'*64),
                           ('calibrationSourceMembershipSha256', '0'*64), ('minimumPerClass', 20)):
            with self.assertRaises(ValueError):
                fit_calibrators([[.1], [.3], [.7], [.9]], [[0], [0], [1], [1]], [[1]]*4, tasks=['herg'], method='platt',
                    model_binding=binding, role_binding=dict(role, **{key: value}), identity_roles=roles, minimum_per_class=2)


if __name__ == '__main__':
    unittest.main()
