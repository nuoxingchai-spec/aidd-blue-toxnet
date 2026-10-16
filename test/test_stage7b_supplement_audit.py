"""Synthetic CPU contracts only; no candidate results or scientific validation."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.dataset_overlap_audit import object_sha256
from backend.stage7b_supplement_audit import (EXPANDED_SCHEMA, _combined_screen,
    audit_expanded, eligibility_gate, full_source_structure_references)
from backend.v3_data import build_forbidden_index
from test.test_stage7_audit import fixture_bundle, fixture_references


class SupplementAuditTests(unittest.TestCase):
    def expanded(self, smiles=('C1CC1', 'C1CCC1')):
        bundle = fixture_bundle(smiles)
        bundle['schema'] = EXPANDED_SCHEMA
        return bundle

    def audit(self, bundle=None, raw_history=None):
        bundle = self.expanded() if bundle is None else bundle
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            specs = {}
            if raw_history is not None:
                raw = raw_history.encode('utf-8')
                (root / 'history.tsv').write_bytes(raw)
                specs = {'fixture-full-source': {'relative': 'history.tsv', 'sha256': hashlib.sha256(raw).hexdigest(),
                                                'delimiter': '\t', 'column': 'Drug'}}
            with patch('backend.dataset_overlap_audit.REFERENCE_FILES', {}), \
                 patch('backend.dataset_overlap_audit.REFERENCE_STRUCTURE_HASHES', {}), \
                 patch('backend.stage7b_supplement_audit.HISTORY_SPECS', specs):
                return audit_expanded(bundle, fixture_references(), root=root)

    def test_reuses_strict_original_schema_without_changing_original_members_or_claiming_ready(self):
        bundle = self.expanded()
        original = copy.deepcopy(bundle)
        result = self.audit(bundle)
        self.assertEqual(bundle, original)
        self.assertEqual(result['baseFixedReferenceAudit']['inputRecordCount'], 2)
        self.assertEqual(result['combinedStructureScreen']['verifiedStructureAndDuplicatePasses'], 2)
        self.assertEqual(result['expandedBundleSha256'], object_sha256(bundle))
        self.assertNotEqual(result['expandedBundleSha256'], result['originalAuditInputSha256'])
        self.assertFalse(result['qualified'])
        self.assertFalse(result['canEnterStage7C'])
        self.assertFalse(result['labelsRead'])
        self.assertTrue(all(value is None for key, value in result['candidateOutcomeClassification'].items() if key != 'status'))
        self.assertNotIn('C1CC1', json.dumps(result))

    def test_candidate_result_fields_versions_and_caps_fail_closed(self):
        for field in ('standard_value', 'standard_relation', 'pchembl_value', 'activity_comment', 'quality_flags', 'label'):
            bundle = self.expanded(); bundle['activities'][0][field] = 'software-only-forbidden'
            with self.subTest(field=field), self.assertRaises(ValueError): self.audit(bundle)
        bundle = self.expanded(); bundle['schema'] = 'unknown'
        with self.assertRaises(ValueError): self.audit(bundle)
        bundle = self.expanded(); bundle['activities'] *= 1001
        with self.assertRaises(ValueError): self.audit(bundle)

    def test_full_raw_source_adds_history_exclusion_without_looking_at_label_cells(self):
        result = self.audit(raw_history='Drug\tY\nC1CC1\tTHIS_IS_NOT_A_BINARY_LABEL\n')
        self.assertEqual(result['baseFixedReferenceAudit']['structureAndDuplicateScreenPassedDistinctCount'], 2)
        self.assertEqual(result['combinedStructureScreen']['directHistoricalOverlapDistinctStructures'], 1)
        self.assertEqual(result['combinedStructureScreen']['verifiedStructureAndDuplicatePasses'], 1)
        source = result['additiveFullSourceHistory']['sources']['fixture-full-source']
        self.assertEqual(source['inputRows'], 1)
        self.assertEqual(source['uniqueCanonicalStructures'], 1)
        self.assertNotIn('THIS_IS_NOT_A_BINARY_LABEL', json.dumps(result))

    def test_source_hash_failure_or_missing_file_is_unknown_not_an_empty_verified_dataset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            specs = {'fixture': {'relative': 'missing.tsv', 'sha256': '0' * 64,
                                 'delimiter': '\t', 'column': 'Drug'}}
            with patch('backend.stage7b_supplement_audit.HISTORY_SPECS', specs), \
                 patch('backend.stage7b_supplement_audit.csv.reader', side_effect=AssertionError('must not parse unverified bytes')):
                result = full_source_structure_references(root)
                self.assertFalse(result['receipt']['complete'])
                self.assertIsNone(result['receipt']['sources']['fixture']['inputRows'])
                (root / 'missing.tsv').write_text('Drug\tY\nC1CC1\t0\n', encoding='utf-8')
                self.assertFalse(full_source_structure_references(root)['receipt']['complete'])

    def test_unsupported_history_and_ambiguous_headers_do_not_silently_become_complete(self):
        for raw in ('Drug\tY\n*CC\tresult-not-read\n', 'Drug\tDrug\nC1CC1\tC1CC1\n',
                    'Wrong\tY\nC1CC1\tresult-not-read\n'):
            with self.subTest(raw=raw):
                result = self.audit(raw_history=raw)
                self.assertFalse(result['additiveFullSourceHistory']['complete'])
                self.assertIsNone(result['combinedStructureScreen']['verifiedStructureAndDuplicatePasses'])
                self.assertEqual(result['combinedStructureScreen']['screenStatus'], 'provisional-incomplete-history')

    def test_parent_stereo_isotope_tautomer_and_scaffold_use_unchanged_policy(self):
        pairs = [('C1CC1.[Cl-]', 'C1CC1'), ('[13CH2]1CC1', 'C1CC1'),
                 ('N[C@H](C)c1ccccc1', 'N[C@@H](C)c1ccccc1'),
                 ('O=c1cccc[nH]1', 'Oc1ccccn1'), ('CC1CC1', 'C1CC1')]
        for candidate, historical in pairs:
            with self.subTest(candidate=candidate):
                result = self.audit(self.expanded((candidate,)), raw_history='Drug\tY\n' + historical + '\tignored\n')
                self.assertEqual(result['combinedStructureScreen']['directHistoricalOverlapDistinctStructures'], 1)
                self.assertEqual(result['combinedStructureScreen']['verifiedStructureAndDuplicatePasses'], 0)

    def test_duplicate_and_invalid_record_disposition_is_deterministic_and_partitions_input(self):
        bundle = fixture_bundle(('C1CC1', 'C1(CC1)', 'C1CCC1', None))
        index, _ = build_forbidden_index({'old-test': ['C1CCC1']})
        first = _combined_screen(bundle['activities'], index, True)
        second = _combined_screen(list(reversed(bundle['activities'])), index, True)
        self.assertEqual(first, second)
        self.assertEqual(sum(first['exclusiveRecordDisposition'].values()), 4)
        self.assertEqual(first['verifiedStructureAndDuplicatePasses'], 0)

    def test_gate_rejects_exposed_wrong_schema_or_evaluation_inputs_and_cannot_be_enabled_by_flags(self):
        chembl = self.audit()
        epa = {'schema': 'aidd.stage7b.epa-structure-audit.v1', 'candidateLabelsRead': False,
               'modelsExecuted': False, 'evaluationPerformed': False}
        contract = {'schema': 'aidd.stage7b.herg-contract-evidence.v1', 'labelsRead': False}
        result = eligibility_gate(chembl, epa, contract)
        self.assertEqual(result['status'], 'STAGE7C_BLOCKED')
        self.assertFalse(result['approvedLabelPolicyFrozen'])
        self.assertFalse(result['evaluationProtocolFrozen'])
        self.assertIsNone(result['candidateClassCounts'])
        self.assertEqual(result['supportRequirements']['minimumPerClass'], 10)
        self.assertEqual(result['supportRequirements']['minimumScaffoldClusters'], 10)
        changed = copy.deepcopy(epa); changed['qualificationGranted'] = True
        self.assertEqual(eligibility_gate(chembl, changed, contract)['status'], 'STAGE7C_BLOCKED')
        for field, bad in (('candidateLabelsRead', True), ('modelsExecuted', True),
                           ('evaluationPerformed', True), ('schema', 'unknown')):
            changed = copy.deepcopy(epa); changed[field] = bad
            with self.subTest(field=field), self.assertRaises(ValueError): eligibility_gate(chembl, changed, contract)


if __name__ == '__main__':
    unittest.main()
