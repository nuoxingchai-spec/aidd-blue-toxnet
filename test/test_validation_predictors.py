"""Small explicit fake-loader CPU fixture; never a trained-model acceptance."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import numpy as np

from backend.validation_predictors import FrozenResearchPredictor


class ValidationPredictorTests(unittest.TestCase):
    def test_order_dimensions_actual_branch_transform_and_invalid_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'fixture-training-references.json'
            path.write_text(json.dumps(['CCO']), encoding='utf-8')
            manifest = {'modelVersion': 'fixture-only', 'tasks': ['herg'], 'taskContractVersion': 'fixture-only',
                'taskContracts': {'herg': {'positiveClass': 'fixture label'}}, 'dataset': {'datasetVersion': 'fixture', 'splitSha256': 'a'*64},
                'artifacts': {key: {'sha256': 'b'*64} for key in ('weights', 'preprocessing', 'trainingReferences')},
                'training': {'endpointStatus': {'herg': 'trained'}}}
            def predict(head, features, **_):
                np.testing.assert_equal(features, [[1, 2]])
                return np.array([[.75]])
            loaded = {'manifest': manifest, 'manifestSha256': 'c'*64, 'paths': {'trainingReferences': path},
                      'encoder': None, 'mean': np.array([1, 1]), 'scale': np.array([2, 2]), 'head': None,
                      'module': SimpleNamespace(morgan_features=lambda _: np.array([[3, 5]]), predict_head=predict)}
            client = SimpleNamespace(_loaded=loaded, _ensure_loaded=lambda: loaded)
            predictor = FrozenResearchPredictor(client=client)
            result = predictor.predict_batch(['CCO', 'invalid-input'], batch_size=1)
            self.assertEqual(result['structureIds'][0], hashlib.sha256(b'CCO').hexdigest())
            self.assertEqual(result['scores'], [[.75], [None]])
            self.assertEqual(result['status'], 'partial-coverage')
            self.assertEqual(result['coverage'][1]['status'], 'unsupported')
            self.assertEqual(predictor.training_references(), ['CCO'])


if __name__ == '__main__':
    unittest.main()
