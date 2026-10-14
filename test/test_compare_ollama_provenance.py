"""Metadata projection fixtures; no model loading or HTTP requests."""
from copy import deepcopy
import json
import unittest

from backend.compare_ollama_txgemma import OllamaTxGemmaAdapter, TOKENIZER_IDENTITY
from backend.compare_persistence import project_record
from test.test_compare_persistence import fixture_record


class OllamaProvenanceTests(unittest.TestCase):
    def test_source_and_generation_contract_survives_minimal_and_full_history(self):
        provenance = OllamaTxGemmaAdapter(env={})._provenance()
        provenance.update(revision='a' * 64, licenseStatus='operator-accepted',
                          localPackageVerified=True, apiKey='sk-private-fixture123',
                          modelDir='C:\\private\\model')
        for full in (False, True):
            with self.subTest(full=full):
                record = fixture_record(full=full)
                record['models']['txgemma-2b-predict'] = {'status': 'completed',
                    'data': None, 'provenance': deepcopy(provenance), 'metrics': {}, 'error': None}
                saved = project_record(record)['models']['txgemma-2b-predict']['provenance']
                for key in ('runtime', 'sourceKind', 'sourceRepository', 'sourceRevision',
                            'ggufSha256', 'upstreamRevisionStatus', 'localPackageVerified',
                            'generationOptions', 'rawPrompt', 'modelContextLength'):
                    self.assertEqual(saved[key], provenance[key])
                self.assertEqual(saved['tokenizerIdentity'], TOKENIZER_IDENTITY)
                self.assertIsNone(saved['upstreamRevision'])
                self.assertNotIn('apiKey', saved)
                self.assertNotIn('modelDir', saved)
                self.assertNotIn('private', json.dumps(saved))

    def test_qwen_generation_budget_survives_summary_without_report_text(self):
        record = fixture_record()
        record['models']['ollama-qwen-local']['provenance'] = {
            'runtime': 'ollama', 'generationOptions': {'num_ctx': 8192, 'num_predict': 4096}}
        saved = project_record(record)
        self.assertEqual(saved['models']['ollama-qwen-local']['provenance']['generationOptions'],
                         {'num_ctx': 8192, 'num_predict': 4096})
        self.assertNotIn('private generated prose', json.dumps(saved))
