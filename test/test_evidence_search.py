"""Explicit local unit fixtures; never published as real experimental evidence."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from backend.evidence_search import prepare_index, EvidenceSearch


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.rows = [{'smiles': smi, 'endpoint': 'herg', 'label': label, 'sourceKey': key,
                      'source_id': 'unit-fixture-only', 'source_row': str(i), 'dataRole': 'unit-fixture'}
                     for i, (smi, label, key) in enumerate([('CCO', 1, 'herg'), ('CCCO', 0, 'herg'), ('[13CH3]CO', 0, 'herg_karim')])]
        self.rows.sort(key=lambda r: (r['smiles'], r['endpoint'], r['sourceKey'], r['source_row']))
        self.mock = patch('backend.evidence_search._source_rows', return_value=(self.rows, [])); self.mock.start()
        self.directory = self.root / 'index'; prepare_index(self.directory)
        self.search = EvidenceSearch(self.directory)
    def tearDown(self): self.mock.stop(); self.temp.cleanup()

    def test_exact_identity_and_similarity_not_confidence(self):
        value = self.search.search('OCC', endpoint='herg', limit=3)
        self.assertEqual(value['status'], 'available'); row = value['results'][0]
        self.assertEqual(row['canonicalSmiles'], 'CCO'); self.assertTrue(row['exactMatch']); self.assertEqual(row['similarity'], 1)
        self.assertNotIn('confidence', row); self.assertEqual(row['label'], 1)
        self.assertTrue(row['imageDataUrl'].startswith('data:image/png;base64,'))
        self.assertEqual(row['source_id'], 'unit-fixture-only')

    def test_isotope_identity_not_merged_even_if_fingerprint_matches(self):
        rows = self.search.search('CCO', limit=3)['results']
        isotope = next(r for r in rows if r['canonicalSmiles'] == '[13CH3]CO')
        self.assertFalse(isotope['exactMatch']); self.assertEqual(isotope['similarity'], 1)

    def test_karim_definition_not_equated_to_original_herg(self):
        row = next(r for r in self.search.search('CCO', limit=3)['results'] if r['sourceKey'] == 'herg_karim')
        self.assertEqual(row['definitionConsistency']['status'], 'not-equivalent-threshold')
        self.assertIn('10', row['positiveDefinition'])

    def test_endpoint_no_observations_evidence_insufficient(self):
        value = self.search.search('CCO', endpoint='dili')
        self.assertEqual(value['status'], 'insufficient-evidence'); self.assertEqual(value['results'], [])

    def test_index_missing_no_fake_similar_samples(self):
        self.assertEqual(EvidenceSearch(self.root/'missing').search('CCO')['results'], [])

    def test_hash_tamper_refused(self):
        (self.directory/'observations.jsonl').write_text('[]')
        self.assertEqual(self.search.search('CCO')['status'], 'insufficient-evidence')

    def test_rehashed_fabricated_label_still_refused(self):
        path = self.directory/'observations.jsonl'; text = path.read_text().replace('"label": 1', '"label": 0')
        path.write_text(text)
        manifest = json.loads((self.directory/'manifest.json').read_text()); h = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest['records']['sha256'] = h; manifest['datasetVersion'] = 'aidd-evidence-' + h[:16]
        (self.directory/'manifest.json').write_text(json.dumps(manifest))
        self.assertEqual(self.search.search('CCO')['results'], [])

    def test_source_failure_invalidates_cached_search(self):
        self.search.search('CCO')
        with patch('backend.evidence_search._source_rows', side_effect=ValueError('source changed')):
            self.assertEqual(self.search.search('CCO')['results'], [])

    def test_bounded_query_and_invalid_smiles(self):
        for limit in (0, 6, True):
            with self.assertRaises(ValueError): self.search.search('CCO', limit=limit)
        with self.assertRaises(ValueError): self.search.search('CCO', endpoint='fake-endpoint')
        self.assertEqual(self.search.search('CC=')['status'], 'invalid-input')

    def test_existing_index_not_overwritten(self):
        with self.assertRaises(ValueError): prepare_index(self.directory)
