"""Synthetic software fixtures only: no real experimental rows, labels or network."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from rdkit import rdBase

from backend import epa_structure_audit as audit
from backend.chemistry import CHEMISTRY_VERSION
from backend.dataset_overlap_audit import KNOWN_LINEAGE
from backend.v3_data import IDENTITY_VERSION, build_forbidden_index
from scripts import prepare_epa_structure_audit as cli


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def member(number=1, aeid=686, **changes):
    value = {'aeid': aeid, 'sourceSubstanceId': 'synthetic-' + str(number),
             'dtxsid': 'DTXSID' + str(number), 'batchId': 'synthetic-batch'}
    return audit.Member.model_validate(value | changes)


def structure(number=1, smiles='c1ccccc1', **changes):
    value = {'dtxsid': 'DTXSID' + str(number), 'dtxcid': 'DTXCID' + str(number),
             'smiles': smiles, 'substanceType': 'single-substance', 'mappingRecordId': 'synthetic-' + str(number)}
    return audit.Structure.model_validate(value | changes)


def manifest_dict():
    entry = {'fileId': 'synthetic.json', 'sha256': '0' * 64,
             'sourceUrl': 'https://comptox.epa.gov/ctx-api/docs/chemical.json',
             'retrievedAtUtc': '2026-10-16T00:00:00+00:00', 'sourceRelease': 'invitrodb4.3'}
    return {'schemaVersion': audit.SCHEMA, 'selectionPolicy': 'endpoint-membership-without-results',
            'chemistryVersion': CHEMISTRY_VERSION, 'identityVersion': IDENTITY_VERSION,
            'rdkitVersion': rdBase.rdkitVersion, 'membership': dict(entry), 'structures': dict(entry)}


class EPAStructureAuditTests(unittest.TestCase):
    def run_audit(self, members, structures, historical=None):
        index = build_forbidden_index(historical or {})
        references = {'completeReferences': True, 'receiptSha256': '0' * 64, 'sourceLineage': KNOWN_LINEAGE}
        with patch.object(audit, 'historical_forbidden_index', return_value=index):
            return audit.audit_projections(audit.ProjectionManifest.model_validate(manifest_dict()),
                                           members, structures, references)

    def test_mapped_substances_structures_and_endpoint_memberships_are_distinct(self):
        result = self.run_audit([member(), member(aeid=3184), member(2)],
                                [structure(), structure(2, smiles='C1CCCCC1')])
        self.assertEqual(result['membershipRows'], 3)
        self.assertEqual(result['uniqueDtxsids'], 2)
        self.assertEqual(result['uniqueCanonicalStructures'], 2)
        self.assertEqual(result['mappedMembershipRows'], 3)
        self.assertEqual(result['structurallyPassedStructures'], 2)
        self.assertFalse(result['qualificationGranted'])
        self.assertFalse(result['canEnterStage7C'])
        self.assertEqual(result['independentlyQualifiedStructures'], 0)

    def test_result_and_unknown_fields_rejected_at_each_input_level(self):
        for cls, value in [(audit.Member, member().model_dump()),
                           (audit.Structure, structure().model_dump()),
                           (audit.ProjectionManifest, manifest_dict())]:
            for key in ('hitcall', 'IC50', 'label', 'standard_value', 'prediction', 'qualityFlag'):
                with self.subTest(cls=cls.__name__, field=key), self.assertRaises(ValidationError):
                    cls.model_validate(value | {key: 0.5})

    def test_only_audited_endpoint_ids_allowed(self):
        with self.assertRaises(ValidationError):
            member(aeid=999)

    def test_missing_mixture_unknown_and_invalid_mapping_counts_separate(self):
        members = [member(i) for i in range(1, 7)]
        members[0] = member(1, dtxsid=None)
        result = self.run_audit(members, [structure(3, substanceType='mixture'),
            structure(4, substanceType='unknown'), structure(5, smiles='invalid'), structure(6, dtxcid=None)])
        self.assertEqual(result['mappingExclusions'], {'missing-dtxsid': 1, 'missing-structure-mapping': 1,
            'mixture': 1, 'unknown-substance-type': 1, 'invalid-or-missing-structure': 2})
        self.assertEqual(result['uniqueCanonicalStructures'], 0)

    def test_one_to_many_substance_mapping_quarantined(self):
        rows = [structure(), structure(dtxcid='DTXCID2', smiles='C1CCCCC1')]
        result = self.run_audit([member()], rows)
        self.assertEqual(result['mappingExclusions'], {'ambiguous-dtxsid-or-dtxcid-mapping': 1})

    def test_conflicting_dtxcid_and_raw_substance_mapping_quarantined(self):
        result = self.run_audit([member(), member(2)], [structure(), structure(2, smiles='C1CCCCC1', dtxcid='DTXCID1')])
        self.assertEqual(result['mappingExclusions'], {'ambiguous-dtxsid-or-dtxcid-mapping': 2})
        result = self.run_audit([member(), member(2, sourceSubstanceId='synthetic-1')], [structure(), structure(2)])
        self.assertEqual(result['mappingExclusions'], {'ambiguous-source-substance-id': 2})

    def test_parent_isotope_stereo_and_scaffold_overlap_reuses_frozen_policy(self):
        rows = [structure(1, 'C[NH+](C)Cc1ccccc1.[Cl-]'), structure(2, '[13CH3]c1ccccc1'),
                structure(3, 'C[C@H](O)c1ccccc1'), structure(4, 'Oc1ccccc1')]
        result = self.run_audit([member(i) for i in range(1, 5)], rows,
                                {'synthetic-history': ['CN(C)Cc1ccccc1', 'Cc1ccccc1', 'C[C@@H](O)c1ccccc1']})
        self.assertEqual(result['historicalOverlapStructures'], 4)
        self.assertEqual(result['structurallyPassedStructures'], 0)
        self.assertIn('parent', result['historicalOverlapReasonsNonexclusive'])
        self.assertIn('stereoIsotopeEquivalent', result['historicalOverlapReasonsNonexclusive'])
        self.assertIn('scaffold', result['historicalOverlapReasonsNonexclusive'])

    def test_salt_mapping_preserves_full_input_and_counts_multifragment(self):
        result = self.run_audit([member()], [structure(smiles='C[NH+](C)Cc1ccccc1.[Cl-]', substanceType='salt')])
        self.assertEqual(result['multifragmentMappedMembershipRows'], 1)
        self.assertEqual(result['mappedMembershipRows'], 1)

    def test_tox21_family_exposure_not_renamed_independent(self):
        result = self.run_audit([member(aeid=3210, batchId=None)], [structure()])
        self.assertEqual(result['missingBatchMembershipRows'], 1)
        self.assertEqual(result['knownSourceFamilyMatches'][0]['historicalSourceId'], 'tox21')
        self.assertEqual(result['sourceIndependenceStatus'], 'MANUAL_REVIEW_REQUIRED')

    def test_candidate_order_does_not_change_aggregate_or_identity_set(self):
        members = [member(), member(2), member(3), member()]
        structures = [structure(), structure(2, smiles='Cc1ccccc1'), structure(3, smiles='C1CCCCC1')]
        a = self.run_audit(members, structures)
        b = self.run_audit(members[::-1], structures[::-1])
        self.assertEqual(a, b)
        self.assertEqual(a['duplicateMembershipRows'], 1)
        self.assertEqual(a['internalNearEquivalentOrScaffoldDuplicates'], 2)
        self.assertEqual(a['structurallyPassedStructures'], 1)

    def test_connected_history_contamination_cannot_escape_through_identity_bridge(self):
        # Deliberate synthetic identities isolate transitive component behavior.
        members = [member(i) for i in (1, 2, 3)]
        structures = [structure(1), structure(2, smiles='C1CCCCC1'), structure(3, smiles='c1ccncc1')]
        def synthetic_identity(canonical, _):
            tokens = {'c1ccccc1': ['bridge-a'], 'C1CCCCC1': ['bridge-a', 'bridge-b'], 'c1ccncc1': ['bridge-b']}
            identity = {name: canonical for name in ('canonical', 'stereoIsotopeEquivalent', 'parent', 'tautomerParent')}
            identity['scaffolds'] = tokens[canonical]
            return identity, ['synthetic-history:canonical'] if canonical == 'c1ccccc1' else []
        with patch.object(audit, 'structural_overlap', side_effect=synthetic_identity):
            a = self.run_audit(members, structures)
            b = self.run_audit(members[::-1], structures[::-1])
        self.assertEqual(a, b)
        self.assertEqual(a['directHistoricalOverlapStructures'], 1)
        self.assertEqual(a['connectedHistoricalOverlapStructures'], 2)
        self.assertEqual(a['historicalOverlapStructures'], 3)
        self.assertEqual(a['structurallyPassedStructures'], 0)

    def test_duplicate_json_keys_and_nonfinite_constants_are_rejected(self):
        for raw in (b'{"smiles":"C","smiles":"N"}', b'{"unexpected":NaN}', b'{"unexpected":Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                audit.strict_json(raw)

    def test_unavailable_inputs_keep_unknown_counts_and_no_candidate_rows(self):
        result = audit.unavailable_audit([])
        for field in ('membershipRows', 'uniqueDtxsids', 'uniqueCanonicalStructures',
                      'mappedMembershipRows', 'structurallyPassedStructures', 'mappingExclusions'):
            self.assertIsNone(result[field])
        self.assertFalse(result['candidateLabelsRead'])
        self.assertFalse(result['modelsExecuted'])

    def test_credential_urls_private_urls_and_non_utc_times_rejected(self):
        base = manifest_dict()['membership']
        self.assertEqual(audit.Projection.official_https_only('https://www.epa.gov/comptox-tools/'),
                         'https://www.epa.gov/comptox-tools/')
        for url in ('http://comptox.epa.gov/a', 'https://evil.example/a',
                    'https://secret@comptox.epa.gov/a', 'https://comptox.epa.gov/a?api_key=secret'):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                audit.Projection.model_validate(base | {'sourceUrl': url})
        with self.assertRaises(ValidationError):
            audit.Projection.model_validate(base | {'retrievedAtUtc': '2026-10-16T08:00:00'})

    def test_manifest_and_projection_hashes_and_version_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = manifest_dict()
            for role, rows in [('membership', [member().model_dump()]), ('structures', [structure().model_dump()])]:
                raw = encoded(rows)
                (root / (role + '.json')).write_bytes(raw)
                manifest[role].update(fileId=role + '.json', sha256=hashlib.sha256(raw).hexdigest())
            raw = encoded(manifest)
            path = root / 'manifest.json'
            path.write_bytes(raw)
            expected = hashlib.sha256(raw).hexdigest()
            self.assertEqual(len(audit.load_projections(path, expected)[1]), 1)
            with self.assertRaises(ValueError):
                audit.load_projections(path, 'f' * 64)
            (root / 'structures.json').write_bytes(b'[]')
            with self.assertRaises(ValueError):
                audit.load_projections(path, expected)
            manifest['rdkitVersion'] = 'unknown'
            path.write_bytes(encoded(manifest))
            with self.assertRaises(ValueError):
                audit.load_projections(path, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_missing_custodian_file_errors_instead_of_constructing_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'manifest.json'
            raw = encoded(manifest_dict())
            path.write_bytes(raw)
            with self.assertRaises(ValueError):
                audit.load_projections(path, hashlib.sha256(raw).hexdigest())

    def test_cli_missing_input_and_existing_output_never_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'aggregate.json'
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(['--manifest', str(Path(directory) / 'missing.json'),
                    '--expected-manifest-sha256', '0' * 64, '--output', str(output)]), 2)
            self.assertFalse(output.exists())
            output.write_text('historical')
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.main(['--network-receipt', str(Path(directory) / 'missing.json'), '--output', str(output)])
            self.assertEqual(output.read_text(), 'historical')

    def test_network_receipt_rechecks_bytes_without_exposing_private_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = b'{"documentation":"synthetic"}'
            (root / 'metadata.json').write_bytes(raw)
            row = {'fileId': 'metadata.json', 'sourceUrl': 'https://comptox.epa.gov/ctx-api/docs/chemical.json',
                'retrievedAtUtc': '2026-10-16T00:00:00+00:00', 'httpStatus': 200,
                'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
            receipt = {'schema': 'aidd.stage7b.epa-network-evidence.v1',
                'candidateLabelsRead': False, 'candidateResultFieldsRequested': False, 'files': [row]}
            path = root / 'receipt.json'
            path.write_bytes(encoded(receipt))
            output = root / 'aggregate.json'
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['--network-receipt', str(path), '--output', str(output)]), 0)
            self.assertNotIn(str(root), output.read_text())
            (root / 'metadata.json').write_bytes(b'changed')
            with self.assertRaises(ValueError):
                cli.verified_network_evidence(path)


if __name__ == '__main__':
    unittest.main()
