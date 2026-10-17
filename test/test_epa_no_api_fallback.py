"""Synthetic software fixtures only; no EPA candidate data, model or network."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from backend import epa_no_api_fallback as audit
from scripts import audit_epa_no_api_fallback as cli


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode('utf-8')


def evidence(**changes):
    return {'fileId': 'synthetic-page.html',
        'sourceUrl': 'https://www.epa.gov/comptox-tools/exploring-toxcast-data',
        'retrievedAtUtc': '2026-10-17T00:00:00+00:00', 'httpStatus': 200,
        'bytes': 0, 'sha256': hashlib.sha256(b'').hexdigest(), 'kind': 'official-page'} | changes


def receipt(files):
    return {'schemaVersion': audit.EVIDENCE_SCHEMA,
        'selectionPolicy': 'official-source-metadata-only-no-CTX-no-outcomes', 'files': files}


class EPANoApiFallbackTests(unittest.TestCase):
    def fixture(self, root):
        inventory = [{'id': '68af6b70e4b02565fc7c3a98', 'size': '7502075199',
            'filename': 'INVITRODB_SUMMARY.zip', 'date-created': 'synthetic date',
            'contentType': 'multi/files-zipped'}]
        html = (b'<strong>inventory size:</strong> 2 <strong>sha256:</strong> ' + b'0' * 64
            + b'<li class="md-block">mc4_synthetic.csv</li>')
        files = []
        for name, url, kind, raw in [
            ('synthetic-inventory.json', 'https://clowder.edap-cluster.com/api/datasets/'
                + audit.SUMMARY_DATASET + '/files', 'file-inventory', encoded(inventory)),
            ('synthetic-archive.html', 'https://clowder.edap-cluster.com/files/68af6b70e4b02565fc7c3a98',
                'official-page', html)]:
            (root / name).write_bytes(raw)
            files.append(evidence(fileId=name, sourceUrl=url, kind=kind,
                bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()))
        raw = encoded(receipt(files))
        path = root / 'receipt.json'
        path.write_bytes(raw)
        return path, hashlib.sha256(raw).hexdigest()

    def test_blocked_counts_remain_unknown_and_no_workflow_can_approve(self):
        with tempfile.TemporaryDirectory() as directory:
            path, sha = self.fixture(Path(directory))
            with patch.object(audit, 'endpoint_identities', return_value=[{'aeid': 686}]):
                result = audit.audit_sources(path, sha, directory)
            for key in ('actualTestedMembershipRows', 'uniqueSubstances', 'uniqueStructures',
                        'mappedMembershipRows', 'missingStructureRows', 'ambiguousMappingRows',
                        'invalidStructureRows', 'historicalOverlapStructures',
                        'structurallyPassedStructures', 'independentlyQualifiedStructures'):
                self.assertIsNone(result[key])
            for key in ('candidateLabelsRead', 'ctxApiCalled', 'candidateModelsExecuted',
                        'evaluationPerformed', 'trainingPerformed', 'sealingPerformed',
                        'formalAvailability', 'qualificationGranted', 'canEnterStage7C'):
                self.assertFalse(result[key])
            self.assertEqual(result['gateStatus'], 'STAGE7C_BLOCKED')
            self.assertEqual(result['summaryFileInventory'][0]['reportedBytes'], 7502075199)
            self.assertFalse(result['summaryFileInventory'][0]['inventoryIsTestedMembership'])
            self.assertIsNone(result['completeArchiveSha256'])
            self.assertFalse(result['archiveContentInventory']['publisherSha256LocallyVerified'])

    def test_all_source_hashes_and_receipt_hash_are_rechecked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, sha = self.fixture(root)
            audit.load_evidence(path, sha)
            with self.assertRaises(ValueError):
                audit.load_evidence(path, 'f' * 64)
            (root / 'synthetic-archive.html').write_bytes(b'changed')
            with self.assertRaises(ValueError):
                audit.load_evidence(path, sha)

    def test_ctx_credentials_private_urls_encoding_and_nonofficial_hosts_rejected(self):
        for url in ('https://comptox.epa.gov/ctx-api/chemical/search',
            'https://comptox.epa.gov/%252fctx-api/chemical',
            'https://www.epa.gov/page?%74oken=secret', 'https://www.epa.gov/page?key=secret',
            'http://www.epa.gov/comptox-tools/page', 'https://secret@www.epa.gov/comptox-tools/page',
            'https://www.epa.gov:444/comptox-tools/page', 'https://localhost/private',
            'https://www.epa.gov.evil.example/comptox-tools/page',
            'https://s3.amazonaws.com/unreviewed-bucket/data', 'https://doi.org/10.1234/other'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                audit.official_url(url)

    def test_metadata_kinds_bound_to_host_and_route_not_activity_downloads(self):
        for kind, url in (
            ('file-metadata', 'https://www.epa.gov/api/files/' + 'a' * 24 + '/metadata'),
            ('file-inventory', 'https://www.epa.gov/api/datasets/' + 'a' * 24 + '/files'),
            ('official-page', 'https://clowder.edap-cluster.com/api/files/' + 'a' * 24),
            ('official-page', 'https://comptox.epa.gov/activities'),
            ('readme', 'https://clowder.edap-cluster.com/api/files/68af6b70e4b02565fc7c3a98'),
            ('official-page', 'https://api.figshare.com/v2/file/download/123')):
            with self.subTest(kind=kind, url=url), self.assertRaises(ValidationError):
                audit.Evidence.model_validate(evidence(kind=kind, sourceUrl=url))

    def test_additional_result_fields_and_non_utc_are_rejected(self):
        for key in ('hitcall', 'IC50', 'label', 'prediction', 'memberCount'):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                audit.Evidence.model_validate(evidence() | {key: 1})
        for time in ('2026-10-17T08:00:00', '2026-10-17T08:00:00+08:00'):
            with self.subTest(time=time), self.assertRaises(ValidationError):
                audit.Evidence.model_validate(evidence(retrievedAtUtc=time))
        for name in ('../data.json', '..\\data.json', 'x/rows.json', 'C:\\private.json'):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                audit.Evidence.model_validate(evidence(fileId=name))

    def test_duplicate_cache_names_and_unknown_top_level_fields_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = receipt([evidence(), evidence()])
            path = root / 'receipt.json'
            path.write_bytes(encoded(value))
            with self.assertRaises(ValueError):
                audit.load_evidence(path, hashlib.sha256(path.read_bytes()).hexdigest())
        with self.assertRaises(ValidationError):
            audit.Receipt.model_validate(receipt([evidence()]) | {'labels': []})

    def test_duplicate_json_keys_and_nonfinite_constants_rejected(self):
        for raw in (b'{"files":[],"files":[]}', b'{"labels":NaN}', b'{"labels":Infinity}'):
            with self.assertRaises(ValueError):
                audit.strict_json(raw)

    def test_missing_or_mislabeled_summary_inventory_is_not_tested_membership(self):
        with tempfile.TemporaryDirectory() as directory:
            path, sha = self.fixture(Path(directory))
            records, bodies = audit.load_evidence(path, sha)
            inventory_name = records.files[0].fileId
            for bad in ([], {'members': []}, [{'aeid': 686, 'spid': 'synthetic'}]):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    audit.summary_inventory(records, bodies | {inventory_name: encoded(bad)})
            with self.assertRaises(ValueError):
                audit.summary_inventory(audit.Receipt.model_validate(receipt([evidence()])), {})

    def test_inventory_schema_changes_and_ambiguous_identifiers_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path, sha = self.fixture(Path(directory))
            records, bodies = audit.load_evidence(path, sha)
            name = records.files[0].fileId
            row = json.loads(bodies[name])[0]
            for bad in ([row, row], [row | {'hitcall': 1}], [row | {'size': '-1'}],
                        [row | {'filename': '../rows.csv'}], [row | {'date-created': {'label': 1}}]):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    audit.summary_inventory(records, bodies | {name: encoded(bad)})

    def test_archive_listing_derived_only_from_metadata_not_bogus_wholefile_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            path, sha = self.fixture(Path(directory))
            records, bodies = audit.load_evidence(path, sha)
            result = audit.archive_inventory(records, bodies)
            self.assertEqual(result['parsedFileNames'], ['mc4_synthetic.csv'])
            self.assertIsNone(result['completeLocalArchiveSha256'])
            self.assertFalse(result['publisherSha256LocallyVerified'])
            name = records.files[1].fileId
            for replacement in (b'changed HTML schema', bodies[name].replace(b'mc4_synthetic.csv', b'../rows.csv')):
                with self.assertRaises(ValueError):
                    audit.archive_inventory(records, bodies | {name: replacement})

    def test_bounded_file_and_cumulative_download_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            path, sha = self.fixture(Path(directory))
            with patch.object(audit, 'MAX_TOTAL_BYTES', 1), self.assertRaises(ValueError):
                audit.load_evidence(path, sha)
            with patch.object(audit, 'MAX_FILE_BYTES', 1), self.assertRaises(ValueError):
                audit.load_evidence(path, sha)
        with self.assertRaises(ValidationError):
            audit.Evidence.model_validate(evidence(bytes=audit.MAX_FILE_BYTES + 1))

    def test_probe_errors_are_distinct_from_downloaded_bodies(self):
        base = {'sourceUrl': 'https://clowder.edap-cluster.com/api/files/68af6b70e4b02565fc7c3a98',
            'retrievedAtUtc': '2026-10-17T00:00:00Z', 'requestMethod': 'GET',
            'requestRange': 'bytes=7502074175-7502075198', 'errorKind': 'ReadTimeout'}
        probe = audit.ArchiveProbe.model_validate(base)
        self.assertIsNone(probe.httpStatus)
        self.assertEqual(probe.responseBodyReadBytes, 0)
        for change in ({'httpStatus': 200}, {'errorKind': None}, {'requestRange': 'bytes=0-7502075198'},
                       {'responseBodyReadBytes': 1025}, {'responseBodyReadBytes': 1},
                       {'observedBodySha256': '0' * 64},
                       {'requestMethod': 'HEAD'}, {'sourceUrl': 'https://clowder.edap-cluster.com/api/files/' + 'a' * 24}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                audit.ArchiveProbe.model_validate(base | change)

    def test_partial_response_must_match_exact_requested_archive_range(self):
        value = {'sourceUrl': 'https://clowder.edap-cluster.com/api/files/68af6b70e4b02565fc7c3a98',
            'retrievedAtUtc': '2026-10-17T00:00:00Z', 'requestMethod': 'GET',
            'requestRange': 'bytes=0-0', 'httpStatus': 206,
            'observedContentRange': 'bytes 0-0/7502075199', 'observedContentLength': 1,
            'responseBodyReadBytes': 1, 'observedBodySha256': hashlib.sha256(b'P').hexdigest()}
        audit.ArchiveProbe.model_validate(value)
        for change in ({'observedContentRange': 'bytes 0-1023/1'}, {'observedContentRange': None},
                       {'observedContentRange': 'bytes 1-1/7502075199'},
                       {'observedContentLength': 1024}, {'requestMethod': 'HEAD'},
                       {'responseBodyReadBytes': 0}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                audit.ArchiveProbe.model_validate(value | change)

    def test_endpoint_component_and_assay_ids_are_pinned_separately(self):
        annotations = [{'aeid': str(aeid), 'acid': str(acid), 'aid': str(aid),
            'assay_component_endpoint_name': name, 'assay_source_name': 'synthetic source',
            'organism': 'human', 'assay_design_type': 'binding reporter',
            'assay_function_type': 'binding', 'normalized_data_type': 'percent_activity'}
            for aeid, (acid, aid, name) in audit.ENDPOINTS.items()]
        targets = [{'aeid': str(aeid), 'official_symbol': 'KCNH2'} for aeid in audit.ENDPOINTS]
        sha = hashlib.sha256(b'synthetic').hexdigest()
        with patch.object(audit, '_regular_bytes', return_value=b'synthetic'), \
                patch.object(audit, 'PINS', {name: sha for name in audit.PINS}), \
                patch.object(audit, 'metadata_rows', side_effect=[annotations, targets]):
            result = audit.endpoint_identities('unused-synthetic-directory')
        self.assertEqual([(r['aeid'], r['acid'], r['aid']) for r in result],
                         [(686, 423, 237), (3184, 2914, 897), (3210, 2939, 910)])
        annotations[0]['acid'] = annotations[0]['aid']  # common identity corruption
        with patch.object(audit, '_regular_bytes', return_value=b'synthetic'), \
                patch.object(audit, 'PINS', {name: sha for name in audit.PINS}), \
                patch.object(audit, 'metadata_rows', side_effect=[annotations, targets]), self.assertRaises(ValueError):
            audit.endpoint_identities('unused-synthetic-directory')

    def test_assay_metadata_hash_failure_stops_before_parsing(self):
        with patch.object(audit, '_regular_bytes', return_value=b'changed'), \
                patch.object(audit, 'metadata_rows') as reader, self.assertRaises(ValueError):
            audit.endpoint_identities('unused-synthetic-directory')
        reader.assert_not_called()

    def test_cli_output_is_deterministic_private_path_free_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, sha = self.fixture(root)
            outputs = []
            for name in ('one.json', 'two.json'):
                output = root / name
                args = ['--evidence-receipt', str(path), '--expected-receipt-sha256', sha,
                        '--metadata-dir', str(root), '--output', str(output)]
                with patch.object(audit, 'endpoint_identities', return_value=[{'aeid': 686}]), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(args), 0)
                outputs.append(output.read_bytes())
            self.assertEqual(*outputs)
            self.assertNotIn(str(root), outputs[0].decode())
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.main(args)
            self.assertEqual(output.read_bytes(), outputs[0])

    def test_missing_input_has_clear_failure_and_does_not_construct_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'out.json'
            with contextlib.redirect_stderr(io.StringIO()) as log:
                code = cli.main(['--evidence-receipt', str(root / 'missing.json'),
                    '--expected-receipt-sha256', '0' * 64, '--metadata-dir', str(root), '--output', str(output)])
            self.assertEqual(code, 2)
            self.assertFalse(output.exists())
            self.assertNotIn(str(root), log.getvalue())


if __name__ == '__main__':
    unittest.main()
