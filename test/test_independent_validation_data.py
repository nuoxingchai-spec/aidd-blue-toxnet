"""Synthetic CPU contracts only; no fixture qualifies as real study evidence."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.chemistry import canonicalize_smiles
from backend.dataset_overlap_audit import (REFERENCE_SCHEMA, KNOWN_LINEAGE, build_historical_references,
    object_sha256, reference_receipt, validate_references, file_sha256, lineage_overlap,
    evaluator_source_sha256, EVALUATOR_SOURCE_FILES)
from backend.independent_validation_data import (CandidateMetadata, IndependentProtocol,
    audit_dataset, seal_dataset, load_sealed_dataset, frozen_evaluation_settings)
from backend.v3_data import IDENTITY_VERSION, IDENTITY_POLICY
from backend.chemistry import CHEMISTRY_VERSION
from rdkit import rdBase


def fixture_references(structures=None):
    structures = structures or {}
    value = {'schema': REFERENCE_SCHEMA, 'identityVersion': IDENTITY_VERSION, 'identityPolicy': IDENTITY_POLICY,
        'chemistryVersion': CHEMISTRY_VERSION, 'rdkitVersion': rdBase.rdkitVersion,
        'completeReferences': True, 'missing': [], 'referenceFiles': [],
        'structures': structures, 'sourceLineage': copy.deepcopy(KNOWN_LINEAGE), 'labelsRead': False}
    value['receiptSha256'] = reference_receipt(value)
    return value


def fixture_contracts(rows, references, role='independent-evaluation'):
    labels = {hashlib.sha256(canonicalize_smiles(row['smiles']).encode()).hexdigest(): {'herg': index % 2}
              for index, row in enumerate(rows)}
    metadata = {'schema': 'aidd.independent.candidate.v1', 'datasetId': 'software-fixture', 'datasetVersion': 'fixture-v1',
        'sourceId': 'fixture-independent-source', 'sourceVersion': 'fixture-v1', 'sourceUrl': 'https://fixture.invalid/source',
        'rawSha256': '0' * 64, 'rowsSha256': object_sha256(rows), 'labelsSha256': object_sha256(labels), 'role': role,
        'taskContractVersion': 'fixture.task-contract.v1',
        'taskContracts': {'herg': {'positiveClass': 'fixture positive', 'assayType': 'fixture-assay', 'species': 'fixture',
            'unit': 'fixture-unit', 'threshold': 'fixture threshold', 'definition': 'Software testing only; not real labels.',
            'trainedTaskContractSha256': '1' * 64, 'comparisonApproved': True, 'comparisonEvidenceSha256': '2' * 64}},
        'lineage': {'families': ['fixture-new-family'], 'publicationIds': ['fixture-new-publication'],
                    'cohortIds': ['fixture-new-cohort'], 'assayIds': ['fixture-new-assay']},
        'rights': {'status': 'approved-for-local-research', 'allowedRoles': [role], 'redistributionApproved': False,
            'cloudTransferAllowed': False, 'evidenceSha256': '3' * 64, 'reviewedBy': 'fixture-reviewer', 'reviewedAt': '2020-01-01T00:00:00Z'},
        'independence': {'status': 'verified', 'sourcePopulationIndependent': True, 'evidenceSha256': '4' * 64,
            'reviewedBy': 'fixture-reviewer', 'reviewedAt': '2020-01-01T00:00:00Z'},
        'exposure': {'labelsPreviouslyViewed': False, 'usedForTraining': False, 'usedForModelSelection': False, 'sourcePreviouslyEvaluated': False}}
    metadata = CandidateMetadata.model_validate(metadata).model_dump(by_alias=True)
    protocol = {'schema': 'aidd.independent.protocol.v1', 'protocolId': 'fixture-protocol', 'protocolVersion': 'fixture-v1',
        'role': role, 'status': 'frozen', 'frozenAt': '2020-01-02T00:00:00Z',
        'candidateMetadataSha256': object_sha256(metadata), 'referencesSha256': references['receiptSha256'],
        'taskContractVersion': 'fixture.task-contract.v1', 'approvedTaskContractHashes': {'herg': '1' * 64},
        'modelBindings': [{'modelId': 'fixture-predictor', 'modelVersion': 'fixture-v1', 'weightsSha256': '5' * 64,
            'manifestSha256': '6' * 64, 'taskContractVersion': 'fixture.task-contract.v1', 'taskContractHashes': {'herg': '1' * 64}}],
        'evaluationSettings': frozen_evaluation_settings(), 'evaluatorSourceSha256': evaluator_source_sha256(),
        'labelReadPermission': {'granted': True, 'approvedBy': 'fixture-operator', 'approvedAt': '2020-01-02T00:00:00Z',
                               'purpose': 'evaluate' if role == 'independent-evaluation' else 'calibrate'},
        'useForTraining': False, 'useForModelSelection': False}
    protocol = IndependentProtocol.model_validate(protocol).model_dump(by_alias=True)
    return metadata, protocol, labels


class IndependentDataTests(unittest.TestCase):
    def setUp(self):
        # This test-only authority replaces historical pins in this process.
        # It does not write real data or produce a publishable independent set.
        self.files = patch('backend.dataset_overlap_audit.REFERENCE_FILES', {})
        self.structures = patch('backend.dataset_overlap_audit.REFERENCE_STRUCTURE_HASHES', {})
        self.files.start(); self.structures.start()
        self.addCleanup(self.files.stop); self.addCleanup(self.structures.stop)
        self.rows = [{'smiles': 'C1CC1', 'sourceRowId': 'fixture-row-1'},
                     {'smiles': 'C1CCC1', 'sourceRowId': 'fixture-row-2'}]
        self.refs = fixture_references()
        self.meta, self.protocol, self.labels = fixture_contracts(self.rows, self.refs)

    def audit(self, rows=None, meta=None, protocol=None, refs=None):
        return audit_dataset(rows or self.rows, meta or self.meta, refs or self.refs, protocol or self.protocol)

    def test_audit_is_label_blind_and_public_aggregate_has_no_rows_structures_or_labels(self):
        result = self.audit()
        self.assertTrue(result['qualified'])
        public = json.dumps(result['publicAudit'])
        self.assertNotIn('C1CC1', public)
        self.assertNotIn('fixture-row-1', public)
        self.assertNotIn('labelsSha256', public)
        self.assertFalse(result['publicAudit']['labelsRead'])
        self.assertEqual(result['publicAudit']['labelCountsStatus'], 'not-read-labelblind-audit')
        self.assertIsNone(result['publicAudit']['positiveCount'])
        self.assertIsNone(result['publicAudit']['negativeCount'])
        self.assertIsNone(result['publicAudit']['missingLabelCount'])
        self.assertTrue(all(len(row['groupId']) == 64 for row in result['eligibleRows']))

    def test_strict_rows_reject_labels_per_record_hashes_extra_fields_and_bad_types(self):
        for field, value in [('labels', {'herg': 1}), ('label', 1), ('observedLabelHashes', {'herg': '0' * 64}), ('path', '/private')]:
            rows = copy.deepcopy(self.rows)
            rows[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): self.audit(rows=rows)
        changed = copy.deepcopy(self.meta); changed['rights']['redistributionApproved'] = 1
        with self.assertRaises(ValueError): self.audit(meta=changed)
        changed = copy.deepcopy(self.meta); changed['apiKey'] = 'not-accepted'
        with self.assertRaises(ValueError): self.audit(meta=changed)

    def test_unknown_rights_exposure_draft_or_incomplete_evidence_cannot_qualify(self):
        for mutation in ('rights', 'independence', 'viewed', 'trained', 'selected', 'source', 'draft', 'frozenAt', 'evidence', 'labels'):
            meta, protocol = copy.deepcopy(self.meta), copy.deepcopy(self.protocol)
            if mutation == 'rights': meta['rights']['status'] = 'unresolved'
            elif mutation == 'independence': meta['independence']['status'] = 'unknown'
            elif mutation in ('viewed', 'trained', 'selected', 'source'):
                key = {'viewed': 'labelsPreviouslyViewed', 'trained': 'usedForTraining', 'selected': 'usedForModelSelection', 'source': 'sourcePreviouslyEvaluated'}[mutation]
                meta['exposure'][key] = True
            elif mutation == 'draft': protocol['status'] = 'draft'
            elif mutation == 'frozenAt': protocol['frozenAt'] = None
            elif mutation == 'evidence': meta['rights']['evidenceSha256'] = None
            else: meta['labelsSha256'] = None
            with self.subTest(mutation=mutation):
                result = self.audit(meta=meta, protocol=protocol)
                self.assertFalse(result['qualified'])
                self.assertEqual(result['status'], 'not-independently-qualified')
                with tempfile.TemporaryDirectory() as temp, self.assertRaises(ValueError):
                    seal_dataset(result, Path(temp) / 'seal', labels=self.labels, label_read_permission=True)

    def test_incomplete_or_coherently_changed_references_do_not_qualify(self):
        refs = copy.deepcopy(self.refs)
        refs['missing'] = [{'role': 'required', 'reason': 'missing-reference'}]
        refs['completeReferences'] = False
        refs['receiptSha256'] = reference_receipt(refs)
        protocol = copy.deepcopy(self.protocol); protocol['referencesSha256'] = refs['receiptSha256']
        self.assertIn('incomplete-historical-references', self.audit(refs=refs, protocol=protocol)['publicAudit']['blockingReasons'])
        refs = copy.deepcopy(self.refs); refs['sourceLineage'] = []
        refs['receiptSha256'] = reference_receipt(refs)
        with self.assertRaises(ValueError): self.audit(refs=refs)
        refs = copy.deepcopy(self.refs); refs['completeReferences'] = False
        with self.assertRaises(ValueError): self.audit(refs=refs)

    def test_all_four_lineage_types_and_same_source_id_are_excluded(self):
        previous = KNOWN_LINEAGE[0]
        for field in ('sourceId', 'families', 'publicationIds', 'cohortIds', 'assayIds'):
            meta = copy.deepcopy(self.meta)
            if field == 'sourceId': meta[field] = previous[field]
            else: meta['lineage'][field] = previous[field]
            with self.subTest(field=field):
                result = self.audit(meta=meta)
                self.assertIn('historical-source-lineage-overlap', result['publicAudit']['blockingReasons'])
        refs = {'sourceLineage': [{'sourceId': 'other', 'publicationIds': ['10.1000/test'], 'families': [], 'cohortIds': [], 'assayIds': []}]}
        meta = copy.deepcopy(self.meta); meta['lineage']['publicationIds'] = ['https://doi.org/10.1000/TEST']
        self.assertTrue(lineage_overlap(meta, refs))

    def test_structural_stereo_salt_isotope_tautomer_and_scaffold_overlap(self):
        variants = ['C1CC1', 'C1CC1.[Cl-]', '[13CH2]1CC1', 'CC1CC1']
        references = fixture_references({'old-test': ['C1CC1']})
        pins = {'old-test': object_sha256(['C1CC1'])}
        with patch('backend.dataset_overlap_audit.REFERENCE_STRUCTURE_HASHES', pins):
            for smiles in variants:
                rows = [{'smiles': smiles, 'sourceRowId': 'fixture-row'}]
                meta, protocol, _ = fixture_contracts(rows, references)
                with self.subTest(smiles=smiles):
                    result = self.audit(rows=rows, meta=meta, protocol=protocol, refs=references)
                    self.assertFalse(result['qualified'])
                    self.assertEqual(result['publicAudit']['eligibleStructureCount'], 0)
                    self.assertFalse(result['publicAudit']['labelsRead'])

    def test_duplicate_equivalent_rows_all_quarantined_and_invalid_structure_not_imputed(self):
        rows = [{'smiles': 'C1CC1', 'sourceRowId': 'one'}, {'smiles': 'C1(CC1)', 'sourceRowId': 'two'},
                {'smiles': 'not-a-smiles', 'sourceRowId': 'three'}]
        meta = copy.deepcopy(self.meta); meta['rowsSha256'] = object_sha256(rows)
        protocol = copy.deepcopy(self.protocol); protocol['candidateMetadataSha256'] = object_sha256(meta)
        result = self.audit(rows=rows, meta=meta, protocol=protocol)
        self.assertFalse(result['qualified'])
        self.assertEqual(result['publicAudit']['quarantineReasons']['duplicate-or-conflicting-canonical-structure'], 2)
        self.assertEqual(result['publicAudit']['quarantineReasons']['invalid-or-unsupported-structure'], 1)

    def test_candidate_model_task_and_metadata_hashes_are_frozen_not_task_name_guesses(self):
        for mutation in ('rows', 'metadata', 'mapping', 'model'):
            meta, protocol, rows = copy.deepcopy(self.meta), copy.deepcopy(self.protocol), copy.deepcopy(self.rows)
            if mutation == 'rows': rows[0]['smiles'] = 'C1CCCC1'
            elif mutation == 'metadata': meta['datasetVersion'] = 'fixture-v2'
            elif mutation == 'mapping': meta['taskContracts']['herg']['comparisonApproved'] = False
            else: protocol['modelBindings'][0]['taskContractHashes']['herg'] = '9' * 64
            with self.subTest(mutation=mutation): self.assertFalse(self.audit(rows, meta, protocol)['qualified'])

    def test_missing_evaluation_settings_or_evaluator_hash_cannot_qualify(self):
        for field in ('evaluationSettings', 'evaluatorSourceSha256'):
            protocol = copy.deepcopy(self.protocol)
            protocol.pop(field)
            with self.subTest(field=field):
                result = self.audit(protocol=protocol)
                self.assertFalse(result['qualified'])
                self.assertEqual(result['status'], 'not-independently-qualified')
                with tempfile.TemporaryDirectory() as temp, self.assertRaises(ValueError):
                    seal_dataset(result, Path(temp)/'seal', labels=self.labels, label_read_permission=True)
        protocol = copy.deepcopy(self.protocol); protocol['evaluatorSourceSha256'] = '0'*64
        self.assertIn('evaluator-source-sha256-mismatch', self.audit(protocol=protocol)['publicAudit']['blockingReasons'])

    def test_analysis_settings_are_strict_fixed_and_not_runtime_defaults(self):
        changed_values = {'version': 'v999', 'metrics': ['auroc'], 'bootstrapReplicates': 20,
            'bootstrapUnit': 'molecule', 'minimumBootstrapPerClass': 2, 'minimumBootstrapGroups': 2, 'seed': 43}
        for field, value in changed_values.items():
            protocol = copy.deepcopy(self.protocol); protocol['evaluationSettings'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): self.audit(protocol=protocol)
        protocol = copy.deepcopy(self.protocol); protocol['evaluationSettings']['minimumBootstrapGroups'] = 10.0
        with self.assertRaises(ValueError): self.audit(protocol=protocol)
        protocol = copy.deepcopy(self.protocol); protocol['evaluationSettings']['extra'] = 'ignored?'
        with self.assertRaises(ValueError): self.audit(protocol=protocol)

    def test_source_reviews_must_precede_frozen_protocol(self):
        meta = copy.deepcopy(self.meta); meta['rights']['reviewedAt'] = '2020-01-03T00:00:00Z'
        protocol = copy.deepcopy(self.protocol); protocol['candidateMetadataSha256'] = object_sha256(meta)
        result = self.audit(meta=meta, protocol=protocol)
        self.assertFalse(result['qualified'])
        self.assertIn('source-review-not-complete-before-protocol-freeze', result['publicAudit']['blockingReasons'])

    def test_seal_roundtrip_no_label_file_access_by_default_and_explicit_permission_required(self):
        result = self.audit()
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / 'sealed'
            sealed = seal_dataset(result, destination, labels=self.labels, label_read_permission=True)
            loaded = load_sealed_dataset(destination, sealed['manifestSha256'])
            self.assertIsNone(loaded['labels'])
            self.assertTrue(all('labels' not in row and 'observedLabelHashes' not in row for row in loaded['rows']))
            label_file = destination / 'labels.private.json'
            label_file.rename(destination / 'temporarily-hidden-labels')
            self.assertIsNone(load_sealed_dataset(destination, sealed['manifestSha256'])['labels'])
            with self.assertRaises(ValueError): load_sealed_dataset(destination, sealed['manifestSha256'], allow_labels=True)
            (destination / 'temporarily-hidden-labels').rename(label_file)
            observed = load_sealed_dataset(destination, sealed['manifestSha256'], allow_labels=True)
            self.assertEqual(observed['labels'], self.labels)
            self.assertEqual(observed['rows'][0]['labels'], self.labels[observed['rows'][0]['structureId']])
            with self.assertRaises(ValueError): seal_dataset(result, destination, labels=self.labels, label_read_permission=True)

    def test_seal_refuses_mutated_audit_labels_wrong_digest_and_missing_permission(self):
        for mutation in ('eligible', 'audit', 'label', 'permission'):
            result, labels = self.audit(), copy.deepcopy(self.labels)
            if mutation == 'eligible': result['eligibleRows'][0]['canonicalSmiles'] = 'CCO'
            elif mutation == 'audit': result['publicAudit']['eligibleStructureCount'] = 999
            elif mutation == 'label': labels[next(iter(labels))]['herg'] ^= 1
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp, self.assertRaises(ValueError):
                seal_dataset(result, Path(temp) / 'seal', labels=labels, label_read_permission=mutation != 'permission')

    def test_sealed_hash_failure_role_confusion_and_postseal_label_tampering_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / 'sealed'
            sealed = seal_dataset(self.audit(), destination, labels=self.labels, label_read_permission=True)
            with self.assertRaises(ValueError): load_sealed_dataset(destination, '0' * 64)
            for purpose in ('train', 'model-selection', 'calibrate'):
                with self.assertRaises(ValueError): load_sealed_dataset(destination, sealed['manifestSha256'], purpose=purpose, allow_labels=True)
            label_file = destination / 'labels.private.json'; label_file.write_text('{}', encoding='utf-8')
            with self.assertRaises(ValueError): load_sealed_dataset(destination, sealed['manifestSha256'], allow_labels=True)
        meta, protocol, labels = fixture_contracts(self.rows, self.refs, role='calibration')
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / 'calibration'
            sealed = seal_dataset(self.audit(meta=meta, protocol=protocol), destination, labels=labels, label_read_permission=True)
            with self.assertRaises(ValueError): load_sealed_dataset(destination, sealed['manifestSha256'], purpose='evaluate', allow_labels=True)
            self.assertEqual(load_sealed_dataset(destination, sealed['manifestSha256'], purpose='calibrate', allow_labels=True)['labels'], labels)

    def test_no_labels_opened_before_protocol_permission_even_if_caller_sets_true(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / 'sealed'
            seal_dataset(self.audit(), destination, labels=self.labels, label_read_permission=True)
            seal_file = destination / 'seal.json'
            manifest = json.loads(seal_file.read_text(encoding='utf-8'))
            manifest['labelReadPermission']['granted'] = False
            seal_file.write_text(json.dumps(manifest), encoding='utf-8')
            opened = []
            original_open = Path.open
            def check_open(path, *args, **kwargs):
                if path.name == 'labels.private.json': opened.append(path)
                return original_open(path, *args, **kwargs)
            with patch.object(Path, 'open', check_open), self.assertRaises(ValueError):
                load_sealed_dataset(destination, file_sha256(seal_file), allow_labels=True)
            self.assertEqual(opened, [])

    def test_actual_protocol_permission_and_source_checked_before_any_label_hash(self):
        for mutation in ('protocol-permission', 'evaluator-hash', 'rights-review'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                destination = Path(temp)/'sealed'
                seal_dataset(self.audit(), destination, labels=self.labels, label_read_permission=True)
                seal_file = destination/'seal.json'; manifest = json.loads(seal_file.read_text(encoding='utf-8'))
                if mutation == 'protocol-permission':
                    path = destination/'protocol.json'; payload = json.loads(path.read_text(encoding='utf-8'))
                    payload['labelReadPermission']['granted'] = False
                    path.write_text(json.dumps(payload), encoding='utf-8')
                    manifest['files']['protocol']['sha256'] = file_sha256(path)
                elif mutation == 'evaluator-hash': manifest['evaluatorSourceSha256'] = '0'*64
                else:
                    path = destination/'metadata.json'; payload = json.loads(path.read_text(encoding='utf-8'))
                    payload['rights']['status'] = 'denied'
                    path.write_text(json.dumps(payload), encoding='utf-8')
                    manifest['files']['metadata']['sha256'] = file_sha256(path)
                seal_file.write_text(json.dumps(manifest), encoding='utf-8')
                opened = []; original_open = Path.open
                def guarded(path, *args, **kwargs):
                    if path.name == 'labels.private.json': opened.append(path)
                    return original_open(path, *args, **kwargs)
                with patch.object(Path, 'open', guarded), self.assertRaises(ValueError):
                    load_sealed_dataset(destination, file_sha256(seal_file), allow_labels=True)
                self.assertEqual(opened, [])

    def test_changed_evaluator_source_refuses_labels_and_structural_load(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp)/'sealed'
            sealed = seal_dataset(self.audit(), destination, labels=self.labels, label_read_permission=True)
            for labels_allowed in (False, True):
                with patch('backend.independent_validation_data.evaluator_source_sha256', return_value='0'*64):
                    with self.assertRaises(ValueError):
                        load_sealed_dataset(destination, sealed['manifestSha256'], allow_labels=labels_allowed)

    def test_role_substitution_cannot_hide_labels_in_nonlabel_file_access(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp)/'sealed'
            seal_dataset(self.audit(), destination, labels=self.labels, label_read_permission=True)
            seal_file = destination/'seal.json'; manifest = json.loads(seal_file.read_text(encoding='utf-8'))
            manifest['files']['metadata'] = copy.deepcopy(manifest['files']['labels'])
            seal_file.write_text(json.dumps(manifest), encoding='utf-8')
            opened = []; original_open = Path.open
            def guarded(path, *args, **kwargs):
                if path.name == 'labels.private.json': opened.append(path)
                return original_open(path, *args, **kwargs)
            with patch.object(Path, 'open', guarded), self.assertRaises(ValueError):
                load_sealed_dataset(destination, file_sha256(seal_file), allow_labels=False)
            self.assertEqual(opened, [])

    def test_duplicate_seal_json_keys_nonfinite_and_ambiguous_permission_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp)/'sealed'
            sealed = seal_dataset(self.audit(), destination, labels=self.labels, label_read_permission=True)
            with self.assertRaises(ValueError): load_sealed_dataset(destination, sealed['manifestSha256'], allow_labels=1)
            path = destination/'seal.json'; text = path.read_text(encoding='utf-8')
            path.write_text(text.replace('{', '{"qualified":false,', 1), encoding='utf-8')
            with self.assertRaises(ValueError): load_sealed_dataset(destination, file_sha256(path))
            self.assertIn('"structureCount":2', text)
            path.write_text(text.replace('"structureCount":2', '"structureCount":NaN'), encoding='utf-8')
            with self.assertRaises(ValueError): load_sealed_dataset(destination, file_sha256(path))


class EvaluatorSourceTests(unittest.TestCase):
    def test_exact_source_file_set_hashes_bytes_and_requires_all_dependencies(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for relative in EVALUATOR_SOURCE_FILES:
                path = root/relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('fixture-source-only', encoding='utf-8')
            first = evaluator_source_sha256(root)
            self.assertEqual(first, evaluator_source_sha256(root))
            changed = root/EVALUATOR_SOURCE_FILES[0]; changed.write_text('changed-fixture-source-only', encoding='utf-8')
            self.assertNotEqual(first, evaluator_source_sha256(root))
            changed.unlink()
            with self.assertRaises(ValueError): evaluator_source_sha256(root)


class CandidateInventoryTests(unittest.TestCase):
    def test_missing_sources_never_create_real_data_or_class_counts(self):
        from scripts.audit_independent_dataset import inventory
        with tempfile.TemporaryDirectory() as temp:
            result = inventory(Path(temp))
            self.assertEqual(result['status'], 'not-independently-qualified')
            self.assertFalse(result['historicalReferences']['completeReferences'])
            self.assertIsNone(result['positiveCount'])
            self.assertFalse(result['newBlindTestCreated'])
            self.assertFalse(result['labelsRead'])
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_fda_metadata_without_verified_source_cannot_claim_file_validation(self):
        from scripts.audit_independent_dataset import inventory
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); provenance = root/'data/candidates/fda-provenance.json'
            provenance.parent.mkdir(parents=True)
            provenance.write_text(json.dumps({'datasets': [{'datasetName':'DILIst','sha256':'0'*64,
                'rawDrugRows':12,'sourceUrl':'https://fixture.invalid/source'}]}), encoding='utf-8')
            candidate = inventory(root)['candidates'][-1]
            self.assertFalse(candidate['sourceFileVerified'])
            self.assertIn('missing-or-changed-pinned-fda-source', candidate['blockingReasons'])
            self.assertEqual(candidate['status'], 'not-independently-qualified')
            self.assertIsNone(candidate['positiveCount'])


class HistoricalProjectionTests(unittest.TestCase):
    def test_structure_projection_does_not_inspect_labels_and_missing_hashes_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            document = {'splits': {split: [{'smiles': 'C1CC1', 'label': {'DO-NOT-READ': 'not a binary label'}}]
                                    for split in ('train', 'validation', 'test')}}
            path = root / 'split.json'; path.write_text(json.dumps(document), encoding='utf-8')
            files = {'fixture-split': ('split.json', file_sha256(path), 'v2')}
            pins = {f'v2-{split}': object_sha256(['C1CC1']) for split in ('train', 'validation', 'test')}
            with patch('backend.dataset_overlap_audit.REFERENCE_FILES', files), patch('backend.dataset_overlap_audit.REFERENCE_STRUCTURE_HASHES', pins):
                refs = build_historical_references(root)
                validate_references(refs)
                self.assertTrue(refs['completeReferences'])
                self.assertNotIn('DO-NOT-READ', json.dumps(refs))
                self.assertNotIn('C1CC1', json.dumps(refs['receipt']))
                self.assertNotIn('split.json', json.dumps(refs['receipt']))
                path.write_text('{}', encoding='utf-8')
                refs = build_historical_references(root)
                self.assertFalse(refs['completeReferences'])
                self.assertEqual(refs['missing'][0]['reason'], 'reference-sha256-mismatch')


if __name__ == '__main__':
    unittest.main()
