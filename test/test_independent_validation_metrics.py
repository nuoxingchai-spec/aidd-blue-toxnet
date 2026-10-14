"""CPU numeric fixtures, never independent scientific results."""
import copy
import unittest

from backend.independent_validation_metrics import cluster_bootstrap, digest, evaluate_paired_models, evaluate_predictions, point_metrics


def fixture():
    contract = 'a'*64
    rows = [{'structureId': f'{i:064x}', 'groupId': f'fixture-scaffold-{i}', 'sourceDataset': 'fixture-source-'+str(i%2), 'labels': {'herg': i%2}} for i in range(12)]
    binding = {'artifactVerified': True, 'weightsSha256': 'b'*64, 'manifestSha256': 'c'*64,
               'taskContractVersion': 'fixture-only', 'taskContractHashes': {'herg': contract}}
    prediction = {'structureIds': [r['structureId'] for r in rows], 'taskIds': ['herg'],
                  'scores': [[.8 if i%2 else .2] for i in range(12)], 'modelBinding': binding}
    protocol = {'qualified': True, 'role': 'independent-evaluation', 'protocolSha256': 'd'*64,
                'tasks': ['herg'], 'taskContractVersion': 'fixture-only', 'taskContractHashes': {'herg': contract},
                'minimumBootstrapPerClass': 2, 'minimumBootstrapGroups': 2}
    return rows, prediction, protocol


class IndependentMetricsTests(unittest.TestCase):
    def test_single_class_and_missing_are_not_ranked(self):
        self.assertIsNone(point_metrics([1, 1], [.2, .8])['auroc'])
        self.assertIsNone(point_metrics([0, 0], [.2, .8])['averagePrecision'])
        self.assertIsNone(point_metrics([], [])['brier'])
        self.assertAlmostEqual(point_metrics([0, 1], [.2, .8])['brier'], .04)

    def test_scaffold_bootstrap_reproducible_and_four_positives_guarded(self):
        y, p = [0]*8+[1]*4, [.2]*8+[.8]*4
        guarded = cluster_bootstrap(y, p, [str(i) for i in range(12)], replicates=20)
        self.assertEqual(guarded['status'], 'insufficient-support')
        first = cluster_bootstrap(y, p, [str(i) for i in range(12)], replicates=20, minimum_per_class=2, minimum_groups=2)
        self.assertEqual(first, cluster_bootstrap(y, p, [str(i) for i in range(12)], replicates=20, minimum_per_class=2, minimum_groups=2))
        self.assertEqual(first['intervals']['auroc']['lower'], 1)

    def test_source_strata_masks_and_paired_membership(self):
        rows, prediction, protocol = fixture()
        result = evaluate_paired_models(rows, {'fixtureA': prediction, 'fixtureB': copy.deepcopy(prediction)}, protocol, bootstrap_replicates=20)
        self.assertEqual(len(result['models']['fixtureA']['sourceStrata']), 2)
        delta = result['pairedDifferences'][0]['endpoints']['herg']
        self.assertEqual(delta['count'], 12)
        self.assertEqual(delta['deltaSecondMinusFirst']['averagePrecision'], 0)
        self.assertFalse(result['selectionPermitted'])

    def test_missing_score_blocks_endpoint_no_silent_drop(self):
        rows, prediction, protocol = fixture()
        prediction['scores'][0][0] = None
        result = evaluate_predictions(rows, prediction, protocol, bootstrap_replicates=20)
        self.assertEqual(result['endpoints']['herg']['count'], 12)
        self.assertEqual(result['endpoints']['herg']['coveredCount'], 11)
        self.assertIsNone(result['endpoints']['herg']['auroc'])
        self.assertEqual(result['coverage']['uncoveredStructures'], 1)

    def test_wrong_order_contract_and_qualification_rejected(self):
        rows, prediction, protocol = fixture()
        for mutate in (lambda p: p['structureIds'].reverse(), lambda p: p['modelBinding']['taskContractHashes'].update(herg='e'*64)):
            bad = copy.deepcopy(prediction)
            mutate(bad)
            with self.assertRaises(ValueError):
                evaluate_predictions(rows, bad, protocol, bootstrap_replicates=20)
        protocol['qualified'] = False
        with self.assertRaises(ValueError):
            evaluate_predictions(rows, prediction, protocol, bootstrap_replicates=20)

    def test_missing_labels_are_not_negatives(self):
        rows, prediction, protocol = fixture()
        rows[0]['labels'] = {}
        result = evaluate_predictions(rows, prediction, protocol, bootstrap_replicates=20)
        self.assertEqual(result['endpoints']['herg']['count'], 11)
        self.assertEqual(result['endpoints']['herg']['positives'], 6)
        self.assertEqual(result['endpoints']['herg']['missingLabelCount'], 1)
        self.assertEqual(result['endpoints']['herg']['coverageDenominator'], 11)
        self.assertAlmostEqual(result['endpoints']['herg']['positiveRate'], 6/11)

    def test_failed_member_remains_in_denominator_and_blocks_rank_difference(self):
        rows, first, protocol = fixture()
        second = copy.deepcopy(first)
        second['scores'][0][0] = None
        comparison = evaluate_paired_models(rows, {'A': first, 'B': second}, protocol, bootstrap_replicates=20)
        result = comparison['pairedDifferences'][0]['endpoints']['herg']
        self.assertEqual(result['status'], 'coverage-incomplete')
        self.assertEqual(result['commonEvaluableMemberCount'], 11)
        self.assertAlmostEqual(result['coverageFraction'], 11/12)
        self.assertEqual(result['coverageDenominator'], 12)
        self.assertEqual(result['commonEvaluableMemberOrderSha256'], digest([row['structureId'] for row in rows[1:]]))
        self.assertTrue(all(value is None for value in result['deltaSecondMinusFirst'].values()))
        self.assertIsNone(result['confidenceIntervals'])

    def test_no_observed_labels_produces_null_metrics_not_paired_ranking(self):
        rows, prediction, protocol = fixture()
        for row in rows:
            row['labels'] = {}
        result = evaluate_paired_models(rows, {'A': prediction, 'B': copy.deepcopy(prediction)}, protocol, bootstrap_replicates=20)
        endpoint = result['models']['A']['endpoints']['herg']
        self.assertEqual(endpoint['status'], 'no-data')
        self.assertIsNone(endpoint['positiveRate'])
        self.assertEqual(endpoint['missingLabelCount'], len(rows))
        self.assertEqual(result['pairedDifferences'][0]['endpoints']['herg']['status'], 'no-observed-labels')

    def test_independent_ranking_low_support_null_development_point_estimate_retained(self):
        rows, prediction, protocol = fixture()
        protocol['minimumBootstrapPerClass'] = 10
        independent = evaluate_predictions(rows, prediction, protocol, bootstrap_replicates=20)['endpoints']['herg']
        self.assertEqual(independent['status'], 'insufficient-class-support')
        self.assertIsNone(independent['auroc'])
        self.assertIsNone(independent['averagePrecision'])
        self.assertTrue(independent['smallSampleWarning'])
        self.assertAlmostEqual(independent['brier'], .04)
        protocol['role'] = 'development-retrospective'
        historical = evaluate_predictions(rows, prediction, protocol, bootstrap_replicates=20)['endpoints']['herg']
        self.assertEqual(historical['auroc'], 1)
        self.assertEqual(historical['confidenceIntervals']['status'], 'insufficient-support')


if __name__ == '__main__':
    unittest.main()
