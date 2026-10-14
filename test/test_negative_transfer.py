"""CPU exposed-development fixtures, not independent scientific evidence."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from backend.negative_transfer import analyze_development_experiment, controlled_ablation_config, gradient_cosine


class NegativeTransferTests(unittest.TestCase):
    def test_gradient_cosine_is_input_only_diagnostic(self):
        self.assertEqual(gradient_cosine([1, 0], [-1, 0])['cosine'], -1)
        self.assertIsNone(gradient_cosine([0, 0], [1, 0])['cosine'])
        self.assertFalse(controlled_ablation_config()['retrainingAuthorized'])

    def test_same_membership_and_exposed_only_report(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            runs = []
            for model in ('A4', 'B16'):
                directory = root/f'{model}-seed-42'
                directory.mkdir()
                predictions = [{'smiles': smiles, 'endpoint': task, 'label': index, 'score': .2 if index == 0 else .8,
                    'split': 'validation', 'predictionStatus': 'trained'} for task in ('herg', 'dili', 'ames', 'clintox')
                    for index, smiles in enumerate(('c1ccccc1', 'C1CCCCC1'))]
                (directory/'predictions.json').write_text(json.dumps(predictions), encoding='utf-8')
                (directory/'weights.bin').write_bytes(b'explicit-unit-fixture-never-real-model')
                artifacts = {key: {'file': filename, 'sha256': hashlib.sha256((directory/filename).read_bytes()).hexdigest()}
                             for key, filename in (('validationPredictions', 'predictions.json'), ('weights', 'weights.bin'))}
                manifest = {'status': 'trained-completed', 'formalAvailability': False, 'artifacts': artifacts,
                    'training': {'fourTaskValidationMembershipSha256': 'a'*64}, 'dataset': {'version': 'fixture'},
                    'taskContracts': {task: {'positiveClass': 'fixture'} for task in ('herg', 'dili', 'ames', 'clintox')}}
                (directory/'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
                runs.append({'model': model, 'seed': 42, 'manifestSha256': hashlib.sha256((directory/'manifest.json').read_bytes()).hexdigest()})
            (root/'experiment.json').write_text(json.dumps({'status': 'completed', 'newBlindTestClaimed': False,
                'datasetVersion': 'fixture-only', 'splitSha256': 'b'*64, 'runs': runs}), encoding='utf-8')
            result = analyze_development_experiment(root, bootstrap_replicates=10, seeds=(42,))
            self.assertTrue(result['previouslyExposed'])
            self.assertFalse(result['independentTest'])
            self.assertIsNone(result['independentSeedSampleSize'])
            self.assertEqual(result['runs'][0]['endpoints']['clintox']['positives'], 1)
            self.assertEqual(result['runs'][0]['endpoints']['clintox']['confidenceIntervals']['status'], 'insufficient-support')
            self.assertIsNone(result['seedVariability']['clintox']['A4']['averagePrecision']['standardDeviationAcrossSeeds'])
            self.assertEqual(result['runs'][0]['gradientInterference']['status'], 'not-measured')
            self.assertEqual(result['runs'][0]['endpoints']['clintox']['coverageFraction'], 1)
            self.assertNotIn('smiles', json.dumps(result).lower())
            (root/'A4-seed-42'/'weights.bin').write_bytes(b'changed')
            with self.assertRaises(ValueError):
                analyze_development_experiment(root, bootstrap_replicates=10, seeds=(42,))


if __name__ == '__main__':
    unittest.main()
