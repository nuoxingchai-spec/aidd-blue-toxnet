"""Offline orchestration fixtures; never execute a molecular encoder/model."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.evaluate_independent_models import MODELS, evaluator_source_sha256, register_exposure, run_evaluation
from backend.independent_validation_data import frozen_evaluation_settings
from test.test_independent_validation_metrics import fixture


class IndependentEvaluationToolTests(unittest.TestCase):
    def test_immutable_exposure_ledger_replay_and_protocol_change(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name)/'seal.json'
            path.write_text('{}', encoding='utf-8')
            kwargs = {'manifest_sha256': 'a'*64, 'protocol_sha256': 'b'*64, 'model_bindings': {'fixture': 'c'*64}, 'seed': 42,
                      'evaluator_sha256': evaluator_source_sha256(), 'evaluation_settings': frozen_evaluation_settings()}
            first = register_exposure(path, **kwargs)
            original = (path.parent/'evaluation-exposure.json').read_bytes()
            second = register_exposure(path, **kwargs)
            self.assertEqual(first['status'], 'first-registered-frozen-protocol')
            self.assertEqual(second['status'], 'previously-evaluated-same-frozen-protocol')
            self.assertEqual((path.parent/'evaluation-exposure.json').read_bytes(), original)
            self.assertFalse(second['freshBlindTestClaimed'])
            with self.assertRaises(ValueError):
                register_exposure(path, **dict(kwargs, protocol_sha256='d'*64))
            with self.assertRaises(ValueError):
                register_exposure(path, **dict(kwargs, evaluator_sha256='d'*64))
            self.assertFalse((path.parent/'.evaluation-exposure.lock').exists())

    def test_predictions_fixed_before_label_file_access_and_bound_models(self):
        rows, prediction, protocol = fixture()
        for row in rows:
            row['canonicalSmiles'] = 'fixture-input-not-used-by-mocked-predictor'
        events = []
        binding = dict(prediction['modelBinding'], modelVersion='fixture-v1')
        expected = [{**binding, 'modelId': model} for model in MODELS]
        manifest = {'qualified': True, 'status': 'sealed-independent-evaluation', 'modelBindings': expected,
                    'frozenProtocolSha256': 'e'*64, 'taskContractHashes': protocol['taskContractHashes'], 'taskContractVersion': 'fixture-only'}
        def load(*_, allow_labels=False, **__):
            events.append('labels' if allow_labels else 'structure-audit')
            returned = copy.deepcopy(rows)
            if not allow_labels:
                for row in returned:
                    row.pop('labels')
            return {'manifest': manifest, 'protocol': {'evaluationSettings': frozen_evaluation_settings(),
                    'evaluatorSourceSha256': evaluator_source_sha256()}, 'rows': returned}
        class Predictor:
            def __init__(self, **_): pass
            def binding(self): return binding
            def predict_batch(self, *_, **__):
                events.append('predict')
                return dict(prediction, modelBinding=binding)
        with tempfile.TemporaryDirectory() as name, patch('backend.independent_validation_data.load_sealed_dataset', side_effect=load):
            path = Path(name)/'seal.json'
            path.write_text('{}', encoding='utf-8')
            result = run_evaluation(path, 'f'*64, {model: {} for model in MODELS}, predictor_factory=Predictor)
            self.assertEqual(events, ['structure-audit', 'predict', 'predict', 'predict', 'labels'])
            self.assertTrue(result['labelReadAfterPrediction'])
            self.assertNotIn('canonicalSmiles', json.dumps(result))
            self.assertFalse(result['formalAvailability'])
            self.assertEqual(result['evaluationSettings']['bootstrapReplicates'], 500)

    def test_protocol_or_source_changes_rejected_before_model_or_label_access(self):
        for frozen in ({'evaluationSettings': frozen_evaluation_settings(), 'evaluatorSourceSha256': '0'*64},
                       {'evaluationSettings': dict(frozen_evaluation_settings(), bootstrapReplicates=10),
                        'evaluatorSourceSha256': evaluator_source_sha256()}):
            def load(*_, allow_labels=False, **__):
                self.assertFalse(allow_labels)
                return {'manifest': {'qualified': True, 'status': 'sealed-independent-evaluation'}, 'protocol': frozen}
            with patch('backend.independent_validation_data.load_sealed_dataset', side_effect=load), \
                    patch('scripts.evaluate_independent_models.FrozenResearchPredictor') as factory:
                with self.assertRaisesRegex(ValueError, 'source or statistical settings'):
                    run_evaluation('fixture-only', 'a'*64, {m: {} for m in MODELS}, predictor_factory=factory)
                factory.assert_not_called()

    def test_bootstrap_override_and_duplicate_model_binding_rejected(self):
        from scripts.evaluate_independent_models import _match_bindings
        with self.assertRaises(ValueError):
            _match_bindings([{'modelId': m} for m in (*MODELS, 'A4')], {})
        frozen = {'evaluationSettings': frozen_evaluation_settings(), 'evaluatorSourceSha256': evaluator_source_sha256()}
        opened = {'manifest': {'qualified': True, 'status': 'sealed-independent-evaluation'}, 'protocol': frozen}
        with patch('backend.independent_validation_data.load_sealed_dataset', return_value=opened), \
                patch('scripts.evaluate_independent_models.FrozenResearchPredictor') as factory:
            with self.assertRaises(ValueError):
                run_evaluation('fixture-only', 'a'*64, {m: {} for m in MODELS}, bootstrap_replicates=10, predictor_factory=factory)
            factory.assert_not_called()

    def test_unsupported_seed_does_not_open_data(self):
        with self.assertRaises(ValueError):
            run_evaluation('absent', 'a'*64, {m: {} for m in MODELS}, seed=43)


if __name__ == '__main__':
    unittest.main()
