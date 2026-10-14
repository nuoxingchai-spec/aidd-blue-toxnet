"""CPU registration/seal fixtures only; no real labels or model execution."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.dataset_overlap_audit import object_sha256
from backend.independent_validation_data import audit_dataset, seal_dataset, load_sealed_dataset
from backend.validation_service import Registration, file_sha, load_json
from scripts.register_independent_dataset import register, validate_registration_binding
from test.test_independent_validation_data import fixture_contracts, fixture_references


class ValidationRegistrationTests(unittest.TestCase):
    def setUp(self):
        # Synthetic reference authority is scoped to this test process only.
        self.files = patch('backend.dataset_overlap_audit.REFERENCE_FILES', {})
        self.structures = patch('backend.dataset_overlap_audit.REFERENCE_STRUCTURE_HASHES', {})
        self.files.start(); self.structures.start()
        self.addCleanup(self.files.stop); self.addCleanup(self.structures.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.rows = [{'smiles': 'C1CC1', 'sourceRowId': 'fixture-one'}, {'smiles': 'C1CCC1', 'sourceRowId': 'fixture-two'}]
        self.refs = fixture_references()
        self.meta, self.protocol, self.labels = fixture_contracts(self.rows, self.refs)
        self.write_sources()

    def write_sources(self, meta=None, protocol=None, rows=None):
        self.meta = copy.deepcopy(meta if meta is not None else self.meta)
        self.protocol = copy.deepcopy(protocol if protocol is not None else self.protocol)
        self.rows = copy.deepcopy(rows if rows is not None else self.rows)
        for name, value in [('metadata.json', self.meta), ('protocol.json', self.protocol)]:
            (self.root / name).write_text(json.dumps(value), encoding='utf-8')
        (self.root / 'rows.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in self.rows), encoding='utf-8')

    def seal(self):
        result = audit_dataset(self.rows, self.meta, self.refs, self.protocol)
        self.assertTrue(result['qualified'], 'This fixture exercises code only, not scientific qualification')
        self.sealed = seal_dataset(result, self.root / 'sealed', labels=self.labels, label_read_permission=True)
        return self.sealed

    def arguments(self, sealed=False):
        value = {'identifier': self.meta['datasetId'], 'name': 'Explicit software fixture', 'version': self.meta['datasetVersion'],
                 'metadata': 'metadata.json', 'rows': 'rows.jsonl', 'protocol': 'protocol.json'}
        if sealed:
            value.update(seal='sealed/seal.json', seal_sha=self.sealed['manifestSha256'])
        return value

    def entry(self, sealed=False):
        return Registration(id=self.meta['datasetId'], name='Explicit software fixture', version=self.meta['datasetVersion'],
            metadataFile='metadata.json', rowsFile='rows.jsonl', protocolFile='protocol.json',
            metadataSha256=file_sha(self.root/'metadata.json'), rowsSha256=file_sha(self.root/'rows.jsonl'),
            protocolSha256=file_sha(self.root/'protocol.json'), sealFile='sealed/seal.json' if sealed else None,
            sealSha256=self.sealed['manifestSha256'] if sealed else None)

    def test_strict_unsealed_registration_is_immutable_and_does_not_grant_independence(self):
        value = register(self.root, **self.arguments())
        self.assertEqual(value['qualification'], 'pending-current-audit')
        self.assertFalse(value['sealed'])
        previous = (self.root/'registry.json').read_bytes()
        with self.assertRaises(ValueError): register(self.root, **self.arguments())
        self.assertEqual((self.root/'registry.json').read_bytes(), previous)
        self.assertFalse((self.root/'.registry.lock').exists())

    def test_real_cpu_fixture_seal_registration_never_reads_the_private_label_file(self):
        self.seal()
        # Label-blind binding must succeed even if private labels are absent.
        # Evaluation with authorized labels will independently reject this.
        (self.root/'sealed/labels.private.json').unlink()
        value = register(self.root, **self.arguments(sealed=True))
        self.assertTrue(value['sealed'])
        entry = Registration.model_validate(load_json(self.root/'registry.json')['datasets'][0])
        bundle = validate_registration_binding(self.root, entry)
        self.assertIsNone(bundle['labels'])
        self.assertTrue(all('labels' not in row for row in bundle['rows']))

    def test_cross_dataset_or_version_seal_cannot_be_registered(self):
        self.seal()
        original_meta, original_protocol = copy.deepcopy(self.meta), copy.deepcopy(self.protocol)
        for field, value in [('datasetId', 'software-other'), ('datasetVersion', 'fixture-other')]:
            meta, protocol = copy.deepcopy(original_meta), copy.deepcopy(original_protocol)
            meta[field] = value
            protocol['candidateMetadataSha256'] = object_sha256(meta)
            self.write_sources(meta, protocol)
            with self.subTest(field=field), self.assertRaises(ValueError):
                register(self.root, **self.arguments(sealed=True))
            self.assertFalse((self.root/'registry.json').exists())

    def test_same_protocol_id_with_different_model_binding_is_rejected(self):
        self.seal()
        protocol = copy.deepcopy(self.protocol)
        protocol['modelBindings'][0]['weightsSha256'] = 'a'*64
        self.write_sources(protocol=protocol)
        with self.assertRaises(ValueError): validate_registration_binding(self.root, self.entry(sealed=True))
        self.assertFalse((self.root/'registry.json').exists())

    def test_same_dataset_id_with_changed_source_members_cannot_reuse_seal(self):
        self.seal()
        rows = copy.deepcopy(self.rows); rows[0]['smiles'] = 'C1CCCC1'
        meta = copy.deepcopy(self.meta); meta['rowsSha256'] = object_sha256(rows)
        protocol = copy.deepcopy(self.protocol); protocol['candidateMetadataSha256'] = object_sha256(meta)
        self.write_sources(meta, protocol, rows)
        with self.assertRaises(ValueError): register(self.root, **self.arguments(sealed=True))

    def test_rows_hash_metadata_binding_and_duplicate_json_fields_are_checked(self):
        (self.root/'rows.jsonl').write_text('{"smiles":"C1CC1","smiles":"C1CCC1","sourceRowId":"fixture-one"}\n', encoding='utf-8')
        with self.assertRaises(ValueError): validate_registration_binding(self.root, self.entry())
        self.write_sources()
        (self.root/'rows.jsonl').write_text(json.dumps({'smiles':'C1CCCC1','sourceRowId':'fixture-one'})+'\n', encoding='utf-8')
        with self.assertRaises(ValueError): validate_registration_binding(self.root, self.entry())
        self.write_sources()
        protocol = copy.deepcopy(self.protocol); protocol['candidateMetadataSha256'] = 'a'*64
        self.write_sources(protocol=protocol)
        with self.assertRaises(ValueError): validate_registration_binding(self.root, self.entry())

    def test_raw_labels_in_registered_audit_rows_are_rejected(self):
        rows = copy.deepcopy(self.rows); rows[0]['labels'] = {'herg': 1}
        meta = copy.deepcopy(self.meta); meta['rowsSha256'] = object_sha256(rows)
        protocol = copy.deepcopy(self.protocol); protocol['candidateMetadataSha256'] = object_sha256(meta)
        self.write_sources(meta, protocol, rows)
        with self.assertRaises(ValueError): register(self.root, **self.arguments())

    def test_unsealed_entry_rejects_caller_supplied_bundle_or_unpaired_seal_hash(self):
        self.seal()
        bundle = load_sealed_dataset(self.root/'sealed/seal.json', self.sealed['manifestSha256'], allow_labels=False)
        with self.assertRaises(ValueError): validate_registration_binding(self.root, self.entry(), seal_bundle=bundle)
        entry = self.entry().model_copy(update={'sealSha256': self.sealed['manifestSha256']})
        with self.assertRaises(ValueError): validate_registration_binding(self.root, entry)
        entry = self.entry(sealed=True)
        bundle['manifestSha256'] = 'a'*64
        with self.assertRaises(ValueError): validate_registration_binding(self.root, entry, seal_bundle=bundle)


if __name__ == '__main__': unittest.main()
