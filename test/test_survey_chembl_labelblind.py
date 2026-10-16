"""Synthetic metadata only: exercise retrieval safeguards without network or labels.

The fixtures are invented software records, never scientific-validation evidence.
httpx.MockTransport prevents any real HTTP request.
"""
from __future__ import annotations

import copy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from scripts import survey_chembl_labelblind as survey


def metadata_rows():
    return {
        'target': [{'target_chembl_id': survey.TARGET, 'pref_name': 'Software fixture',
                    'organism': 'Homo sapiens', 'target_type': 'SINGLE PROTEIN'}],
        'activity': [{'activity_id': 1, 'molecule_chembl_id': 'CHEMBL101',
                      'canonical_smiles': 'C1CC1', 'assay_chembl_id': 'CHEMBL201',
                      'document_chembl_id': 'CHEMBL301', 'src_id': 1, 'record_id': 1,
                      'standard_type': 'IC50', 'standard_units': 'nM', 'type': 'IC50', 'units': 'nM'}],
        'assay': [{'assay_chembl_id': 'CHEMBL201', 'assay_type': 'F',
                   'assay_organism': 'Homo sapiens', 'assay_tax_id': 9606,
                   'assay_cell_type': None, 'assay_tissue': None, 'confidence_score': 9,
                   'relationship_type': 'D', 'document_chembl_id': 'CHEMBL301', 'src_id': 1}],
        'document': [{'document_chembl_id': 'CHEMBL301', 'doi': '10.0000/software-fixture',
                      'pubmed_id': None, 'title': 'Synthetic software test', 'journal': 'Fixture',
                      'year': 2020, 'src_id': 1}],
    }


def page(resource, rows, **pagination):
    meta = {'limit': survey.MAX_RECORDS, 'offset': 0, 'total_count': len(rows),
            'next': None, 'previous': None}
    meta.update(pagination)
    return {survey.PLURALS[resource]: rows, 'page_meta': meta}


class FakeOfficialAPI:
    def __init__(self, mutate=None):
        self.calls = []
        self.mutate = mutate
        self.rows = metadata_rows()

    def __call__(self, request):
        self.calls.append(request)
        url = str(request.url).split('?')[0]
        if url.startswith(survey.RELEASE):
            return httpx.Response(200, content=b'Synthetic license metadata; not real rights evidence.')
        resource = request.url.path.rsplit('/', 1)[-1].removesuffix('.json')
        payload = ({'chembl_db_version': survey.VERSION, 'status': 'UP'} if resource == 'status'
                   else page(resource, copy.deepcopy(self.rows[resource])))
        if self.mutate:
            replacement = self.mutate(resource, payload)
            if replacement is not None:
                payload = replacement
        return httpx.Response(200, json=payload)


class ChEMBLLabelblindRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def collect(self, api=None, name='snapshot'):
        api = api or FakeOfficialAPI()
        with httpx.Client(transport=httpx.MockTransport(api), follow_redirects=False) as client:
            bundle, receipt = survey.collect(self.root / name, client)
        return bundle, receipt, api

    def load(self, directory=None, pin=None):
        directory = directory or self.root / 'snapshot'
        pin = pin or survey.sha((directory / 'receipt.json').read_bytes())
        return survey.load_snapshot(directory, expected_receipt_sha256=pin)

    def test_all_data_requests_use_exact_server_side_projection(self):
        bundle, receipt, api = self.collect()
        self.assertEqual(len(bundle['activities']), 1)
        self.assertFalse(receipt['labelsRead'])
        self.assertFalse(receipt['resultFieldsRequested'])
        self.assertEqual(receipt['retrievedRecordCount'], 1)
        self.assertTrue(receipt['wholeTargetSurvey'])
        for request in api.calls:
            resource = request.url.path.rsplit('/', 1)[-1].removesuffix('.json')
            self.assertIn(request.url.host, {'www.ebi.ac.uk', 'ftp.ebi.ac.uk'})
            if resource in survey.FIELDS:
                self.assertEqual(request.url.params['only'], ','.join(survey.FIELDS[resource]))
                self.assertEqual(request.url.params['offset'], '0')
                self.assertEqual(request.url.params['limit'], str(survey.MAX_RECORDS))
                self.assertFalse(any('value' in field or 'comment' in field or 'relation' == field
                                     for field in request.url.params['only'].split(',')))
            if resource == 'activity':
                self.assertEqual(request.url.params['target_chembl_id'], survey.TARGET)
                self.assertEqual(request.url.params['order_by'], 'activity_id')
                self.assertEqual(set(request.url.params), {'only', 'limit', 'offset', 'target_chembl_id', 'order_by'})
        cached, cached_receipt = self.load()
        self.assertEqual(cached, bundle)
        self.assertEqual(cached_receipt, receipt)

    def test_version_mismatch_stops_before_activity_or_license_download(self):
        api = FakeOfficialAPI(lambda resource, payload:
                              {'chembl_db_version': 'ChEMBL_38', 'status': 'UP'} if resource == 'status' else None)
        with self.assertRaises(ValueError):
            self.collect(api)
        self.assertEqual(len(api.calls), 1)
        self.assertTrue(str(api.calls[0].url).startswith(survey.API + 'status.json'))
        self.assertFalse((self.root / 'snapshot' / 'bundle.json').exists())

    def test_server_ignoring_projection_is_rejected_without_persisting_body(self):
        for key in ('standard_value', 'value', 'pchembl_value', 'standard_relation', 'activity_comment', 'label'):
            def mutate(resource, payload, forbidden=key):
                if resource == 'activity':
                    payload['activities'][0][forbidden] = 'forbidden-software-sentinel'
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.collect(FakeOfficialAPI(mutate), name=key)
            directory = self.root / key
            self.assertFalse((directory / 'activity.json').exists())
            self.assertFalse((directory / 'bundle.json').exists())
            self.assertFalse((directory / 'receipt.json').exists())

    def test_fixed_allowlist_and_projection_cannot_be_bypassed_by_direct_read(self):
        api = FakeOfficialAPI()
        with httpx.Client(transport=httpx.MockTransport(api)) as client:
            reader = survey.BoundedReader(client, self.root)
            for url, params, resource in (
                ('http://127.0.0.1/activity.json', None, None),
                ('https://attacker.example/activity.json', None, None),
                (survey.API + 'activity.json', None, None),
                (survey.API + 'activity.json', {'only': 'activity_id,standard_value'}, 'activity'),
                (survey.API + 'activity.json', {'only': ','.join(survey.FIELDS['activity'])}, 'document'),
            ):
                with self.subTest(url=url, resource=resource), self.assertRaises(ValueError):
                    reader.read('unsafe.json', url, params, resource)
        self.assertEqual(api.calls, [])
        self.assertFalse((self.root / 'unsafe.json').exists())

    def test_outcome_ordering_alternate_target_and_offset_rejected_online_and_offline(self):
        self.collect()
        directory = self.root / 'snapshot'
        original = survey.strict_json((directory / 'receipt.json').read_bytes())
        base_query = next(item['params'] for item in original['responses']
                          if item['resource'] == 'activity.json')
        for field, value in (('order_by', 'standard_value'), ('offset', 1),
                             ('target_chembl_id', 'CHEMBL241'), ('standard_value__lt', 100)):
            query = dict(base_query, **{field: value})
            api = FakeOfficialAPI()
            with self.subTest(field=field, phase='online'):
                with httpx.Client(transport=httpx.MockTransport(api)) as client:
                    reader = survey.BoundedReader(client, self.root)
                    with self.assertRaises(ValueError):
                        reader.read('unsafe-query.json', survey.API + 'activity.json', query, 'activity')
                self.assertEqual(api.calls, [])
                self.assertFalse((self.root / 'unsafe-query.json').exists())
            receipt = copy.deepcopy(original)
            next(item for item in receipt['responses'] if item['resource'] == 'activity.json')['params'] = query
            (directory / 'receipt.json').write_bytes(survey.encoded(receipt))
            # Re-anchoring only this synthetic test receipt must not bypass
            # independent verification of metadata-only query semantics.
            with self.subTest(field=field, phase='offline'), self.assertRaises(ValueError):
                self.load(directory)

    def test_redirect_is_not_followed(self):
        calls = []
        def redirect(request):
            calls.append(request)
            return httpx.Response(302, headers={'location': 'https://attacker.example/results'})
        with httpx.Client(transport=httpx.MockTransport(redirect), follow_redirects=False) as client:
            with self.assertRaises((ValueError, httpx.HTTPError)):
                survey.collect(self.root / 'redirect', client)
        self.assertEqual(len(calls), 1)
        self.assertFalse((self.root / 'redirect' / 'status-before.json').exists())

    def test_response_and_total_byte_limits_fail_before_saving(self):
        for limit_name in ('MAX_RESPONSE_BYTES', 'MAX_TOTAL_BYTES'):
            directory = self.root / limit_name
            directory.mkdir()
            with httpx.Client(transport=httpx.MockTransport(FakeOfficialAPI())) as client, \
                 patch.object(survey, limit_name, 3):
                reader = survey.BoundedReader(client, directory)
                with self.subTest(limit=limit_name), self.assertRaises(ValueError):
                    reader.read('status.json', survey.API + 'status.json')
            self.assertFalse((directory / 'status.json').exists())

    def test_request_budget_has_no_extra_network_call(self):
        api = FakeOfficialAPI()
        with httpx.Client(transport=httpx.MockTransport(api)) as client:
            reader = survey.BoundedReader(client, self.root)
            reader.requests = survey.MAX_REQUESTS
            with self.assertRaises(ValueError):
                reader.read('status.json', survey.API + 'status.json')
        self.assertEqual(api.calls, [])

    def test_duplicate_keys_and_nonfinite_json_are_rejected(self):
        for body in (b'{"a":1,"a":2}', b'{"a":{"b":1,"b":2}}', b'{"a":NaN}', b'{"a":Infinity}'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                survey.strict_json(body)

    def test_malformed_envelopes_and_pagination_fail_closed(self):
        valid = page('activity', metadata_rows()['activity'])
        bad_pages = []
        for key in ('page_meta', 'activities'):
            item = copy.deepcopy(valid); del item[key]; bad_pages.append(item)
        item = copy.deepcopy(valid); item['extra'] = []; bad_pages.append(item)
        for key, value in (('total_count', True), ('offset', -1), ('limit', '200'), ('total_count', 0)):
            item = copy.deepcopy(valid); item['page_meta'][key] = value; bad_pages.append(item)
        for item in bad_pages:
            with self.subTest(page=item), self.assertRaises(ValueError):
                survey.projected_page(item, 'activity')

    def test_duplicate_unordered_or_noninteger_activity_ids_rejected(self):
        for ids in ([1, 1], [2, 1], [True], ['1']):
            api = FakeOfficialAPI()
            api.rows['activity'] = [dict(api.rows['activity'][0], activity_id=value) for value in ids]
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.collect(api, name='ids-' + str(len(list(self.root.iterdir()))))

    def test_missing_substituted_duplicate_or_paginated_metadata_joins_rejected(self):
        for resource in ('assay', 'document'):
            for fault in ('missing', 'substituted', 'duplicate', 'paginated'):
                def mutate(kind, payload, wanted=resource, error=fault):
                    if kind != wanted:
                        return
                    rows = payload[survey.PLURALS[kind]]
                    if error == 'missing':
                        rows.clear(); payload['page_meta']['total_count'] = 0
                    elif error == 'substituted':
                        rows[0][kind + '_chembl_id'] = 'CHEMBL999'
                    elif error == 'duplicate':
                        rows.append(copy.deepcopy(rows[0])); payload['page_meta']['total_count'] = len(rows)
                    else:
                        payload['page_meta']['next'] = 'https://attacker.example/page'
                with self.subTest(resource=resource, fault=fault), self.assertRaises(ValueError):
                    self.collect(FakeOfficialAPI(mutate), name=resource + '-' + fault)

    def test_snapshot_rejects_tampered_bundle_raw_response_or_receipt(self):
        self.collect()
        directory = self.root / 'snapshot'
        original = {item.name: item.read_bytes() for item in directory.iterdir()}
        trusted_pin = survey.sha(original['receipt.json'])
        for name in ('bundle.json', 'activity.json', 'receipt.json'):
            with self.subTest(file=name):
                (directory / name).write_bytes(original[name] + b' ')
                with self.assertRaises(ValueError):
                    self.load(directory, trusted_pin)
                (directory / name).write_bytes(original[name])

    def test_updated_self_hash_does_not_replace_trusted_receipt_pin(self):
        self.collect()
        directory = self.root / 'snapshot'
        trusted_pin = survey.sha((directory / 'receipt.json').read_bytes())
        raw = (directory / 'bundle.json').read_bytes() + b' '
        (directory / 'bundle.json').write_bytes(raw)
        receipt = survey.strict_json((directory / 'receipt.json').read_bytes())
        receipt['bundleSha256'] = survey.sha(raw)
        (directory / 'receipt.json').write_bytes(survey.encoded(receipt))
        with self.assertRaises(ValueError):
            self.load(directory, trusted_pin)

    def test_bundle_must_equal_reconciled_raw_projection_rows(self):
        self.collect()
        directory = self.root / 'snapshot'
        bundle = survey.strict_json((directory / 'bundle.json').read_bytes())
        bundle['activities'][0]['canonical_smiles'] = 'C1CCC1'
        raw = survey.encoded(bundle)
        (directory / 'bundle.json').write_bytes(raw)
        receipt = survey.strict_json((directory / 'receipt.json').read_bytes())
        receipt['bundleSha256'] = survey.sha(raw)
        (directory / 'receipt.json').write_bytes(survey.encoded(receipt))
        # Even a deliberately new trusted pin cannot make contradictory source
        # projections and the derived bundle into a reproducible snapshot.
        with self.assertRaises(ValueError):
            self.load(directory)

    def test_response_role_traversal_and_missing_provenance_are_rejected(self):
        self.collect()
        directory = self.root / 'snapshot'
        original = survey.strict_json((directory / 'receipt.json').read_bytes())
        for fault in ('traversal', 'missing-response'):
            receipt = copy.deepcopy(original)
            if fault == 'traversal':
                receipt['responses'][0]['resource'] = '../status-before.json'
            else:
                receipt['responses'] = [row for row in receipt['responses'] if row['resource'] != 'activity.json']
            (directory / 'receipt.json').write_bytes(survey.encoded(receipt))
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                self.load(directory)

    def test_existing_private_directory_and_public_output_are_never_overwritten(self):
        self.collect()
        directory = self.root / 'snapshot'
        before = {item.name: item.read_bytes() for item in directory.iterdir()}
        with self.assertRaises(FileExistsError):
            self.collect()
        self.assertEqual(before, {item.name: item.read_bytes() for item in directory.iterdir()})
        output = self.root / 'existing-aggregate.json'
        output.write_bytes(b'existing-evidence')
        with patch.object(survey, 'collect') as collect, patch('sys.stderr', new_callable=io.StringIO), \
             self.assertRaises(SystemExit):
            survey.main(['--private-dir', str(self.root / 'another-snapshot'), '--fetch', '--output', str(output)])
        collect.assert_not_called()
        self.assertEqual(output.read_bytes(), b'existing-evidence')


if __name__ == '__main__':
    unittest.main()
