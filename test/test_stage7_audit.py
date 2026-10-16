"""Synthetic software fixtures only; no real experimental labels or evidence."""
import copy
import json
import unittest
from unittest.mock import patch

from rdkit import rdBase

from backend.chemistry import CHEMISTRY_VERSION
from backend.dataset_overlap_audit import KNOWN_LINEAGE, REFERENCE_SCHEMA, object_sha256, reference_receipt
from backend.stage7_audit import audit_survey
from backend.v3_data import IDENTITY_POLICY, IDENTITY_VERSION, leakage_identity


def fixture_bundle(smiles=('C1CC1', 'C1CCC1')):
    return {
        'schema': 'aidd.stage7.chembl-labelblind.v1', 'version': 'ChEMBL_37',
        'target': {'target_chembl_id': 'CHEMBL240', 'pref_name': 'Software-only hERG target',
                   'organism': 'Homo sapiens', 'target_type': 'SINGLE PROTEIN'},
        'activities': [{'activity_id': i + 1, 'molecule_chembl_id': f'CHEMBL{100 + i}',
                        'canonical_smiles': value, 'assay_chembl_id': 'CHEMBL201',
                        'document_chembl_id': 'CHEMBL301', 'src_id': 1, 'record_id': 1000 + i,
                        'standard_type': 'IC50', 'standard_units': 'nM', 'type': 'IC50', 'units': 'nM'} for i, value in enumerate(smiles)],
        'assays': [{'assay_chembl_id': 'CHEMBL201', 'assay_type': 'F', 'assay_organism': 'Homo sapiens',
                    'assay_tax_id': 9606, 'assay_cell_type': None, 'assay_tissue': None,
                    'confidence_score': 9, 'relationship_type': 'D', 'document_chembl_id': 'CHEMBL301', 'src_id': 1}],
        'documents': [{'document_chembl_id': 'CHEMBL301', 'doi': '10.0000/software-fixture-only',
                       'pubmed_id': None, 'title': 'Synthetic software fixture', 'journal': 'Fixture',
                       'year': 2020, 'src_id': 1}],
    }


def fixture_references(structures=None, complete=True):
    structures = structures or {}
    result = {'schema': REFERENCE_SCHEMA, 'identityVersion': IDENTITY_VERSION, 'identityPolicy': IDENTITY_POLICY,
              'chemistryVersion': CHEMISTRY_VERSION, 'rdkitVersion': rdBase.rdkitVersion,
              'completeReferences': complete, 'missing': [] if complete else [{'role': 'fixture', 'reason': 'missing-reference'}],
              'referenceFiles': [], 'structures': structures, 'sourceLineage': copy.deepcopy(KNOWN_LINEAGE), 'labelsRead': False}
    result['receiptSha256'] = reference_receipt(result)
    return result


class Stage7AuditTests(unittest.TestCase):
    def audit(self, bundle=None, structures=None, complete=True):
        # Replace immutable pins only inside this synthetic unit test process.
        references = fixture_references(structures, complete)
        pins = {role: object_sha256(sorted(set(values))) for role, values in (structures or {}).items()}
        with patch('backend.dataset_overlap_audit.REFERENCE_FILES', {}), \
             patch('backend.dataset_overlap_audit.REFERENCE_STRUCTURE_HASHES', pins):
            return audit_survey(fixture_bundle() if bundle is None else bundle, references)

    def test_aggregate_is_labelblind_unqualified_and_does_not_mutate_input(self):
        bundle = fixture_bundle()
        before = copy.deepcopy(bundle)
        result = self.audit(bundle)
        self.assertEqual(bundle, before)
        self.assertEqual(result['inputRecordCount'], 2)
        self.assertEqual(result['structureAndDuplicateScreenPassedDistinctCount'], 2)
        self.assertEqual(result['independentlyQualifiedStructureCount'], 0)
        self.assertFalse(result['qualified'])
        self.assertFalse(result['canEnterStage7C'])
        self.assertFalse(result['labelsRead'])
        self.assertFalse(result['sealingPerformed'])
        self.assertFalse(result['independentAccuracyEvaluated'])
        self.assertIsNone(result['positiveCount'])
        self.assertIsNone(result['negativeCount'])
        self.assertEqual(result['surveySha256'], object_sha256(bundle))
        encoded = json.dumps(result)
        for value in ('C1CC1', 'CHEMBL100', 'CHEMBL201', '10.0000/software-fixture-only', 'canonical_smiles', 'record_id'):
            self.assertNotIn(value, encoded)
        self.assertEqual(sum(result['exclusiveRecordExclusions'].values()), 2)

    def test_extra_label_fields_rejected_at_every_level(self):
        for field in ('label', 'standard_value', 'pchembl_value', 'standard_relation', 'activity_comment', 'value', 'labelsSha256'):
            for location in ('top', 'activity', 'assay', 'document', 'target'):
                bundle = fixture_bundle()
                container = {'top': bundle, 'activity': bundle['activities'][0], 'assay': bundle['assays'][0],
                             'document': bundle['documents'][0], 'target': bundle['target']}[location]
                container[field] = 'must-not-be-consumed'
                with self.subTest(field=field, location=location), self.assertRaises(ValueError):
                    self.audit(bundle)

    def test_strict_ids_versions_and_target_are_not_coerced(self):
        for field, bad in (('activity_id', True), ('activity_id', '1'), ('src_id', 1.0), ('canonical_smiles', 23)):
            bundle = fixture_bundle(); bundle['activities'][0][field] = bad
            with self.subTest(field=field, value=bad), self.assertRaises(ValueError): self.audit(bundle)
        for field, bad in (('version', 'ChEMBL_36'), ('schema', 'unknown')):
            bundle = fixture_bundle(); bundle[field] = bad
            with self.assertRaises(ValueError): self.audit(bundle)
        for field, bad in (('organism', 'Mus musculus'), ('target_chembl_id', 'CHEMBL241'), ('target_type', 'PROTEIN COMPLEX')):
            bundle = fixture_bundle(); bundle['target'][field] = bad
            with self.assertRaises(ValueError): self.audit(bundle)

    def test_missing_invalid_and_unsupported_structures_are_not_imputed(self):
        result = self.audit(fixture_bundle((None, '', 'not-a-smiles', '*CC', '[301C]', 'C1CC1')))
        self.assertEqual(result['inputRecordCount'], 6)
        self.assertEqual(result['missingStructureRecordCount'], 2)
        self.assertEqual(result['invalidStructureRecordCount'], 3)
        self.assertEqual(result['validStructureRecordCount'], 1)
        self.assertEqual(sum(result['exclusiveRecordExclusions'].values()), 6)

    def test_canonical_and_molecule_duplicates_all_quarantined(self):
        bundle = fixture_bundle(('C1CC1', 'C1(CC1)', 'C1CCC1', 'C1CCCC1'))
        bundle['activities'][3]['molecule_chembl_id'] = bundle['activities'][2]['molecule_chembl_id']
        result = self.audit(bundle)
        self.assertEqual(result['distinctCanonicalStructureCount'], 3)
        self.assertEqual(result['duplicateCanonicalGroupCount'], 1)
        self.assertEqual(result['duplicateCanonicalRecordCount'], 2)
        self.assertEqual(result['duplicateMoleculeIdCount'], 1)
        self.assertEqual(result['duplicateMoleculeRecordCount'], 2)
        self.assertEqual(result['structureAndDuplicateScreenPassedDistinctCount'], 0)
        self.assertEqual(result['exclusiveRecordExclusions'], {'duplicate-canonical-structure': 2, 'duplicate-molecule-identifier': 2})

    def test_duplicate_metadata_or_activity_ids_reject_corrupt_pagination(self):
        for field in ('assays', 'documents', 'activities'):
            bundle = fixture_bundle(); bundle[field].append(copy.deepcopy(bundle[field][0]))
            with self.subTest(field=field), self.assertRaises(ValueError): self.audit(bundle)

    def test_structural_identity_levels_and_original_chemistry_preserved(self):
        pairs = [
            ('C1CC1', 'C1(CC1)', 'canonical'),
            ('N[C@H](C)c1ccccc1', 'N[C@@H](C)c1ccccc1', 'stereoIsotopeEquivalent'),
            ('[13CH2]1CC1', 'C1CC1', 'stereoIsotopeEquivalent'),
            ('C1CC1.[Cl-]', 'C1CC1', 'parent'),
            ('O=c1cccc[nH]1', 'Oc1ccccn1', 'tautomerParent'),
            ('CC1CC1', 'C1CC1', 'scaffold'),
            ('CCC', 'CCO', 'scaffold'),
        ]
        for candidate, historical, kind in pairs:
            bundle = fixture_bundle((candidate,))
            with self.subTest(kind=kind, candidate=candidate):
                result = self.audit(bundle, {'old-test': [historical]})
                self.assertEqual(result['directHistoricalOverlapDistinctStructureCount'], 1)
                self.assertEqual(result['structureAndDuplicateScreenPassedDistinctCount'], 0)
                self.assertEqual(result['historicalOverlapDistinctStructuresByIdentity'][kind], 1)
                self.assertEqual(bundle['activities'][0]['canonical_smiles'], candidate)

    def test_connected_overlap_propagates_before_duplicate_exclusion(self):
        # The tested component graph is synthetic; real chemistry levels are
        # separately exercised above. A duplicate cannot remove the bridge.
        def synthetic_overlap(canonical, forbidden):
            identity = leakage_identity(canonical)
            identity['scaffolds'] = ['software-only-shared-component']
            return identity, ['old-test:parent'] if canonical == 'C1CC1' else []
        with patch('backend.stage7_audit.structural_overlap', side_effect=synthetic_overlap):
            result = self.audit(fixture_bundle(('C1CC1', 'C1CC1', 'C1CCC1')))
        self.assertEqual(result['directHistoricalOverlapDistinctStructureCount'], 1)
        self.assertEqual(result['connectedOverlapDistinctStructureCount'], 1)
        self.assertEqual(result['structureAndDuplicateScreenPassedDistinctCount'], 0)
        self.assertEqual(result['exclusiveRecordExclusions'], {'historical-overlap-connected-component': 1, 'historical-structure-overlap': 2})

    def test_missing_references_cannot_count_any_structure_as_screen_passed(self):
        result = self.audit(complete=False)
        self.assertIn('incomplete-historical-references', result['blockingReasons'])
        self.assertEqual(result['structureAndDuplicateScreenPassedDistinctCount'], 0)
        self.assertFalse(result['completeReferences'])
        references = fixture_references(); references['receiptSha256'] = '0' * 64
        with self.assertRaises(ValueError): audit_survey(fixture_bundle(), references)

    def test_source_family_overlap_does_not_claim_publication_or_cohort_disjointness(self):
        result = self.audit()
        source_names = {row['historicalSourceId'] for row in result['sourceOverlap']}
        self.assertIn('tdc-herg', source_names)
        self.assertIn('herg_karim', source_names)
        self.assertIn('unresolved', result['exactPublicationAssayCohortIndependence'])
        self.assertEqual(result['nonAdditiveRecordReasonCounts']['historical-herg-literature-source-family-not-disjoint'], 2)
        bundle = fixture_bundle(); bundle['documents'][0]['doi'] = 'https://doi.org/10.1186/S13321-021-00541-Z'
        match = next(row for row in self.audit(bundle)['sourceOverlap'] if row['historicalSourceId'] == 'herg_karim')
        self.assertIn('publicationIds', match['matchedFields'])

    def test_missing_mapping_publication_and_inconsistent_joins_are_counted(self):
        bundle = fixture_bundle(); bundle['assays'] = []; bundle['documents'] = []
        result = self.audit(bundle)
        self.assertEqual(result['nonAdditiveRecordReasonCounts']['missing-assay-mapping'], 2)
        self.assertEqual(result['nonAdditiveRecordReasonCounts']['missing-activity-document-mapping'], 2)
        bundle = fixture_bundle(); bundle['documents'][0]['doi'] = None
        bundle['assays'][0]['document_chembl_id'] = 'CHEMBL302'
        bundle['assays'][0]['src_id'] = 2; bundle['documents'][0]['src_id'] = 2
        result = self.audit(bundle)
        for reason in ('missing-original-publication-identifier', 'activity-assay-document-mismatch',
                       'missing-assay-document-mapping', 'activity-assay-source-mismatch', 'activity-document-source-mismatch'):
            self.assertEqual(result['nonAdditiveRecordReasonCounts'][reason], 2)
        self.assertEqual(result['exclusiveRecordExclusions']['unresolved-record-metadata'], 2)

    def test_target_human_does_not_make_assays_human_or_endpoints_equivalent(self):
        bundle = fixture_bundle(('C1CC1', 'C1CCC1', 'C1CCCC1'))
        bundle['assays'][0]['assay_organism'] = 'Cavia porcellus'; bundle['assays'][0]['assay_tax_id'] = 10141
        bundle['activities'][1]['standard_type'] = 'Ki'
        bundle['activities'][2]['standard_type'] = 'Inhibition'; bundle['activities'][2]['standard_units'] = '%'
        result = self.audit(bundle)
        self.assertEqual(result['assaySpeciesRecordCounts'], {'non-human': 3})
        self.assertEqual(result['nonAdditiveRecordReasonCounts']['human-assay-species-not-established'], 3)
        self.assertEqual(len(result['endpointUnitRecordGroups']), 3)
        self.assertEqual(result['nonAdditiveRecordReasonCounts']['task-equivalence-not-reviewed'], 3)

    def test_unknown_free_text_never_leaks_into_public_metadata_groups(self):
        bundle = fixture_bundle()
        bundle['activities'][0]['standard_type'] = 'private-path-or-payload'
        bundle['activities'][0]['standard_units'] = 'private-path-or-payload'
        bundle['assays'][0]['assay_type'] = 'private-path-or-payload'
        result = self.audit(bundle)
        self.assertNotIn('private-path-or-payload', json.dumps(result))
        self.assertIn('other-unreviewed', json.dumps(result))

    def test_empty_survey_is_explicitly_unqualified(self):
        result = self.audit(fixture_bundle(()))
        self.assertEqual(result['inputRecordCount'], 0)
        self.assertIn('no-real-candidate-records', result['blockingReasons'])
        self.assertFalse(result['canEnterStage7C'])


if __name__ == '__main__':
    unittest.main()
